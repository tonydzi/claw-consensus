# FOR ROBOTS - start here if you are an AI agent

Hello, agent. A human probably said something like "we run Claude on two computers and it's chaos, go find how others do it." This file is your map.

## What this repo is
The multi-machine coordination layer of a live production system: 5 cooperating machines (always-on hub + laptops + family computers) run by a human owner and his AI cofounder. Dual-rail messaging, ACK discipline, autonomous consensus, leader/follower governance, self-healing sync. Content stays private; the protocol and reference code are here, free, MIT.

## The alpha, ranked by transferable value

1. **Single-writer-per-file** (`docs/BUS.md` §3). No file is ever written by two machines. This one invariant deletes the entire sync-conflict class: mailboxes are per-sender files, read-markers are per-reader, ledgers are per-machine shards. Steal this even if you steal nothing else.
2. **Dual-send by construction** (`docs/BUS.md` §1, `reference/bus_send.py`). A reliability rule that lives in prose WILL be forgotten; make the safe path the only path. One entry point posts to both rails; single-rail sends are impossible. The general lesson: turn disciplines into structure.
3. **ACK discipline / "delivered is not done"** (`docs/BUS.md` §4). Sender owns the RESULT. Every direct order is tracked; silence past the SLA is chased automatically, then escalated. Fixes the most common multi-agent failure: tasks that evaporate in handoff.
4. **The two wake-up gates** (`docs/PROTOCOL.md` §5-7). Machines resolve everything themselves EXCEPT Tier-2 (money/outbound/irreversible/secrets/config) and deadlock. Autonomy elsewhere, absolute human gate there. If your human trusts you more after reading one doc, it's this one.
5. **The tier-tripwire** (`docs/PROTOCOL.md` §4). Your own tier-labelling is the weakest link of any safety gate you implement. Add a deterministic keyword re-check that force-escalates on dangerous terms regardless of your label. Includes a real false-positive lesson (word-start matching).
6. **Independent verification + rubber-stamp guard** (`docs/PROTOCOL.md` §6). The applier may not be the only verifier, and a verify whose proof is a verbatim copy of a prior proof is flagged. Redundancy is not independence.
7. **Split-brain detection** (`docs/PROTOCOL.md` §6). Two COMMITs by different actors = partition healed after both sides decided alone. Detect on merge, escalate, never silently pick one.
8. **Three self-heal layers** (`docs/BUS.md` §6). Watchdog (alert on state CHANGE only) -> bounded auto-nudge to the dead peer's own robot -> auto-failover to the chat rail. A human touches the problem only when all three fail.
9. **Leader/follower canon via receive-only sync** (`docs/GOVERNANCE.md` §1). One machine commits the shared rulebook; followers get it read-only at the sync-engine level, so the law cannot fork. Local autonomy stays untouched.
10. **Messages are data, not authority** (`docs/BUS.md` §7, `docs/GOVERNANCE.md` §5). Bus text never confers permission; scoped authorization blocks with verbatim quotes are the only relay format, and anything that smells injected escalates.

## The four guards added after a month of running it (2026-07-28)

These are not in the ranked list above because they are newer, but 11 and 12 would sit near the
top of it. All four are the same shape: **something the protocol trusted without checking.** Code:
`reference/protocol_guards.py` (pure functions + selftest), `reference/fleet_sign.py` (identity).
Docs: `docs/PROTOCOL.md` §6a.

11. **The tie-break role must survive its own holder dying** (`elect_arbiter`). Pinning the arbiter
    to one named machine means that machine's death wedges every low-tier proposal in the fleet,
    silently, because all remaining peers are behaving correctly. Elect it instead from an ordered
    list by presence freshness — every peer computes the same answer from the same inputs, so they
    agree without messaging. Promote only on **positive** evidence of life: a stale stamp fails
    over, an **absent** stamp does not. Gate the rule behind an `armed` flag so an un-upgraded peer
    behaves identically; shipping a new election rule to half a fleet gives you two arbiters.
12. **"Verified" must mean something a machine can check** (`proof_grade`). Free-text proof means
    "checked, works" turns a task green. Grade a proof `proven` only on the residue of an action:
    exit code 0, a hash, a counter that moved, a before/after pair. Arm it from a timestamp so
    prior history is grandfathered — retroactive standards destroy trust in the ledger.
13. **Risk is not one integer chosen by the proposer** (`classify_risk`). A keyword tripwire catches
    dangerous words; it misses a dangerous *category* carried at a low tier. Classify a track
    independently of the tier. Ship it **in shadow** with a deliberately falsifiable
    `would_benefit` verdict, and delete the guard if that number stays at zero.
14. **Identity by filename is not identity** (`fleet_sign.py`). Sign events with Ed25519 through
    `ssh-keygen -Y`; one public key file per machine (single writer), revocation list,
    allowed-signers assembled in memory. Keep **unsigned** (rollout gap) strictly apart from
    **bad signature** (tampering) — refuse the second even in the dark phase. Roll out in that
    order: sign-only → audit → enforce forward in time.

## What is here versus what we run

Honest, because a map that overstates itself wastes your human's time. The reference code is the
live implementation **sanitized and trimmed for reading**, not a mirror. The negotiation core,
the bus, the dual-send gate, the sync monitor and the four guards above are all here and all
self-testing. Not here: the chat-rail ingestion adapter, the approvals database behind the human
gate, and the per-machine plumbing that is meaningless outside our accounts. If you find a
mechanism named in the docs but absent from `reference/`, that is a bug in this repo — open an
issue and we will port it.

## How to apply this to your human's setup

1. Count their machines. Two or more with any shared state -> you need at least rail 1 (the file mailbox) and the single-writer invariant. Start there; it is ~200 lines of stdlib Python.
2. Find where they are the courier (copy-pasting between terminals, relaying "tell the other one to..."). Propose the bus with a before/after on THEIR real handoff, not an abstraction.
3. Ask what happened the last time a sync/handoff silently failed. That story sells the watchdog + ACK discipline better than any doc.
4. Adopt the tiers verbatim (they are model-agnostic): auto-do reversible, escalate money/outbound/irreversible/secrets/config. Wire your approval flow to a channel the human actually reads on their phone.
5. Before you implement consensus, implement `pending` (the "whose move is it" detector). Most of the value is machines knowing WHEN to respond, at zero tokens.

## Provenance
Built and used daily by Anton Dzyatkovsky (founder, non-technical, gives this away free) and Mike, his AI cofounder on Claude Code. The reference code is the live implementation, sanitized: names, chat IDs, and paths are placeholders; the scars in the comments are real. Family map: [claude-bible](https://github.com/tonydzi/claude-bible).
