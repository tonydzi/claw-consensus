# -*- coding: utf-8 -*-
"""consensus.py - autonomous machine<->machine CONSENSUS engine.

WHY: the machines already MOVE messages reliably (bus_send.py dual-rail), but they could not
NEGOTIATE a decision and converge WITHOUT the human owner acting as courier. This adds the
missing layer: a structured propose -> counter -> accept -> commit negotiation with a
deterministic tie-break, an append-only decision log, idempotency over the redundant rails,
and a hard human gate for risky (Tier-2) actions.

ARCHITECTURE:
  * SOURCE OF TRUTH (machine-readable): per-machine single-writer JSONL shards
      <bus>/_decisions/log-<MACHINE>.jsonl   (single-writer => zero sync conflicts;
      append-only text also 3-way-merges cleanly if you sync via git).
      Each machine appends ONLY to its own shard; readers MERGE all shards by proposal_id and
      dedup events by event_id (idempotent across duplicate multi-rail delivery).
  * ALWAYS-ON DUAL + human-visible feed + recoverable record: the group chat. EVERY event is
      emitted through bus_send.py, which dual-sends to BOTH the chat rail and the file rail BY
      CONSTRUCTION (single rail is forbidden) -> "chat always on" is structural, not optional,
      and every envelope carries type+id+actor+subject so the negotiation is readable in plain
      sight and the ledger is reconstructable from the chat alone if a shard is lost.

GOVERNANCE: one fixed leader machine (disagree-and-commit tie-break), set in config. Tier-2
(money/outbound/irreversible/secrets/config) NEVER auto-commits -> escalates to the human owner.

Verbs:
  propose "<subject>" [--details '<json>'] [--tier N] [--id <id>]
  respond <id> accept|counter|reject ["text"]
  commit  <id>                      # record the agreed decision-of-record (the AGENT then applies it);
                                    #   the leader committing WITHOUT agreement writes LEADER_OVERRIDE
  verify  <id> "<proof>" [--advisory]  # cross-agent epistemic flag: independent verify needed before
                                    #   'done'; a proof copied verbatim from a prior VERIFY is REFUSED
                                    #   (--advisory = old behaviour: warn and record anyway)
  escalate <id> "<reason>"          # hand a stuck/risky proposal to the human
  status  <id>                      # show one proposal's merged state + event timeline
  list    [--open|--all]            # list proposals (default: open ones)
  tick                              # deterministic driver: timeouts, round-cap, tie-break, Tier-2 gate
  pending [--json]                  # 0-LLM detector: open proposals AWAITING MY RESPONSE (judge work-list)
  approve <id> "<proof>"            # record the HUMAN's OK (who/where/msg-id) -> unlocks commit for tier-2

Env: MACHINE_KEY / COMPUTERNAME = me; MACHINE_BUS_DIR = bus root; CONSENSUS_NO_BUS=1 skips the
dual-send (tests); HUMAN_ALERT_CMD = optional command (one text arg) that pings a DEDICATED
human-attention channel on deadlock (keep it separate from the noisy machine chat, or the
question drowns in heartbeats). Config <bus>/_decisions/consensus.json overrides
leader/round_cap/timeout_min; optional "peer_timeout_min": {"<MACHINE>": min} = per-machine SLA
(a sleeping laptop gets a longer window than the always-on hub) -- effective deadline = slowest
peer we are WAITING ON.
"""
import os, sys, json, glob, uuid, datetime, shlex, subprocess, re

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
BUS = os.environ.get("MACHINE_BUS_DIR", os.path.join(os.path.expanduser("~"), "machine-bus"))
ME  = os.environ.get("MACHINE_KEY", os.environ.get("COMPUTERNAME", os.environ.get("HOSTNAME", "unknown"))).strip()

DEFAULTS = {"leader": "HUB-1", "round_cap": 3, "timeout_min": 30}
TYPES = {"PROPOSE", "COUNTER", "ACCEPT", "REJECT", "COMMIT", "LEADER_OVERRIDE", "VERIFY", "ESCALATE",
         "HUMAN_APPROVED"}
