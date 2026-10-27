# -*- coding: utf-8 -*-
"""make_figures.py -- draw figures/*.pdf from data/aggregates-cain-2027.json (no raw data needed).
Written 2026-10-27, Claude Fable 5.1."""
import json, os
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
HERE = os.path.dirname(os.path.abspath(__file__)); FIG = os.path.join(HERE, "..", "figures"); os.makedirs(FIG, exist_ok=True)
A = json.load(open(os.path.join(HERE, "..", "data", "aggregates-cain-2027.json"), encoding="utf-8"))
W = A["window"]["weeks"]; c = A["consensus"]; b = A["bus"]; cl = A["claims"]; dp = A["deploy"]; d = A["debt"]
plt.rcParams.update({"font.size": 8, "font.family": "serif", "axes.spines.top": False, "axes.spines.right": False})

x = list(range(len(W))); lab = [w[5:].replace("-", "/") for w in W]
fig, ax = plt.subplots(figsize=(3.5, 2.9))
series = [("Consensus proposals", [c["proposals_by_week"][w] for w in W], "k", "o"),
          ("Work claims", [cl["by_week"][w] for w in W], "tab:blue", "s"),
          ("Deployment packages", [dp["by_week"][w] for w in W], "tab:orange", "^"),
          ("Bus task messages (text rail, lower bound)", [b["by_week_class"][w].get("task", 0) for w in W], "tab:green", "x"),
          ("Delivery debts (UTC ledger)", [d["by_week"][w] for w in W], "tab:gray", "d")]
for name, ys, col, mk in series:
    ax.plot(x, [y if y > 0 else np.nan for y in ys], marker=mk, ms=3, lw=1, color=col, label=name)
ax.set_yscale("log"); ax.set_xticks(x[::2]); ax.set_xticklabels(lab[::2], rotation=60); ax.set_ylabel("events per ISO week (log)")
ax.legend(fontsize=6, frameon=False, loc="upper center", bbox_to_anchor=(0.5, -0.32), ncol=2)
fig.tight_layout(); fig.savefig(os.path.join(FIG, "fig_weekly.pdf")); plt.close(fig)

e = c["escalations_sorted"]; cum = np.cumsum(e) / sum(e) * 100
fig, ax = plt.subplots(figsize=(3.5, 2.1))
ax.bar(range(1, len(e) + 1), e, color="tab:gray", width=0.8); ax2 = ax.twinx(); ax2.plot(range(1, len(e) + 1), cum, color="k", lw=1)
ax2.set_ylim(0, 100); ax2.set_ylabel("cumulative share (%)"); ax2.spines["top"].set_visible(False)
ax.set_xlabel("proposals ranked by escalation events (n=%d)" % len(e)); ax.set_ylabel("escalation events"); ax.set_xlim(0, len(e) + 1)
ax.axvline(8.5, color="k", ls=":", lw=0.8); fig.tight_layout(); fig.savefig(os.path.join(FIG, "fig_pareto.pdf")); plt.close(fig)

fig, ax = plt.subplots(figsize=(3.5, 2.3))
for name, xs, col in [("Propose to commit, ledger (n=%d)" % len(c["propose_to_commit_latencies_min"]), np.array(c["propose_to_commit_latencies_min"]) / 60, "k"),
                      ("Task to first ack, text rail (n=%d)" % len(b["task_ack_latencies_h"]), np.array(b["task_ack_latencies_h"]), "tab:green"),
                      ("Debt to repayment, ledger (n=%d)" % len(d["repay_latencies_h"]), np.array(d["repay_latencies_h"]), "tab:gray")]:
    xs = np.sort(xs[xs > 0]); ax.step(xs, np.arange(1, len(xs) + 1) / len(xs), where="post", color=col, lw=1, label=name)
ax.set_xscale("log"); ax.set_xlabel("hours (log)"); ax.set_ylabel("CDF"); ax.legend(fontsize=6, frameon=False, loc="upper left")
fig.tight_layout(); fig.savefig(os.path.join(FIG, "fig_cdf.pdf")); plt.close(fig)

order = c["tier2_fate_order"]; vals = [c["tier2_fate"].get(k, 0) for k in order]
fig, ax = plt.subplots(figsize=(3.5, 1.9))
ax.barh(range(len(order)), vals, color=["k"] + ["tab:gray"] * 5)
ax.set_yticks(range(len(order))); ax.set_yticklabels([k.replace("_", " ") for k in order]); ax.invert_yaxis(); ax.set_xlabel("Tier-2 proposals (n=%d)" % c["tier2_proposals"])
for i, v in enumerate(vals):
    ax.text(v + 0.3, i, str(v), va="center", fontsize=7)
fig.tight_layout(); fig.savefig(os.path.join(FIG, "fig_funnel.pdf")); plt.close(fig)
print("figures written")
