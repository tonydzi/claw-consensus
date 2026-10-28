# -*- coding: utf-8 -*-
"""fleet_cain_stats.py -- aggregate-only statistics for the CAIN 2027 revision of the Fleet report.

Inputs (read-only): the production bus (_machine-bus), its Syncthing version store (.stversions),
the consensus ledger shards, the delivery-debt ledgers, the work-claim archive, the deployment
register inbox, the laptop approval database and the classifier audit labels (scratch dir).
Output: data/aggregates-cain-2027.json (numbers only; no message bodies, no proposal subjects,
no chat identifiers). Figures are drawn from that JSON by make_figures.py.

Every number in cain-2027/main.tex is a field of the JSON this script writes.
Written 2026-10-27 by Claude Fable 5.1 on the fleet laptop; rail: claude.
Usage: python fleet_cain_stats.py <scratch dir with tz_est.json, clf_sample.json, labels_*.txt>
"""
import glob, json, os, re, sys, math, random, sqlite3, collections, datetime as dt
import numpy as np
from scipy.stats import norm

BUS = os.environ["MACHINE_BUS_DIR"]   # the synced bus folder of the fleet (set by the operator)
# host names never leave the machine: they are mapped to role labels before anything is written.
# Supply the map as JSON ({"<hostname>": "<role>"}) in FLEET_ROLES_JSON; unknown hosts become node-x.
ROLE = json.load(open(os.environ["FLEET_ROLES_JSON"], encoding="utf-8")) if os.environ.get("FLEET_ROLES_JSON") else {}
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "data"); FIG = os.path.join(HERE, "..", "figures")
SCRATCH = sys.argv[1] if len(sys.argv) > 1 else None
WIN_LO, WIN_HI = "2026-06-20", "2026-09-30"
CONS_HI = "2026-10-01T23:59:59"
random.seed(20261027); np.random.seed(20261027)


def wilson(k, n, z=1.96):
    if n == 0:
        return None
    p = k / n; d = 1 + z * z / n; c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return [round(100 * p, 1), round(100 * (c - h), 1), round(100 * (c + h), 1)]


def boot(xs, fn, B=2000):
    xs = np.asarray(xs, float)
    if len(xs) == 0:
        return None
    est = fn(xs); bs = [fn(np.random.choice(xs, len(xs))) for _ in range(B)]
    return [round(float(est), 1), round(float(np.percentile(bs, 2.5)), 1), round(float(np.percentile(bs, 97.5)), 1)]


def med(a):
    return float(np.median(a))


def p90(a):
    return float(np.percentile(a, 90))


def mann_kendall(x):
    x = list(x); n = len(x); s = 0
    for i in range(n - 1):
        for j in range(i + 1, n):
            s += np.sign(x[j] - x[i])
    var = n * (n - 1) * (2 * n + 5) / 18.0
    z = (s - 1) / math.sqrt(var) if s > 0 else (s + 1) / math.sqrt(var) if s < 0 else 0.0
    p = 2 * (1 - norm.cdf(abs(z)))
    return {"n": n, "S": int(s), "z": round(float(z), 2), "p": round(float(p), 4), "tau": round(float(s / (n * (n - 1) / 2)), 2)}


def rule_of_three(n):
    return round(100 * 3.0 / n, 1)


def wk(d):
    d = dt.date.fromisoformat(d[:10]); return (d - dt.timedelta(days=d.weekday())).isoformat()


def weeks(lo, hi):
    w = []; d = dt.date.fromisoformat(wk(lo))
    while d.isoformat() <= wk(hi):
        w.append(d.isoformat()); d += dt.timedelta(days=7)
    return w


WEEKS = weeks(WIN_LO, WIN_HI)
A = {"window": {"bus": [WIN_LO, WIN_HI], "consensus_end": CONS_HI[:10], "weeks": WEEKS}}

# ---------- 1. consensus ledger ----------
ev = {}
for f in glob.glob(os.path.join(BUS, "_decisions", "log-*.jsonl")):
    b = os.path.basename(f)
    if b.startswith("log-tg-") or "TESTPEER" in b:
        continue
    for line in open(f, encoding="utf-8", errors="replace"):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("event_id") and e.get("ts", "") <= CONS_HI:
            ev[e["event_id"]] = e
P = collections.defaultdict(list)
for e in ev.values():
    P[e["proposal_id"]].append(e)


