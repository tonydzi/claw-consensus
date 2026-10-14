# The consensus protocol

How two or more machines negotiate a decision, commit it, and verify it, without a human courier. Reference implementation: [`reference/consensus.py`](../reference/consensus.py).

## §1. Design goals

1. **Autonomy by default, human gates by exception.** Machines settle safe, reversible questions themselves. The human is woken up exactly twice: (a) a risky (Tier-2) action is on the table, (b) the machines deadlocked.
2. **Survives unreliable transport.** Every event may arrive twice (dual rails) or late (a sleeping laptop). The protocol is idempotent and order-tolerant.
3. **Reconstructable.** The ledger is append-only text. If a file is lost, the negotiation can be replayed from the group-chat feed, because every event was mirrored there by construction.
4. **Deterministic where possible.** Timeouts, round caps, tie-breaks, and safety gates are plain code (`tick`), not LLM judgment. The LLM only does what only an LLM can: propose, counter, and judge content.

## §2. The ledger: single-writer JSONL shards

Source of truth = `<bus>/_decisions/log-<MACHINE>.jsonl`, one shard per machine.

- Each machine **appends only to its own shard**. Two machines never write the same file, so whole-file sync (Syncthing, Dropbox) never conflicts, and git merges append-only text cleanly.
- Readers **merge all shards**, group events by `proposal_id`, and **dedup by `event_id`**. A duplicate delivery over the redundant rails is a no-op by design.
- Parse **per line, not per file**: one corrupt line (partial write, sync-conflict junk) must not silently drop every event after it. This was a real bug.
- Timestamps carry **microseconds**. Two events in the same second used to sort by shard filename instead of by causality; that was a real bug too.
- **Order is logical, not wall-clock.** Every event carries `seq`, a Lamport counter: the writer stamps 1 + the highest `seq` it can read in any shard. An answer therefore always sorts after the position it answers, even when the answering machine's clock runs minutes behind. Projection order is `(seq, ts, actor, event_id)`: `ts` only breaks ties between concurrent events, and the last two keys make the order total and identical on every machine. Events written before `seq` existed read as `seq` 0 and keep their old `ts` order. (Issue #9, row E: a laptop five minutes slow used to write an ACCEPT that sorted before the PROPOSE it accepted, and the proposal never reached `agreed`.)

Event shape:

```json
{"event_id": "1f0c...", "proposal_id": "9a2b...", "type": "PROPOSE",
 "actor": "HUB-1", "seq": 41, "ts": "2026-07-02T10:15:03.412000Z",
 "subject": "adopt nightly reindex at 03:30", "risk_tier": 0}
```

## §3. Verbs and state machine

Event types: `PROPOSE`, `COUNTER`, `ACCEPT`, `REJECT`, `COMMIT`, `LEADER_OVERRIDE`, `VERIFY`, `ESCALATE`, `HUMAN_APPROVED`.

```
PROPOSE ──> proposed ──ACCEPT(by non-owner)──> agreed ──COMMIT──> committed ──VERIFY x2──> done
   │            │                                                     (>=1 independent)
   │         COUNTER ──> countered ──ACCEPT──> agreed
   │            │
   │         REJECT ──> rejected
   │
   ├─ not agreed, leader decides ──LEADER_OVERRIDE──> overridden ──VERIFY x2──> done
   │
   └─ tier>=2 ──> escalated ──HUMAN_APPROVED──> commit unlocked
```

