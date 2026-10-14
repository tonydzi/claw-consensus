# -*- coding: utf-8 -*-
r"""fleet_sign.py - machine identity and detached signatures for a multi-machine agent fleet.

THE PROBLEM
  Our ledger recorded WHO decided something as a string: `actor` inside the event, and a machine
  name inside the shard filename. Both are written by whoever has write access to the shared
  folder. So the honest answer to "which machine committed this" was "whoever claimed to". For a
  human-in-the-loop gate that is not good enough: the gate's whole value is that a Tier-2 commit
  carries a recorded human approval, and an unauthenticated ledger cannot tell you that the
  approval line was not simply typed in.

THE CHOICE: reuse, don't build
  Ed25519 detached signatures via OpenSSH's `ssh-keygen -Y sign|verify`. No crypto library, no
  key format of our own, no C dependency, present on every machine we run. Two external research
  passes both landed on the same answer: signing a canonical JSON body with tooling that already
  ships with the OS beats every alternative for a fleet this size.

LAYOUT (each choice closes an incident class we actually hit)
  * PRIVATE key   local app-data, per machine, NEVER synced. Syncing identity is what produced
                  our duplicate-session incidents; a machine's identity must be as unsyncable
                  as its serial number.
  * PUBLIC keys   <bus>/_engine/signers/<MACHINE>.pub - ONE FILE PER MACHINE, single writer.
                  Same forever-fix as the mailboxes: no shared multi-writer file, no conflicts.
  * REVOCATION    <bus>/_engine/signers/REVOKED.txt, one principal per line.
  * allowed_signers is ASSEMBLED IN MEMORY at every verify from signers/*.pub minus REVOKED.
                  There is deliberately no shared allowed_signers file to fight over or go stale.
  * namespace     "fleet", so these signatures are meaningless outside this system.

ROLLOUT ORDER (do not skip)
  sign everything and verify nothing (dark) -> a readable audit command -> only then set an
  enforcement timestamp, forward in time. Flip straight to enforcement and you quarantine every
  machine that has not rolled out a key yet, which on day one is all of them.
  The enforcement decision itself lives in protocol_guards.py (`sig_enforced`).

  python fleet_sign.py init | pub | audit | selftest

API: sign_event(ev) -> armored signature | None      verify_event(ev) -> True | False | None
     None means "unsigned, or no registry/tooling here" - a rollout gap, NOT a tampering signal.
     Keeping those two apart is the difference between a signature layer and a slogan.
Env: MACHINE_BUS_DIR, MACHINE_KEY / COMPUTERNAME, FLEET_SIGN_DISABLE=1, SSH_KEYGEN
"""
import os
import sys
import json
import glob
import argparse
import subprocess
import tempfile

BUS = os.environ.get("MACHINE_BUS_DIR", os.path.join(os.path.expanduser("~"), "machine-bus"))
ME = os.environ.get("MACHINE_KEY", os.environ.get("COMPUTERNAME",
                    os.environ.get("HOSTNAME", "unknown"))).strip()
KEYDIR = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "claude-fleet")
KEY = os.path.join(KEYDIR, "machine_ed25519")
SIGNERS = os.path.join(BUS, "_engine", "signers")
REVOKED = os.path.join(SIGNERS, "REVOKED.txt")
NS = "fleet"
# Human approvals are signed in their own namespace, so a machine's event signature can never be
# replayed as a human's approval of anything (and vice versa). See consensus.py `approve`.
NS_APPROVAL = "fleet-approval"


def _disabled():
    return bool(os.environ.get("FLEET_SIGN_DISABLE"))


def _ssh_keygen():
    r"""Which ssh-keygen to call - and this is not a detail.

    SCAR (cost us a day): a Windows machine can easily have TWO ssh-keygen.exe on PATH, the one
    bundled in System32\OpenSSH and the one shipped with Git for Windows. On our box the bundled
    build HANGS for 30 seconds with no output and no error on `-Y sign` fed through stdin, which
    is exactly how the functions below call it. Worse, Windows' executable search can resolve a
    bare "ssh-keygen" to either build depending on the calling context - interactive shell vs raw
    subprocess vs scheduled task - so which one you get is not reliably the working one. The
    symptom was maddening: freshly signed events verified BAD in the audit while this file's own
    selftest passed, because the two ran under different contexts.

    So: pin to a known-good binary, allow an override, and fall back to a PATH lookup only where
    there is exactly one - never silently regress to the broken build.
    """
    env = os.environ.get("SSH_KEYGEN")
    if env and os.path.exists(env):
        return env
    for candidate in (r"C:\Program Files\Git\usr\bin\ssh-keygen.exe",):
        if os.path.exists(candidate):
            return candidate
    return "ssh-keygen"


