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
SIGNER = os.path.join(HERE, "fleet_sign.py")
HUB, LAPTOP = "HUB-1", "LAPTOP-2"
sys.path.insert(0, HERE)
import fleet_sign  # noqa: E402  (to locate a working ssh-keygen; the engine under test is a CLI)


def _have_ssh_keygen():
    try:
        subprocess.run([fleet_sign._ssh_keygen(), "-h"], capture_output=True, timeout=20)
        return True
    except Exception:
        return False


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
        args = ["approval-request", pid, "--ttl-min", ttl]
        if principal:
            args += ["--principal", principal]
        req = self.run(HUB, *args)
        assert req.returncode == 0, req.stdout + req.stderr
        env = json.loads(req.stdout)
        if patch:
            env.update(patch)
        ask = os.path.join(self.root, "ask-%s.json" % env["nonce"][:8])
        with open(ask, "w", encoding="utf-8") as fh:
            json.dump(env, fh)
        out = ask.replace("ask-", "signed-")
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


if __name__ == "__main__":
    print("engine under test: %s" % ENGINE)
    unittest.main(verbosity=2)
