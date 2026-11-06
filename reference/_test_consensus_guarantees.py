# -*- coding: utf-8 -*-
"""_test_consensus_guarantees.py - each documented guarantee of consensus.py, as a test that
would go red if the code stopped enforcing it (issue #9).

    python reference/_test_consensus_guarantees.py          # stdlib unittest, offline, 0 tokens

Every test drives the real CLI as two simulated machines (MACHINE_KEY flipped per call) against a
throwaway bus dir, exactly like demo/demo.py. Set CONSENSUS_ENGINE=<path to consensus.py> to run
the same tests against another copy of the engine (that is how the red run on the pre-fix `main`
was produced: every test below marked "red on main" failed there).
"""
import os
import sys
import json
import shutil
import tempfile
import unittest
import subprocess
import hashlib
import datetime
import uuid
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.environ.get("CONSENSUS_ENGINE") or os.path.join(HERE, "consensus.py")
SIGNER = os.path.join(HERE, "fleet_sign.py")
HUB, LAPTOP = "HUB-1", "LAPTOP-2"
sys.path.insert(0, HERE)
import fleet_sign  # noqa: E402  (to locate a working ssh-keygen; the engine under test is a CLI)


def _have_ssh_keygen():
    # Presence only. Timing `ssh-keygen -h` under a timeout read a loaded machine as "no ssh-keygen"
    # and silently skipped every signature test, which is how a green run stops meaning anything.
    return bool(shutil.which(fleet_sign._ssh_keygen()))


HAVE_SSH = _have_ssh_keygen()