# A decision of record is either a COMMIT (reached through agreement, or a recorded human approval
# on Tier-2) or a LEADER_OVERRIDE (the leader decided without agreement: disagree-and-commit, or a
# manual commit on a proposal that was never agreed). Both close the proposal; the ledger keeps
# them apart so an arbiter's call is never read back as consensus.
DECISION_TYPES = ("COMMIT", "LEADER_OVERRIDE")

# Tier-tripwire: deterministic keyword guard against TIER MIS-CLASSIFICATION (the top risk of
# the whole design). The Tier-2 -> human-gate safety hinges on the agent labelling --tier
# correctly; if it mislabels a destructive action as Tier-0 the gate never fires. This bumps
# tier to 2 on any dangerous keyword, regardless of the agent's label. The list is an editable
# txt the owner maintains; a built-in fallback keeps the guard alive if the file is missing.
TRIPWIRE_FILE = os.path.join(SCRIPTS, "tier_tripwire.txt")
_TRIPWIRE_FALLBACK = [
    "delete", "rm -", "drop", "wipe", "truncate", "format", "overwrite", "purge",
    "remove-item", "send", "wire", "transfer", "pay", "money",
    "mass", "publish", "deploy", "secret", "password", "token",
    "api key", "credential", ".env", "canon", "claude.md",
    "uninstall", "revoke", "factory reset", "git push --force",
]


def _tripwire_terms():
    """Editable keyword list (one per line, # comments); fall back to built-in if file missing.
    Add terms in YOUR language too -- the tripwire only sees what it can match."""
    try:
        terms = []
        for ln in open(TRIPWIRE_FILE, encoding="utf-8"):
            ln = ln.strip()
            if ln and not ln.startswith("#"):
                terms.append(ln.lower())
        return terms or list(_TRIPWIRE_FALLBACK)
    except Exception:
        return list(_TRIPWIRE_FALLBACK)


def _tripwire_hit(text):
    """Return the first dangerous term found in text, else None.

    Word-terms that START with a word char match only at a WORD START: not preceded by another
    word char. So 'send' fires on 'send money' / 'sending' but NOT on the same letters buried
    inside an identifier like 'bus_send' (preceded by '_'). Suffix inflections still match
    (deploy->deployment), preserving the stem intent. Phrases or terms starting with
    punctuation ('.env', 'rm -', 'api key') keep plain substring matching.
    (Fixes a real false-positive where 'bus_send.py' tripped the 'send' term.
    The gate stays fail-safe: this only removes a FALSE class, real terms still catch.)"""
    low = (text or "").lower()
    for t in _tripwire_terms():
        if t and (t[0].isalnum() or t[0] == "_"):
            if re.search(r"(?<!\w)" + re.escape(t), low):
                return t
        elif t and t in low:
            return t
    return None


def _dir():
    d = os.path.join(BUS, "_decisions")
    os.makedirs(d, exist_ok=True)
    return d


def _cfg():
    p = os.path.join(_dir(), "consensus.json")
    c = dict(DEFAULTS)
    try:
        with open(p, encoding="utf-8") as fh:
            c.update(json.load(fh))
    except Exception:
        pass
    return c


def _shard():
    return os.path.join(_dir(), "log-%s.jsonl" % ME)


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _iso(dt):
    # microsecond precision: kills same-second ordering ties between PROPOSE/COUNTER/ACCEPT
    # (a real causality bug -- two events in the same second sorted by shard filename, not order).
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _parse_ts(s):
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):  # new + back-compat
        try:
            return datetime.datetime.strptime(s, fmt).replace(tzinfo=datetime.timezone.utc)
        except Exception:
            pass
    return _now()


def _append(ev):
    """Append one event to MY shard only (single-writer)."""
    with open(_shard(), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(ev, ensure_ascii=False) + "\n")


def _all_events():
    """Read every shard, dedup by event_id (idempotent over redundant multi-rail delivery)."""
    seen, out = set(), []
    for f in sorted(glob.glob(os.path.join(_dir(), "log-*.jsonl"))):
        # Parse PER LINE, not per file -- one corrupt line (partial write, sync-conflict junk)
        # used to silently drop EVERY event after it in that shard.
        try:
            lines = open(f, encoding="utf-8").read().splitlines()
        except Exception:
            continue
        for ln in lines:
            ln = ln.strip()
            if not ln:
                continue
            try:
                ev = json.loads(ln)
            except Exception:
                continue
            eid = ev.get("event_id")
            if eid in seen:
                continue
            seen.add(eid)
            out.append(ev)
    out.sort(key=_order_key)
    return out


