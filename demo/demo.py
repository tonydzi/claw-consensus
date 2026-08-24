# -*- coding: utf-8 -*-
"""demo.py - reproducible, offline demonstration of the consensus engine.

Runs the PUBLISHED reference engine (../reference/consensus.py) through five
scenarios on ONE host, simulating two machines by flipping the MACHINE_KEY env
between calls. No network, no chat rail, no LLM, no packages -- it sets
CONSENSUS_NO_BUS=1 so every step is a pure local file operation. What you are
timing is the engine's own deterministic work, end to end.

    python demo/demo.py                 # run all scenarios, print traces
    python demo/demo.py --json out.json # also dump machine-readable timings
    python demo/demo.py --keep          # keep the temp ledgers for inspection

Scenarios
  A  happy path      propose -> counter -> accept -> commit -> verify x2 -> DONE
  B  human gate      Tier-2 auto-escalates; commit REFUSED until `approve`
  C  tier-tripwire   an innocuous-looking Tier-0 subject is force-bumped to Tier-2
  D  split-brain     two machines commit the same proposal -> conflict, escalated
  E  corrupt line    a garbage line in a shard does NOT eat the events after it

Exit code is 0 only if every scenario's end-state matches expectation, so this
file doubles as the engine's integration test. Numbers vary per machine; the
point is the SHAPE (0 tokens, sub-millisecond core, process-startup-bound CLI),
not the exact milliseconds. See docs/EVALS.md.
"""
import os
import sys
import json
import time
import shutil
import tempfile
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ENGINE = os.path.join(ROOT, "reference", "consensus.py")

HUB = "HUB-1"        # the always-on leader (matches docs default)
LAPTOP = "LAPTOP-2"  # a sleeping peer


def seed_config(bus_dir):
    """Give a bus dir the same leader config the docs quickstart uses."""
    os.makedirs(os.path.join(bus_dir, "_decisions"), exist_ok=True)
    with open(os.path.join(bus_dir, "_decisions", "consensus.json"), "w",
              encoding="utf-8") as fh:
        json.dump({"leader": HUB, "round_cap": 3, "timeout_min": 30}, fh)


