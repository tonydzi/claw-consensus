# -*- coding: utf-8 -*-
"""make_macros.py -- turn data/aggregates-cain-2027.json into numbers.tex (LaTeX macros).

Every numeric claim in main.tex is a macro defined here, so a number cannot appear in the
paper without a field in the JSON. Written 2026-10-27, Claude Fable 5.1.
"""
import json, os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
A = json.load(open(os.path.join(HERE, "..", "data", "aggregates-cain-2027.json"), encoding="utf-8"))
out = []


def m(name, val):
    if isinstance(val, float):
        val = ("%.1f" % val).rstrip("0").rstrip(".") if abs(val) < 100 else "%d" % round(val)
    if isinstance(val, int):
        val = "{:,}".format(val)
    out.append("\\newcommand{\\%s}{%s}" % (name, val))


def ci(name, trip, unit=""):
    if not trip:
        m(name, "n/a"); return
    e, lo, hi = trip
    f = (lambda v: ("%.1f" % v).rstrip("0").rstrip(".")) if max(abs(lo), abs(hi)) < 100 else (lambda v: "{:,}".format(int(round(v))))
    m(name, "%s%s [%s, %s]" % (f(e), unit, f(lo), f(hi)))


def pct(name, trip):
    e, lo, hi = trip
    m(name, "%.1f\\%% [%.1f, %.1f]" % (e, lo, hi))