def ts(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


types = collections.Counter(e["type"] for e in ev.values())
committed = {p for p, es in P.items() if any(e["type"] == "COMMIT" for e in es)}
tier = {}
for p, es in P.items():
    pr = [e for e in es if e["type"] == "PROPOSE"]
    if pr:
        tier[p] = str(pr[0].get("risk_tier"))
t2 = [p for p in P if tier.get(p) == "2"]
t2c = [p for p in t2 if p in committed]
t2_unapproved = 0
for p in t2c:
    es = P[p]; c0 = min(ts(e["ts"]) for e in es if e["type"] == "COMMIT")
    if not any(e["type"] == "HUMAN_APPROVED" and ts(e["ts"]) <= c0 for e in es):
        t2_unapproved += 1
dual_machines = sum(1 for es in P.values() if len({e["actor"] for e in es if e["type"] == "COMMIT"}) >= 2)
same_machine_repeat = sum(1 for es in P.values() if len([e for e in es if e["type"] == "COMMIT"]) >= 2 and len({e["actor"] for e in es if e["type"] == "COMMIT"}) == 1)
verified = 0
for p in committed:
    cm = {e["actor"] for e in P[p] if e["type"] == "COMMIT"}; vs = {e["actor"] for e in P[p] if e["type"] == "VERIFY"}
    if len(vs) >= 2 and vs - cm:
        verified += 1
lat = []
for p in committed:
    pr = [ts(e["ts"]) for e in P[p] if e["type"] == "PROPOSE"]; cm = [ts(e["ts"]) for e in P[p] if e["type"] == "COMMIT"]
    if pr and cm and min(cm) >= min(pr):
        lat.append((min(cm) - min(pr)).total_seconds() / 60)
esc_nz = [n for n in sorted([sum(1 for e in es if e["type"] == "ESCALATE") for es in P.values()], reverse=True) if n]
t2_fate = collections.Counter()
for p in t2:
    es = {e["type"] for e in P[p]}
    if p in committed:
        t2_fate["committed"] += 1
    elif "REJECT" in es:
        t2_fate["rejected"] += 1
    elif "HUMAN_APPROVED" in es:
        t2_fate["approved_not_committed"] += 1
    elif "ESCALATE" in es:
        t2_fate["escalated_no_decision"] += 1
    elif "ACCEPT" in es or "COUNTER" in es:
        t2_fate["responded_no_decision"] += 1
    else:
        t2_fate["no_response"] += 1
pbw = {w: 0 for w in WEEKS}
pbw.update(collections.Counter(wk(e["ts"]) for e in ev.values() if e["type"] == "PROPOSE" and WIN_LO <= e["ts"][:10] <= WIN_HI))
A["consensus"] = {
    "events": len(ev), "proposals": len(P), "event_types": dict(types.most_common()),
    "first": min(e["ts"] for e in ev.values())[:10], "last": max(e["ts"] for e in ev.values())[:10],
    "writers": sorted(collections.Counter(e["actor"] for e in ev.values()).values(), reverse=True),
    "proposals_by_month": dict(sorted(collections.Counter(e["ts"][:7] for e in ev.values() if e["type"] == "PROPOSE").items())),
    "proposals_by_week": pbw,
    "tiers": dict(collections.Counter(tier.values())), "tiered_proposals": len(tier),
    "committed": len(committed), "committed_pct_wilson": wilson(len(committed), len(P)),
    "commit_events": types["COMMIT"],
    "dual_commit_distinct_machines": dual_machines, "same_machine_repeated_commit": same_machine_repeat,
    "tier2_proposals": len(t2), "tier2_committed": len(t2c), "tier2_committed_without_prior_approval": t2_unapproved,
    "tier2_zero_rule_of_three_pct": rule_of_three(len(t2c)),
    "tier2_fate": dict(t2_fate),
    "committed_with_independent_verifier": verified, "independent_verifier_pct_wilson": wilson(verified, len(committed)),
    "propose_to_commit_min": {"n": len(lat), "median_ci": boot(lat, med), "p90_ci": boot(lat, p90)},
    "escalations_total": types["ESCALATE"], "escalated_proposals": len(esc_nz),
    "escalations_top8": sum(esc_nz[:8]), "escalations_top8_pct": round(100 * sum(esc_nz[:8]) / types["ESCALATE"], 1),
    "escalations_sorted": esc_nz,
    "human_approved_events": types["HUMAN_APPROVED"],
}
A["consensus"]["proposals_trend_mk"] = mann_kendall([pbw[w] for w in WEEKS if w >= "2026-06-29"])

# ---------- 2. work claims ----------
claims = []
for f in glob.glob(os.path.join(BUS, "_onair", "archive", "*.json")) + glob.glob(os.path.join(BUS, "_onair", "active", "*.json")):
    m = re.search(r"__(\d{4}-\d{2}-\d{2})T", os.path.basename(f))
    if not m:
        continue
    try:
        d = json.load(open(f, encoding="utf-8"))
    except Exception:
        d = {}
    h = d.get("holder") or {}
    claims.append((m.group(1), d.get("mode"), d.get("status"), h.get("machine"), h.get("session_id"), h.get("agent_kind")))
cw = [c for c in claims if WIN_LO <= c[0] <= WIN_HI]
cbw = {w: 0 for w in WEEKS}; cbw.update(collections.Counter(wk(c[0]) for c in cw))
sw = collections.defaultdict(set)
for c in cw:
    sw[wk(c[0])].add((c[3], c[4]))
A["claims"] = {
    "in_window": len(cw), "by_month": dict(sorted(collections.Counter(c[0][:7] for c in cw).items())),
    "by_week": cbw,
    "modes": dict(collections.Counter(c[1] for c in cw)), "status": dict(collections.Counter(c[2] for c in cw)),
    "by_machine_sorted": sorted(collections.Counter(c[3] for c in cw).values(), reverse=True),
    "distinct_sessions_by_week": {w: len(sw.get(w, ())) for w in WEEKS},
    "agent_kind": dict(collections.Counter(c[5] for c in cw)),
    "first_claim": min(c[0] for c in claims),
}

# ---------- 3. deployment packages ----------
pk = {}
for f in glob.glob(os.path.join(BUS, "_deploy", "_inbox", "*", "*.json")):
    try:
        d = json.load(open(f, encoding="utf-8"))
    except Exception:
        continue
    s = str(d.get("sent", ""))[:10]
    if d.get("id") and s:
        pk.setdefault(d["id"], (s, d.get("verdict"), d.get("tier"), d.get("fleetwide")))
pw = {k: v for k, v in pk.items() if WIN_LO <= v[0] <= WIN_HI}
dbw = {w: 0 for w in WEEKS}; dbw.update(collections.Counter(wk(v[0]) for v in pw.values()))
A["deploy"] = {
    "in_window": len(pw), "by_month": dict(sorted(collections.Counter(v[0][:7] for v in pw.values()).items())),
    "by_week": dbw,
    "verdict_pass": sum(1 for v in pw.values() if v[1] == "PASS"), "fleetwide": sum(1 for v in pw.values() if v[3]),
    "first_package": min(v[0] for v in pk.values()),
}

# ---------- 4. text-rail bus: live + version store, deduplicated ----------
MSG = re.compile(r"^## MSG from (\S+) -> (\S+)\s+\((\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\)\s*(?:#([0-9a-f]{6,}))?")
seen = {}


def scan(files):
    for f in files:
        try:
            L = open(f, encoding="utf-8", errors="replace").read().split("\n")
        except Exception:
            continue
        for i, l in enumerate(L):
            m = MSG.match(l)
            if not m:
                continue
            body = []; j = i + 1
            while j < len(L) and not L[j].startswith("## MSG from"):
                body.append(L[j]); j += 1
            body = "\n".join(body).strip(); mid = m.group(4)
            if not mid:
                m2 = re.match(r"^#([0-9a-f]{6,})", body); mid = m2.group(1) if m2 else None
            first = re.sub(r"^#\S+\s*", "", body.split("\n")[0] if body else "").strip()
            # dedup key: sender, target, second and the message id when present (else the first 80 chars);
            # panel finding 27.10: two distinct messages in one second with equal prefixes must not collapse
            key = (m.group(1), m.group(2), m.group(3), mid or first[:80])
            if key not in seen:
                seen[key] = {"s": m.group(1), "t": m.group(2), "ts": m.group(3), "id": mid, "first": first, "body": body}


scan(glob.glob(os.path.join(BUS, "inbox*.md"))); n_live = len(seen)
scan(glob.glob(os.path.join(BUS, ".stversions", "inbox*.md")))
M = [v for v in seen.values() if WIN_LO <= v["ts"][:10] <= WIN_HI]


def cls(first):
    """The regex classifier audited in the paper (first line of the payload)."""
    b = first.strip(); u = b.upper()
    if u.startswith(("ACK", "AUTO-ACK")) or b.startswith("✅") or "AUTO-ACK" in u:
        return "ack"
    if u.startswith(("TASK", "ЗАДАЧА", "ORDER")) or b.startswith("🤖"):
        return "task"
    if "ПЕРЕПИНГ" in u[:40] or "ДОГОН" in u[:40] or u.startswith(("RE-PING", "REPING", "REMINDER", "🔁 REMINDER", "⏰")):
        return "reping"
    if b.startswith(("🔴", "🧟", "🚨", "⚠️", "🐢", "🔁 [")) or "PROC-PATROL" in u[:40] or "BUS RAIL DOWN" in u[:40] or "POWER-BLACKBOX" in u[:60] or "[PEER-WATCH" in u[:40] or "[ST_SHARE" in u[:20] or "[СЛОН:" in u[:12]:
        return "alert"
    if "[CONSENSUS]" in u[:40] or b.startswith("🤝"):
        return "consensus"
    if b.startswith("💓") or "HEARTBEAT" in u[:30]:
        return "heartbeat"
    return "other"


for v in M:
    v["c"] = cls(v["first"])
snap = {"2026-06": 445, "2026-07": 5781, "2026-08": 5707, "2026-09": 10309}
bym = collections.Counter(v["ts"][:7] for v in M)
nd = collections.defaultdict(set)
for v in M:
    nd[wk(v["ts"])].add((v["s"], v["ts"][:10]))
A["bus"] = {
    "live_blocks": n_live, "merged_distinct_all_dates": len(seen), "in_window": len(M),
    "by_month": dict(sorted(bym.items())), "snapshot_2026_10_03_by_month": snap,
    "retention_vs_snapshot": {k: round(bym[k] / v, 2) for k, v in snap.items()},
    "by_class": dict(collections.Counter(v["c"] for v in M).most_common()),
    "task_bearing_classes": ["task", "reping", "consensus"], "automated_classes": ["alert", "heartbeat"],
    "by_week_class": {w: dict(collections.Counter(v["c"] for v in M if wk(v["ts"]) == w)) for w in WEEKS},
    "by_week_total": {w: sum(1 for v in M if wk(v["ts"]) == w) for w in WEEKS},
    "senders_sorted": sorted(collections.Counter(v["s"] for v in M).values(), reverse=True),
    "broadcast_share_wilson": wilson(sum(1 for v in M if v["t"] == "ALL"), len(M)),
    "node_days_by_week": {w: len(nd.get(w, ())) for w in WEEKS},
}
tz = {}
if SCRATCH and os.path.exists(os.path.join(SCRATCH, "tz_est.json")):
    # offsets inferred per node and month from the debt ledger (p90 of header minus ledger UTC);
    # implausible values (|offset| > 14 h, produced by reused ids) are dropped
    tz = {k: v for k, v in json.load(open(os.path.join(SCRATCH, "tz_est.json"))).items() if abs(v) <= 14}
    months = ("2026-06", "2026-07", "2026-08", "2026-09", "2026-10")
    for node in {k.split("|")[0] for k in tz}:
        have = [m for m in months if "%s|%s" % (node, m) in tz]
        for m in months:   # a month without an estimate borrows the nearest estimated month of the same node
            if "%s|%s" % (node, m) not in tz and have:
                tz["%s|%s" % (node, m)] = tz["%s|%s" % (node, min(have, key=lambda h: abs(int(h[5:]) - int(m[5:]))))]
    for m in months:
        tz.setdefault("FLEET-ANCHOR|" + m, 0)   # Hetzner VPS, assumed UTC (too few ledger matches to estimate)


def utc(v):
    off = tz.get("%s|%s" % (v["s"], v["ts"][:7]))
    if off is None:
        off = 0 if v["s"] == "FLEET-ANCHOR" else tz.get("%s|2026-09" % v["s"])
    if off is None:
        return None
    return dt.datetime.strptime(v["ts"], "%Y-%m-%d %H:%M:%S") - dt.timedelta(hours=off)


tasks = {v["id"]: v for v in M if v["c"] == "task" and v["id"]}
first_ack = {}
for a in (v for v in M if v["c"] == "ack"):
    for ref in set(re.findall(r"#([0-9a-f]{6,})", a["body"])):
        for tid in tasks:
            if tid.startswith(ref) or ref.startswith(tid):
                if tid not in first_ack or a["ts"] < first_ack[tid]["ts"]:
                    first_ack[tid] = a
pairs = [(tasks[t], a) for t, a in first_ack.items()]
raw = [(dt.datetime.strptime(a["ts"], "%Y-%m-%d %H:%M:%S") - dt.datetime.strptime(t["ts"], "%Y-%m-%d %H:%M:%S")).total_seconds() / 3600 for t, a in pairs]
normd = [(utc(a) - utc(t)).total_seconds() / 3600 for t, a in pairs if utc(a) and utc(t)]
pos = [x for x in normd if x >= 0]
A["bus"]["task_ack"] = {
    "tasks_with_id": len(tasks), "matched": len(pairs), "matched_pct_wilson": wilson(len(pairs), len(tasks)),
    "raw_negative": sum(1 for x in raw if x < 0), "raw_negative_pct": round(100 * sum(1 for x in raw if x < 0) / max(1, len(raw)), 1),
    "normalized_n": len(normd), "normalized_negative": sum(1 for x in normd if x < 0),
    "normalized_negative_pct": round(100 * sum(1 for x in normd if x < 0) / max(1, len(normd)), 1),
    "latency_h_median_ci": boot(pos, med), "latency_h_p90_ci": boot(pos, p90), "n_positive": len(pos),
    "tz_offsets_used": {ROLE.get(k.split("|")[0], "node-x") + "|" + k.split("|")[1]: v for k, v in tz.items()},
}
A["bus"]["task_ack_latencies_h"] = sorted(round(x, 2) for x in pos)

# ---------- 5. delivery-debt ledger (UTC epoch on both sides) ----------
debt, pay = {}, {}
for f in glob.glob(os.path.join(BUS, "_inbox_debt_ledger__from-*.jsonl")):
    for line in open(f, encoding="utf-8", errors="replace"):
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if not e.get("id") or not e.get("ts"):
            continue
        if e.get("op") == "debt":
            debt.setdefault(e["id"], e)
        elif e.get("op") == "pay":
            pay.setdefault(e["id"], e)
lo = dt.datetime.fromisoformat(WIN_LO).replace(tzinfo=dt.timezone.utc).timestamp()
hi = dt.datetime.fromisoformat(WIN_HI).replace(tzinfo=dt.timezone.utc).timestamp() + 86400
D = {k: v for k, v in debt.items() if lo <= float(v["ts"]) < hi}
dl = sorted((float(pay[k]["ts"]) - float(D[k]["ts"])) / 3600 for k in D if k in pay and float(pay[k]["ts"]) >= float(D[k]["ts"]))
debw = {w: 0 for w in WEEKS}
debw.update(collections.Counter(wk(dt.datetime.fromtimestamp(float(v["ts"]), dt.timezone.utc).strftime("%Y-%m-%d")) for v in D.values()))
A["debt"] = {
    "debts_in_window": len(D), "repaid": sum(1 for k in D if k in pay), "repaid_pct_wilson": wilson(sum(1 for k in D if k in pay), len(D)),
    "repay_h_median_ci": boot(dl, med), "repay_h_p90_ci": boot(dl, p90), "n": len(dl),
    "by_week": debw,
    "first": dt.datetime.fromtimestamp(min(float(v["ts"]) for v in debt.values()), dt.timezone.utc).strftime("%Y-%m-%d"),
    "repay_latencies_h": [round(x, 2) for x in dl],
}

# ---------- 6. approval database of the laptop ----------
try:
    con = sqlite3.connect("file:%s?mode=ro" % os.environ["APPROVAL_DB"].replace("\\", "/"), uri=True)   # the node-local approval database
    cut = dt.datetime(2026, 10, 3, 23, 59, 59, tzinfo=dt.timezone.utc).timestamp()
    rows = con.execute("select created,status,decided,decided_by from pending where created<=?", (cut,)).fetchall()
    stat = collections.Counter(r[1] for r in rows)
    answered = [r for r in rows if r[1] in ("approved", "rejected")]
    tta = sorted((r[2] - r[0]) / 60 for r in answered if r[2])
    A["approvals"] = {"requests": len(rows), "status": dict(stat), "explicit_decisions": len(answered),
                      "explicit_pct_wilson": wilson(len(answered), len(rows)),
                      "expired_or_stale": stat["stale"] + stat["expired"],
                      "expired_pct_wilson": wilson(stat["stale"] + stat["expired"], len(rows)),
                      "time_to_answer_min": {"n": len(tta), "median_ci": boot(tta, med), "p90_ci": boot(tta, p90)},
                      "first": dt.datetime.fromtimestamp(min(r[0] for r in rows), dt.timezone.utc).strftime("%Y-%m-%d"),
                      "note": "rows created through 2026-10-03; the live table keeps growing and a later sweep relabelled one early row, so counts can differ by one from the 2026-10-03 snapshot"}
except Exception as e:
    A["approvals"] = {"error": str(e)}

# ---------- 7. classifier audit ----------
if SCRATCH and os.path.exists(os.path.join(SCRATCH, "clf_sample.json")):
    S = json.load(open(os.path.join(SCRATCH, "clf_sample.json"), encoding="utf-8"))

    def readlab(p):
        d = {}
        if not os.path.exists(p):
            return d
        for l in open(p, encoding="utf-8", errors="replace"):
            m = re.match(r"^\s*(\d+)\s+([a-z]+)", l.strip())
            if m:
                d[int(m.group(1))] = m.group(2)
        return d

    L1 = readlab(os.path.join(SCRATCH, "labels_claude.txt"))
    rails = {r: readlab(os.path.join(SCRATCH, "labels_%s.txt" % r)) for r in ("codex", "gemini", "mistral", "grok", "glm")}
    rails = {r: d for r, d in rails.items() if len(d) >= 0.9 * len(S)}
    labels = ["ack", "task", "reping", "alert", "consensus", "heartbeat", "other"]

    def kappa(a, b):
        ks = [k for k in a if k in b]; n = len(ks)
        po = sum(1 for k in ks if a[k] == b[k]) / n
        pe = sum((sum(1 for k in ks if a[k] == l) / n) * (sum(1 for k in ks if b[k] == l) / n) for l in labels)
        return [round((po - pe) / (1 - pe), 3), round(po, 3), n]

    def prf(gold, pred):
        out = {}
        for l in labels:
            tp = sum(1 for k in gold if gold[k] == l and pred.get(k) == l)
            fp = sum(1 for k in gold if pred.get(k) == l and gold[k] != l)
            fn = sum(1 for k in gold if gold[k] == l and pred.get(k) != l)
            out[l] = {"n": sum(1 for k in gold if gold[k] == l), "precision": round(tp / (tp + fp), 2) if tp + fp else None, "recall": round(tp / (tp + fn), 2) if tp + fn else None}
        return out

    heur = {s["n"]: cls(s["first"]) for s in S}
    A["classifier"] = {"sample": len(S), "strata_quota": dict(collections.Counter(s["heur"] for s in S)),
                       "labeler1": "Claude Fable 5.1 (author-side)", "rails_available": list(rails),
                       "kappa_claude_vs_rail": {r: kappa(L1, d) for r, d in rails.items()},
                       "kappa_rails_pairwise": {"%s_vs_%s" % (a, b): kappa(rails[a], rails[b]) for a in rails for b in rails if a < b},
                       "heuristic_accuracy_vs_claude": round(sum(1 for k in L1 if heur.get(k) == L1[k]) / len(L1), 3),
                       "heuristic_prf_vs_claude": prf(L1, heur),
                       "kappa_heuristic_vs_claude": kappa(L1, heur)}
    if rails:
        gold = {}
        for k in L1:
            votes = collections.Counter([L1[k]] + [d[k] for d in rails.values() if k in d]); l, c = votes.most_common(1)[0]
            if c >= 2:
                gold[k] = l
        A["classifier"]["majority_gold_n"] = len(gold)
        A["classifier"]["heuristic_accuracy_vs_majority"] = round(sum(1 for k in gold if heur.get(k) == gold[k]) / len(gold), 3)
        A["classifier"]["heuristic_prf_vs_majority"] = prf(gold, heur)
        A["classifier"]["kappa_heuristic_vs_majority"] = kappa(gold, heur)

A["consensus"]["propose_to_commit_latencies_min"] = sorted(round(x, 1) for x in lat)
A["consensus"]["tier2_fate_order"] = ["committed", "rejected", "approved_not_committed", "escalated_no_decision", "responded_no_decision", "no_response"]

json.dump(A, open(os.path.join(OUT, "aggregates-cain-2027.json"), "w", encoding="utf-8"), indent=1, ensure_ascii=False)
small = {k: v for k, v in A.items() if k not in ("bus", "debt")}
small["bus"] = {k: v for k, v in A["bus"].items() if k not in ("task_ack_latencies_h", "by_week_class")}
small["debt"] = {k: v for k, v in A["debt"].items() if k != "repay_latencies_h"}
print(json.dumps(small, indent=1, ensure_ascii=False))
