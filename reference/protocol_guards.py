# -*- coding: utf-8 -*-
r"""protocol_guards.py - the four guards that grew around the consensus core.

`consensus.py` is the negotiation: propose -> counter -> accept -> commit. It shipped first
because it is the part people ask for. Then we ran it across six machines for a month, and
four separate things bit us. Each fix is a small deterministic guard that wraps the core
without changing the protocol, and each is here as a pure function with a selftest:

  1. ARBITER ELECTION      the tie-break role was pinned to one named machine. That machine
                           dying wedged every tier-0 proposal in the fleet, silently.
  2. PROOF GRADING         "verified" was a free-text field, so "looks fine to me" counted as
                           evidence and a task went green on a sentence.
  3. RISK TRACKING         risk was one integer chosen by the proposing agent. A sensitive
                           action carried at a low tier walked straight past the human gate.
  4. SIGNATURE AUDIT       "who said this" was a machine name inside a filename. Anything
                           able to write to the shared folder could be any machine.

None of these is exotic. All four are the same shape: something the protocol TRUSTED without
CHECKING. If you run agents across machines you will meet all four, roughly in this order.

    python protocol_guards.py selftest

Sanitized from the live implementation: machine names, chat ids and paths are placeholders,
the reasoning in the comments is verbatim.
"""
import os
import re
import sys
import glob
import json
import time
import datetime

# ---------------------------------------------------------------------------
# 1. ARBITER ELECTION - the tie-break role must survive its own holder dying
# ---------------------------------------------------------------------------
# The original design named one leader in config and gave it the tie-break function (resolve a
# tier-0 proposal whose round cap or timeout expired). Single-writer, deterministic, fine - until
# the leader is the machine that dies. Then nothing re-assigns the role: tier-0 proposals sit
# unresolved and NOBODY IS ALERTED, because every remaining peer is behaving correctly.
#
# The fix reuses the presence lease the fleet already writes (a per-machine heartbeat file whose
# mtime is the only signal) - no new heartbeat, no new state to keep in sync:
#
#   * every ticking peer computes the arbiter the SAME way from the SAME ordered list, so all
#     peers agree on who arbitrates this tick without exchanging a single message;
#   * a machine is eligible only on POSITIVE evidence of being alive (fresh stamp). An ABSENT
#     stamp does not promote anybody - fail-safe, because "no evidence" is not "dead";
#   * ME is always eligible (I am running, therefore I am awake);
#   * a travelling laptop at the head of the list never wedges the fleet: it is skipped to the
#     next live machine and reclaims the role when its stamp goes fresh again.
#
# The ordered list is a human decision (whose machine should arbitrate first), so it lives in
# config, not in code. Ours is: always-on hub, then the anchor VPS, then laptops.
LEADER_DOWN_MIN_DEFAULT = 70          # minutes of stale presence before we consider it down
PRESENCE_PREFIX = ".robot-alive-"     # <bus>/.robot-alive-<MACHINE>.log, mtime is the signal


def presence_age_min(machine, bus_dir, now=None):
    """Age in MINUTES of a machine's presence stamp; None if there is no stamp at all.
    None is deliberately NOT treated as 'down' by the caller - see the fail-safe note above."""
    try:
        p = os.path.join(bus_dir, "%s%s.log" % (PRESENCE_PREFIX, machine))
        return ((now or time.time()) - os.path.getmtime(p)) / 60.0
    except OSError:
        return None


def elect_arbiter(cfg, me, bus_dir, now=None, ages=None):
    """Who holds the tie-break function THIS tick, and did we fail over?

    cfg keys:
      arbiter_order        ordered list of machine names, most preferred first
      arbiter_order_armed  bool - when false, fall back to the fixed-leader behaviour so that
                           a peer running an older build behaves IDENTICALLY. Rolling out a new
                           election rule to half a fleet is how you get two arbiters.
      leader, fallback_arbiter, leader_down_min

    `ages` is an injectable {machine: age_min|None} for testing; production reads the stamps.
    Returns (arbiter, failed_over, age_of_arbiter_or_None).
    """
    thresh = int(cfg.get("leader_down_min") or LEADER_DOWN_MIN_DEFAULT)

    def age(m):
        if ages is not None:
            return ages.get(m)
        return presence_age_min(m, bus_dir, now)

    order = cfg.get("arbiter_order")
    if order and cfg.get("arbiter_order_armed"):
        for m in order:
            if m == me:                          # I am running, so I am alive
                return m, (m != order[0]), None
            a = age(m)
            if a is not None and a <= thresh:
                return m, (m != order[0]), a
        # nobody provably alive: last resort is the head, flagged as a failover so it gets said
        # out loud rather than pretended to be normal
        return order[0], True, None

    # ---- legacy fixed-leader path, unchanged on purpose ----
    leader = cfg.get("leader")
    if leader == me:
        return leader, False, None
    a = age(leader)
    if a is not None and a > thresh:
        return (cfg.get("fallback_arbiter") or (order[0] if order else leader)), True, a
    return leader, False, a