class Bus:
    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="consensus-test-")
        self.dd = os.path.join(self.root, "_decisions")
        os.makedirs(self.dd)
        self.config()

    def config(self, **extra):
        cfg = {"leader": HUB, "round_cap": 3, "timeout_min": 30}
        cfg.update(extra)
        with open(os.path.join(self.dd, "consensus.json"), "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)

    def env(self, machine):
        # each simulated machine gets its own private key dir inside the temp bus
        env = dict(os.environ, MACHINE_KEY=machine, MACHINE_BUS_DIR=self.root,
                   CONSENSUS_NO_BUS="1", PYTHONIOENCODING="utf-8",
                   LOCALAPPDATA=os.path.join(self.root, "_local", machine))
        for k in ("CONSENSUS_DRY", "CONSENSUS_SIG_ENFORCE_AFTER", "CONSENSUS_HUMAN_PRINCIPALS",
                  "FLEET_SIGN_DISABLE", "FLEET_SIGNERS_DIR", "CONSENSUS_HUMAN_SIGNERS_DIR"):
            env.pop(k, None)
        env.update(getattr(self, "pins", {}).get(machine, {}))
        return env

    def run(self, machine, *args):
        return subprocess.run([sys.executable, ENGINE, *args], env=self.env(machine),
                              capture_output=True, text=True, encoding="utf-8", errors="replace")

    def init_key(self, machine):
        """`fleet_sign.py init` as that machine: private key local, public key in the registry."""
        subprocess.run([sys.executable, SIGNER, "init"], env=self.env(machine), check=True,
                       capture_output=True, timeout=120)

    def human_key(self, principal="OWNER"):
        """A human's key on their own device; only the public half goes to the registry."""
        d = os.path.join(self.root, "_device_%s" % principal)
        os.makedirs(d, exist_ok=True)
        key = os.path.join(d, "id_ed25519")
        subprocess.run([fleet_sign._ssh_keygen(), "-q", "-t", "ed25519", "-N", "", "-f", key],
                       check=True, capture_output=True, timeout=60)
        signers = os.path.join(self.root, "_engine", "signers")
        os.makedirs(signers, exist_ok=True)
        shutil.copy(key + ".pub", os.path.join(signers, "%s.pub" % principal))
        return key

    def full_id(self, short):
        return next(e["proposal_id"] for e in self.events() if e["proposal_id"].startswith(short))

    def signed_envelope(self, pid, key, ttl="15", principal=None, patch=None):
        """approval-request -> (optional edit) -> sign-approval with the human's key -> path."""
        minutes = int(ttl)
        args = ["approval-request", pid, "--ttl-min", ttl if minutes > 0 else "15"]
        if principal:
            args += ["--principal", principal]
        req = self.run(HUB, *args)
        assert req.returncode == 0, req.stdout + req.stderr
        env = json.loads(req.stdout)
        doctored = bool(patch) or minutes <= 0
        if minutes <= 0:
            past = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(minutes=minutes)
            env["expires_at"] = past.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        if patch:
            env.update(patch)
        ask = os.path.join(self.root, "ask-%s.json" % uuid.uuid4().hex[:8])
        with open(ask, "w", encoding="utf-8") as fh:
            json.dump(env, fh)
        out = ask.replace("ask-", "signed-")
        if doctored:
            # What the engine must refuse on its own: `sign-approval` would not sign this, another tool would.
            env["sig"] = fleet_sign.sign_bytes(fleet_sign.canonical(env), key=key, ns=fleet_sign.NS_APPROVAL)
            with open(out, "w", encoding="utf-8") as fh:
                json.dump(env, fh)
            return out
        p = self.run("OWNER-DEVICE", "sign-approval", ask, "--key", key, "--out", out)
        assert p.returncode == 0, p.stdout + p.stderr
        return out

    def propose(self, machine, subject, tier="0"):
        p = self.run(machine, "propose", subject, "--tier", tier)
        for ln in p.stdout.splitlines():
            if ln.startswith("PROPOSED #"):
                return ln.split("#", 1)[1].split()[0]
        raise AssertionError("no PROPOSED id:\n" + p.stdout + p.stderr)

    def events(self, machine=None):
        out = []
        for name in sorted(os.listdir(self.dd)):
            if name.startswith("log-") and name.endswith(".jsonl"):
                if machine and name != "log-%s.jsonl" % machine:
                    continue
                for ln in open(os.path.join(self.dd, name), encoding="utf-8"):
                    if ln.strip():
                        out.append(json.loads(ln))
        return out

    def rewrite(self, machine, fn):
        """Rewrite one shard line by line (simulates what a skewed clock would have written)."""
        path = os.path.join(self.dd, "log-%s.jsonl" % machine)
        evs = [json.loads(ln) for ln in open(path, encoding="utf-8") if ln.strip()]
        with open(path, "w", encoding="utf-8") as fh:
            for ev in evs:
                fh.write(json.dumps(fn(ev)) + "\n")

    def status(self, machine, pid):
        return self.run(machine, "status", pid).stdout

    def close(self):
        shutil.rmtree(self.root, ignore_errors=True)


class Base(unittest.TestCase):
    def setUp(self):
        self.bus = Bus()

    def tearDown(self):
        self.bus.close()

    def committed(self):
        """A tier-0 proposal by LAPTOP, accepted by HUB, committed by HUB (the leader)."""
        pid = self.bus.propose(LAPTOP, "adopt nightly reindex at 04:00")
        self.assertEqual(self.bus.run(HUB, "respond", pid, "accept", "ok").returncode, 0)
        self.assertEqual(self.bus.run(HUB, "commit", pid).returncode, 0)
        return pid


class D_RubberStamp(Base):
    """GOVERNANCE/PROTOCOL: 'a rubber-stamp guard rejects copy-pasted proofs'."""

    def test_duplicate_proof_is_refused_and_not_recorded(self):   # red on main
        pid = self.committed()
        proof = "cron present, `crontab -l` exit 0, next run 04:00"
        self.assertEqual(self.bus.run(HUB, "verify", pid, proof).returncode, 0)
        p = self.bus.run(LAPTOP, "verify", pid, proof)
        self.assertEqual(p.returncode, 1, p.stdout)
        verifies = [e for e in self.bus.events() if e["type"] == "VERIFY"]
        self.assertEqual(len(verifies), 1, "a copied proof must not be appended")
        self.assertNotIn("DONE", self.bus.status(HUB, pid))

    def test_independent_proof_still_counts(self):
        pid = self.committed()
        self.assertEqual(self.bus.run(HUB, "verify", pid, "cron installed, exit 0").returncode, 0)
        self.assertEqual(self.bus.run(LAPTOP, "verify", pid,
                                      "read crontab from laptop over ssh, exit 0").returncode, 0)
        self.assertIn("DONE", self.bus.status(HUB, pid))

    def test_advisory_flag_keeps_old_behaviour(self):
        pid = self.committed()
        proof = "cron present, exit 0"
        self.bus.run(HUB, "verify", pid, proof)
        p = self.bus.run(LAPTOP, "verify", pid, proof, "--advisory")
        self.assertEqual(p.returncode, 0, p.stdout)
        verifies = [e for e in self.bus.events() if e["type"] == "VERIFY"]
        self.assertEqual(len(verifies), 2)
        self.assertEqual(verifies[-1]["proof"], proof, "--advisory must not leak into the proof")


class C_LeaderOverride(Base):
    """GOVERNANCE: consensus = propose -> accept -> commit; an arbiter's call is not consensus."""

    def test_leader_commit_without_agreement_is_an_override(self):   # red on main
        pid = self.bus.propose(LAPTOP, "switch embeddings backend")
        self.assertIn("[PROPOSED]", self.bus.status(HUB, pid))
        p = self.bus.run(HUB, "commit", pid)
        self.assertEqual(p.returncode, 0, p.stdout)
        types = [e["type"] for e in self.bus.events()]
        self.assertNotIn("COMMIT", types, "the leader's unilateral call was recorded as COMMIT")
        self.assertIn("LEADER_OVERRIDE", types)
        self.assertIn("[OVERRIDDEN]", self.bus.status(HUB, pid))

    def test_agreed_commit_is_still_a_commit(self):
        pid = self.committed()
        types = [e["type"] for e in self.bus.events()]
        self.assertIn("COMMIT", types)
        self.assertNotIn("LEADER_OVERRIDE", types)
        self.assertIn("[COMMITTED]", self.bus.status(HUB, pid))

    def test_non_leader_still_cannot_commit_unagreed(self):
        pid = self.bus.propose(HUB, "switch embeddings backend")
        self.assertEqual(self.bus.run(LAPTOP, "commit", pid).returncode, 1)

    def test_tick_disagree_and_commit_is_an_override(self):   # red on main
        pid = self.bus.propose(HUB, "rename the dashboard tile")
        # nobody answered for an hour: age the proposal past the 30-minute timeout
        self.bus.rewrite(HUB, lambda ev: dict(ev, ts="2000-01-01T00:00:00.000000Z"))
        self.assertEqual(self.bus.run(HUB, "tick").returncode, 0)
        types = [e["type"] for e in self.bus.events()]
        self.assertNotIn("COMMIT", types)
        self.assertIn("LEADER_OVERRIDE", types)
        self.assertIn("[OVERRIDDEN]", self.bus.status(HUB, pid))


class E_LogicalOrder(Base):
    """PROTOCOL: deterministic projection on every machine, whatever their clocks say."""

    def test_accept_from_a_slow_clock_still_counts(self):   # red on main
        pid = self.bus.propose(HUB, "add a health dashboard tile")
        self.assertEqual(self.bus.run(LAPTOP, "respond", pid, "accept", "ok").returncode, 0)
        # The laptop's clock runs 5 minutes behind the hub's: its ACCEPT carries a wall-clock
        # time EARLIER than the PROPOSE it answers, although it was written after reading it.
        self.bus.rewrite(LAPTOP, lambda ev: dict(ev, ts="2000-01-01T00:00:00.000000Z"))
        self.assertIn("[AGREED]", self.bus.status(HUB, pid))

    def test_every_event_carries_a_monotonic_seq(self):   # red on main
        pid = self.bus.propose(HUB, "add a health dashboard tile")
        self.bus.run(LAPTOP, "respond", pid, "counter", "make it two tiles")
        self.bus.run(HUB, "respond", pid, "accept", "two tiles")
        seqs = [e.get("seq") for e in self.bus.events(HUB)]
        self.assertTrue(all(isinstance(s, int) for s in seqs), seqs)
        self.assertEqual(seqs, sorted(seqs))
        counter = next(e for e in self.bus.events() if e["type"] == "COUNTER")
        accept = next(e for e in self.bus.events() if e["type"] == "ACCEPT")
        self.assertGreater(accept["seq"], counter["seq"], "an answer must outrank what it answers")

    def test_legacy_events_without_seq_keep_ts_order(self):
        pid = self.bus.propose(HUB, "add a health dashboard tile")
        self.bus.run(LAPTOP, "respond", pid, "accept", "ok")
        for m in (HUB, LAPTOP):
            self.bus.rewrite(m, lambda ev: {k: v for k, v in ev.items() if k != "seq"})
        self.assertIn("[AGREED]", self.bus.status(HUB, pid))


class Hostile_Lines(Base):
    """Lines a misbehaving machine can append to its own shard (found by adversarial review of
    the first fix). In dark mode they are unsigned and trusted, so the projection itself has to
    survive them; signatures (row A) then decide WHO may write them at all."""

    def append(self, name, ev):
        with open(os.path.join(self.bus.dd, "log-%s.jsonl" % name), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(ev) + "\n")

    def full_id(self, short):
        return next(e["proposal_id"] for e in self.bus.events() if e["proposal_id"].startswith(short))

    def test_cosmetic_change_to_a_copied_proof_is_still_a_copy(self):   # red on main
        pid = self.committed()
        self.bus.run(HUB, "verify", pid, "cron present, exit 0")
        p = self.bus.run(LAPTOP, "verify", pid, "Cron present -- exit 0.")
        self.assertEqual(p.returncode, 1, p.stdout)

    def test_hand_appended_duplicate_verify_does_not_count(self):   # red on main
        pid = self.committed()
        self.bus.run(HUB, "verify", pid, "cron present, exit 0")
        self.append(LAPTOP, {"event_id": "dup-1", "proposal_id": self.full_id(pid), "type": "VERIFY",
                             "actor": LAPTOP, "seq": 50, "ts": "2026-07-11T00:00:00.000000Z",
                             "proof": "cron present, exit 0"})
        self.assertNotIn("DONE", self.bus.status(HUB, pid))

    def test_second_propose_cannot_lower_the_tier(self):   # red on main
        pid = self.bus.propose(HUB, "wire the quarterly payment", tier="2")
        self.append("EVIL", {"event_id": "takeover", "proposal_id": self.full_id(pid),
                             "type": "PROPOSE", "actor": "EVIL", "seq": 0, "risk_tier": 0,
                             "ts": "2000-01-01T00:00:00.000000Z", "subject": "harmless"})
        st = self.bus.status(HUB, pid)
        self.assertIn("tier=2", st)
        self.assertIn("[CONFLICT]", st)
        self.assertEqual(self.bus.run(HUB, "commit", pid).returncode, 1)

    def test_negative_seq_is_refused(self):
        pid = self.bus.propose(HUB, "wire the quarterly payment", tier="2")
        self.append("EVIL", {"event_id": "neg", "proposal_id": self.full_id(pid), "type": "PROPOSE",
                             "actor": "EVIL", "seq": -1, "risk_tier": 0,
                             "ts": "2026-07-11T00:00:00.000000Z", "subject": "harmless"})
        st = self.bus.status(HUB, pid)
        self.assertIn("tier=2", st)
        self.assertIn("by HUB-1", st)

    def test_non_finite_seq_does_not_crash_readers(self):
        pid = self.bus.propose(HUB, "add a health dashboard tile")
        with open(os.path.join(self.bus.dd, "log-EVIL.jsonl"), "w", encoding="utf-8") as fh:
            fh.write('{"event_id": "inf", "proposal_id": "%s", "type": "ACCEPT", "actor": "EVIL", '
                     '"seq": Infinity, "ts": "2026-07-11T00:00:00.000000Z"}\n' % self.full_id(pid))
        self.assertEqual(self.bus.run(HUB, "list").returncode, 0)
        self.assertEqual(self.bus.run(LAPTOP, "respond", pid, "accept", "ok").returncode, 0)

    def test_reused_event_id_cannot_erase_another_machines_vote(self):   # red on main
        pid = self.bus.propose(HUB, "add a health dashboard tile")
        self.bus.run(LAPTOP, "respond", pid, "reject", "no")
        reject = next(e for e in self.bus.events(LAPTOP) if e["type"] == "REJECT")
        # "log-AAA" sorts before "log-LAPTOP-2", so its line is read first
        self.append("AAA", {"event_id": reject["event_id"], "proposal_id": self.full_id(pid),
                            "type": "VERIFY", "actor": "AAA", "seq": 1,
                            "ts": "2026-07-11T00:00:00.000000Z", "proof": "unrelated"})
        self.assertIn("[REJECTED]", self.bus.status(HUB, pid))

    def test_non_string_proof_does_not_crash_readers(self):   # review finding on #11
        pid = self.bus.propose(HUB, "add a health dashboard tile")
        for i, bad in enumerate((5, True, {"a": 1})):
            self.append("EVIL", {"event_id": "np%d" % i, "proposal_id": self.full_id(pid),
                                 "type": "VERIFY", "actor": "EVIL", "seq": 9 + i,
                                 "ts": "2026-07-11T00:00:00.000000Z", "proof": bad})
        for verb in (("list",), ("tick",), ("pending",), ("status", pid)):
            p = self.bus.run(HUB, *verb)
            self.assertEqual(p.returncode, 0, "%s: %s" % (verb, p.stderr))

    def test_non_string_text_does_not_crash_status(self):   # review finding on #11, same class
        pid = self.bus.propose(HUB, "add a health dashboard tile")
        self.append("EVIL", {"event_id": "nt", "proposal_id": self.full_id(pid), "type": "COUNTER",
                             "actor": "EVIL", "seq": 9, "ts": "2026-07-11T00:00:00.000000Z", "text": 7})
        p = self.bus.run(HUB, "status", pid)
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_infinite_tier_does_not_crash_readers(self):
        with open(os.path.join(self.bus.dd, "log-EVIL.jsonl"), "w", encoding="utf-8") as fh:
            fh.write('{"event_id": "it", "proposal_id": "p-inf", "type": "PROPOSE", "actor": "EVIL", '
                     '"seq": 1, "risk_tier": Infinity, "ts": "2026-07-11T00:00:00.000000Z", "subject": "x"}\n')
        p = self.bus.run(HUB, "status", "p-inf")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("tier=2", p.stdout)

    def test_blank_and_identical_proofs_never_reach_done(self):   # review finding on #11: holds already
        pid = self.committed()
        for i, (who, proof) in enumerate(((HUB, ""), (LAPTOP, ""), (HUB, "same"), (LAPTOP, "same!"))):
            self.append(who, {"event_id": "bl%d" % i, "proposal_id": self.full_id(pid), "type": "VERIFY",
                              "actor": who, "seq": 40 + i, "ts": "2026-07-11T00:00:00.000000Z", "proof": proof})
        self.assertNotIn("DONE", self.bus.status(HUB, pid))

    def test_late_propose_does_not_reopen_a_settled_decision(self):   # review finding on #11
        pid = self.committed()
        self.append("EVIL", {"event_id": "late", "proposal_id": self.full_id(pid), "type": "PROPOSE",
                             "actor": "EVIL", "seq": 999, "risk_tier": 2,
                             "ts": "2026-07-11T00:00:00.000000Z", "subject": "reopen"})
        st = self.bus.status(HUB, pid)
        self.assertIn("[COMMITTED]", st)
        self.assertIn("tier=0", st)
        self.bus.run(HUB, "tick")
        self.assertNotIn("ESCALATE", [e["type"] for e in self.bus.events()])

    def test_propose_before_the_decision_is_still_a_conflict(self):
        pid = self.committed()
        commit = next(e for e in self.bus.events() if e["type"] == "COMMIT")
        self.append("EVIL", {"event_id": "early", "proposal_id": self.full_id(pid), "type": "PROPOSE",
                             "actor": "EVIL", "seq": commit["seq"] - 1, "risk_tier": 2,
                             "ts": "2026-07-11T00:00:00.000000Z", "subject": "contested"})
        self.assertIn("[CONFLICT]", self.bus.status(HUB, pid))

    def test_ceiling_seq_does_not_erase_later_events(self):   # review finding on #11
        pid = self.bus.propose(HUB, "add a health dashboard tile")
        self.append("EVIL", {"event_id": "max", "proposal_id": "other", "type": "PROPOSE",
                             "actor": "EVIL", "seq": 2 ** 53 - 1, "risk_tier": 0,
                             "ts": "2000-01-01T00:00:00.000000Z", "subject": "poison"})
        p = self.bus.run(LAPTOP, "respond", pid, "accept", "ok")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("[AGREED]", self.bus.status(HUB, pid))
        accept = next(e for e in self.bus.events(LAPTOP) if e["type"] == "ACCEPT")
        self.assertEqual(accept["seq"], 2 ** 53 - 1, "the clock saturates, it never writes an unreadable seq")


class Retry_And_Conflict(Base):
    """A retried verb must not change history; a contested row must not get a decision of record."""

    def test_repeated_commit_is_idempotent(self):   # review finding on #11
        pid = self.committed()
        p = self.bus.run(HUB, "commit", pid)
        self.assertEqual(p.returncode, 0, p.stdout)
        types = [e["type"] for e in self.bus.events()]
        self.assertEqual(types.count("COMMIT"), 1)
        self.assertNotIn("LEADER_OVERRIDE", types)
        self.assertIn("[COMMITTED]", self.bus.status(HUB, pid))

    def test_repeated_commit_after_override_is_idempotent(self):
        pid = self.bus.propose(LAPTOP, "switch embeddings backend")
        self.bus.run(HUB, "commit", pid)
        self.assertEqual(self.bus.run(HUB, "commit", pid).returncode, 0)
        self.assertEqual([e["type"] for e in self.bus.events()].count("LEADER_OVERRIDE"), 1)

    def test_commit_refuses_a_conflict_on_tier0(self):   # review finding on #11
        pid = self.bus.propose(LAPTOP, "switch embeddings backend")
        full = next(e["proposal_id"] for e in self.bus.events() if e["proposal_id"].startswith(pid))
        with open(os.path.join(self.bus.dd, "log-EVIL.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"event_id": "second", "proposal_id": full, "type": "PROPOSE",
                                 "actor": "EVIL", "seq": 9, "risk_tier": 0,
                                 "ts": "2026-07-11T00:00:00.000000Z", "subject": "other"}) + "\n")
        p = self.bus.run(HUB, "commit", pid)
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertNotIn("LEADER_OVERRIDE", [e["type"] for e in self.bus.events()])

    def test_tick_escalates_a_conflict_once(self):
        pid = self.bus.propose(LAPTOP, "switch embeddings backend")
        full = next(e["proposal_id"] for e in self.bus.events() if e["proposal_id"].startswith(pid))
        with open(os.path.join(self.bus.dd, "log-EVIL.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"event_id": "second", "proposal_id": full, "type": "PROPOSE",
                                 "actor": "EVIL", "seq": 9, "risk_tier": 0,
                                 "ts": "2026-07-11T00:00:00.000000Z", "subject": "other"}) + "\n")
        self.bus.run(HUB, "tick")
        self.bus.run(HUB, "tick")
        self.assertEqual([e["type"] for e in self.bus.events()].count("ESCALATE"), 1)


