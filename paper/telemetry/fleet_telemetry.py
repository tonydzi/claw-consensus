# -*- coding: utf-8 -*-
"""fleet_telemetry.py - aggregate-only telemetry for the fleet experience paper.

Reads the production coordination artefacts of one fleet and prints ONLY
aggregate counts (no message bodies, no proposal subjects, no chat ids).
Host names are mapped to role labels before anything is emitted.

    python fleet_telemetry.py --bus <machine-bus dir> --until 2026-09-30 \
        [--approvals <approvals.db>] [--breakage <Breakage-Journal.md>] \
        [--onair <onair archive dir>] --out telemetry.json

Every number in Section 5 of paper/main.tex is a field of the JSON this script
writes. Inputs it expects inside --bus:
  inbox*.md                  text rail, lines '## MSG from A -> B  (ts)'
  _decisions/log-*.jsonl     consensus ledger, one single-writer shard per node
  _inbox_debt_ledger__from-*.jsonl   delivery-debt ledger (op = debt|pay)
  _deploy/_inbox/*/*.json    fleet deploy packages (verdict, tier, fleetwide)
Missing inputs yield null fields, never a guessed value.

Run with --selftest to check the parsers on built-in fixtures (exit 0 = ok).
"""
import argparse
import collections
import glob
import json
import os
import re
import sqlite3
import statistics as st
import sys
from datetime import datetime

ROLE = {}  # host -> role, filled from --roles json; unknown hosts become node-N


def role(host):
    if host not in ROLE:
        ROLE[host] = "node-%d" % (len(ROLE) + 1)
    return ROLE[host]


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


MSG = re.compile(r"^## MSG from (\S+) -> (\S+)\s+\((.*?)\)")


def bus_stats(bus, until):
    seen, month, sender, kind = set(), collections.Counter(), collections.Counter(), collections.Counter()
    for f in glob.glob(os.path.join(bus, "inbox*.md")):
        lines = open(f, encoding="utf-8", errors="replace").read().split("\n")
        for i, line in enumerate(lines):
            m = MSG.match(line)
            if not m:
                continue
            first = lines[i + 1] if i + 1 < len(lines) else ""
            key = (m.group(1), m.group(2), m.group(3), first[:80])
            if key in seen or m.group(3)[:10] > until:
                continue
            seen.add(key)
            month[m.group(3)[:7]] += 1
            sender[role(m.group(1))] += 1
            body = re.sub(r"^#\S+\s*", "", first).strip().upper()
            if body.startswith(("ACK", "✅ ПРИНЯЛ", "AUTO-ACK")) or "AUTO-ACK" in body:
                kind["ack"] += 1
            elif body.startswith(("TASK", "ЗАДАЧА")):
                kind["task"] += 1
            else:
                kind["other"] += 1
    if not seen:
        return None
    return {"messages": sum(month.values()), "by_month": dict(sorted(month.items())),
            "senders": len(sender), "by_sender": dict(sender.most_common()), "by_kind": dict(kind)}


def consensus_stats(bus):
    ev = {}
    for f in glob.glob(os.path.join(bus, "_decisions", "log-*.jsonl")):
        b = os.path.basename(f)
        if b.startswith("log-tg-") or "TESTPEER" in b:   # chat mirrors / test peer
            continue
        for line in open(f, encoding="utf-8", errors="replace"):
            try:
                e = json.loads(line)
            except ValueError:
                continue          # per-line parse: a corrupt line never hides its neighbours
            if e.get("event_id"):
                ev[e["event_id"]] = e
    if not ev:
        return None
    P = collections.defaultdict(list)
    for e in ev.values():
        P[e["proposal_id"]].append(e)
    types = collections.Counter(e["type"] for e in ev.values())
    committed = [p for p, es in P.items() if any(e["type"] == "COMMIT" for e in es)]
    t2 = [p for p, es in P.items() if any(e["type"] == "PROPOSE" and str(e.get("risk_tier")) == "2" for e in es)]
    t2_commit = t2_unapproved = 0
    for p in t2:
        es = sorted(P[p], key=lambda e: ts(e["ts"]))
        cs = [e for e in es if e["type"] == "COMMIT"]
        if cs:
            t2_commit += 1
            if not any(e["type"] == "HUMAN_APPROVED" and ts(e["ts"]) <= ts(cs[0]["ts"]) for e in es):
                t2_unapproved += 1
    dual = sum(1 for es in P.values() if len({e["actor"] for e in es if e["type"] == "COMMIT"}) >= 2)
    verified = 0
    for p in committed:
        commiters = {e["actor"] for e in P[p] if e["type"] == "COMMIT"}
        vs = {e["actor"] for e in P[p] if e["type"] == "VERIFY"}
        if len(vs) >= 2 and vs - commiters:
            verified += 1
    lat = []
    for p in committed:
        pr = [ts(e["ts"]) for e in P[p] if e["type"] == "PROPOSE"]
        cm = [ts(e["ts"]) for e in P[p] if e["type"] == "COMMIT"]
        if pr and cm and min(cm) >= min(pr):
            lat.append((min(cm) - min(pr)).total_seconds() / 60)
    esc = sorted((sum(1 for e in es if e["type"] == "ESCALATE") for es in P.values()), reverse=True)
    esc = [n for n in esc if n]
    lat.sort()
    tiers = collections.Counter(str(e.get("risk_tier")) for e in ev.values() if e["type"] == "PROPOSE")
    prop_month = collections.Counter(e["ts"][:7] for e in ev.values() if e["type"] == "PROPOSE")
    return {
        "events": len(ev), "proposals": len(P), "event_types": dict(types.most_common()),
        "first": min(e["ts"] for e in ev.values()), "last": max(e["ts"] for e in ev.values()),
        "writers": len({e["actor"] for e in ev.values()}),
        "proposals_by_month": dict(sorted(prop_month.items())), "propose_tiers": dict(tiers),
        "committed": len(committed),
        "rejected": sum(1 for es in P.values() if any(e["type"] == "REJECT" for e in es)),
        "tier2_proposals": len(t2), "tier2_committed": t2_commit,
        "tier2_committed_without_prior_approval": t2_unapproved,
        "dual_commit_distinct_actors": dual,
        "committed_with_2_verifiers_1_independent": verified,
        "propose_to_commit_min": {"n": len(lat), "median": round(st.median(lat), 1) if lat else None,
                                  "p90": round(lat[int(0.9 * len(lat))], 1) if lat else None},
        "escalations_per_escalated_proposal": esc,
    }