c = A["consensus"]; b = A["bus"]; d = A["debt"]; cl = A["claims"]; dp = A["deploy"]; ap = A["approvals"]; k = A.get("classifier", {})
m("nEvents", c["events"]); m("nProposals", c["proposals"]); m("nPropose", c["event_types"]["PROPOSE"])
m("nEscalate", c["event_types"]["ESCALATE"]); m("nVerify", c["event_types"]["VERIFY"]); m("nAccept", c["event_types"]["ACCEPT"])
m("nCommitEv", c["event_types"]["COMMIT"]); m("nHumanApproved", c["event_types"]["HUMAN_APPROVED"]); m("nReject", c["event_types"]["REJECT"])
m("nCounter", c["event_types"]["COUNTER"]); m("nClarify", c["event_types"]["CLARIFY"])
m("nTiered", c["tiered_proposals"]); m("nTierZero", c["tiers"]["0"]); m("nTierOne", c["tiers"]["1"]); m("nTierTwo", c["tiers"]["2"])
m("nCommitted", c["committed"]); pct("pCommitted", c["committed_pct_wilson"])
m("nDualCommit", c["dual_commit_distinct_machines"]); m("nSameMachineRepeat", c["same_machine_repeated_commit"])
m("nTierTwoCommitted", c["tier2_committed"]); m("nTierTwoUnapproved", c["tier2_committed_without_prior_approval"])
m("pRuleOfThree", c["tier2_zero_rule_of_three_pct"])
f = c["tier2_fate"]; m("nTierTwoRejected", f.get("rejected", 0)); m("nTierTwoEscalatedOpen", f.get("escalated_no_decision", 0)); m("nTierTwoOther", sum(v for kk, v in f.items() if kk not in ("committed", "rejected", "escalated_no_decision")))
m("nIndepVerified", c["committed_with_independent_verifier"]); pct("pIndepVerified", c["independent_verifier_pct_wilson"])
m("nLatCommit", c["propose_to_commit_min"]["n"]); ci("ciCommitMedianMin", c["propose_to_commit_min"]["median_ci"])
ci("ciCommitPninetyMin", c["propose_to_commit_min"]["p90_ci"])
m("ciCommitPninetyDays", "%.1f [%.1f, %.1f]" % tuple(v / 1440 for v in c["propose_to_commit_min"]["p90_ci"]))
m("nEscalated", c["escalated_proposals"]); m("nEscTopEight", c["escalations_top8"]); m("pEscTopEight", c["escalations_top8_pct"])
m("escTopSeven", ", ".join(str(v) for v in c["escalations_sorted"][:7])); m("escEighth", c["escalations_sorted"][7])
m("nWriters", len([w for w in c["writers"] if w > 1]))
pm = c["proposals_by_month"]; m("propJun", pm.get("2026-06", 0)); m("propJul", pm.get("2026-07", 0)); m("propAug", pm.get("2026-08", 0)); m("propSep", pm.get("2026-09", 0)); m("propOct", pm.get("2026-10", 0))
mk = c["proposals_trend_mk"]; m("mkS", mk["S"]); m("mkZ", "%.2f" % mk["z"]); m("mkP", "%.3f" % mk["p"]); m("mkTau", "%.2f" % mk["tau"]); m("mkN", mk["n"])
m("nClaims", cl["in_window"]); m("claimsJul", cl["by_month"]["2026-07"]); m("claimsAug", cl["by_month"]["2026-08"]); m("claimsSep", cl["by_month"]["2026-09"])
m("claimsExclusive", cl["modes"]["exclusive"]); m("claimsCollab", cl["modes"]["collab"]); m("claimsFyi", cl["modes"]["fyi"])
m("claimsClosed", cl["status"]["closed"]); m("claimsExpired", cl["status"]["expired"]); m("claimsTopMachine", cl["by_machine_sorted"][0])
m("claimsInteractive", cl["agent_kind"].get("interactive", 0)); m("claimsHeadless", cl["agent_kind"].get("headless", 0))
m("nPackages", dp["in_window"]); m("pkgJul", dp["by_month"]["2026-07"]); m("pkgAug", dp["by_month"]["2026-08"]); m("pkgSep", dp["by_month"]["2026-09"])
m("pkgPass", dp["verdict_pass"]); m("pkgFleetwide", dp["fleetwide"])
m("busMerged", b["in_window"]); m("busLive", b["live_blocks"]); m("busJul", b["by_month"]["2026-07"]); m("busAug", b["by_month"]["2026-08"]); m("busSep", b["by_month"]["2026-09"]); m("busJun", b["by_month"]["2026-06"])
r = b["retention_vs_snapshot"]; m("retJul", int(round(100 * r["2026-07"]))); m("retAug", int(round(100 * r["2026-08"]))); m("retSep", int(round(100 * r["2026-09"])))
bc = b["by_class"]; m("busAlert", bc.get("alert", 0)); m("busAck", bc.get("ack", 0)); m("busOther", bc.get("other", 0)); m("busTask", bc.get("task", 0)); m("busReping", bc.get("reping", 0)); m("busConsensus", bc.get("consensus", 0)); m("busHeartbeat", bc.get("heartbeat", 0))
m("busAutomatedPct", "%.1f" % (100 * (bc.get("alert", 0) + bc.get("heartbeat", 0)) / b["in_window"]))
m("busTaskBearingPct", "%.1f" % (100 * (bc.get("task", 0) + bc.get("reping", 0) + bc.get("consensus", 0)) / b["in_window"]))
m("busAckPct", "%.1f" % (100 * bc.get("ack", 0) / b["in_window"]))
pct("pBroadcast", b["broadcast_share_wilson"])
ta = b["task_ack"]; m("nTasksId", ta["tasks_with_id"]); m("nMatched", ta["matched"]); pct("pMatched", ta["matched_pct_wilson"])
m("nRawNeg", ta["raw_negative"]); m("pRawNeg", ta["raw_negative_pct"]); m("nNormNeg", ta["normalized_negative"]); m("pNormNeg", ta["normalized_negative_pct"])
m("nAckPos", ta["n_positive"]); ci("ciAckMedianH", ta["latency_h_median_ci"]); ci("ciAckPninetyH", ta["latency_h_p90_ci"])
m("nDebts", d["debts_in_window"]); m("nRepaid", d["repaid"]); pct("pRepaid", d["repaid_pct_wilson"]); ci("ciRepayMedianH", d["repay_h_median_ci"]); ci("ciRepayPninetyH", d["repay_h_p90_ci"])
m("ciRepayMedianDays", "%.1f [%.1f, %.1f]" % tuple(v / 24 for v in d["repay_h_median_ci"]))
m("nApprovals", ap["requests"]); m("nApproved", ap["status"].get("approved", 0)); m("nRejected", ap["status"].get("rejected", 0)); m("nStale", ap["status"].get("stale", 0)); m("nExpired", ap["status"].get("expired", 0)); m("nResolved", ap["status"].get("resolved", 0))
m("nExplicit", ap["explicit_decisions"]); pct("pExplicit", ap["explicit_pct_wilson"]); m("nExpiredOrStale", ap["expired_or_stale"]); pct("pExpiredOrStale", ap["expired_pct_wilson"])
m("nTta", ap["time_to_answer_min"]["n"]); ci("ciTtaMedianMin", ap["time_to_answer_min"]["median_ci"]); ci("ciTtaPninetyMin", ap["time_to_answer_min"]["p90_ci"])
m("ciTtaPninetyH", "%.1f [%.1f, %.1f]" % tuple(v / 60 for v in ap["time_to_answer_min"]["p90_ci"]))
# weekly normalization: first full July week vs last full September week
W = A["window"]["weeks"]; nd = b["node_days_by_week"]
w1, w2 = "2026-07-13", "2026-09-21"
for tag, w in (("A", w1), ("B", w2)):
    m("wk%sLabel" % tag, w[5:].replace("-", "/")); m("wk%sNodeDays" % tag, nd[w]); m("wk%sProp" % tag, c["proposals_by_week"][w]); m("wk%sClaims" % tag, cl["by_week"][w]); m("wk%sPkg" % tag, dp["by_week"][w])
    m("wk%sTask" % tag, b["by_week_class"][w].get("task", 0)); m("wk%sSessions" % tag, cl["distinct_sessions_by_week"][w])
    m("wk%sPropPerNd" % tag, "%.2f" % (c["proposals_by_week"][w] / nd[w])); m("wk%sClaimsPerNd" % tag, "%.2f" % (cl["by_week"][w] / nd[w])); m("wk%sPkgPerNd" % tag, "%.2f" % (dp["by_week"][w] / nd[w])); m("wk%sTaskPerNd" % tag, "%.2f" % (b["by_week_class"][w].get("task", 0) / nd[w]))