class MixedVersion(Base):
    """A rolling upgrade: one machine writes seq, the other has not been upgraded yet."""

    def strip_seq(self, machine, etype):
        self.bus.rewrite(machine, lambda ev: {k: v for k, v in ev.items()
                                              if not (k == "seq" and ev["type"] == etype)})

    def test_old_peers_accept_still_counts(self):   # review finding on #11
        pid = self.bus.propose(HUB, "add a health dashboard tile")
        self.bus.run(LAPTOP, "respond", pid, "accept", "ok")
        self.strip_seq(LAPTOP, "ACCEPT")
        self.assertIn("[AGREED]", self.bus.status(HUB, pid))

    def test_old_peers_counter_is_the_latest_position(self):   # review finding on #11
        pid = self.bus.propose(HUB, "add a health dashboard tile")
        self.bus.run(LAPTOP, "respond", pid, "counter", "two tiles")
        self.strip_seq(LAPTOP, "COUNTER")
        self.assertIn("[COUNTERED]", self.bus.status(HUB, pid))

    def test_old_peers_accept_needs_no_override(self):
        pid = self.bus.propose(HUB, "add a health dashboard tile")
        self.bus.run(LAPTOP, "respond", pid, "accept", "ok")
        self.strip_seq(LAPTOP, "ACCEPT")
        self.assertEqual(self.bus.run(HUB, "commit", pid).returncode, 0)
        self.assertNotIn("LEADER_OVERRIDE", [e["type"] for e in self.bus.events()])

    def test_order_is_the_same_on_every_machine(self):
        pid = self.bus.propose(HUB, "add a health dashboard tile")
        self.bus.run(LAPTOP, "respond", pid, "counter", "two tiles")
        self.bus.run(HUB, "respond", pid, "accept", "two")
        self.strip_seq(LAPTOP, "COUNTER")
        self.assertEqual(self.bus.status(HUB, pid), self.bus.status(LAPTOP, pid))


