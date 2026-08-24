# AGENTS.md — working in this repo

Written for AI coding agents, and equally readable by a human contributor. Short on purpose.

## What this repo is

The reference implementation of a multi-machine coordination layer: a dual-rail message bus, ACK
discipline, and a consensus protocol (propose → counter → accept → commit → verify) with an
append-only ledger. It is a sanitized extract of a system that runs daily across several real
machines; the personal content is gone, the protocol and the discipline are what's published.

Read [`docs/PROTOCOL.md`](docs/PROTOCOL.md) before changing anything under `reference/`. The code
is small; the invariants are the hard part, and they are written down.

## Stack and layout

- **Python, stdlib only.** No packages, by design: this has to run on a family laptop with a bad
  connection. Adding a dependency is a design change, not a convenience.
- `reference/consensus.py` — the engine. Append-only JSONL ledger.
- `reference/machine_bus.py` — file-rail mailbox, capability addressing, auto-failover.
- `reference/bus_send.py` — **the single entry point** for machine-to-machine messages. Both rails
  fire from here, which is what makes a single-rail send impossible rather than discouraged.
- `reference/sync_monitor.py`, `reference/fleet_sign.py`, `reference/protocol_guards.py`.
- `demo/demo.py` — five self-checking offline scenarios.
- `docs/FAILURE-MODES.md` — every known failure, its root cause, and the guard that closed it.

## How to verify a change

```bash
python demo/demo.py                  # all five scenarios, offline, self-checking
python demo/demo.py --json out.json  # machine-readable timings
```

No network, no chat rail, no model, no packages — the demo sets `CONSENSUS_NO_BUS=1` and simulates
two machines by flipping `MACHINE_KEY`. If your change is real, it shows up here. Paste the output
in the PR.

Scenario E exists to prove a corrupt line in a shard does not eat the events after it. If you touch
ledger parsing, that scenario is the one that must stay green.

## Conventions — the invariants, in order of how much they cost to break

1. **Single-writer files.** A sender appends only to its own file; a logger writes only its own
   shard. Two machines never touch the same file. This one invariant removes an entire class of
   sync-conflict bugs — anything that introduces a second writer is a no.
2. **Dual-send by construction.** Every send goes through `bus_send.py`. A code path that posts on
   one rail directly is a bug even if it works.
3. **"Delivered" is not "done".** An order expects an explicit ACK and then a result report.
   Silence is a signal to chase, never a success.
4. **Append-only ledger.** Events are appended; nothing is rewritten in place. A garbage line
   degrades visibly and locally, never silently.
5. **Fail loud on a dead rail.** A rail that is down is a reported condition, not a silent
   downgrade to the other one.

## Boundaries — what needs a human

- **The protocol itself** — verbs, state machine, tier semantics, the tie-break rule. Open an
  issue; expect a design conversation before code.
- **Risk tiers and the human-approval gate.** Tier-2 means money, deletion, or something else
  irreversible. Loosening when the human is woken up is not a refactor.
- **Anything that would let a machine mark its own work verified.** Independent verification by a
  second machine is the point; the rubber-stamp guard exists because that was tried.
- **Numbers in `docs/EVALS.md` and `paper/`.** They come from a dated run on a named machine. If
  your change moves them, re-run and replace the file with a new dated one — never edit a number
  to match an expectation.

## The deal

Your copyright stays yours, there is no CLA, and issues labelled `accepted` are free to take —
comment "claiming this". Full terms:
[CONTRIBUTING.md](https://github.com/tonydzi/.github/blob/main/CONTRIBUTING.md).

If an AI wrote your change, say so in the PR and confirm you ran it. Welcome here — we do it daily.
Unread generated code is the one thing that gets closed on sight.