def leaderdown_transition(state, failed_over):
    """Edge detector so a leader-down episode is announced ONCE, not once per tick.

    Returns (new_state, announcement|None). Only the machine that actually took over should
    call this, otherwise N peers N-plex the same alert - alert fatigue is how real alerts get
    ignored. State is a small dict you persist next to the ledger.
    """
    was_down = bool(state.get("down"))
    if failed_over and not was_down:
        return {"down": True}, "LEADER_DOWN"
    if not failed_over and was_down:
        return {"down": False}, "LEADER_RECOVERED"
    return state, None


# ---------------------------------------------------------------------------
# 2. PROOF GRADING - "verified" has to mean something a machine can check
# ---------------------------------------------------------------------------
# The protocol has a VERIFY event carrying a free-text proof. In practice agents wrote things
# like "checked, works" and the task went green. A claim is not evidence. So a proof is graded
# `proven` only if it carries a pattern that a machine could not have written without actually
# running something: a zero exit code, a hash, a counter that moved, a before/after pair.
#
# This is not a language check, it is a check for the RESIDUE of an action. An agent that wants
# to fake it now has to fabricate a specific number, which is a much brighter line to cross and
# a much easier lie to catch.
#
# Grading is armed from a timestamp, so every event recorded before you turned it on is
# grandfathered. Retroactive standards invalidate honest history and nobody trusts the ledger
# after that.
_PROOF_PATTERNS = (
    re.compile(r"(?<!\w)(?:exit(?:\s*code)?|rc|returncode|errorlevel)\s*[=:]?\s*0(?!\d)", re.I),
    re.compile(r"\b(?:[0-9a-f]{64}|[0-9a-f]{40}|[0-9a-f]{32})\b", re.I),      # sha256/sha1/md5
    re.compile(r"\d+\s*(?:->|=>|→)\s*\d+"),                              # counter N -> M
    re.compile(r"(?:before|was)\W{0,3}\d+.{0,60}?(?:after|now)\W{0,3}\d+", re.I | re.S),
)


def proof_grade(proof):
    """'proven' iff the text carries deterministic evidence of an action, else 'unproven'."""
    return "proven" if any(p.search(proof or "") for p in _PROOF_PATTERNS) else "unproven"


def verify_accepted(event, armed_after):
    """Is this VERIFY event good enough to count? Events at or before `armed_after` are
    grandfathered. Pass armed_after='' to keep grading dark (audit only)."""
    if not armed_after:
        return True
    if (event.get("ts") or "") <= armed_after:
        return True
    return proof_grade(event.get("proof", "")) == "proven"


# ---------------------------------------------------------------------------
# 3. RISK TRACKING - one integer chosen by the proposer is not a risk model
# ---------------------------------------------------------------------------
# The human gate fires on tier >= 2, and the tier is set by the agent making the proposal. That
# is the weakest link in the whole design, and we already guard it with a keyword tripwire that
# force-bumps obvious cases (see consensus.py `_tripwire_hit`).
#
# The tripwire catches dangerous WORDS. It does not catch a dangerous CATEGORY carried at a low
# tier: "update the outreach template" is tier-0 by any reasonable reading and still ends with
# text going to strangers. So we classify a proposal into a track as well, independently of the
# tier the proposer chose.
#
# Note how this ships: in SHADOW. It classifies, it logs, it changes nothing. Because the honest
# question is not "is a second signal nicer" but "does it catch anything the tier alone misses",
# and you can only answer that with a log. `would_benefit` is written to be FALSIFIABLE: it is
# False for anything already tier-2 (the engine escalates it anyway) and False for the general
# track. It is True only for the narrow case this guard exists for - a sensitive track carried
# at a low tier. If that stays at zero for a month, the guard is not needed and we delete it.
V2_TRACKS = (
    ("financial", ("payment", "invoice", "trade", "purchase", "transfer", "crypto", "budget",
                   "spend", "refund", "payout", "wire")),
    ("secrets",   ("secret", "password", "token", "credential", "api key", "api-key",
                   "private key", "seed phrase", "mnemonic")),
    ("outbound",  ("outreach", "email", "publish", "broadcast", "reply", "post to", "tweet",
                   "comment", " dm ", "lead", "message the client")),
    ("canon",     ("canon", "rulebook", "policy", "claude.md", "memory.md", "bible",
                   "operating-agreement", "standing-", "always-loaded")),
    ("infra",     ("sync", "heartbeat", "leader", "arbiter", "fence", "shard", "consensus",
                   " cron", "scheduled", "watchdog", "backup", "config", "engine", "ledger",
                   "quorum")),
)
SENSITIVE_TRACKS = ("financial", "secrets", "outbound", "canon")