def _approve_free_text(bus, pid):
    """The pre-v0.5.0 CLI shape: approve <id> "<proof>"."""
    return bus.run(HUB, "approve", pid, "owner approved, alerts-channel msg 1790183")


class B_SignedHumanApproval(Base):
    """GOVERNANCE §2/§4: Tier-2 needs a HUMAN's approval; peer agreement does not substitute."""

    def test_free_text_approval_is_refused(self):   # red on main
        pid = self.bus.propose(HUB, "rotate the production API token", tier="2")
        p = _approve_free_text(self.bus, pid)
        self.assertNotEqual(p.returncode, 0, "a typed proof string unlocked the human gate")
        self.assertEqual(self.bus.run(HUB, "commit", pid).returncode, 1)
        self.assertNotIn("HUMAN_APPROVED", [e["type"] for e in self.bus.events()])

    @unittest.skipUnless(HAVE_SSH, "no ssh-keygen")
    def test_signed_envelope_unlocks_commit(self):
        key = self.bus.human_key()
        self.bus.config(human_principals=["OWNER"])
        pid = self.bus.propose(HUB, "rotate the production API token", tier="2")
        self.assertEqual(self.bus.run(HUB, "commit", pid).returncode, 1)
        p = self.bus.run(HUB, "approve", pid, "--envelope", self.bus.signed_envelope(pid, key))
        self.assertEqual(p.returncode, 0, p.stdout)
        self.assertEqual(self.bus.run(HUB, "commit", pid).returncode, 0)
        self.assertIn("COMMIT", [e["type"] for e in self.bus.events()])

    @unittest.skipUnless(HAVE_SSH, "no ssh-keygen")
    def test_expired_envelope_is_refused(self):
        key = self.bus.human_key()
        self.bus.config(human_principals=["OWNER"])
        pid = self.bus.propose(HUB, "rotate the production API token", tier="2")
        p = self.bus.run(HUB, "approve", pid, "--envelope",
                         self.bus.signed_envelope(pid, key, ttl="-1"))
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertIn("expired", p.stdout)

    @unittest.skipUnless(HAVE_SSH, "no ssh-keygen")
    def test_replayed_nonce_is_refused(self):
        key = self.bus.human_key()
        self.bus.config(human_principals=["OWNER"])
        p1 = self.bus.propose(HUB, "rotate the production API token", tier="2")
        first = self.bus.signed_envelope(p1, key)
        self.assertEqual(self.bus.run(HUB, "approve", p1, "--envelope", first).returncode, 0)
        nonce = json.load(open(first, encoding="utf-8"))["nonce"]
        p2 = self.bus.propose(HUB, "rotate the staging API token", tier="2")
        again = self.bus.signed_envelope(p2, key, patch={"nonce": nonce})
        p = self.bus.run(HUB, "approve", p2, "--envelope", again)
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertIn("nonce", p.stdout)

    @unittest.skipUnless(HAVE_SSH, "no ssh-keygen")
    def test_envelope_for_another_proposal_is_refused(self):
        key = self.bus.human_key()
        self.bus.config(human_principals=["OWNER"])
        p1 = self.bus.propose(HUB, "rotate the production API token", tier="2")
        p2 = self.bus.propose(HUB, "delete the production database", tier="2")
        p = self.bus.run(HUB, "approve", p2, "--envelope", self.bus.signed_envelope(p1, key))
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertEqual(self.bus.run(HUB, "commit", p2).returncode, 1)

    @unittest.skipUnless(HAVE_SSH, "no ssh-keygen")
    def test_counter_after_approval_voids_it(self):
        key = self.bus.human_key()
        self.bus.config(human_principals=["OWNER"])
        pid = self.bus.propose(HUB, "rotate the production API token", tier="2")
        self.bus.run(HUB, "approve", pid, "--envelope", self.bus.signed_envelope(pid, key))
        self.bus.run(LAPTOP, "respond", pid, "counter", "rotate ALL tokens instead")
        self.assertEqual(self.bus.run(HUB, "commit", pid).returncode, 1,
                         "the human approved a different action than the one now on the table")

    @unittest.skipUnless(HAVE_SSH, "no ssh-keygen")
    def test_a_machine_key_is_not_a_human(self):
        self.bus.init_key(HUB)
        hub_key = os.path.join(self.bus.root, "_local", HUB, "claude-fleet", "machine_ed25519")
        self.bus.human_key()                                   # OWNER is registered ...
        self.bus.config(human_principals=["OWNER"])
        pid = self.bus.propose(HUB, "rotate the production API token", tier="2")
        # ... but the hub signs as itself, then claims to be OWNER: both refused
        as_itself = self.bus.signed_envelope(pid, hub_key, principal=HUB)
        self.assertEqual(self.bus.run(HUB, "approve", pid, "--envelope", as_itself).returncode, 1)
        as_owner = self.bus.signed_envelope(pid, hub_key, principal="OWNER")
        p = self.bus.run(HUB, "approve", pid, "--envelope", as_owner)
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertIn("BAD signature", p.stdout)