def _seq(ev):
    try:
        return int(ev.get("seq") or 0)
    except (TypeError, ValueError):
        return 0


def _order_key(ev):
    """Projection order: the logical clock first, wall clock only as a tie-break.

    `seq` is a Lamport counter (see _mkevent): every event a machine writes gets 1 + the highest
    seq it has SEEN, so an event is always ordered after everything its writer had read when it
    wrote it, whatever the two machines' clocks say. Wall-clock `ts` used to be the only key, and
    a laptop running five minutes slow wrote an ACCEPT that sorted BEFORE the proposal it
    accepted. `ts` now only breaks ties between concurrent events (same seq, neither saw the
    other), then actor and event_id make the order total and identical on every machine.
    Events written before seq existed carry seq 0 and keep their old ts order."""
    return (_seq(ev), ev.get("ts", ""), ev.get("actor", ""), ev.get("event_id", ""))


def _by_proposal():
    groups = {}
    for ev in _all_events():
        groups.setdefault(ev.get("proposal_id"), []).append(ev)
    return groups


def _state(evs):
    """Derive the current state of one proposal from its merged events, in projection order."""
    evs = sorted(evs, key=_order_key)
    prop = next((e for e in evs if e.get("type") == "PROPOSE"), evs[0])
    s = {
        "proposal_id": prop.get("proposal_id"),
        "subject": prop.get("subject", ""),
        "proposer": prop.get("actor", "?"),
        "tier": int(prop.get("risk_tier", 0) or 0),
        "created": prop.get("ts"),
        "rounds": sum(1 for e in evs if e.get("type") in ("PROPOSE", "COUNTER")),
        "accepts": sorted({e["actor"] for e in evs if e.get("type") == "ACCEPT"}),
        "verifies": sorted({e["actor"] for e in evs if e.get("type") == "VERIFY"}),
        "committers": sorted({e["actor"] for e in evs if e.get("type") in DECISION_TYPES}),
        "overrides": sorted({e["actor"] for e in evs if e.get("type") == "LEADER_OVERRIDE"}),
        "events": evs,
        "last_ts": evs[-1].get("ts"),
    }
    # Causality-robust status (do NOT rely on the single "last event"): find the latest POSITION
    # on the table (PROPOSE or COUNTER); the side that did NOT make it can ACCEPT it. This handles
    # both "responder accepts proposal" and "proposer accepts a counter". "After" means after in
    # projection order (logical clock), never a wall-clock comparison between two machines.
    positions = [i for i, e in enumerate(evs) if e.get("type") in ("PROPOSE", "COUNTER")]
    pos_i = positions[-1] if positions else evs.index(prop)
    latest_pos = evs[pos_i]
    owner = latest_pos.get("actor")
    accept_after = [e for e in evs[pos_i + 1:] if e.get("type") == "ACCEPT"
                    and e.get("actor") != owner]
    reject_after = [e for e in evs[pos_i + 1:] if e.get("type") == "REJECT"]
    types = [e.get("type") for e in evs]
    # Guard #3 (anti split-brain): only ONE machine should ever apply+COMMIT a given proposal.
    # COMMITs from >1 distinct actor = the partition healed and two sides diverged -> conflict,
    # never silently "committed". Detected on merge; must escalate to the human for reconciliation.
    if len(s["committers"]) > 1:
        s["status"] = "conflict"
    elif "COMMIT" in types:
        s["status"] = "committed"
    elif "LEADER_OVERRIDE" in types:
        s["status"] = "overridden"
    elif "ESCALATE" in types:
        s["status"] = "escalated"
    elif accept_after:
        s["status"] = "agreed"
    elif reject_after:
        s["status"] = "rejected"
    elif latest_pos.get("type") == "COUNTER":
        s["status"] = "countered"
    else:
        s["status"] = "proposed"
    return s