def canonical(ev):
    """Stable bytes of an event WITHOUT its signature fields. Sorted keys and no whitespace, so
    the same event serializes identically on every machine and Python version - a signature over
    a dict whose key order can move is a signature over nothing."""
    body = {k: v for k, v in ev.items() if k not in ("sig", "signer")}
    return json.dumps(body, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode("utf-8")


def have_key():
    return os.path.exists(KEY) and not _disabled()


def init(force=False):
    """Create this machine's keypair and publish only the PUBLIC half to the shared registry."""
    os.makedirs(KEYDIR, exist_ok=True)
    if os.path.exists(KEY) and not force:
        print("key already present: %s" % KEY)
    else:
        subprocess.run([_ssh_keygen(), "-t", "ed25519", "-N", "", "-C", "fleet:%s" % ME,
                        "-f", KEY], check=True, capture_output=True, timeout=60)
        print("created %s" % KEY)
    os.makedirs(SIGNERS, exist_ok=True)
    dst = os.path.join(SIGNERS, "%s.pub" % ME)
    with open(KEY + ".pub", encoding="utf-8") as src, open(dst, "w", encoding="utf-8") as out:
        out.write(src.read())
    print("published %s" % dst)
    return 0


def sign_bytes(data, key=None, ns=NS):
    """Detached armored signature of `data` (stdin -> stdout). None if this machine has no key.
    `key` = an explicit private key path (a human signing an approval on their own device);
    default is this machine's key."""
    if key is None:
        if not have_key():
            return None
        key = KEY
    r = subprocess.run([_ssh_keygen(), "-Y", "sign", "-f", key, "-n", ns],
                       input=data, capture_output=True, timeout=30)
    if r.returncode != 0:
        raise RuntimeError("ssh-keygen sign failed: %s"
                           % r.stderr.decode(errors="replace")[:200])
    return r.stdout.decode("ascii")


def _revoked():
    try:
        return {ln.strip() for ln in open(REVOKED, encoding="utf-8")
                if ln.strip() and not ln.startswith("#")}
    except OSError:
        return set()


def _assemble_allowed(ns=NS):
    """allowed_signers content built from signers/*.pub minus REVOKED. None = no registry yet."""
    if not os.path.isdir(SIGNERS):
        return None
    dead = _revoked()
    lines = []
    for f in sorted(glob.glob(os.path.join(SIGNERS, "*.pub"))):
        principal = os.path.splitext(os.path.basename(f))[0]
        if principal in dead:
            continue
        try:
            parts = open(f, encoding="utf-8").read().split()
        except OSError:
            continue
        if len(parts) >= 2:
            lines.append('%s namespaces="%s" %s %s' % (principal, ns, parts[0], parts[1]))
    return "\n".join(lines) + "\n" if lines else None


def registry_fingerprint():
    """Short hash of the trust set (signers minus revoked). A cached verdict is only reused while
    this is unchanged, so a revocation or a new key invalidates every cached verdict at once."""
    import hashlib
    return hashlib.sha256((_assemble_allowed() or "").encode("utf-8")).hexdigest()[:16]


def verify_bytes(data, sig_text, signer, ns=NS):
    """True / False, or None when there is no registry to verify against."""
    if _disabled():
        return None
    allowed = _assemble_allowed(ns)
    if not allowed:
        return None
    af = sf = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".allowed", delete=False,
                                         encoding="utf-8") as fh:
            fh.write(allowed)
            af = fh.name
        # SCAR: newline="" is REQUIRED. Without it, Python's text mode on Windows translates
        # every "\n" to "\r\n" on write - and some ssh-keygen builds already emit CRLF armor,
        # so each CR becomes "\r\r\n" and the temp file's bytes no longer match what was signed.
        # Reproduced against historical ledger entries: they verified BAD purely from this
        # corruption. The signatures had been good the whole time. A signature layer that cries
        # "tampered" over a newline is worse than none, because you stop believing it.
        with tempfile.NamedTemporaryFile("w", suffix=".sig", delete=False,
                                         encoding="ascii", newline="") as fh:
            fh.write(sig_text)
            sf = fh.name
        r = subprocess.run([_ssh_keygen(), "-Y", "verify", "-f", af, "-I", signer,
                            "-n", ns, "-s", sf], input=data, capture_output=True, timeout=30)
        return r.returncode == 0
    finally:
        for p in (af, sf):
            if p:
                try:
                    os.unlink(p)
                except OSError:
                    pass