@unittest.skipUnless(HAVE_SSH, "no ssh-keygen")
class B_ApprovalAttacks(Base):
    """Attacks on the approval found by adversarial review of the first cut of row B; each one
    went through on that cut (a926c04) and is refused now."""

    def test_pinned_human_key_survives_a_swapped_shared_key(self):   # red on first cut
        owner = self.bus.human_key()
        pinned = os.path.join(self.bus.root, "_hub_local_pins")
        os.makedirs(pinned)
        shutil.copy(owner + ".pub", os.path.join(pinned, "OWNER.pub"))
        self.bus.pins = {HUB: {"CONSENSUS_HUMAN_SIGNERS_DIR": pinned}}
        self.bus.config(human_principals=["OWNER"])
        pid = self.bus.propose(HUB, "delete the production database", tier="2")
        # a machine that can write the bus replaces OWNER.pub with its own key and signs
        evil = self.bus.human_key("EVILTMP")
        shutil.copy(evil + ".pub", os.path.join(self.bus.root, "_engine", "signers", "OWNER.pub"))
        forged = self.bus.signed_envelope(pid, evil, principal="OWNER")
        p = self.bus.run(HUB, "approve", pid, "--envelope", forged)
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertEqual(self.bus.run(HUB, "commit", pid).returncode, 1)
        # the real human, verified against the pin, still gets through
        self.assertEqual(self.bus.run(HUB, "approve", pid, "--envelope",
                                      self.bus.signed_envelope(pid, owner)).returncode, 0)
        self.assertEqual(self.bus.run(HUB, "commit", pid).returncode, 0)

    def test_expired_approval_appended_by_hand_does_not_unlock_commit(self):   # red on first cut
        key = self.bus.human_key()
        self.bus.config(human_principals=["OWNER"])
        pid = self.bus.propose(HUB, "rotate the production API token", tier="2")
        stale = json.load(open(self.bus.signed_envelope(pid, key, ttl="-60"), encoding="utf-8"))
        with open(os.path.join(self.bus.dd, "log-EVIL.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"event_id": "stale-ok", "proposal_id": self.bus.full_id(pid),
                                 "type": "HUMAN_APPROVED", "actor": "EVIL", "seq": 50,
                                 "ts": "2026-07-11T00:00:00.000000Z", "approval": stale}) + "\n")
        self.assertEqual(self.bus.run(HUB, "commit", pid).returncode, 1)

    def test_unreadable_enforcement_setting_fails_closed(self):   # red on first cut
        self.bus.init_key(HUB)
        self.bus.config(sig_enforce_after=True)
        pid = self.bus.propose(HUB, "add a health dashboard tile")
        with open(os.path.join(self.bus.dd, "log-attacker.jsonl"), "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"event_id": "forged", "proposal_id": self.bus.full_id(pid),
                                 "type": "ACCEPT", "actor": LAPTOP, "seq": 9,
                                 "ts": "2026-07-11T00:00:00.000000Z"}) + "\n")
        st = self.bus.status(HUB, pid)
        self.assertIn("[PROPOSED]", st)
        self.assertIn("QUARANTINE", st)