def debt_stats(bus):
    debt, pay = set(), set()
    files = glob.glob(os.path.join(bus, "_inbox_debt_ledger__from-*.jsonl"))
    for f in files:
        for line in open(f, encoding="utf-8", errors="replace"):
            try:
                e = json.loads(line)
            except ValueError:
                continue
            (debt if e.get("op") == "debt" else pay if e.get("op") == "pay" else set()).add(e.get("id"))
    if not files:
        return None
    return {"debts": len(debt), "paid": len(debt & pay), "unpaid": len(debt - pay)}


def deploy_stats(bus):
    pk = {}
    for f in glob.glob(os.path.join(bus, "_deploy", "_inbox", "*", "*.json")):
        try:
            d = json.load(open(f, encoding="utf-8"))
        except (ValueError, OSError):
            continue
        if isinstance(d, dict) and d.get("id"):
            pk[d["id"]] = d
    if not pk:
        return None
    v = collections.Counter(str(d.get("verdict")) for d in pk.values())
    return {"packages": len(pk), "verdicts": dict(v),
            "fleetwide": sum(1 for d in pk.values() if d.get("fleetwide"))}


def approval_stats(db):
    if not db or not os.path.exists(db):
        return None
    c = sqlite3.connect("file:%s?mode=ro" % db, uri=True)
    rows = c.execute("select status, created, decided from pending").fetchall()
    st_ = collections.Counter(r[0] for r in rows)
    dec = sorted((r[2] - r[1]) / 60 for r in rows if r[0] in ("approved", "rejected") and r[2] and r[1])
    return {"asks": len(rows), "by_status": dict(st_),
            "decided_minutes": {"n": len(dec), "median": round(st.median(dec), 1) if dec else None}}


def breakage_stats(path):
    if not path or not os.path.exists(path):
        return None
    rx = re.compile(r"^\|\s*(\d{4}-\d{2}-\d{2})[^|]*\|\s*([^|]+?)\s*\|")
    cls, dates = collections.Counter(), []
    for line in open(path, encoding="utf-8", errors="replace"):
        m = rx.match(line)
        if m:
            dates.append(m.group(1))
            cls[m.group(2)] += 1
    if not dates:
        return None
    return {"entries": sum(cls.values()), "classes": len(cls),
            "classes_ge3": sum(1 for n in cls.values() if n >= 3),
            "largest_class": max(cls.values()), "first": min(dates), "last": max(dates)}


def selftest():
    import tempfile
    d = tempfile.mkdtemp()
    os.makedirs(os.path.join(d, "_decisions"))
    evs = [("e1", "p1", "PROPOSE", "A", "2026-07-01T00:00:00Z", 2),
           ("e2", "p1", "HUMAN_APPROVED", "A", "2026-07-01T00:05:00Z", None),
           ("e3", "p1", "COMMIT", "A", "2026-07-01T00:10:00Z", None),
           ("e4", "p2", "PROPOSE", "B", "2026-07-02T00:00:00Z", 2),
           ("e5", "p2", "COMMIT", "B", "2026-07-02T00:01:00Z", None)]
    with open(os.path.join(d, "_decisions", "log-A.jsonl"), "w") as fh:
        for e in evs:
            fh.write(json.dumps(dict(zip(["event_id", "proposal_id", "type", "actor", "ts", "risk_tier"], e))) + "\n")
        fh.write("{corrupt\n")
    c = consensus_stats(d)
    ok = (c["events"] == 5 and c["tier2_committed"] == 2 and c["tier2_committed_without_prior_approval"] == 1
          and c["dual_commit_distinct_actors"] == 0)
    print("selftest", "OK" if ok else "FAIL", json.dumps(c)[:200])
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bus")
    ap.add_argument("--until", default="9999-12-31")
    ap.add_argument("--approvals")
    ap.add_argument("--breakage")
    ap.add_argument("--roles", help="json {host: role}")
    ap.add_argument("--out")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if a.roles:
        ROLE.update(json.load(open(a.roles, encoding="utf-8")))
    out = {"generated": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"), "until": a.until,
           "bus": bus_stats(a.bus, a.until), "consensus": consensus_stats(a.bus),
           "delivery_debt": debt_stats(a.bus), "deploy": deploy_stats(a.bus),
           "human_gate_one_node": approval_stats(a.approvals), "breakage_journal": breakage_stats(a.breakage)}
    s = json.dumps(out, indent=1, ensure_ascii=False)
    if a.out:
        open(a.out, "w", encoding="utf-8").write(s)
    print(s)
    return 0


if __name__ == "__main__":
    sys.exit(main())