def _deadline(s, cfg):
    # Per-peer SLA: sleeping nodes (laptops) need a longer response window than the always-on
    # hub, else tier-1 proposals die to timeout while the peer sleeps.
    # cfg["peer_timeout_min"] = {"<MACHINE>": minutes}; the effective timeout is the SLOWEST
    # machine we are WAITING ON (= everyone in the map except the owner of the latest position).
    # Machines absent from the map / empty map -> global timeout_min.
    positions = [e for e in s["events"] if e.get("type") in ("PROPOSE", "COUNTER")]
    owner = (positions[-1] if positions else s["events"][0]).get("actor")
    waiting = [int(v) for k, v in (cfg.get("peer_timeout_min") or {}).items() if k != owner]
    mins = max(waiting) if waiting else int(cfg["timeout_min"])
    return _parse_ts(s["created"]) + datetime.timedelta(minutes=mins)


def _bus(text):
    """Dual-send through bus_send.py -> chat rail + file rail BY CONSTRUCTION."""
    if os.environ.get("CONSENSUS_NO_BUS"):
        print("   (bus skipped: CONSENSUS_NO_BUS)")
        return
    try:
        subprocess.run([sys.executable, os.path.join(SCRIPTS, "bus_send.py"), "ALL", text],
                       timeout=90)
    except Exception as e:
        print("   (bus_send failed: %s)" % e)


def _human_alert(text):
    """TOP-ESCALATION channel: machines ping here ONLY when consensus genuinely CANNOT resolve
    (round-cap with no agreement / timeout on a tier>0 proposal / manual escalate) and a human
    must answer NOW. Keep this channel SEPARATE from the machine chat and STRICTLY for
    deadlocks -- if it shares the noisy heartbeat feed, the question drowns and dies.
    Configure env HUMAN_ALERT_CMD = command taking one text arg. Best-effort, never raises."""
    if os.environ.get("CONSENSUS_NO_BUS"):
        return
    raw = (os.environ.get("HUMAN_ALERT_CMD") or "").strip()
    if not raw:
        return  # no dedicated channel configured -> the dual-sent bus line is the only ping
    try:
        subprocess.run(shlex.split(raw) +
                       ["\U0001F6A8\U0001F6A8 [DEADLOCK] machines could not agree -- a human is needed NOW.\n" + text],
                       timeout=90)
    except Exception as e:
        print("   (human alert failed: %s)" % e)


def _emit(ev, extra="", alert_human=False):
    short = ev["proposal_id"][:8]
    tier = ev.get("risk_tier")
    tier_s = (" tier=%s" % tier) if tier is not None else ""
    line = "\U0001F91D [CONSENSUS] %s #%s%s by %s: %s" % (
        ev["type"], short, tier_s, ev["actor"], (ev.get("subject") or ev.get("text") or "")[:140])
    if extra:
        line += " | " + extra
    _bus(line)
    if alert_human:
        _human_alert(line)


def _next_seq():
    """Lamport clock: 1 + the highest seq in any shard I can read. Monotonic per machine (my own
    shard is part of what I read) and causal across machines (an answer always outranks the
    position it answers)."""
    return 1 + max([_seq(e) for e in _all_events()] or [0])


def _mkevent(etype, proposal_id, **kw):
    ev = {"event_id": uuid.uuid4().hex, "proposal_id": proposal_id, "type": etype,
          "actor": ME, "seq": _next_seq(), "ts": _iso(_now())}
    ev.update({k: v for k, v in kw.items() if v is not None})
    return ev


# ---------------- verbs ----------------

def cmd_propose(subject, details=None, tier=0, pid=None):
    pid = pid or uuid.uuid4().hex
    d = None
    if details:
        try:
            d = json.loads(details)
        except Exception:
            d = {"note": details}
    tier = int(tier)
    hit = _tripwire_hit("%s %s" % (subject, details or ""))
    if hit and tier < 2:
        print("   ⚠️ TIER-TRIPWIRE: matched '%s' -> bumping tier %s->2 (safety gate; tune %s)"
              % (hit, tier, os.path.basename(TRIPWIRE_FILE)))
        tier = 2
    ev = _mkevent("PROPOSE", pid, subject=subject, details=d, risk_tier=tier)
    _append(ev)
    print("PROPOSED #%s tier=%s: %s" % (pid[:8], tier, subject))
    _emit(ev, extra="awaiting response (cap=%s rounds, %smin)" % (_cfg()["round_cap"], _cfg()["timeout_min"]))
    if int(tier) >= 2:
        esc = _mkevent("ESCALATE", pid, subject=subject, text="Tier-2 proposal needs the owner's OK")
        _append(esc)
        _emit(esc, extra="❓ NEEDS YOUR OK -- reply with your approval token / NO")
        print("   Tier-2 -> escalated to the human owner (no auto-commit).")
    return pid