@unittest.skipUnless(HAVE_SSH, "no ssh-keygen")
class A_SignaturesOnLoad(Base):
    """README/fleet_sign: events are signed per machine; a synced line is not state by itself."""

    def keys(self):
        self.bus.init_key(HUB)
        self.bus.init_key(LAPTOP)

    def forge(self, name, ev):
        with open(os.path.join(self.bus.dd, "log-%s.jsonl" % name), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(ev) + "\n")

    def test_hand_written_line_does_not_change_status(self):   # red on main
        self.keys()
        self.bus.config(sig_enforce_after="*")
        pid = self.bus.propose(HUB, "add a health dashboard tile")
        self.forge("attacker", {"event_id": "forged-1", "proposal_id": self.bus.full_id(pid),
                                "type": "ACCEPT", "actor": LAPTOP, "seq": 99,
                                "ts": "2026-07-11T00:00:00.000000Z", "text": "ok"})
        st = self.bus.status(HUB, pid)
        self.assertIn("[PROPOSED]", st)
        self.assertIn("QUARANTINE", st)
        # and the real, signed accept still works
        self.assertEqual(self.bus.run(LAPTOP, "respond", pid, "accept", "ok").returncode, 0)
        self.assertIn("[AGREED]", self.bus.status(HUB, pid))

    def test_tampered_signed_line_is_quarantined_even_in_dark_mode(self):   # red on main
        self.keys()
        pid = self.bus.propose(HUB, "add a health dashboard tile")
        self.bus.run(LAPTOP, "respond", pid, "reject", "no")
        self.bus.rewrite(LAPTOP, lambda ev: dict(ev, type="ACCEPT", text="yes"))
        st = self.bus.status(HUB, pid)
        self.assertNotIn("[AGREED]", st, "an edited signed line was trusted")
        self.assertIn("bad-signature", st)

    def test_a_key_speaks_only_for_its_own_machine(self):
        self.keys()
        self.bus.init_key("ATTACKER")             # a registered machine, but not the laptop
        self.bus.config(sig_enforce_after="*")
        pid = self.bus.propose(HUB, "add a health dashboard tile")
        ev = {"event_id": "imp-1", "proposal_id": self.bus.full_id(pid), "type": "ACCEPT",
              "actor": LAPTOP, "seq": 99, "ts": "2026-07-11T00:00:00.000000Z", "text": "ok"}
        code = ("import sys,json; sys.path.insert(0,%r); import fleet_sign as f; "
                "print(json.dumps(f.sign_event(json.loads(sys.argv[1]))))" % HERE)
        sig = json.loads(subprocess.run([sys.executable, "-c", code, json.dumps(ev)],
                                        env=self.bus.env("ATTACKER"), capture_output=True,
                                        text=True, check=True).stdout)
        self.forge("ATTACKER", dict(ev, sig=sig, signer="ATTACKER"))
        st = self.bus.status(HUB, pid)
        self.assertIn("[PROPOSED]", st)
        self.assertIn("QUARANTINE", st)

    def test_signed_fleet_runs_normally_under_strict_mode(self):
        self.keys()
        self.bus.config(sig_enforce_after="*")
        pid = self.bus.propose(LAPTOP, "adopt nightly reindex at 04:00")
        self.bus.run(HUB, "respond", pid, "accept", "ok")
        self.assertEqual(self.bus.run(HUB, "commit", pid).returncode, 0)
        st = self.bus.status(HUB, pid)
        self.assertIn("[COMMITTED]", st)
        self.assertIn("quarantined=0", st)
        self.assertTrue(all(e.get("sig") for e in self.bus.events()))

    def test_dark_mode_says_it_is_dark(self):
        pid = self.bus.propose(HUB, "add a health dashboard tile")
        self.assertIn("DARK", self.bus.status(HUB, pid))

