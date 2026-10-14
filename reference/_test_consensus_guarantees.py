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

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.environ.get("CONSENSUS_ENGINE") or os.path.join(HERE, "consensus.py")
HUB, LAPTOP = "HUB-1", "LAPTOP-2"


class Bus:
    def __init__(self):
        self.root = tempfile.mkdtemp(prefix="consensus-test-")
        self.dd = os.path.join(self.root, "_decisions")
        os.makedirs(self.dd)
        with open(os.path.join(self.dd, "consensus.json"), "w", encoding="utf-8") as fh:
            json.dump({"leader": HUB, "round_cap": 3, "timeout_min": 30}, fh)

    def run(self, machine, *args):
        env = dict(os.environ, MACHINE_KEY=machine, MACHINE_BUS_DIR=self.root,
                   CONSENSUS_NO_BUS="1", PYTHONIOENCODING="utf-8")
        env.pop("CONSENSUS_DRY", None)
        return subprocess.run([sys.executable, ENGINE, *args], env=env, capture_output=True,
                              text=True, encoding="utf-8", errors="replace")

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


if __name__ == "__main__":
    print("engine under test: %s" % ENGINE)
    unittest.main(verbosity=2)