def _resolve(idfrag):
    groups = _by_proposal()
    hits = [p for p in groups if p and p.startswith(idfrag)]
    if len(hits) == 1:
        return hits[0], groups[hits[0]]
    if not hits:
        print("no proposal matches #%s" % idfrag)
    else:
        print("ambiguous #%s -> %s" % (idfrag, ", ".join(h[:8] for h in hits)))
    return None, None


def cmd_respond(idfrag, decision, text=None):
    pid, evs = _resolve(idfrag)
    if not pid:
        return 2
    s = _state(evs)
    dmap = {"accept": "ACCEPT", "counter": "COUNTER", "reject": "REJECT"}
    etype = dmap.get(decision.lower())
    if not etype:
        print("decision must be accept|counter|reject"); return 2
    ev = _mkevent(etype, pid, subject=s["subject"], text=text, risk_tier=s["tier"])
    _append(ev)
    print("%s #%s: %s" % (etype, pid[:8], text or ""))
    _emit(ev)
    return 0


def _human_approvals(evs):
    return [e for e in evs if e.get("type") == "HUMAN_APPROVED"]


def cmd_approve(idfrag, proof):
    """Closes a real gap: tier-2 escalates to the human, but the ledger had NO closure path --
    cmd_commit hard-refused tier>=2, so human-approved proposals sat ESCALATED forever.
    This verb RECORDS the human approval; `commit` then allows tier>=2 only WITH a recorded
    approval. Auto-commit without approve stays banned. Proof string is REQUIRED and must say
    who/where (e.g. 'owner approved, alerts-channel msg 1790183')."""
    pid, evs = _resolve(idfrag)
    if not pid:
        return 2
    if not (proof or "").strip():
        print("approve requires a proof string (who/where/msg-id of the human OK)"); return 2
    s = _state(evs)
    if _human_approvals(evs):
        print("already HUMAN-APPROVED #%s (idempotent skip)" % pid[:8]); return 0
    ev = _mkevent("HUMAN_APPROVED", pid, subject=s["subject"], proof=proof, risk_tier=s["tier"])
    _append(ev)
    print("HUMAN-APPROVED #%s: %s" % (pid[:8], proof))
    _emit(ev, extra="tier-%s approved by the human -> commit unlocked" % s["tier"])
    return 0


def cmd_commit(idfrag):
    pid, evs = _resolve(idfrag)
    if not pid:
        return 2
    s = _state(evs)
    if s["tier"] >= 2 and not _human_approvals(evs):
        print("REFUSED: Tier-2 needs a RECORDED human approval first -> `approve <id> \"<proof>\"`; auto-commit stays banned."); return 1
    leader = ME == _cfg()["leader"]
    human_ok = s["tier"] >= 2 and bool(_human_approvals(evs))
    if s["status"] == "agreed" or (human_ok and leader):
        ev = _mkevent("COMMIT", pid, subject=s["subject"], text="decision of record; applying")
        _append(ev)
        print("COMMITTED #%s: %s  (now APPLY the change, then `verify`)" % (pid[:8], s["subject"]))
        _emit(ev, extra="agent applies the change, then both VERIFY")
        return 0
    if not leader:
        print("not agreed yet (status=%s) and you are not the leader -> cannot commit." % s["status"]); return 1
    # The leader may still decide (that is what a tie-break is), but it is NOT consensus and the
    # ledger must say so: a separate event type, so an audit never counts it as an agreement.
    ev = _mkevent("LEADER_OVERRIDE", pid, subject=s["subject"],
                  text="leader override: decided without agreement (status was %s)" % s["status"])
    _append(ev)
    print("LEADER-OVERRIDE #%s: %s  (status was %s; this is the leader's call, not consensus)"
          % (pid[:8], s["subject"], s["status"]))
    _emit(ev, extra="leader override without agreement; peers may still VERIFY/object")
    return 0