@unittest.skipUnless(HAVE_SSH, "no ssh-keygen")
class ReviewFindings_Approval(Base):
    """Found by the review bots on the signed-approval pull request; each was red before its fix."""

    def full_ledger_line(self, machine, ev):
        """Sign ev with `machine`'s own key, exactly as a process holding that key could."""
        code = ("import sys,json; sys.path.insert(0,%r); import fleet_sign as f; "
                "print(json.dumps(f.sign_event(json.loads(sys.argv[1]))))" % HERE)
        sig = json.loads(subprocess.run([sys.executable, "-c", code, json.dumps(ev)],
                                        env=self.bus.env(machine), capture_output=True,
                                        text=True, check=True).stdout)
        return dict(ev, sig=sig, signer=machine)

    def append(self, name, ev):
        with open(os.path.join(self.bus.dd, "log-%s.jsonl" % name), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(ev) + "\n")

    def test_displayed_subject_must_match_the_action(self):
        key = self.bus.human_key()
        self.bus.config(human_principals=["OWNER"])
        pid = self.bus.propose(HUB, "delete the production database", tier="2")
        for patch in ({"subject": "rename a dashboard tile"}, {"risk_tier": 0}):
            p = self.bus.run(HUB, "approve", pid, "--envelope",
                             self.bus.signed_envelope(pid, key, patch=patch))
            self.assertEqual(p.returncode, 1, p.stdout)
        self.assertEqual(self.bus.run(HUB, "commit", pid).returncode, 1)

    def test_envelope_shows_the_position_being_approved(self):
        self.bus.human_key()
        self.bus.config(human_principals=["OWNER"])
        pid = self.bus.propose(HUB, "rotate the production API token", tier="2")
        self.bus.run(LAPTOP, "respond", pid, "counter", "rotate ALL tokens instead")
        env = json.loads(self.bus.run(HUB, "approval-request", pid).stdout)
        self.assertEqual(env["action"]["text"], "rotate ALL tokens instead")
        self.assertEqual(env["action"]["position_type"], "COUNTER")

    def test_human_device_refuses_a_display_that_does_not_hash_to_the_digest(self):
        key = self.bus.human_key()
        self.bus.config(human_principals=["OWNER"])
        pid = self.bus.propose(HUB, "delete the production database", tier="2")
        env = json.loads(self.bus.run(HUB, "approval-request", pid).stdout)
        env["action"]["subject"] = "rename a dashboard tile"
        ask = os.path.join(self.bus.root, "ask-doctored.json")
        with open(ask, "w", encoding="utf-8") as fh:
            json.dump(env, fh)
        p = self.bus.run("OWNER-DEVICE", "sign-approval", ask, "--key", key)
        self.assertEqual(p.returncode, 1, p.stdout)
        self.assertNotIn('"sig"', p.stdout)

    def test_non_string_nonce_is_refused_and_cannot_poison_the_row(self):
        key = self.bus.human_key()
        self.bus.config(human_principals=["OWNER"])
        pid = self.bus.propose(HUB, "rotate the production API token", tier="2")
        bad = self.bus.signed_envelope(pid, key, patch={"nonce": ["x"]})
        p = self.bus.run(HUB, "approve", pid, "--envelope", bad)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        self.append("EVIL", {"event_id": "np", "proposal_id": self.bus.full_id(pid), "type": "HUMAN_APPROVED",
                             "actor": "EVIL", "seq": 50, "ts": "2026-07-11T00:00:00.000000Z",
                             "approval": json.load(open(bad, encoding="utf-8"))})
        p = self.bus.run(HUB, "commit", pid)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        p = self.bus.run(HUB, "approve", pid, "--envelope", self.bus.signed_envelope(pid, key))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_signed_commit_without_approval_is_not_a_decision(self):
        self.bus.init_key(HUB)
        self.bus.human_key()
        self.bus.config(human_principals=["OWNER"], sig_enforce_after="*")
        pid = self.bus.propose(HUB, "delete the production database", tier="2")
        full = self.bus.full_id(pid)
        self.append(HUB, self.full_ledger_line(HUB, {
            "event_id": "bypass", "proposal_id": full, "type": "COMMIT", "actor": HUB, "seq": 90,
            "ts": "2026-11-02T00:00:00.000000Z", "subject": "delete the production database"}))
        st = self.bus.status(HUB, pid)
        self.assertNotIn("[COMMITTED]", st)
        self.assertIn("tier2-decision-without-approval", st)

    def test_commit_with_a_real_approval_survives_the_approvals_expiry(self):
        key = self.bus.human_key()
        self.bus.config(human_principals=["OWNER"])
        pid = self.bus.propose(HUB, "rotate the production API token", tier="2")
        soon = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=4)
                ).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        env = self.bus.signed_envelope(pid, key, patch={"expires_at": soon})
        self.assertEqual(self.bus.run(HUB, "approve", pid, "--envelope", env).returncode, 0)
        self.assertEqual(self.bus.run(HUB, "commit", pid).returncode, 0)
        time.sleep(4.5)
        self.assertIn("[COMMITTED]", self.bus.status(HUB, pid))

    def test_hostile_tier_does_not_crash_the_approval_path(self):
        key = self.bus.human_key()
        self.bus.config(human_principals=["OWNER"])
        with open(os.path.join(self.bus.dd, "log-EVIL.jsonl"), "w", encoding="utf-8") as fh:
            fh.write('{"event_id": "ht", "proposal_id": "p-hostile", "type": "PROPOSE", "actor": "EVIL", '
                     '"seq": 1, "risk_tier": "many", "ts": "2026-07-11T00:00:00.000000Z", "subject": "x"}\n')
        req = self.bus.run(HUB, "approval-request", "p-hostile")
        self.assertEqual(req.returncode, 0, req.stderr)
        self.assertEqual(json.loads(req.stdout)["risk_tier"], 2)
        p = self.bus.run(HUB, "approve", "p-hostile", "--envelope", self.bus.signed_envelope("p-hostile", key))
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_copied_nonce_on_an_unsigned_line_cannot_void_a_real_approval(self):
        key = self.bus.human_key()
        self.bus.config(human_principals=["OWNER"])
        pid = self.bus.propose(HUB, "rotate the production API token", tier="2")
        good = self.bus.signed_envelope(pid, key)
        nonce = json.load(open(good, encoding="utf-8"))["nonce"]
        forged = {"event_id": "noise", "proposal_id": "p-other", "type": "HUMAN_APPROVED", "actor": "EVIL",
                  "seq": 3, "ts": "2026-07-11T00:00:00.000000Z", "approval": {"nonce": nonce}}
        self.append("EVIL", forged)
        p = self.bus.run(HUB, "approve", pid, "--envelope", good)
        self.assertEqual(p.returncode, 0, "a junk line blocked approve: " + p.stdout)
        self.append("EVIL", dict(forged, event_id="noise2"))
        self.assertEqual(self.bus.run(HUB, "commit", pid).returncode, 0, "a junk line voided the approval")
        self.append("EVIL", dict(forged, event_id="noise3"))
        self.assertIn("[COMMITTED]", self.bus.status(HUB, pid), "a junk line dropped the decision")

    def test_pinning_machine_keys_does_not_hide_the_human_key(self):
        self.bus.init_key(HUB)
        key = self.bus.human_key()
        pinned = os.path.join(self.bus.root, "_hub_machine_pins")
        os.makedirs(pinned)
        shutil.copy(os.path.join(self.bus.root, "_engine", "signers", HUB + ".pub"), pinned)
        self.bus.pins = {HUB: {"FLEET_SIGNERS_DIR": pinned}}
        self.bus.config(human_principals=["OWNER"])
        pid = self.bus.propose(HUB, "rotate the production API token", tier="2")
        p = self.bus.run(HUB, "approve", pid, "--envelope", self.bus.signed_envelope(pid, key))
        self.assertEqual(p.returncode, 0, p.stdout)

    def test_ttl_must_be_a_positive_number(self):
        self.bus.human_key()
        self.bus.config(human_principals=["OWNER"])
        pid = self.bus.propose(HUB, "rotate the production API token", tier="2")
        for bad in ("abc", "-1", "0"):
            p = self.bus.run(HUB, "approval-request", pid, "--ttl-min", bad)
            self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
            self.assertNotIn("Traceback", p.stderr)

    def test_human_device_shows_terms_and_refuses_a_doctored_display(self):
        key = self.bus.human_key()
        self.bus.config(human_principals=["OWNER"])
        pid = self.bus.propose(HUB, "delete the production database", tier="2")
        env = json.loads(self.bus.run(HUB, "approval-request", pid).stdout)
        ask = os.path.join(self.bus.root, "ask-terms.json")
        with open(ask, "w", encoding="utf-8") as fh:
            json.dump(env, fh)
        p = self.bus.run("OWNER-DEVICE", "sign-approval", ask, "--key", key)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn(env["expires_at"], p.stderr)
        self.assertIn("OWNER", p.stderr)
        for patch in ({"subject": "rename a tile"}, {"risk_tier": 0}, {"nonce": ""}):
            with open(ask, "w", encoding="utf-8") as fh:
                json.dump(dict(env, **patch), fh)
            p = self.bus.run("OWNER-DEVICE", "sign-approval", ask, "--key", key)
            self.assertEqual(p.returncode, 1, str(patch) + p.stdout)
            self.assertNotIn('"sig"', p.stdout)