**`COMMIT` vs `LEADER_OVERRIDE`.** `COMMIT` is written only when the proposal is `agreed`, or when a Tier-2 proposal carries a recorded human approval (the human decided). When the leader decides anything else (a manual `commit` on a proposal nobody accepted, or `tick`'s disagree-and-commit), the event is `LEADER_OVERRIDE` and the status is `overridden`. Both close the proposal and both count for split-brain detection; the ledger keeps them apart so an arbiter's call is never read back as consensus. A non-leader still cannot commit an unagreed proposal at all.

Status is derived from the merged event set, never stored. Two subtleties that bit us:

- **Causality-robust acceptance.** Don't look at "the last event". Find the latest POSITION on the table (`PROPOSE` or `COUNTER`); only an `ACCEPT` from a machine that did NOT make that position, after it in projection order (§2), counts. This handles both "responder accepts proposal" and "proposer accepts a counter" without timestamp fragility.
- **A self-accept never counts.** The owner of the current position cannot accept it. See §6 for the infinite loop this prevents.

After `COMMIT`, the ledger records the decision; the AGENT applies the actual change (the engine never edits user files). Then both sides `verify`.

## §4. Risk tiers and the tripwire

| Tier | Meaning | Who decides |
|---|---|---|
| 0 | Safe, reversible, idempotent (reindex, read, draft-to-file) | machines auto-resolve, leader tie-breaks |
| 1 | Reversible but non-trivial | machines negotiate; timeout escalates to the human |
| 2 | Money, outbound to third parties, irreversible, secrets, shared config/canon | NEVER auto-commits; needs a recorded human approval |

The proposer labels the tier, and that is the weakest link of the whole design: if the agent mislabels a destructive action as Tier-0, the human gate never fires. So a **deterministic tier-tripwire** re-checks every proposal's text against an editable keyword list (delete, wipe, deploy, password, `git push --force`, ...) and force-bumps the tier to 2 on a hit, regardless of the label. The list is a plain text file the owner maintains; a built-in fallback keeps the guard alive if the file is missing.

Tripwire lesson: word-start matching, not plain substring. The term `send` used to fire on the identifier `bus_send.py`, spamming false Tier-2 escalations. Now word-terms match only at word starts (`send money` yes, `bus_send` no), while punctuation-led terms (`.env`, `rm -`) keep substring matching. The fix removes a false-positive class only; real hits still catch.

**Tier-2 closure.** Escalation alone is not enough: our first version had cmd_commit hard-refuse tier>=2, so even human-approved proposals sat ESCALATED forever. The `approve <id> "<proof>"` verb records the human's OK (who/where/message-id, proof string required), and only then does `commit` unlock. Auto-commit without a recorded approval stays banned.

## §5. The deterministic driver: `tick`

`tick` runs on a schedule (we use ~20 min on the always-on hub) and applies policy with zero LLM tokens:

1. **Split-brain** (see §6) -> escalate, never auto-resolve.
2. **Tier-2 still open** -> ensure escalated (belt and suspenders).
3. **Agreed but uncommitted** -> remind the responsible actor.
4. **Round cap hit** (default 3 PROPOSE/COUNTER rounds) without agreement -> escalate to the human. Two well-prompted agents that haven't converged in 3 rounds won't converge in 30; more rounds just burn tokens and delay the human's involvement. Machines must not filibuster.
5. **Timeout with no response:**
   - Tier-0 and I am the leader -> **disagree-and-commit**: the leader accepts (or, on its own proposal, records a `LEADER_OVERRIDE`) and moves on. The peer may still VERIFY and object after the fact. Autonomy beats stalling for safe actions.
   - Any other tier -> escalate. Only Tier-0 auto-resolves.

Timeouts respect a **per-peer SLA** (`peer_timeout_min` in the config): a sleeping laptop gets a longer window than the always-on hub, otherwise every proposal made during the night dies to timeout before the laptop wakes.

`tick` can only time out and escalate; it never ANSWERS a peer. The missing piece is `pending`: a 0-LLM detector that lists open proposals where the move is mine (latest position made by someone else). An inbox robot greps `pending` on its schedule and wakes the LLM judge only when the list is non-empty; the judge answers via `respond`. Cheap detector, expensive judge, in that order.

## §6. The three guards (each one paid for by a real incident)

**Guard 1: the self-accept loop.** The leader proposed something, the peer never answered, and every 20 minutes the tick auto-accepted the leader's OWN proposal, which never counts, forever. 17 identical ACCEPTs by morning. Fixes: a self-accept never advances state (§3); the leader's tie-break on its OWN proposal is a direct decision (`LEADER_OVERRIDE` since v0.4.1, `COMMIT` before), not another ACCEPT; and if my ACCEPT is already on the table but the state hasn't advanced, escalate the anomaly instead of re-spamming.

**Guard 2: independent verification.** The machine that applied the change may self-verify, but "globally done" requires at least one VERIFY from a machine that did NOT apply it. A **rubber-stamp guard** refuses a proof string that duplicates a prior VERIFY verbatim: `verify` exits 1 and records nothing, so a copied proof can never be counted toward "done". Re-check independently, don't copy the other machine's homework. `verify ... --advisory` keeps the pre-v0.4.1 behaviour (warn, record anyway) for anyone who needs it; it is advisory by name because it no longer guards anything. (Before v0.4.1 the guard only printed a warning and appended the VERIFY regardless; issue #9, row D.)

**Guard 3: split-brain detection.** Only one machine should ever apply+COMMIT a given proposal. If the merged ledger shows COMMITs from more than one actor, the network partitioned, both sides decided alone, and the partition then healed. That state is `conflict`, never silently "committed", and it always escalates to the human for reconciliation (the leader's log is authoritative as a starting point).

## §6a. Four more guards, from the month after this repo first shipped

Guards 1-3 above shipped with v0.1.0. Then we ran the engine across six machines for another
month and four more things bit us. Each one is the same shape as the first three and as each
other: **something the protocol trusted without checking.** All four are in
[`reference/protocol_guards.py`](../reference/protocol_guards.py) as pure functions with a
selftest (`python protocol_guards.py selftest`).

**Guard 4: arbiter election — the tie-break role must survive its own holder dying.** §5 pins
the tie-break to one leader named in config. Deterministic, single-writer, fine — until the
leader is the machine that dies. Then nothing re-assigns the role: tier-0 proposals sit
unresolved and *nobody is alerted*, because every remaining peer is behaving correctly. The fix
reuses the presence lease the bus already writes (a per-machine heartbeat file whose mtime is
the only signal), so there is no new heartbeat and no new state to sync:

- every ticking peer computes the arbiter the **same way** from the **same ordered list**, so all
  peers agree on who arbitrates this tick without exchanging one message;
- a machine is eligible only on **positive evidence** of life (fresh stamp). An **absent** stamp
  promotes nobody — "no evidence" is not "dead", and fail-safe beats fail-fast for a role whose
  failure mode is two arbiters;
- the machine running the tick is always eligible, so a travelling laptop at the head of the list
  is skipped to the next live machine and reclaims the role when its stamp goes fresh;
- rollout is gated by `arbiter_order_armed`: unarmed, a peer behaves **identically** to the old
  fixed-leader build. Shipping a new election rule to half a fleet is how you get two arbiters.

The failover is announced **once per episode** by an edge detector, not once per tick — N peers
N-plexing the same alert is how real alerts stop being read.

**Guard 5: proof grading — "verified" has to mean something a machine can check.** The VERIFY
verb carries a free-text proof, and in practice agents wrote "checked, works" and the task went
green. A claim is not evidence. A proof now grades as `proven` only if it carries the **residue
of an action**: a zero exit code, a hash, a counter that moved, a before/after pair. This is not
a language check — an agent that wants to fake it must now fabricate a specific number, which is
a far brighter line to cross and a far easier lie to catch. Grading arms from a timestamp, so
events recorded before you turned it on are grandfathered: retroactive standards invalidate
honest history, and nobody trusts a ledger after that.

**Guard 6: risk tracking — one integer chosen by the proposer is not a risk model.** The tripwire
(§4) catches dangerous *words*. It does not catch a dangerous *category* carried at a low tier:
"update the outreach template" is tier-0 by any reasonable reading and still ends with text going
to strangers. So a proposal is also classified into a track (financial / secrets / outbound /
canon / infra / general) independently of the tier its proposer chose.

Note how it ships: **in shadow**. It classifies, it logs, it changes nothing. The honest question
is not "is a second signal nicer" but "does it catch anything the tier alone misses", and only a
log answers that. The verdict field is written to be **falsifiable** — `would_benefit` is False
for anything already tier-2 (the engine escalates it regardless) and False for the general track;
it is True only for the narrow case the guard exists for. If it stays at zero for a month, the
guard is not needed and we delete it. `shadow_report()` prints that number.

**Guard 7: signature audit — identity by filename is not identity.** Every event carries `actor`,
and its shard is named after a machine. Both are strings written by whoever has write access to a
shared folder, so the ledger's answer to "who decided this" was "whoever claimed to" — which
hollows out the Tier-2 gate, whose entire value is a *recorded* human approval. The identity layer
is [`reference/fleet_sign.py`](../reference/fleet_sign.py): Ed25519 detached signatures via
`ssh-keygen -Y`, one public key file per machine (single writer, no conflicts), a revocation list,
and an allowed-signers set assembled in memory at every verify so there is no shared file to go
stale. Private keys are never synced — syncing identity is what produced our duplicate-session
incidents.

Two distinctions carry the whole design:

- **unsigned ≠ forged.** Unsigned is a rollout gap (`None`); a bad signature is a tampering
  signal (`False`). A bad signature is refused even during the dark phase; an unsigned one is not.
  Conflating them is why "we have signatures" often means nothing.
- **rollout order is not optional:** sign everything and verify nothing → a readable audit
  command → only then set an enforcement timestamp, forward in time. Flip straight to enforcement
  and you quarantine every machine without a key yet, which on day one is all of them.

## §7. Escalation channels: keep the alarm away from the noise

Routine consensus traffic (every PROPOSE/ACCEPT/COMMIT) is mirrored to the machine group chat: humans can watch the negotiation in plain sight. But a question that NEEDS a human must not live in that feed: heartbeats and progress lines bury it in minutes. Run a **second, quiet channel** (another group chat) that carries ONLY "a human is needed NOW" events: deadlocks, Tier-2 approvals. In the reference code that is `HUMAN_ALERT_CMD`, fired only on round-cap deadlock, tier>0 timeout, or manual escalate.

The human answers with an approval token from their phone (see [GOVERNANCE.md](GOVERNANCE.md) §4), and any machine records it via `approve`.

## §8. What the engine deliberately does NOT do

- It never edits user files. It is a ledger, not an executor: the agent applies agreed changes.
- It never lets redundancy stand in for independence: two verifies from the same machine don't count.
- It never deletes or rewrites history. Supersede with new events; the log is append-only.
- It never trusts the transport. Every rail can duplicate, delay, or drop; the ledger absorbs all three.