def cmd_verify(idfrag, proof, advisory=False):
    pid, evs = _resolve(idfrag)
    if not pid:
        return 2
    s = _state(evs)
    # Guard #2 (independent re-verify): the agent that APPLIED the change (the committer) may
    # self-verify, but "globally done" needs at least one INDEPENDENT verify from a machine that
    # did NOT apply it -- a second machine re-checking, not the applier vouching for itself.
    committer = s["committers"][0] if s["committers"] else None
    prior_proofs = [e.get("proof", "").strip() for e in evs if e.get("type") == "VERIFY"]
    if committer and ME == committer:
        print("   note: you COMMITTED #%s -> this is a SELF-verify; still needs an INDEPENDENT machine's verify." % pid[:8])
    if proof.strip() and proof.strip() in prior_proofs:
        if not advisory:
            print("REFUSED (rubber-stamp guard): your proof duplicates a prior VERIFY verbatim -> "
                  "re-check INDEPENDENTLY (re-run / re-read) and write what YOU saw. Nothing recorded.")
            return 1
        print("   ⚠️ RUBBER-STAMP GUARD (advisory): your proof duplicates a prior VERIFY verbatim -> recorded anyway.")
    ev = _mkevent("VERIFY", pid, subject=s["subject"], proof=proof)
    _append(ev)
    after = sorted(set(s["verifies"]) | {ME})
    independent = [a for a in after if a != committer]
    print("VERIFY #%s by %s: %s" % (pid[:8], ME, proof))
    done = len(after) >= 2 and len(independent) >= 1
    if done:
        extra = "✅ both verified (incl. independent) -> DONE"
    elif len(after) >= 2 and not independent:
        extra = "⚠️ 2 verifies but all by the committer -> need an INDEPENDENT machine"
    else:
        extra = "awaiting the other machine's independent verify"
    _emit(ev, extra=extra)
    if done:
        print("   cross-agent validation complete (>=2 distinct, >=1 independent of the applier) -> globally done.")
    return 0


def cmd_escalate(idfrag, reason):
    pid, evs = _resolve(idfrag)
    if not pid:
        return 2
    s = _state(evs)
    ev = _mkevent("ESCALATE", pid, subject=s["subject"], text=reason)
    _append(ev)
    print("ESCALATED #%s -> the human owner: %s" % (pid[:8], reason))
    _emit(ev, extra="❓ NEEDS YOUR OK -- reply with your approval token / NO", alert_human=True)
    return 0


def _print_state(s):
    print("#%s  [%s]  tier=%s  rounds=%s  by %s" % (
        s["proposal_id"][:8], s["status"].upper(), s["tier"], s["rounds"], s["proposer"]))
    print("   subject: %s" % s["subject"])
    if s["accepts"]:
        print("   accepts: %s" % ", ".join(s["accepts"]))
    if s["verifies"]:
        print("   verified by: %s  (%s)" % (", ".join(s["verifies"]),
              "DONE" if len(s["verifies"]) >= 2 else "need 2"))


def cmd_status(idfrag):
    pid, evs = _resolve(idfrag)
    if not pid:
        return 2
    s = _state(evs)
    _print_state(s)
    print("   timeline:")
    for e in s["events"]:
        print("     %s  %-9s %-16s %s" % (e.get("ts"), e.get("type"), e.get("actor"),
              (e.get("text") or e.get("proof") or e.get("subject") or "")[:80]))
    return 0


def cmd_list(which="open"):
    groups = _by_proposal()
    if not groups:
        print("(no proposals yet)"); return 0
    rows = [_state(evs) for evs in groups.values()]
    rows.sort(key=lambda s: s.get("created") or "")
    openset = {"proposed", "countered", "agreed"}
    shown = 0
    for s in rows:
        if which == "open" and s["status"] not in openset:
            continue
        _print_state(s)
        shown += 1
    if not shown:
        print("(no %s proposals)" % which)
    return 0