def classify_track(subject, details=None):
    hay = subject or ""
    if isinstance(details, dict):
        hay += " " + json.dumps(details, ensure_ascii=False)
    elif details:
        hay += " " + str(details)
    hay = hay.lower()
    for name, keywords in V2_TRACKS:
        if any(k in hay for k in keywords):
            return name
    return "general"


def classify_risk(subject, details=None, tier=0, tripwire_hit=False):
    """Deterministic typing plus an HONEST verdict on whether this guard earned its keep."""
    tier = int(tier or 0)
    track = classify_track(subject, details)
    sensitive = track in SENSITIVE_TRACKS
    freeze = sensitive or tier >= 2 or bool(tripwire_hit)
    if tier >= 2:
        return {"risk_tier": tier, "track": track, "freeze": freeze, "would_benefit": False,
                "reason": "tier-2 already escalates to a human; the track adds no routing",
                "tripwire": bool(tripwire_hit)}
    if sensitive:
        return {"risk_tier": tier, "track": track, "freeze": freeze, "would_benefit": True,
                "reason": "track=%s at tier-%d would be frozen; the tier-only engine auto-paths it"
                          % (track, tier), "tripwire": bool(tripwire_hit)}
    return {"risk_tier": tier, "track": track, "freeze": freeze, "would_benefit": False,
            "reason": "track=general, tier<2, no freeze - the tier-only path is adequate",
            "tripwire": bool(tripwire_hit)}