m("peakClaimsWeek", max(cl["by_week"].values())); m("peakSessionsWeek", max(cl["distinct_sessions_by_week"].values()))
if k:
    m("clfSample", k["sample"]); kr = k["kappa_claude_vs_rail"]
    m("clfRails", ", ".join(k["rails_available"]))
    k3 = lambda v: "%.2f" % v if isinstance(v, (int, float)) else str(v)
    m("kappaCodex", k3(kr.get("codex", ["n/a"])[0])); m("kappaGemini", k3(kr.get("gemini", ["n/a"])[0])); m("kappaMistral", k3(kr.get("mistral", ["n/a"])[0]))
    m("kappaMin", k3(min(v[0] for v in kr.values()))); m("kappaMax", k3(max(v[0] for v in kr.values())))
    m("goldN", k.get("majority_gold_n", 0)); m("clfAcc", "%.1f" % (100 * k.get("heuristic_accuracy_vs_majority", 0))); m("kappaHeur", k3(k.get("kappa_heuristic_vs_majority", ["n/a"])[0]))
    for lab, v in k.get("heuristic_prf_vs_majority", {}).items():
        m("prf%sN" % lab.capitalize(), v["n"]); m("prf%sP" % lab.capitalize(), "%.2f" % v["precision"] if v["precision"] is not None else "--"); m("prf%sR" % lab.capitalize(), "%.2f" % v["recall"] if v["recall"] is not None else "--")
open(os.path.join(HERE, "..", "numbers.tex"), "w", encoding="utf-8").write("% generated by analysis/make_macros.py from data/aggregates-cain-2027.json; do not edit by hand\n" + "\n".join(out) + "\n")
print(len(out), "macros")