def cmd_tick():
    """Deterministic driver (0-LLM). Applies timeout, round-cap, tie-break, Tier-2 gate.
    Mutates ONLY the log (append ESCALATE / leader-auto-ACCEPT); never edits user files -- the
    AGENT performs the agreed change when it sees status=agreed/committed."""
    cfg = _cfg()
    groups = _by_proposal()
    if not groups:
        print("(tick: no proposals)"); return 0
    now = _now()
    acted = 0
    for pid, evs in groups.items():
        s = _state(evs)
        if s["status"] in ("committed", "overridden", "escalated", "rejected"):
            continue
        overdue = now > _deadline(s, cfg)
        # 0) Guard #3: split-brain detected (>1 machine committed the same proposal) -> never
        #    auto-resolve; hand to the human for reconciliation (the leader's log is authoritative).
        if s["status"] == "conflict":
            print("#%s SPLIT-BRAIN: committed by %s -> escalate for reconciliation" % (
                pid[:8], ", ".join(s["committers"])))
            if not os.environ.get("CONSENSUS_DRY"):
                ev = _mkevent("ESCALATE", pid, subject=s["subject"],
                              text="split-brain: conflicting commits by %s" % ", ".join(s["committers"]))
                _append(ev); _emit(ev, extra="❓ two commits diverged -- the human must reconcile")
            acted += 1; continue
        # 1) Tier-2 already proposed but somehow open -> ensure escalated (belt & suspenders)
        if s["tier"] >= 2:
            print("#%s Tier-2 still open -> escalate to the human" % pid[:8])
            if not os.environ.get("CONSENSUS_DRY"):
                ev = _mkevent("ESCALATE", pid, subject=s["subject"], text="Tier-2 auto-gate")
                _append(ev); _emit(ev, extra="❓ approval token = yes / NO = no")
            acted += 1; continue
        # 2) agreed but uncommitted -> remind the responsible actor to apply + commit
        if s["status"] == "agreed":
            print("#%s AGREED -> ready to commit & apply (proposer=%s)" % (pid[:8], s["proposer"]))
            acted += 1; continue
        # 3) round cap hit without agreement -> tie-break = hand to the human (conservative)
        if s["rounds"] >= int(cfg["round_cap"]) and s["status"] != "agreed":
            print("#%s round-cap %s hit, no agreement -> escalate (tie-break)" % (pid[:8], cfg["round_cap"]))
            if not os.environ.get("CONSENSUS_DRY"):
                ev = _mkevent("ESCALATE", pid, subject=s["subject"], text="no agreement after %s rounds" % cfg["round_cap"])
                _append(ev); _emit(ev, extra="❓ the human settles the dispute", alert_human=True)
            acted += 1; continue
        # 4) timeout with no response: Tier-0 + I am the leader -> disagree-and-commit (autonomy)
        if overdue and s["status"] == "proposed":
            if s["tier"] == 0 and ME == cfg["leader"]:
                evl = s["events"]
                positions = [i for i, e in enumerate(evl) if e.get("type") in ("PROPOSE", "COUNTER")]
                pos_i = positions[-1] if positions else 0
                owner = evl[pos_i].get("actor")
                mine_after = [e for e in evl[pos_i + 1:] if e.get("type") == "ACCEPT"
                              and e.get("actor") == ME]
                if owner == ME:
                    # LOOP-FIX (a real incident: 17 identical self-ACCEPTs overnight): a
                    # self-accept NEVER counts in _state (accept must come from a non-owner), so
                    # tick re-accepted my own proposal every 20 min forever. Disagree-and-commit
                    # on the leader's OWN tier-0 proposal = decide directly. Nobody agreed, so the
                    # record is a LEADER_OVERRIDE, not a COMMIT (issue #9, row C).
                    print("#%s Tier-0 timeout on MY OWN proposal -> leader disagree-and-commit (LEADER_OVERRIDE)" % pid[:8])
                    if not os.environ.get("CONSENSUS_DRY"):
                        ev = _mkevent("LEADER_OVERRIDE", pid, subject=s["subject"],
                                      text="leader disagree-and-commit: own tier-0 proposal, no peer response by deadline")
                        _append(ev); _emit(ev, extra="tier-0 timeout tie-break; peer may still VERIFY/object")
                elif mine_after:
                    # idempotency belt: my ACCEPT is already on the table but the state did not
                    # advance -> never spam another one; surface the anomaly instead of looping.
                    print("#%s already accepted by me, state stuck=%s -> escalate (anomaly, no re-spam)" % (pid[:8], s["status"]))
                    if not os.environ.get("CONSENSUS_DRY"):
                        ev = _mkevent("ESCALATE", pid, subject=s["subject"],
                                      text="stuck: leader ACCEPT recorded but state not advancing")
                        _append(ev); _emit(ev, extra="state anomaly, needs a look")
                else:
                    print("#%s Tier-0 timeout -> leader auto-accepts (disagree-and-commit)" % pid[:8])
                    if not os.environ.get("CONSENSUS_DRY"):
                        ev = _mkevent("ACCEPT", pid, subject=s["subject"], text="leader auto-accept on timeout")
                        _append(ev); _emit(ev, extra="leader tie-break (Tier-0 timeout)")
            else:
                print("#%s timeout, tier=%s -> escalate (only Tier-0 auto-resolves)" % (pid[:8], s["tier"]))
                if not os.environ.get("CONSENSUS_DRY"):
                    ev = _mkevent("ESCALATE", pid, subject=s["subject"], text="timeout, tier>0 needs a human")
                    _append(ev); _emit(ev, extra="❓ approval token = yes / NO = no", alert_human=True)
            acted += 1; continue
        print("#%s [%s] waiting (rounds=%s, %s)" % (
            pid[:8], s["status"], s["rounds"], "overdue" if overdue else "within window"))
    if not acted:
        print("(tick: nothing actionable)")
    return 0