def invoke(machine, bus, args, expect=0):
    """Run the engine CLI as `machine` against `bus`; return (ms, proc)."""
    env = dict(os.environ)
    env["MACHINE_KEY"] = machine
    env["MACHINE_BUS_DIR"] = bus
    env["CONSENSUS_NO_BUS"] = "1"          # offline: no chat rail, no network
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("CONSENSUS_DRY", None)
    t0 = time.perf_counter()
    p = subprocess.run([sys.executable, ENGINE, *args], env=env,
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    ms = (time.perf_counter() - t0) * 1000.0
    if expect is not None and p.returncode != expect:
        sys.stderr.write("\n  [!] `%s %s` exited %s (expected %s)\n%s\n"
                         % (machine, " ".join(args), p.returncode, expect,
                            (p.stdout or "") + (p.stderr or "")))
    return ms, p


class Runner:
    """Drives one isolated consensus ledger and records per-step timings."""

    def __init__(self, bus_dir):
        self.bus = bus_dir
        seed_config(bus_dir)
        self.steps = []

    def run(self, machine, *args, expect=0):
        """Invoke the engine CLI as `machine`, time the whole process, record it."""
        ms, p = invoke(machine, self.bus, list(args), expect=expect)
        self.steps.append({"machine": machine, "verb": args[0],
                           "args": list(args), "ms": round(ms, 1),
                           "exit": p.returncode, "out": (p.stdout or "").strip()})
        return p

    def propose_id(self, p):
        """Pull the 8-char short id the engine printed on PROPOSE."""
        for ln in (p.stdout or "").splitlines():
            if ln.startswith("PROPOSED #"):
                return ln.split("#", 1)[1].split()[0]
        raise RuntimeError("no PROPOSED id in output:\n" + (p.stdout or ""))

    def events(self):
        """Merge every shard the way the engine's readers do (dedup by event_id)."""
        seen, out = set(), []
        dd = os.path.join(self.bus, "_decisions")
        for name in sorted(os.listdir(dd)):
            if not (name.startswith("log-") and name.endswith(".jsonl")):
                continue
            for ln in open(os.path.join(dd, name), encoding="utf-8").read().splitlines():
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    ev = json.loads(ln)
                except Exception:
                    continue           # per-line tolerance, same as the engine
                eid = ev.get("event_id")
                if eid in seen:
                    continue
                seen.add(eid)
                out.append(ev)
        return out


def hdr(title):
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def trace_table(steps):
    print("\n  %-9s %-8s %8s  %-4s  %s" % ("machine", "verb", "wall/ms", "exit", "result"))
    print("  " + "-" * 68)
    for s in steps:
        head = ""
        for ln in s["out"].splitlines():
            if ln and not ln.startswith("   "):
                head = ln
                break
        print("  %-9s %-8s %8.1f  %-4s  %s"
              % (s["machine"], s["verb"], s["ms"], s["exit"], head[:38]))
    total = sum(s["ms"] for s in steps)
    print("  " + "-" * 68)
    print("  %-9s %-8s %8.1f  (%d CLI calls, 0 LLM calls, 0 tokens)"
          % ("TOTAL", "", total, len(steps)))
    return total


# ----------------------------------------------------------------------------

def scenario_a(bus):
    hdr("A. HAPPY PATH  propose -> counter -> accept -> commit -> verify x2")
    r = Runner(bus)
    p = r.run(LAPTOP, "propose", "adopt nightly reindex at 03:30", "--tier", "0")
    pid = r.propose_id(p)
    r.run(HUB, "respond", pid, "counter", "agree, but run it at 04:00 to miss the backup window")
    r.run(LAPTOP, "respond", pid, "accept", "04:00 works")
    r.run(HUB, "commit", pid)                                   # leader applies
    r.run(HUB, "verify", pid, "cron installed on hub, next run 04:00")   # self-verify
    r.run(LAPTOP, "verify", pid, "confirmed cron present from laptop, independent check")
    total = trace_table(r.steps)
    ev = r.events()
    types = [e["type"] for e in ev]
    verifiers = sorted({e["actor"] for e in ev if e["type"] == "VERIFY"})
    ok = (types.count("COUNTER") == 1 and types.count("COMMIT") == 1
          and len(verifiers) == 2)
    print("\n  end-state: %d events, verifiers=%s -> %s"
          % (len(ev), verifiers, "DONE (>=2 distinct, >=1 independent)" if ok else "UNEXPECTED"))
    return {"name": "A happy-path", "pass": ok, "total_ms": total, "steps": r.steps}


def scenario_b(bus):
    hdr("B. HUMAN GATE  Tier-2 auto-escalates; commit REFUSED until approve")
    r = Runner(bus)
    p = r.run(HUB, "propose", "rotate the production API token", "--tier", "2")
    pid = r.propose_id(p)
    # commit must be REFUSED (no recorded human approval yet) -> exit 1
    r.run(HUB, "commit", pid, expect=1)
    r.run(HUB, "approve", pid, "owner approved, alerts-channel msg 1790183")
    r.run(HUB, "commit", pid)
    r.run(HUB, "verify", pid, "token rotated, old key revoked")
    total = trace_table(r.steps)
    ev = r.events()
    has_escalate = any(e["type"] == "ESCALATE" for e in ev)
    has_human = any(e["type"] == "HUMAN_APPROVED" for e in ev)
    committed = any(e["type"] == "COMMIT" for e in ev)
    refused = r.steps[1]["exit"] == 1
    ok = has_escalate and refused and has_human and committed
    print("\n  end-state: auto-escalated=%s  commit-refused-before-approve=%s  "
          "approved+committed=%s -> %s"
          % (has_escalate, refused, has_human and committed, "GATE HELD" if ok else "UNEXPECTED"))
    return {"name": "B human-gate", "pass": ok, "total_ms": total, "steps": r.steps}


def scenario_c(bus):
    hdr("C. TIER-TRIPWIRE  a Tier-0 label on a dangerous verb is force-bumped")
    r = Runner(bus)
    # labelled tier-0, but the subject contains 'delete' -> deterministic bump to 2
    p = r.run(HUB, "propose", "delete the archived leads table", "--tier", "0")
    pid = r.propose_id(p)
    r.run(HUB, "status", pid)
    total = trace_table(r.steps)
    ev = r.events()
    prop = next(e for e in ev if e["type"] == "PROPOSE")
    bumped = int(prop.get("risk_tier", 0)) == 2
    escalated = any(e["type"] == "ESCALATE" for e in ev)
    tripped = "TIER-TRIPWIRE" in r.steps[0]["out"]
    ok = bumped and escalated and tripped
    print("\n  end-state: labelled tier-0, stored tier=%s, tripwire-fired=%s -> %s"
          % (prop.get("risk_tier"), tripped, "BUMPED + GATED" if ok else "UNEXPECTED"))
    return {"name": "C tripwire", "pass": ok, "total_ms": total, "steps": r.steps}


def scenario_d(bus):
    hdr("D. SPLIT-BRAIN  a partition makes both sides commit -> conflict caught on heal")
    # A split-brain needs a real partition: while the sync link is down, each side
    # sees only 'agreed' and commits independently. We model that with two separate
    # bus dirs that share the pre-partition history, then MERGE the shards (heal).
    shared, hub_side, laptop_side = (os.path.join(bus, "shared"),
                                     os.path.join(bus, "hub-side"),
                                     os.path.join(bus, "laptop-side"))
    r = Runner(shared)
    p = r.run(HUB, "propose", "switch embeddings backend to model-large", "--tier", "0")
    pid = r.propose_id(p)
    r.run(LAPTOP, "respond", pid, "accept", "agreed")   # both sides now know: agreed

    # --- partition: clone the shared history to each side, then each commits blind ---
    dd = lambda root: os.path.join(root, "_decisions")
    for side in (hub_side, laptop_side):
        seed_config(side)
        for name in os.listdir(dd(shared)):
            if name.startswith("log-"):
                shutil.copy(os.path.join(dd(shared), name), os.path.join(dd(side), name))
    ms1, _ = invoke(HUB, hub_side, ["commit", pid])          # HUB commits on its island
    ms2, _ = invoke(LAPTOP, laptop_side, ["commit", pid])    # LAPTOP commits on its island
    r.steps.append({"machine": HUB, "verb": "commit", "ms": round(ms1, 1), "exit": 0,
                    "out": "COMMIT on hub island (partitioned)"})
    r.steps.append({"machine": LAPTOP, "verb": "commit", "ms": round(ms2, 1), "exit": 0,
                    "out": "COMMIT on laptop island (partitioned)"})

    # --- heal: merge each island's shard back into the shared ledger ---
    shutil.copy(os.path.join(dd(hub_side), "log-%s.jsonl" % HUB),
                os.path.join(dd(shared), "log-%s.jsonl" % HUB))
    shutil.copy(os.path.join(dd(laptop_side), "log-%s.jsonl" % LAPTOP),
                os.path.join(dd(shared), "log-%s.jsonl" % LAPTOP))
    r.run(HUB, "tick")                 # detector sees 2 committers -> escalate to reconcile

    total = trace_table(r.steps)
    ev = r.events()   # r's bus IS the shared (healed) ledger
    committers = sorted({e["actor"] for e in ev if e["type"] == "COMMIT"})
    escalated = any(e["type"] == "ESCALATE" for e in ev)
    ok = len(committers) == 2 and escalated
    print("\n  end-state: committers=%s (conflict), escalated-for-reconcile=%s -> %s"
          % (committers, escalated, "CAUGHT" if ok else "UNEXPECTED"))
    return {"name": "D split-brain", "pass": ok, "total_ms": total, "steps": r.steps}


def scenario_e(bus):
    hdr("E. CORRUPT LINE  garbage in a shard must not eat later events")
    r = Runner(bus)
    p = r.run(HUB, "propose", "add a health dashboard tile", "--tier", "0")
    pid = r.propose_id(p)
    # inject a corrupt line into HUB's shard, BEFORE the commit it will write next
    shard = os.path.join(bus, "_decisions", "log-%s.jsonl" % HUB)
    with open(shard, "a", encoding="utf-8") as fh:
        fh.write('{"event_id": "partial-write-junk", "type": "COMM\n')  # truncated JSON
    r.run(LAPTOP, "respond", pid, "accept", "ok")
    r.run(HUB, "commit", pid)          # this COMMIT lands AFTER the corrupt line
    r.run(HUB, "status", pid)
    total = trace_table(r.steps)
    raw = open(shard, encoding="utf-8").read()
    has_junk = "partial-write-junk" in raw
    ev = r.events()
    committed = any(e["type"] == "COMMIT" for e in ev)   # survived the bad line above it
    ok = has_junk and committed
    print("\n  end-state: corrupt line present in shard=%s, COMMIT after it still read=%s -> %s"
          % (has_junk, committed, "RESILIENT" if ok else "UNEXPECTED"))
    return {"name": "E corrupt-line", "pass": ok, "total_ms": total, "steps": r.steps}


def microbench(bus):
    """Pure-engine latency: how long the state derivation itself takes, in-process,
    with zero process-startup cost, over a realistic ledger (50 proposals, ~6 events
    each). This is the real 'consensus compute' number the CLI wall-time hides behind
    Python interpreter startup."""
    hdr("CORE COMPUTE  in-process state derivation over 50 proposals (no subprocess)")
    # Build a synthetic single-writer ledger straight as JSONL (the engine's own format).
    seed_config(bus)
    dd = os.path.join(bus, "_decisions")
    for machine in (HUB, LAPTOP):
        with open(os.path.join(dd, "log-%s.jsonl" % machine), "w", encoding="utf-8") as fh:
            for i in range(50):
                pid = "p%04d" % i
                base = "2026-07-11T%02d:%02d:00.000000Z" % (i // 60, i % 60)
                if machine == HUB:
                    for etype, subj in (("PROPOSE", "decision %d" % i), ("COMMIT", None),
                                        ("VERIFY", None)):
                        ev = {"event_id": "%s-%s-h" % (pid, etype), "proposal_id": pid,
                              "type": etype, "actor": HUB, "ts": base}
                        if subj:
                            ev["subject"] = subj
                            ev["risk_tier"] = 0
                        fh.write(json.dumps(ev) + "\n")
                else:
                    for etype in ("ACCEPT", "VERIFY"):
                        fh.write(json.dumps({"event_id": "%s-%s-l" % (pid, etype),
                                             "proposal_id": pid, "type": etype,
                                             "actor": LAPTOP, "ts": base}) + "\n")
    code = (
        "import os,sys,time\n"
        "os.environ['MACHINE_KEY']=%r\n"
        "os.environ['MACHINE_BUS_DIR']=%r\n"
        "os.environ['CONSENSUS_NO_BUS']='1'\n"
        "sys.path.insert(0,%r)\n"
        "import consensus as c\n"
        "for _ in range(50):\n"
        "    g=c._by_proposal(); [c._state(v) for v in g.values()]\n"
        "N=2000; t=time.perf_counter()\n"
        "for _ in range(N):\n"
        "    g=c._by_proposal(); [c._state(v) for v in g.values()]\n"
        "dt=(time.perf_counter()-t)/N*1e6\n"
        "ne=sum(len(v) for v in g.values())\n"
        "print('%%d proposals / %%d events, merge+derive-all: %%.1f microseconds/pass'%%(len(g),ne,dt))\n"
        % (HUB, bus, os.path.join(ROOT, "reference"))
    )
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    p = subprocess.run([sys.executable, "-c", code], env=env,
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    line = (p.stdout or "").strip() or (p.stderr or "").strip()
    print("\n  " + line)
    return line


def main():
    argv = sys.argv[1:]
    keep = "--keep" in argv
    json_out = None
    if "--json" in argv:
        i = argv.index("--json")
        json_out = argv[i + 1] if i + 1 < len(argv) else os.path.join(HERE, "last-run.json")

    print("claw-consensus demo -- offline, 0 tokens, stdlib only")
    print("engine: %s" % os.path.relpath(ENGINE, ROOT))
    print("python: %s" % sys.version.split()[0])

    root_tmp = tempfile.mkdtemp(prefix="consensus-demo-")
    results = []
    try:
        results.append(scenario_a(os.path.join(root_tmp, "a")))
        results.append(scenario_b(os.path.join(root_tmp, "b")))
        results.append(scenario_c(os.path.join(root_tmp, "c")))
        results.append(scenario_d(os.path.join(root_tmp, "d")))
        results.append(scenario_e(os.path.join(root_tmp, "e")))
        bench = microbench(os.path.join(root_tmp, "bench"))

        hdr("SUMMARY")
        allpass = True
        for r in results:
            allpass = allpass and r["pass"]
            print("  [%s]  %-16s  %7.1f ms  (%d CLI calls)"
                  % ("PASS" if r["pass"] else "FAIL", r["name"], r["total_ms"],
                     len(r["steps"])))
        print("\n  core compute: %s" % bench)
        print("  LLM calls across the whole demo: 0        tokens: 0")
        print("  every number above is deterministic file I/O + Python startup")

        if json_out:
            payload = {"python": sys.version.split()[0], "engine": os.path.relpath(ENGINE, ROOT),
                       "core_compute": bench, "scenarios": results}
            with open(json_out, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)
            print("\n  machine-readable timings -> %s" % json_out)

        print("\n  RESULT: %s" % ("ALL SCENARIOS PASS" if allpass else "SOME SCENARIOS FAILED"))
        return 0 if allpass else 1
    finally:
        if keep:
            print("\n  ledgers kept at: %s" % root_tmp)
        else:
            shutil.rmtree(root_tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
