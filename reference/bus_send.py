# -*- coding: utf-8 -*-
"""bus_send.py - the ONE entry point for every machine->machine message. DUAL-SEND BY
CONSTRUCTION: posts to BOTH rails (chat group + file-synced mailbox) in a single call,
so an actor can NEVER send on one rail only.

WHY (a real incident): a task went out only on the file rail (via `machine_bus.py send`)
and the owner did not see it in the chat group. Root cause: DUAL-SEND was a PROSE rule with
no enforcement -> the human/LLM had to remember to call both rails, and forgot. This gate
removes the choice.

CANON: the chat group is ALWAYS the mirror channel of ALL machine<->machine comms -- never
optional, never "fallback only". If one rail fails, the message STILL goes on the other AND
a failure SIGNAL is raised (a dead rail is a signal to the owner, not a reason to send
silently on one). Deeper fallbacks (email, a second messenger, a cloud drive) can sit behind
these two primaries.

Rails:
  rail 1 = machine_bus.py (file mailbox over your synced folder)
  rail 2 = env BUS_RAIL2_CMD: any command that takes ONE text argument and posts it to a
           group chat both your machines and your humans read (Telegram group, Slack
           channel, Discord webhook, ...). Not configured -> rail 1 only, with a nudge.

Usage: python bus_send.py [<target>] "<message>"
       target defaults to ALL (broadcast).
         python bus_send.py "hello fleet"
         python bus_send.py LAPTOP-1 "task for the laptop"
Exit: 0 = both rails OK; 1 = degraded (one rail down, other delivered + signalled); 3 = both down.
Test hook (non-destructive): env BUS_SEND_FAIL=file|chat forces that rail to fail.
"""
import os, sys, shlex, subprocess, time

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS)
FLAG = os.path.join(SCRIPTS, "_BUS_RAIL_DOWN.flag")

# ONE finite budget for the whole send. Per-call caps (60 + 90 + 90 + 60) used to stack: a send
# with both rails slow could hold its caller for ~300 s. Each call now gets min(its cap, time left).
TOTAL_BUS_TIMEOUT_S = 210
_BUS_DEADLINE = None


def _remaining(cap):
    if _BUS_DEADLINE is None:
        return float(cap)
    return max(0.0, min(float(cap), _BUS_DEADLINE - time.monotonic()))

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def _send_file_rail(target, msg):
    if os.environ.get("BUS_SEND_FAIL") == "file":
        return False, "forced-test-fail"
    allowance = _remaining(60)
    if allowance <= 0:
        return False, "bus-deadline-spent"
    try:
        r = subprocess.run(
            [sys.executable, os.path.join(SCRIPTS, "machine_bus.py"), "send", target, msg],
            capture_output=True, text=True, timeout=allowance)
        info = ((r.stdout or "") + (r.stderr or "")).strip().splitlines()
        return r.returncode == 0, (info[-1] if info else "")
    except Exception as e:
        return False, str(e)


def _send_chat_rail(target, msg):
    if os.environ.get("BUS_SEND_FAIL") == "chat":
        return False, "forced-test-fail"
    raw = (os.environ.get("BUS_RAIL2_CMD") or "").strip()
    if not raw:
        return None, "not configured (set BUS_RAIL2_CMD to make dual-send structural)"
    allowance = _remaining(90)
    if allowance <= 0:
        return False, "bus-deadline-spent"
    try:
        text = msg if str(target).upper() == "ALL" else "[-> %s] %s" % (target, msg)
        r = subprocess.run(shlex.split(raw) + [text], timeout=allowance)
        return r.returncode == 0, "posted" if r.returncode == 0 else "rc=%s" % r.returncode
    except Exception as e:
        return False, str(e)


def _maybe_register(target, msg, delivered):
    """Register a DIRECT order with the ACK-watchdog so silence gets chased.
    Skips: broadcasts (ALL), undelivered, the watchdog's own re-pings (BUS_NO_TRACK),
    and non-orders (ACKs / heartbeats / rail-down signals). The watchdog module is
    optional; the discipline it enforces is in docs/BUS.md ("delivered is not done")."""
    if not delivered or str(target).upper() == "ALL":
        return
    if os.environ.get("BUS_NO_TRACK"):
        return
    head = (msg or "").lstrip()[:24]
    if any(s in head for s in ("✅", "\U0001F493", "\U0001F534", "RECEIVED", "ACK")):
        return
    try:
        import ack_watchdog  # optional companion module
        ack_watchdog.register(target, msg)
    except Exception:
        pass  # watchdog optional -- never break a send


def main():
    a = sys.argv[1:]
    if not a:
        print("usage: bus_send.py [<target>] \"<message>\"")
        return 2
    # --help/-h guard (a real incident: without it `bus_send.py --help` broadcast the
    # literal flag as a message into the group chat).
    if a[0].lower() in ("-h", "--help", "help", "/?"):
        print(__doc__)
        return 2
    target, msg = (a[0], a[1]) if len(a) >= 2 else ("ALL", a[0])
    if not (msg or "").strip():
        print("refusing to send an empty message")
        return 2

    global _BUS_DEADLINE
    _BUS_DEADLINE = time.monotonic() + TOTAL_BUS_TIMEOUT_S
    f_ok, f_info = _send_file_rail(target, msg)
    c_ok, c_info = _send_chat_rail(target, msg)
    print("DUAL-SEND -> file:%s(%s)  chat:%s(%s)" % (
        "OK" if f_ok else "FAIL", f_info,
        "OK" if c_ok else ("SKIP" if c_ok is None else "FAIL"), c_info))

    _maybe_register(target, msg, f_ok or bool(c_ok))

    if c_ok is None:
        # rail 2 not configured: single-rail mode, allowed for bootstrap but nagged every send
        return 0 if f_ok else 3

    if f_ok and c_ok:
        if os.path.exists(FLAG):
            os.remove(FLAG)
        return 0

    down = ([] if f_ok else ["file-rail/machine-bus"]) + ([] if c_ok else ["chat-rail"])
    sig = "\U0001F534 BUS RAIL DOWN: %s -- msg still delivered on the surviving rail: '%s'" % (
        ", ".join(down), msg[:60])
    open(FLAG, "w", encoding="utf-8").write(sig)
    # raise the signal on whichever rail IS up (can't signal via a dead rail)
    # (the flag file above is already written, so a spent budget skips the live signal, not the record)
    if c_ok and not f_ok and _remaining(90) > 0:
        try:
            raw = (os.environ.get("BUS_RAIL2_CMD") or "").strip()
            subprocess.run(shlex.split(raw) + [sig], timeout=_remaining(90))
        except Exception:
            pass
    if f_ok and not c_ok and _remaining(60) > 0:
        try:
            subprocess.run([sys.executable, os.path.join(SCRIPTS, "machine_bus.py"), "send", "ALL", sig], timeout=_remaining(60))
        except Exception:
            pass
    print(sig)
    return 1 if (f_ok or c_ok) else 3


if __name__ == "__main__":
    sys.exit(main())
