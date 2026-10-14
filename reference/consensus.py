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
  approval-request <id> [--principal NAME] [--ttl-min 15]
                                    # print the approval envelope a human must sign for this proposal
  sign-approval <envelope.json> --key <private key> [--out file]
                                    # (on the HUMAN's device) sign that envelope with their own key
  approve <id> --envelope <file|json>  # record a SIGNED human approval -> unlocks commit for tier-2

Env: MACHINE_KEY / COMPUTERNAME = me; MACHINE_BUS_DIR = bus root; CONSENSUS_NO_BUS=1 skips the
dual-send (tests); HUMAN_ALERT_CMD = optional command (one text arg) that pings a DEDICATED
human-attention channel on deadlock (keep it separate from the noisy machine chat, or the
question drowns in heartbeats). Config <bus>/_decisions/consensus.json overrides
leader/round_cap/timeout_min; optional "peer_timeout_min": {"<MACHINE>": min} = per-machine SLA
(a sleeping laptop gets a longer window than the always-on hub) -- effective deadline = slowest
peer we are WAITING ON.

Trust (issue #9, rows A and B):
  * Every event this machine writes is signed with its fleet key when it has one (fleet_sign.py).
    Every event read from any shard is checked BEFORE it can enter state: a bad signature, or a
    signature by a key other than the event's own `actor`, is quarantined always. Unsigned
    events are trusted only while enforcement is off ("dark"); with `sig_enforce_after` set they
    are quarantined too. "*" = strict (no grandfathering, recommended once every machine signs);
    a timestamp grandfathers older events, but `ts` is self-reported, so a forged line can
    backdate itself past it. `status` and `list` print the quarantine; nothing is dropped silently.
  * A human approval is a signed ENVELOPE, not a free-text proof: human_principal, proposal_id,
    action_digest (hash of the position on the table), expires_at, nonce, signature in the
    "fleet-approval" namespace by a key in <bus>/_engine/signers/<principal>.pub, where the
    principal is listed in `human_principals`. Expired, replayed (nonce), re-targeted (digest)
    or unsigned approvals are refused; a COUNTER after the approval voids it.
  Config keys (consensus.json): sig_enforce_after, human_principals. A synced config can be
  edited by any machine that can write the bus, so the env overrides CONSENSUS_SIG_ENFORCE_AFTER
  and CONSENSUS_HUMAN_PRINCIPALS (comma list) pin both locally; set them on machines you trust
  more than the shared folder.
"""
import os, sys, json, glob, uuid, datetime, shlex, subprocess, re, hashlib

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPTS)
import fleet_sign as fs                       # noqa: E402  (sibling module, stdlib only)
from protocol_guards import sig_enforced      # noqa: E402
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
    """Append one event to MY shard only (single-writer), signed with this machine's key if it
    has one. A signing failure writes the event unsigned and says so: in the dark phase that is a
    counted rollout gap; under enforcement the event will be quarantined on read, loudly."""
    try:
        if fs.have_key():
            ev["signer"] = ME
            ev["sig"] = fs.sign_event(ev)
    except Exception as e:
        ev.pop("sig", None)
        ev.pop("signer", None)
        print("   ⚠️ signing failed, event written UNSIGNED: %s" % e)
    with open(_shard(), "a", encoding="utf-8") as fh:
        fh.write(json.dumps(ev, ensure_ascii=False) + "\n")


QUARANTINE = []          # lines refused on the last read: {"file", "reason", "type", "actor", "id"}
TRUST = {}               # counts from the last read: enforce, ok, unsigned, quarantined
_VERDICTS = {}           # line hash -> verify result, this process
_CACHE = {"loaded": False, "fp": None, "good": set()}


def _enforce_after():
    env = os.environ.get("CONSENSUS_SIG_ENFORCE_AFTER")
    if env is not None:
        return env.strip()
    return str(_cfg().get("sig_enforce_after") or "").strip()


def _cache_path():
    return os.path.join(fs.KEYDIR, "verified-events.txt")


def _cache_load():
    """Good verdicts persist in a LOCAL (never synced) file, keyed by the registry fingerprint, so
    a long ledger is not re-verified line by line on every CLI call. A changed registry (new key,
    revocation) changes the fingerprint and silently retires every cached verdict."""
    if _CACHE["loaded"]:
        return
    _CACHE["loaded"] = True
    _CACHE["fp"] = fs.registry_fingerprint()
    try:
        for ln in open(_cache_path(), encoding="utf-8"):
            fp, _, h = ln.strip().partition(" ")
            if fp == _CACHE["fp"]:
                _CACHE["good"].add(h)
    except OSError:
        pass


def _cache_add(h):
    _CACHE["good"].add(h)
    try:
        os.makedirs(fs.KEYDIR, exist_ok=True)
        with open(_cache_path(), "a", encoding="utf-8") as fh:
            fh.write("%s %s\n" % (_CACHE["fp"], h))
    except OSError:
        pass


def _verify(ev, line):
    """True = signed by its own actor and verified; False = forged; None = unsigned/unverifiable."""
    if not ev.get("sig"):
        return None
    if (ev.get("signer") or ev.get("actor")) != ev.get("actor"):
        return False             # a machine's key speaks for that machine only
    h = hashlib.sha256(line.encode("utf-8")).hexdigest()
    if h in _VERDICTS:
        return _VERDICTS[h]
    _cache_load()
    if h in _CACHE["good"]:
        v = True
    else:
        try:
            v = fs.verify_event(ev)
        except Exception:
            v = None             # tooling missing here: unverifiable, never "good"
        if v is True:
            _cache_add(h)
    _VERDICTS[h] = v
    return v


def _all_events():
    """Read every shard, verify each line, dedup by event_id (idempotent over redundant
    multi-rail delivery). A line enters state only if its signature policy accepts it; the rest
    goes to QUARANTINE with a reason, which `status` and `list` print."""
    seen, out, quarantine = set(), [], []
    enforce = _enforce_after()
    ok = unsigned = 0
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
                if not isinstance(ev, dict):
                    raise ValueError("not an object")
            except Exception:
                quarantine.append({"file": os.path.basename(f), "reason": "unparsable",
                                   "type": None, "actor": None, "id": None})
                continue
            if not _well_formed(ev):
                quarantine.append({"file": os.path.basename(f), "reason": "malformed",
                                   "type": ev.get("type"), "actor": ev.get("actor"),
                                   "id": str(ev.get("proposal_id") or "?")[:8]})
                continue
            # dedup per (actor, event_id): a duplicate delivery of MY event is a no-op, but
            # another machine reusing my event_id must not make my event disappear.
            key = (ev["actor"], ev["event_id"])
            if key in seen:
                continue
            v = _verify(ev, ln)
            accepted, reason = sig_enforced(ev, enforce, v)
            if not accepted:
                quarantine.append({"file": os.path.basename(f), "reason": reason,
                                   "type": ev.get("type"), "actor": ev.get("actor"),
                                   "id": (ev.get("proposal_id") or "?")[:8]})
                continue
            ok += v is True
            unsigned += v is None
            seen.add(key)
            out.append(ev)
    out.sort(key=_order_key)
    QUARANTINE[:] = quarantine
    TRUST.clear()
    TRUST.update({"enforce": enforce or "DARK", "ok": ok, "unsigned": unsigned,
                  "quarantined": len(quarantine)})
    return out


SEQ_MAX = 2 ** 53        # beyond this a JSON number stops being an exact integer everywhere


def _well_formed(ev):
    """The minimum shape an event needs before it may influence state. A line that fails this
    is refused, never repaired: a negative or non-integer seq would reorder history (a forged
    PROPOSE with seq -1 used to sort first and take the proposal over), Infinity crashed every
    reader, and a non-string ts broke the sort for everyone."""
    if not isinstance(ev, dict):
        return False
    for k in ("event_id", "proposal_id", "actor", "ts"):
        if not isinstance(ev.get(k), str) or not ev.get(k):
            return False
    if ev.get("type") not in TYPES:
        return False
    return _seq(ev) is not None


def _print_trust():
    """One line on what the projection trusted, plus every refused line (capped)."""
    if not (QUARANTINE or TRUST.get("unsigned") or TRUST.get("enforce") != "DARK"):
        return
    print("ledger trust: enforce=%s  signed-ok=%d  unsigned-trusted=%d  quarantined=%d%s" % (
        TRUST.get("enforce"), TRUST.get("ok", 0), TRUST.get("unsigned", 0), len(QUARANTINE),
        "  (DARK: unsigned lines are trusted; set sig_enforce_after)" if TRUST.get("enforce") == "DARK"
        and TRUST.get("unsigned") else ""))
    for q in QUARANTINE[:20]:
        print("   QUARANTINE %-12s %-15s #%-8s actor=%s in %s" % (
            q["reason"], q["type"] or "-", q["id"] or "-", q["actor"] or "-", q["file"]))
    if len(QUARANTINE) > 20:
        print("   ... and %d more" % (len(QUARANTINE) - 20))


def _seq(ev):
    """The event's logical clock: 0 when absent (written before seq existed), None when present
    but not a plain integer in [0, 2**53) -- such an event is malformed (see _well_formed)."""
    v = ev.get("seq", 0)
    if v is None:
        return 0
    if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v < SEQ_MAX:
        return None
    return v


def _order_key(ev):
    """Projection order: the logical clock first, wall clock only as a tie-break.

    `seq` is a Lamport counter (see _mkevent): every event a machine writes gets 1 + the highest
    seq it has SEEN, so an event is always ordered after everything its writer had read when it
    wrote it, whatever the two machines' clocks say. Wall-clock `ts` used to be the only key, and
    a laptop running five minutes slow wrote an ACCEPT that sorted BEFORE the proposal it
    accepted. `ts` now only breaks ties between concurrent events (same seq, neither saw the
    other), then actor and event_id make the order total and identical on every machine.
    Events written before seq existed carry seq 0 and keep their old ts order."""
    return (_seq(ev) or 0, ev.get("ts", ""), ev.get("actor", ""), ev.get("event_id", ""))


def _by_proposal():
    groups = {}
    for ev in _all_events():
        groups.setdefault(ev.get("proposal_id"), []).append(ev)
    return groups


def _state(evs):
    """Derive the current state of one proposal from its merged events, in projection order."""
    evs = sorted(evs, key=_order_key)
    proposes = [e for e in evs if e.get("type") == "PROPOSE"]
    prop = proposes[0] if proposes else evs[0]
    s = {
        "proposal_id": prop.get("proposal_id"),
        "subject": prop.get("subject", ""),
        "proposer": prop.get("actor", "?"),
        # fail-safe: if a proposal_id ever carries more than one PROPOSE, the HIGHEST tier wins,
        # so a second PROPOSE can never talk a Tier-2 action down to Tier-0
        "tier": max([_tier(e) for e in proposes] or [_tier(prop)]),
        "created": prop.get("ts"),
        "rounds": sum(1 for e in evs if e.get("type") in ("PROPOSE", "COUNTER")),
        "accepts": sorted({e["actor"] for e in evs if e.get("type") == "ACCEPT"}),
        "verifies": _counted_verifiers(evs),
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
    s["conflict"] = None
    if len(s["committers"]) > 1:
        s["conflict"] = "conflicting decisions by %s" % ", ".join(s["committers"])
    elif len(proposes) > 1:
        s["conflict"] = "%d PROPOSE events for one proposal_id (by %s)" % (
            len(proposes), ", ".join(sorted({e.get("actor", "?") for e in proposes})))
    if s["conflict"]:
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


def _tier(ev):
    try:
        return int(ev.get("risk_tier", 0) or 0)
    except (TypeError, ValueError):
        return 2             # an unreadable tier is treated as the dangerous one


def _norm_proof(p):
    """A proof reduced to its words: case, punctuation and spacing do not make a copy new."""
    return " ".join(re.findall(r"[0-9a-z]+", (p or "").lower()))


def _counted_verifiers(evs):
    """Machines whose VERIFY counts: only the FIRST VERIFY carrying a given proof counts, so a
    copied proof (recorded with --advisory, or appended to a shard by hand) never adds a
    verifier. evs must be in projection order."""
    seen, out = set(), set()
    for e in evs:
        if e.get("type") != "VERIFY":
            continue
        n = _norm_proof(e.get("proof"))
        if not n or n in seen:
            continue
        seen.add(n)
        out.add(e["actor"])
    return sorted(out)


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
    return 1 + max([_seq(e) or 0 for e in _all_events()] or [0])


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


ENVELOPE_FIELDS = ("human_principal", "proposal_id", "action_digest", "expires_at", "nonce", "sig")


def _human_principals():
    raw = os.environ.get("CONSENSUS_HUMAN_PRINCIPALS")
    if raw is not None:
        return [x.strip() for x in raw.split(",") if x.strip()]
    v = _cfg().get("human_principals") or []
    return [str(x) for x in (v if isinstance(v, list) else [v])]


def _action_digest(evs):
    """What exactly the human is approving: the position currently on the table (the PROPOSE,
    or the latest COUNTER). A counter after the approval changes the digest and voids it."""
    evl = sorted(evs, key=_order_key)
    pos = [e for e in evl if e.get("type") in ("PROPOSE", "COUNTER")]
    prop = next((e for e in evl if e.get("type") == "PROPOSE"), evl[0])
    cur = pos[-1] if pos else prop
    body = {"proposal_id": prop.get("proposal_id"), "position_event_id": cur.get("event_id"),
            "position_type": cur.get("type"), "subject": prop.get("subject", ""),
            "details": prop.get("details"), "text": cur.get("text"),
            "risk_tier": int(prop.get("risk_tier", 0) or 0)}
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _strict_ts(s):
    try:
        return datetime.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=datetime.timezone.utc)
    except Exception:
        return None


def _check_envelope(env, pid, evs, check_expiry=True):
    """(ok, reason). Every refusal names its reason; nothing here trusts a free-text claim."""
    if not isinstance(env, dict):
        return False, "envelope is not a JSON object"
    missing = [k for k in ENVELOPE_FIELDS if not env.get(k)]
    if missing:
        return False, "envelope lacks %s" % ", ".join(missing)
    principal = env["human_principal"]
    if principal not in _human_principals():
        return False, ("'%s' is not a configured human principal (human_principals / "
                       "CONSENSUS_HUMAN_PRINCIPALS)" % principal)
    if env["proposal_id"] != pid:
        return False, "envelope is for proposal %s, not %s" % (str(env["proposal_id"])[:8], pid[:8])
    if env["action_digest"] != _action_digest(evs):
        return False, "action_digest does not match the position on the table (changed since the ask?)"
    if check_expiry:
        exp = _strict_ts(env["expires_at"])
        if not exp:
            return False, "expires_at is not a timestamp"
        if _now() > exp:
            return False, "approval expired at %s (re-ask, don't honour a stale OK)" % env["expires_at"]
    try:
        v = fs.verify_bytes(fs.canonical(env), env["sig"], principal, ns=fs.NS_APPROVAL)
    except Exception as e:
        v = None
        print("   (signature check failed to run: %s)" % e)
    if v is not True:
        return False, ("BAD signature for %s" % principal if v is False else
                       "cannot verify: no public key for '%s' in _engine/signers" % principal)
    return True, "ok"


def _human_approvals(evs):
    """Recorded approvals that still hold: a valid signed envelope for the CURRENT position.
    A legacy free-text HUMAN_APPROVED (before v0.5.0) does not count; re-approve with a signed
    envelope."""
    out = []
    for e in evs:
        if e.get("type") == "HUMAN_APPROVED":
            ok, _ = _check_envelope(e.get("approval"), e.get("proposal_id"), evs, check_expiry=False)
            if ok:
                out.append(e)
    return out


def cmd_approval_request(idfrag, principal=None, ttl_min=15):
    """Print the unsigned envelope for the human to sign (on their own device, with their key)."""
    pid, evs = _resolve(idfrag)
    if not pid:
        return 2
    s = _state(evs)
    principals = _human_principals()
    principal = principal or (principals[0] if principals else None)
    if not principal:
        print("no human principal configured: set human_principals in consensus.json "
              "(or CONSENSUS_HUMAN_PRINCIPALS)"); return 2
    env = {"v": 1, "proposal_id": pid, "subject": s["subject"], "risk_tier": s["tier"],
           "action_digest": _action_digest(evs), "human_principal": principal,
           "expires_at": _iso(_now() + datetime.timedelta(minutes=int(ttl_min))),
           "nonce": uuid.uuid4().hex}
    print(json.dumps(env, ensure_ascii=False, indent=2))
    return 0


def cmd_sign_approval(path, key, out=None):
    """Run by the HUMAN, on a device that holds their key: sign an approval envelope."""
    if not key:
        print("sign-approval needs --key <private key file>"); return 2
    env = json.load(open(path, encoding="utf-8"))
    env.pop("sig", None)
    env["sig"] = fs.sign_bytes(fs.canonical(env), key=key, ns=fs.NS_APPROVAL)
    text = json.dumps(env, ensure_ascii=False, indent=2)
    if out:
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        print("signed envelope -> %s" % os.path.basename(out))
    else:
        print(text)
    return 0


def cmd_approve(idfrag, envelope_src):
    """Record a human approval; `commit` then allows tier>=2 only WITH a valid one.

    History: tier-2 used to escalate with no closure path, so this verb was added and took a
    free-text proof ("owner approved, msg 1790183"). Any process able to type that string could
    unlock a Tier-2 commit (issue #9, row B). It now takes only a signed envelope (see the module
    header); a proof string is refused, whoever claims to have sent it."""
    pid, evs = _resolve(idfrag)
    if not pid:
        return 2
    if not envelope_src:
        print("REFUSED: approve needs a SIGNED approval envelope: `approve <id> --envelope <file|json>`.\n"
              "   Make one with `approval-request <id>`, have the human sign it with "
              "`sign-approval` on their own device. A free-text proof is not an approval."); return 2
    try:
        env = (json.load(open(envelope_src, encoding="utf-8")) if os.path.exists(envelope_src)
               else json.loads(envelope_src))
    except Exception as e:
        print("REFUSED: envelope is not readable JSON (%s)" % e); return 2
    s = _state(evs)
    if _human_approvals(evs):
        print("already HUMAN-APPROVED #%s (idempotent skip)" % pid[:8]); return 0
    nonce = env.get("nonce") if isinstance(env, dict) else None
    used = [e for e in _all_events() if e.get("type") == "HUMAN_APPROVED"
            and isinstance(e.get("approval"), dict) and e["approval"].get("nonce") == nonce]
    if nonce and used:
        print("REFUSED: nonce %s already used (replayed approval)" % str(nonce)[:12]); return 1
    ok, why = _check_envelope(env, pid, evs)
    if not ok:
        print("REFUSED: %s" % why); return 1
    ev = _mkevent("HUMAN_APPROVED", pid, subject=s["subject"], approval=env,
                  proof="signed approval by %s, nonce %s" % (env["human_principal"], env["nonce"][:12]),
                  risk_tier=s["tier"])
    _append(ev)
    print("HUMAN-APPROVED #%s by %s (signed, expires %s)" % (pid[:8], env["human_principal"],
                                                           env["expires_at"]))
    _emit(ev, extra="tier-%s approved by the human (signed) -> commit unlocked" % s["tier"])
    return 0


def cmd_commit(idfrag):
    pid, evs = _resolve(idfrag)
    if not pid:
        return 2
    s = _state(evs)
    if s["tier"] >= 2 and not _human_approvals(evs):
        print("REFUSED: Tier-2 needs a valid SIGNED human approval first -> `approval-request` / "
              "`approve <id> --envelope ...`; auto-commit stays banned."); return 1
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
    prior_proofs = {_norm_proof(e.get("proof")) for e in evs if e.get("type") == "VERIFY"}
    if not _norm_proof(proof):
        print("REFUSED: a proof needs words or numbers, not just punctuation."); return 2
    duplicate = _norm_proof(proof) in prior_proofs
    if committer and ME == committer:
        print("   note: you COMMITTED #%s -> this is a SELF-verify; still needs an INDEPENDENT machine's verify." % pid[:8])
    if duplicate:
        if not advisory:
            print("REFUSED (rubber-stamp guard): your proof duplicates a prior VERIFY verbatim -> "
                  "re-check INDEPENDENTLY (re-run / re-read) and write what YOU saw. Nothing recorded.")
            return 1
        print("   ⚠️ RUBBER-STAMP GUARD (advisory): your proof duplicates a prior VERIFY -> recorded, "
              "but it does NOT count toward done.")
    ev = _mkevent("VERIFY", pid, subject=s["subject"], proof=proof)
    _append(ev)
    after = sorted(set(s["verifies"]) | (set() if duplicate else {ME}))
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
    _print_trust()
    print("   timeline:")
    for e in s["events"]:
        print("     %s  %-9s %-16s %s" % (e.get("ts"), e.get("type"), e.get("actor"),
              (e.get("text") or e.get("proof") or e.get("subject") or "")[:80]))
    return 0


def cmd_list(which="open"):
    groups = _by_proposal()
    _print_trust()
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
            print("#%s CONFLICT: %s -> escalate for reconciliation" % (pid[:8], s["conflict"]))
            if not os.environ.get("CONSENSUS_DRY"):
                ev = _mkevent("ESCALATE", pid, subject=s["subject"], text="conflict: %s" % s["conflict"])
                _append(ev); _emit(ev, extra="❓ the ledger diverged -- the human must reconcile")
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
    if cmd == "approve" and len(a) >= 2:
        return cmd_approve(a[1], _getopt(a, "--envelope"))
    if cmd == "approval-request" and len(a) >= 2:
        return cmd_approval_request(a[1], principal=_getopt(a, "--principal"),
                                    ttl_min=_getopt(a, "--ttl-min", 15))
    if cmd == "sign-approval" and len(a) >= 2:
        return cmd_sign_approval(a[1], _getopt(a, "--key"), out=_getopt(a, "--out"))
    print("usage: consensus.py propose|respond|commit|verify|escalate|status|list|tick|pending|"
          "approval-request|sign-approval|approve   (see header)")
    return 2


if __name__ == "__main__":
    sys.exit(main() or 0)