def shadow_observe(record_dir, me, event, subject, details, tier, tripwire_hit, build="shadow-1"):
    """Append one shadow observation. Wrapped so a bug in a DARK sensor can never break a live
    proposal - that is the whole point of shadow-first: dark code in a live engine must fail
    silently, not brick the engine. Per-machine file = single writer = no sync conflict."""
    try:
        v2 = classify_risk(subject, details, tier, tripwire_hit)
        rec = {"ts": datetime.datetime.now().isoformat(timespec="seconds"), "machine": me,
               "build": build, "kind": "would_append",
               "event": {"proposal_id": event.get("proposal_id"), "type": "PROPOSE",
                         "subject": (subject or "")[:70]},
               "v2": v2}
        os.makedirs(record_dir, exist_ok=True)
        with open(os.path.join(record_dir, "observe-%s.jsonl" % me), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return v2
    except Exception as e:                                   # never propagate
        sys.stderr.write("shadow observe skipped: %s\n" % e)
        return None


def shadow_report(record_dir):
    """Read the shadow log back and answer the only question that matters: how many proposals
    would this guard have caught that the tier alone missed? Zero for a month = delete it."""
    total = caught = 0
    tracks = {}
    for path in glob.glob(os.path.join(record_dir, "observe-*.jsonl")):
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                try:
                    v2 = json.loads(line)["v2"]
                except Exception:
                    continue
                total += 1
                tracks[v2["track"]] = tracks.get(v2["track"], 0) + 1
                if v2.get("would_benefit"):
                    caught += 1
    return {"observed": total, "would_have_caught": caught, "tracks": tracks}


# ---------------------------------------------------------------------------
# 4. SIGNATURE AUDIT - identity by filename is not identity
# ---------------------------------------------------------------------------
# Every event carries `actor`, and the shard it lives in is named after a machine. Both are just
# strings written by whoever had write access to a shared folder. So the ledger's answer to
# "who decided this" was "whoever claimed to".
#
# The full identity layer is `fleet_sign.py` next to this file (Ed25519 via ssh-keygen, one public
# key file per machine, revocation list, allowed-signers assembled in memory at each verify).
# Here is only the part that belongs to the protocol: the audit, and the enforcement threshold.
#
# The rollout order is the interesting bit, and it is the same shape as guard 3:
#   1. sign everything, verify nothing, count failures  (dark)
#   2. audit command a human can run and read           (visible)
#   3. only then set `sig_enforce_after` to a timestamp (armed, and only forward in time)
# Flipping straight to enforcement quarantines every node that has not rolled out a key yet,
# which is the whole fleet on day one.


_ISO_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z?$")


def sig_enforced(event, enforce_after, verify_result):
    """Should this event be trusted, given the enforcement threshold?

    verify_result: True = good signature, False = BAD signature, None = unsigned.
    Returns (accepted, reason). A BAD signature is never accepted, threshold or not: an absent
    signature is a rollout gap, a wrong one is a tampering signal, and those are not the same
    thing. Conflating them is why "we have signatures" often means nothing.
    """
    if verify_result is False:
        return False, "bad-signature"
    if not enforce_after:
        return True, "dark"                          # audit-only phase
    # enforce_after = "*" means STRICT: no grandfathering at all. A timestamp threshold trusts
    # the event's own `ts` to decide whether it is old enough to be grandfathered, and `ts` is
    # written by whoever wrote the line: a forged unsigned line simply backdates itself. Use a
    # timestamp only while legacy unsigned history still matters, then switch to "*".
    # A `ts` that is not a well-formed timestamp is never grandfathered ("!" sorts before "2").
    ts = event.get("ts") or ""
    if enforce_after != "*" and _ISO_TS.match(ts) and ts <= enforce_after:
        return True, "grandfathered"
    if verify_result is None:
        return False, "unsigned-after-enforcement"
    return True, "verified"


def audit_signatures(events, verify, enforce_after=""):
    """Count ok / bad / unsigned and list what would be quarantined. `verify` is a callable
    event -> True|False|None so this stays testable without keys or network."""
    ok = bad = unsigned = 0
    quarantined = []
    for ev in events:
        v = verify(ev)
        if v is True:
            ok += 1
        elif v is False:
            bad += 1
        else:
            unsigned += 1
        accepted, reason = sig_enforced(ev, enforce_after, v)
        if not accepted:
            quarantined.append({"id": (ev.get("proposal_id") or "?")[:8],
                                "type": ev.get("type"), "actor": ev.get("actor"),
                                "reason": reason})
    return {"ok": ok, "bad": bad, "unsigned": unsigned,
            "enforce_after": enforce_after or "DARK (audit only)",
            "quarantined": quarantined}


# ---------------------------------------------------------------------------
def _selftest():
    # ---- 1. arbiter election ----
    cfg = {"arbiter_order": ["LAPTOP-1", "HUB-1", "ANCHOR-1"], "arbiter_order_armed": True,
           "leader": "LAPTOP-1", "leader_down_min": 70}
    # head is alive -> head arbitrates, no failover
    a, fo, _ = elect_arbiter(cfg, "HUB-1", "", ages={"LAPTOP-1": 5})
    assert (a, fo) == ("LAPTOP-1", False), (a, fo)
    # head travelling (stale) -> next live machine takes over, and says so
    a, fo, _ = elect_arbiter(cfg, "ANCHOR-1", "", ages={"LAPTOP-1": 600, "HUB-1": 3})
    assert (a, fo) == ("HUB-1", True), (a, fo)
    # head has NO stamp at all -> absent is not dead, do NOT promote on missing evidence
    a, fo, _ = elect_arbiter(cfg, "ANCHOR-1", "", ages={"LAPTOP-1": None, "HUB-1": 3})
    assert a == "HUB-1" and fo is True, (a, fo)
    # every peer computes the same answer from the same inputs -> one arbiter, no split brain
    seen = {elect_arbiter(cfg, who, "", ages={"LAPTOP-1": 600, "HUB-1": 3, "ANCHOR-1": 4})[0]
            for who in ("ANCHOR-1", "NAT-1", "MAC-1")}
    assert seen == {"HUB-1"}, seen
    # I am always eligible, because I am the one running
    a, _, _ = elect_arbiter(cfg, "HUB-1", "", ages={"LAPTOP-1": 600, "HUB-1": None})
    assert a == "HUB-1", a
    # unarmed config must behave exactly like the old fixed-leader build
    old = dict(cfg, arbiter_order_armed=False, fallback_arbiter="HUB-1")
    a, fo, _ = elect_arbiter(old, "ANCHOR-1", "", ages={"LAPTOP-1": 5})
    assert (a, fo) == ("LAPTOP-1", False), (a, fo)
    # announce once, not per tick
    st, msg = leaderdown_transition({}, True)
    assert msg == "LEADER_DOWN"
    st, msg = leaderdown_transition(st, True)
    assert msg is None, "second tick must stay quiet"
    st, msg = leaderdown_transition(st, False)
    assert msg == "LEADER_RECOVERED"

    # ---- 2. proof grading ----
    assert proof_grade("looks fine to me") == "unproven"
    assert proof_grade("ran it, all good, trust me") == "unproven"
    assert proof_grade("exit code 0") == "proven"
    assert proof_grade("counter 41 -> 0") == "proven"
    assert proof_grade("sha256 " + "a" * 64) == "proven"
    assert proof_grade("before 12 files, after 0 files") == "proven"
    # 'exit 0' inside a bigger number must not count
    assert proof_grade("exit code 01") == "unproven"
    # grandfathering: old events survive arming, new ones must earn it
    armed = "2026-07-20T00:00:00"
    assert verify_accepted({"ts": "2026-07-01T10:00:00", "proof": "looks fine"}, armed)
    assert not verify_accepted({"ts": "2026-07-25T10:00:00", "proof": "looks fine"}, armed)
    assert verify_accepted({"ts": "2026-07-25T10:00:00", "proof": "exit code 0"}, armed)
    assert verify_accepted({"ts": "2026-07-25T10:00:00", "proof": "whatever"}, "")  # dark

    # ---- 3. risk tracking ----
    # the case the guard exists for: sensitive category, harmless-looking tier
    r = classify_risk("update the outreach template and email the list", tier=0)
    assert r["track"] == "outbound" and r["freeze"] and r["would_benefit"], r
    # already tier-2 -> the engine escalates anyway, so the guard honestly claims no credit
    r = classify_risk("wire the payment", tier=2)
    assert r["track"] == "financial" and r["freeze"] and not r["would_benefit"], r
    # ordinary work must not be frozen, or the gate becomes noise and gets ignored
    r = classify_risk("rename a local variable in the parser", tier=0)
    assert r["track"] == "general" and not r["freeze"] and not r["would_benefit"], r
    # a tripwire hit freezes even when the track looks general
    r = classify_risk("routine cleanup", tier=0, tripwire_hit=True)
    assert r["freeze"] and not r["would_benefit"], r

    import tempfile
    d = tempfile.mkdtemp()
    shadow_observe(d, "HUB-1", {"proposal_id": "p1"}, "publish the post", None, 0, False)
    shadow_observe(d, "HUB-1", {"proposal_id": "p2"}, "tidy up the log format", None, 0, False)
    rep = shadow_report(d)
    assert rep == {"observed": 2, "would_have_caught": 1,
                   "tracks": {"outbound": 1, "general": 1}}, rep

    # ---- 4. signature audit ----
    evs = [{"proposal_id": "old1", "ts": "2026-07-01T00:00:00", "type": "COMMIT", "actor": "HUB-1"},
           {"proposal_id": "new1", "ts": "2026-07-25T00:00:00", "type": "COMMIT", "actor": "HUB-1"},
           {"proposal_id": "bad1", "ts": "2026-07-26T00:00:00", "type": "COMMIT", "actor": "???"}]
    verdicts = {"old1": None, "new1": None, "bad1": False}

    def verify(ev):
        return verdicts[ev["proposal_id"]]

    dark = audit_signatures(evs, verify, "")
    # dark phase: a BAD signature is STILL refused. Unsigned is a rollout gap; wrong is tampering.
    assert dark["ok"] == 0 and dark["bad"] == 1 and dark["unsigned"] == 2, dark
    assert [q["reason"] for q in dark["quarantined"]] == ["bad-signature"], dark

    armed = audit_signatures(evs, verify, "2026-07-20T00:00:00")
    reasons = sorted(q["reason"] for q in armed["quarantined"])
    assert reasons == ["bad-signature", "unsigned-after-enforcement"], reasons
    # and the pre-threshold event is never punished retroactively
    assert all(q["id"] != "old1" for q in armed["quarantined"]), armed
    # a forged unsigned line cannot grandfather itself with a junk ts that sorts low ...
    assert sig_enforced({"ts": "!"}, "2026-07-20T00:00:00", None)[0] is False
    # ... and "*" (strict) grandfathers nothing, however old the ts claims to be
    strict = audit_signatures(evs, verify, "*")
    assert sorted(q["id"] for q in strict["quarantined"]) == ["bad1", "new1", "old1"], strict

    print("SELFTEST OK - 4 guards:\n"
          "  arbiter election  : failover on stale-only evidence, all peers agree, "
          "unarmed build unchanged, announced once\n"
          "  proof grading     : a sentence is not evidence, history grandfathered\n"
          "  risk tracking     : sensitive-at-low-tier caught, ordinary work untouched, "
          "shadow log answers whether the guard is worth keeping\n"
          "  signature audit   : bad signature refused even in dark mode, unsigned "
          "distinguished from forged, no retroactive punishment")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "selftest":
        _selftest()
    else:
        print(__doc__.strip().split("\n\n")[0])
        print("\nusage: python protocol_guards.py selftest")