def cmd_pending(as_json=False):
    """The missing detector: tick is deterministic and can only timeout/escalate -- it never
    ANSWERS a peer's propose, so (before this verb existed) every tier-1 died to "timeout,
    tier>0 needs a human". This lists open proposals where the MOVE IS MINE (latest position
    PROPOSE/COUNTER made by someone else, status still open, tier<2 -- Tier-2 stays
    human-gated). An inbox robot greps its output to WAKE the LLM judge; the judge uses it as
    the work-list, then answers via `respond <id> accept|counter|reject`."""
    groups = _by_proposal()
    rows = []
    for pid, evs in groups.items():
        s = _state(evs)
        if s["status"] not in ("proposed", "countered") or s["tier"] >= 2:
            continue
        positions = [e for e in s["events"] if e.get("type") in ("PROPOSE", "COUNTER")]
        latest = positions[-1] if positions else None
        if not latest or latest.get("actor") == ME:
            continue
        rows.append({"id": s["proposal_id"], "short": s["proposal_id"][:8], "tier": s["tier"],
                     "from": latest.get("actor"), "position": latest.get("type"),
                     "subject": s["subject"], "text": latest.get("text") or ""})
    if as_json:
        print(json.dumps(rows, ensure_ascii=False)); return 0
    if not rows:
        print("(pending: nothing awaits my response)"); return 0
    for r in rows:
        print("AWAIT-MY-RESPONSE #%s tier=%s from=%s %s: %s" % (
            r["short"], r["tier"], r["from"], r["position"], r["subject"][:160]))
    return 0


def _getopt(args, name, default=None):
    if name in args:
        i = args.index(name)
        if i + 1 < len(args):
            return args[i + 1]
    return default


def main():
    a = sys.argv[1:]
    if not a:
        print(__doc__); return 2
    cmd = a[0]
    if cmd == "propose" and len(a) >= 2:
        return cmd_propose(a[1], details=_getopt(a, "--details"),
                           tier=_getopt(a, "--tier", 0), pid=_getopt(a, "--id")) and 0
    if cmd == "respond" and len(a) >= 3:
        return cmd_respond(a[1], a[2], " ".join(a[3:]) or None)
    if cmd == "commit" and len(a) >= 2:
        return cmd_commit(a[1])
    if cmd == "verify" and len(a) >= 3:
        rest = [x for x in a[2:] if x != "--advisory"]
        return cmd_verify(a[1], " ".join(rest), advisory="--advisory" in a[2:])
    if cmd == "escalate" and len(a) >= 3:
        return cmd_escalate(a[1], " ".join(a[2:]))
    if cmd == "status" and len(a) >= 2:
        return cmd_status(a[1])
    if cmd == "list":
        return cmd_list("all" if "--all" in a else "open")
    if cmd == "tick":
        return cmd_tick()
    if cmd == "pending":
        return cmd_pending("--json" in a)
    if cmd == "approve" and len(a) >= 3:
        return cmd_approve(a[1], " ".join(a[2:]))
    print("usage: consensus.py propose|respond|commit|verify|escalate|status|list|tick|pending|approve   (see header)")
    return 2


if __name__ == "__main__":
    sys.exit(main() or 0)