@unittest.skipUnless(HAVE_SSH, "no ssh-keygen")
class ReviewFindings_Signatures(Base):

    def test_failed_rotation_keeps_the_old_key(self):
        self.bus.init_key(HUB)
        key = os.path.join(self.bus.root, "_local", HUB, "claude-fleet", "machine_ed25519")
        before = open(key, encoding="utf-8").read()
        env = dict(self.bus.env(HUB), SSH_KEYGEN=sys.executable)   # exists everywhere, and rejects ssh-keygen's flags
        p = subprocess.run([sys.executable, SIGNER, "init", "--force"], env=env, capture_output=True, timeout=120)
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(open(key, encoding="utf-8").read(), before, "a failed rotation lost the private key")

    def test_init_publishes_the_key_this_machine_can_sign_with(self):
        self.bus.init_key(HUB)
        local = os.path.join(self.bus.root, "_local", HUB, "claude-fleet", "machine_ed25519.pub")
        other = self.bus.human_key("OTHERKEY")
        shutil.copy(other + ".pub", local)     # a rotation interrupted between the two swaps
        self.bus.init_key(HUB)
        published = open(os.path.join(self.bus.root, "_engine", "signers", HUB + ".pub"), encoding="utf-8").read()
        self.assertNotEqual(published.split()[:2], open(other + ".pub", encoding="utf-8").read().split()[:2])
        self.assertEqual(open(local, encoding="utf-8").read().split()[:2], published.split()[:2],
                         "the local public key stayed behind the key it belongs to")

    def test_empty_env_pin_does_not_switch_enforcement_off(self):
        self.bus.init_key(HUB)
        self.bus.config(sig_enforce_after="*")
        self.bus.pins = {HUB: {"CONSENSUS_SIG_ENFORCE_AFTER": ""}}
        pid = self.bus.propose(HUB, "add a health dashboard tile")
        self.assertIn("enforce=*", self.bus.status(HUB, pid))

    def test_impossible_enforcement_timestamp_fails_closed(self):
        import protocol_guards as pg
        for bad in ("9999-99-99T99:99:99Z", "2026-13-45T00:00:00Z"):
            self.assertFalse(pg.sig_enforced({"ts": "2026-01-01T00:00:00Z"}, bad, None)[0], bad)
        self.assertFalse(pg.sig_enforced({"ts": "2026-13-45T00:00:00Z"},
                                         "2027-01-01T00:00:00Z", None)[0], "impossible event ts")
        self.assertTrue(pg.sig_enforced({"ts": "2026-01-01T00:00:00.5Z"},
                                        "2027-01-01T00:00:00Z", None)[0], "real grandfathering stays")

    def test_verdict_cache_is_not_proof_by_default(self):
        self.bus.init_key(HUB)
        self.bus.init_key(LAPTOP)
        self.bus.init_key("ATTACKER")
        self.bus.config(sig_enforce_after="*")
        pid = self.bus.propose(HUB, "add a health dashboard tile")
        ev = {"event_id": "cache-1", "proposal_id": self.bus.full_id(pid), "type": "ACCEPT",
              "actor": LAPTOP, "seq": 99, "ts": "2026-07-11T00:00:00.000000Z", "text": "ok"}
        code = ("import sys,json; sys.path.insert(0,%r); import fleet_sign as f; ev=json.loads(sys.argv[1]); "
                "print(json.dumps({'sig':f.sign_event(ev),'fp':f.registry_fingerprint()}))" % HERE)
        out = json.loads(subprocess.run([sys.executable, "-c", code, json.dumps(ev)],
                                        env=self.bus.env("ATTACKER"), capture_output=True,
                                        text=True, check=True).stdout)
        line = json.dumps(dict(ev, sig=out["sig"], signer=LAPTOP))
        with open(os.path.join(self.bus.dd, "log-ATTACKER.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        keydir = os.path.join(self.bus.root, "_local", HUB, "claude-fleet")
        with open(os.path.join(keydir, "verified-events.txt"), "a", encoding="utf-8") as fh:
            fh.write("%s %s\n" % (out["fp"], hashlib.sha256(line.encode("utf-8")).hexdigest()))
        st = self.bus.status(HUB, pid)
        self.assertNotIn("[AGREED]", st, "a line nobody verified was trusted because of a local file")
        self.assertIn("bad-signature", st)

    def test_key_rotation_keeps_history_and_revocation_still_kills_it(self):
        self.bus.init_key(HUB)
        pid = self.bus.propose(HUB, "add a health dashboard tile")
        subprocess.run([sys.executable, SIGNER, "init", "--force"], env=self.bus.env(HUB), check=True,
                       capture_output=True, timeout=120)
        st = self.bus.status(HUB, pid)
        self.assertIn("[PROPOSED]", st, "a routine rotation erased a signed proposal")
        self.assertNotIn("bad-signature", st)
        self.assertEqual(self.bus.run(LAPTOP, "respond", pid, "accept", "ok").returncode, 0)
        self.bus.run(HUB, "respond", pid, "counter", "signed with the new key")
        self.assertNotIn("bad-signature", self.bus.status(HUB, pid))
        with open(os.path.join(self.bus.root, "_engine", "signers", "REVOKED.txt"), "w", encoding="utf-8") as fh:
            fh.write(HUB + ".retired-1\n")
        st = self.bus.status(HUB, pid)
        self.assertIn("bad-signature", st, "a revoked old key must stop counting")

    def test_unsigned_write_is_refused_under_enforcement(self):
        self.bus.init_key(HUB)
        self.bus.config(sig_enforce_after="*")
        pid = self.bus.propose(HUB, "add a health dashboard tile")
        before = len(self.bus.events())
        p = self.bus.run(LAPTOP, "respond", pid, "accept", "ok")       # LAPTOP has no key
        self.assertNotEqual(p.returncode, 0, p.stdout)
        self.assertEqual(len(self.bus.events()), before, "a line nobody will ever trust was written")
        self.assertNotIn("ACCEPT #", p.stdout)


class TestBusSendTotalBudget(unittest.TestCase):
    """bus_send.py: the whole send has ONE finite budget, per-call caps no longer stack."""

    def setUp(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("bus_send_budget", os.path.join(HERE, "bus_send.py"))
        self.bs = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.bs)

    def test_each_call_gets_at_most_the_time_left(self):
        self.bs._BUS_DEADLINE = self.bs.time.monotonic() + 5
        self.assertLessEqual(self.bs._remaining(90), 5.0)
        self.assertLessEqual(self.bs.TOTAL_BUS_TIMEOUT_S, 210)

    def test_spent_budget_fails_the_rail_instead_of_waiting(self):
        self.bs._BUS_DEADLINE = self.bs.time.monotonic() - 1
        ok, info = self.bs._send_file_rail("ALL", "x")
        self.assertFalse(ok)
        self.assertEqual(info, "bus-deadline-spent")


if __name__ == "__main__":
    print("engine under test: %s" % ENGINE)
    unittest.main(verbosity=2)