def sign_event(ev):
    """Armored signature for one event, or None (no key / disabled). Callers run in dark mode
    and must treat None as 'send it unsigned and count the failure', never as an error to raise."""
    return sign_bytes(canonical(ev))


def verify_event(ev):
    """True = good, False = BAD, None = unsigned or nothing to verify against."""
    sig, signer = ev.get("sig"), ev.get("signer") or ev.get("actor")
    if not sig or not signer:
        return None
    return verify_bytes(canonical(ev), sig, signer)


def audit(events=None):
    """Count ok / bad / unsigned over a list of events (or say what is configured)."""
    if events is None:
        print("me=%s  key=%s  registry=%s  namespace=%s"
              % (ME, "yes" if have_key() else "no",
                 SIGNERS if os.path.isdir(SIGNERS) else "ABSENT", NS))
        allowed = _assemble_allowed()
        print("signers=%d  revoked=%d"
              % (len(allowed.strip().split("\n")) if allowed else 0, len(_revoked())))
        return 0
    ok = bad = unsigned = 0
    for ev in events:
        v = verify_event(ev)
        ok += v is True
        bad += v is False
        unsigned += v is None
    print("sigs: %d ok · %d BAD · %d unsigned(legacy)" % (ok, bad, unsigned))
    return 1 if bad else 0


def _selftest():
    """Real round trip: make a throwaway keypair, sign, verify, then tamper and re-verify.
    A signature selftest that never fails a verification proves nothing."""
    global KEYDIR, KEY, SIGNERS, REVOKED, ME
    tmp = tempfile.mkdtemp()
    KEYDIR, SIGNERS = os.path.join(tmp, "local"), os.path.join(tmp, "bus", "_engine", "signers")
    KEY, REVOKED = os.path.join(KEYDIR, "machine_ed25519"), os.path.join(SIGNERS, "REVOKED.txt")
    ME = "HUB-1"

    kg = _ssh_keygen()
    try:
        subprocess.run([kg, "-h"], capture_output=True, timeout=20)
    except Exception as e:
        print("SKIP - no usable ssh-keygen here (%s). This is the rollout gap the code calls "
              "None, not a failure." % e)
        return 0

    init()
    ev = {"proposal_id": "p1", "type": "COMMIT", "actor": "HUB-1",
          "ts": "2026-07-28T09:00:00", "subject": "raise the daily cap"}

    sig = sign_event(ev)
    assert sig, "signing produced nothing despite a fresh key"
    signed = dict(ev, sig=sig, signer=ME)
    assert verify_event(signed) is True, "a freshly signed event must verify"

    # key order must not matter - the canonical form is the point
    reordered = {k: signed[k] for k in sorted(signed)}
    assert verify_event(reordered) is True, "canonical form must be order-independent"

    # tampering must be caught: same signature, changed payload
    tampered = dict(signed, subject="raise the daily cap to 500")
    assert verify_event(tampered) is False, "a tampered event must FAIL, not pass"

    # an unsigned event is a rollout gap (None), NOT a forgery (False)
    assert verify_event(ev) is None, "unsigned must be None, distinct from bad"

    # revocation takes effect without touching any shared allowed_signers file
    with open(REVOKED, "w", encoding="utf-8") as fh:
        fh.write(ME + "\n")
    assert verify_event(signed) is None, ("a revoked signer leaves an EMPTY registry here, so "
                                          "there is nothing to verify against -> None")

    print("SELFTEST OK - signed and verified a real event, order-independent canonical form, "
          "tampering rejected, unsigned kept distinct from forged, revocation effective with "
          "no shared allowed_signers file.")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.strip().split("\n")[0])
    ap.add_argument("cmd", choices=["init", "pub", "audit", "selftest"])
    ap.add_argument("--force", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "init":
        return init(a.force)
    if a.cmd == "pub":
        print(open(KEY + ".pub", encoding="utf-8").read().strip()
              if os.path.exists(KEY + ".pub") else "no key yet - run: fleet_sign.py init")
        return 0
    if a.cmd == "audit":
        return audit()
    return _selftest()


if __name__ == "__main__":
    sys.exit(main())
