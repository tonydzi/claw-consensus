# Evals: reproduce our numbers

Most "autonomous multi-agent" claims come with a demo video and no way to check
them. This page is the opposite: one command reproduces a full negotiation on
your own machine, prints per-step timings, and self-checks every end-state. If a
number here is wrong, you will see it fail on your terminal.

The honest headline: **the consensus engine does zero LLM calls and spends zero
tokens.** It is deterministic file I/O. The only thing an LLM does in this system
is what only an LLM can (propose an idea, counter it, judge content) and that
happens in the *agent*, above this engine, not inside it. Everything measured
below is plain Python.

## Run it

```bash
python demo/demo.py                 # all scenarios, trace tables, self-check
python demo/demo.py --json out.json # also dump machine-readable timings
python demo/demo.py --keep          # keep the temp ledgers to inspect by hand
```

No arguments, no network, no packages, no API key. The demo runs the **published**
[`reference/consensus.py`](../reference/consensus.py), simulates two machines
(`HUB-1` the leader and `LAPTOP-2` a peer) by flipping the `MACHINE_KEY` env
between calls, and sets `CONSENSUS_NO_BUS=1` so there is no chat rail and no
network in the loop. Exit code is `0` only if all six scenarios reach their
expected end-state, so `demo/demo.py` is also the engine's integration test.

A captured run is committed at [`demo/example-run.txt`](../demo/example-run.txt) so
you can see the expected shape without running anything.

## What the demo exercises

| # | Scenario | What it proves | Expected end-state |
|---|---|---|---|
| A | Happy path | `propose -> counter -> accept -> commit -> verify x2` converges | `committed`, 2 distinct verifiers incl. 1 independent -> **done** |
| B | Human gate | a Tier-2 action never auto-commits | auto-`escalated`; `commit` **refused** (exit 1); a free-text `approve` **refused** (exit 2); a signed approval envelope unlocks `commit` |
| C | Tier-tripwire | a dangerous verb mislabelled Tier-0 is force-bumped | stored `risk_tier=2`, escalated, tripwire fired |
| D | Split-brain | a partition where both sides commit is caught on heal | 2 committers -> `conflict` -> escalated to reconcile |
| E | Corrupt line | a garbage shard line does not eat the events after it | the `COMMIT` written after the bad line is still read |
| F | Forged line | with signatures enforced, a hand-written line is not state | the forged ACCEPT is quarantined (status stays `proposed`); the real signed ACCEPT reaches `agreed` |

B and F need `ssh-keygen` (OpenSSH); without it they print SKIP with the reason. Scenario F (and
B's signing) were added in v0.5.0, after the numbers below were measured; the timings in this file
come from the earlier run described below and were not edited.

## The numbers (this machine)

Reference environment: Python 3.12.10, Windows, warm disk cache. **Your numbers
will differ** and that is fine: the claim is not a specific millisecond count, it
is the *shape*. Regenerate the table any time with the command above.

Per-step wall time, happy path (scenario A):

| Step | Actor | Verb | Wall time |
|---|---|---|---|
| 1 | LAPTOP-2 | propose | ~79 ms |
| 2 | HUB-1 | respond counter | ~75 ms |
| 3 | LAPTOP-2 | respond accept | ~66 ms |
| 4 | HUB-1 | commit | ~69 ms |
| 5 | HUB-1 | verify (self) | ~74 ms |
| 6 | LAPTOP-2 | verify (independent) | ~71 ms |
| | | **full cycle** | **~433 ms / 6 calls** |

### Where that time actually goes

Each step is a **fresh Python process**. That process spin-up, not consensus
logic, is essentially the whole wall time:

| Measurement | Median |
|---|---|
| bare `python -c pass` (interpreter startup) | ~34 ms |
| `python -c "import consensus"` (startup + parse the engine) | ~72 ms |
| a full consensus CLI verb (startup + import + do the work) | ~70 ms |

The verb's own work is **below the noise floor of process startup.** To measure it
directly, the demo derives full state in-process over a realistic ledger with no
subprocess cost:

```
50 proposals / 250 events, merge + derive-all: ~920 microseconds per pass
```

That is **~3.7 microseconds per event** to merge every shard and recompute every
proposal's state from scratch. A single 6-event proposal resolves in roughly
**20 microseconds**. There is no incremental cache to invalidate and no index to
corrupt: the engine re-reads the append-only log and re-derives state every time,
and it is still three orders of magnitude faster than the interpreter it runs on.

**Takeaway:** the consensus layer is free. In production these verbs run as
scheduled `tick`s every ~10-20 minutes, so even the 70 ms process startup is
irrelevant. Nothing here needs optimizing; that is the point of keeping it
stdlib-only and deterministic.

## Cost

| | LLM calls | Tokens |
|---|---|---|
| The whole demo (all 5 scenarios + core bench) | **0** | **0** |
| One `propose` / `respond` / `commit` / `verify` / `tick` | **0** | **0** |

The engine is a state machine over an append-only log. Tokens are spent by the
*agent* that decides **what** to propose or **how** to judge a counter, which is a
separate layer this repo does not benchmark (it depends on your model and prompt).
Splitting the two is deliberate: the safety-critical parts (tie-break, the Tier-2
gate, the tripwire, split-brain detection) are all deterministic code you can
audit line by line, not model output you have to trust.

## What these numbers are NOT

Be as skeptical of this page as you would of anyone else's.

- **This is a single-host simulation, not a network benchmark.** Both "machines"
  are the same computer, so there is zero transport latency here. Real
  cross-machine time is dominated by the sync/chat rail (a synced folder or a
  group chat), which is **seconds to minutes**, not microseconds. That rail, its
  failure modes, and its self-heal layers are described in
  [BUS.md](BUS.md), not measured here.
- **It measures the engine, not the fleet.** End-to-end "laptop proposes, hub
  responds while the laptop sleeps, both verify" is governed by the per-peer SLA
  timeouts in [`consensus.json`](PROTOCOL.md) (30 minutes by default), not by the
  microsecond compute cost.
- **The wall-time is dominated by process startup**, which is an artifact of
  running each verb as a separate CLI call. A long-lived agent that imports the
  module once pays it once.

Known ways this system fails, and the guards for each, are in
[FAILURE-MODES.md](FAILURE-MODES.md) - including three that this demo reproduces
live (scenarios C, D, E).
