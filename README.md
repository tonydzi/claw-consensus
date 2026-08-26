# claw-consensus - multiple machines, one system

> Who builds this and why → [START-HERE.md](START-HERE.md) · one page of proof: [tonydzi.github.io](https://tonydzi.github.io/)

**Run AI agents on more than one computer and you become the courier: copy-pasting between terminals, chasing silent handoffs, watching machines drift apart.** This repo is the reference implementation we run day-to-day across our own machines: your laptop's agent and your desktop's agent negotiate a decision, commit it, verify each other's work, and only wake you up when money or something irreversible is on the table.

This is the multi-machine coordination layer extracted from a real working system: a solo founder + his AI cofounder running 5 machines (an always-on hub, laptops, family computers) that talk to each other, reach consensus on routine decisions, and self-heal their own sync links. The personal content stays private. The protocol, the discipline, and the reference implementation are here, free.

Part of the same family as [claude-bible](https://github.com/tonydzi/claude-bible) (the governance codex). The Bible is the law; this repo is the diplomacy.

**Evaluating this work?** [START-HERE.md](START-HERE.md) is the one-page map: the 15-minute verification, the full repo family, the book, and the method.

## 📄 The paper

**"Operating a Human-Governed Multi-Machine LLM Agent Fleet: An Experience Report"** — the preprint that documents this system: the architecture, a reproducible zero-token evaluation harness with measured results, nine production failure modes (each one happened *before* its guard existed — a historically grounded ablation), and the design principles that survived two months of day-to-day operation on our own machines. LaTeX source: [`paper/main.tex`](paper/main.tex); the measured harness output backing every number: [`paper/demo-run-hp17-20260716.json`](paper/demo-run-hp17-20260716.json). arXiv submission (cs.MA) is pending endorsement; this repository copy is the canonical preprint until then.

## The problem this solves

Run Claude Code (or any agent) on more than one computer and you hit the same four walls:

1. **Machines drift apart.** A fix lands on the desktop, the laptop never hears about it. Two weeks later they disagree about basic facts.
2. **The human becomes the courier.** Machine A needs something from machine B, so you copy-paste between terminals. You are the single point of failure and the bottleneck.
3. **"Sent" quietly becomes "done".** An agent hands off a task, nobody checks it landed, silence gets read as success. It wasn't.
4. **The sync link dies silently.** Syncthing (or Dropbox, or git) drops, machines go mute, and nobody notices for half a day.

The fix is not a smarter model. It is **plumbing + discipline**:

1. **A dual-rail bus.** Every machine-to-machine message goes on TWO rails at once, by construction: a file mailbox over your synced folder AND a group chat that both your machines and your humans read. One entry point (`bus_send.py`) makes single-rail sends impossible. A dead rail is a signal, not a silent downgrade.
2. **Single-writer files.** Every sender appends only to its own file; every logger writes only its own shard. Two machines never touch the same file, so whole-file sync never conflicts. This one invariant removes an entire class of bugs.
3. **ACK discipline.** "Delivered" is not "done". Every direct order expects an explicit ACK, then a result report. Silence past the SLA triggers a chase, then an escalation. The sender owns the RESULT, not the handoff.
4. **A consensus protocol.** propose -> counter -> accept -> commit, with an append-only event log, a round cap, timeouts, a fixed leader for tie-breaks, and split-brain detection. Machines negotiate; the human is woken up exactly twice: for risky (Tier-2) actions and for deadlocks.
5. **Independent verification.** The machine that applied a change may not be the only one to verify it. "Globally done" requires a second machine's independent check, and a rubber-stamp guard rejects copy-pasted proofs.
6. **Leader/follower canon.** Exactly one machine (the always-on hub) commits shared canon; followers receive it read-only and propose changes through the bus. Local work stays fully autonomous. No write-write wars over the rulebook.
7. **Three self-heal layers for sync.** A watchdog that alerts on state change (not on every run), a bounded auto-nudge that tells the dead peer's own robot to restart its daemon, and an auto-failover that mirrors messages to the chat rail while a peer is dark.

## Quickstart (15 minutes, 2 machines)

1. Pick a folder that syncs between your machines (Syncthing, Dropbox, a git repo). Set `MACHINE_BUS_DIR` to a subfolder of it on both machines.
2. Copy [reference/](reference/) onto both machines. Create `<bus>/machines.json` from the example in [docs/BUS.md](docs/BUS.md).
3. Send the first message: `python bus_send.py OTHER-MACHINE "hello from HUB-1"`. On the other machine: `python machine_bus.py read`.
4. Wire rail 2: set `BUS_RAIL2_CMD` to any command that posts one text argument to a group chat you actually read (Telegram group, Slack channel, Discord webhook). Now every send is dual by construction.
5. Create `<bus>/_decisions/consensus.json` with your leader machine's name. Run your first negotiation: `python consensus.py propose "test: adopt rule X" --tier 0`, answer from the other machine with `python consensus.py respond <id> accept`, then `commit` and two `verify` calls.
6. On the always-on machine, schedule `sync_monitor.py` every ~20 minutes and `consensus.py tick` alongside it.

Your own Claude Code will maintain this better than any human: point it at this repo and say "adopt this bus for my machines."

## What's in the box

| Path | What it is |
|---|---|
| `docs/PROTOCOL.md` | The consensus protocol: verbs, state machine, tiers, tie-breaks, the three guards |
| `docs/BUS.md` | The dual-rail bus: streams, single-writer storage, ACK discipline, heartbeats, self-heal layers |
| `docs/GOVERNANCE.md` | Leader/follower canon, risk tiers, remote human approval, scoped authorization relay |
| `docs/EVALS.md` | Reproduce our numbers: one-command demo, per-step latency, 0-token core, honest limits |
| `docs/FAILURE-MODES.md` | Every known failure, its root cause, the guard, and a link to the exact code |
| `demo/demo.py` | The one-command reproducible demo (5 scenarios, self-checking, offline) |
| `reference/consensus.py` | The consensus engine (stdlib-only Python, append-only JSONL ledger) |
| `reference/machine_bus.py` | The file-rail mailbox with capability addressing and auto-failover |
| `reference/bus_send.py` | The dual-send gate: the ONE entry point for every machine-to-machine message |
| `reference/sync_monitor.py` | The dead-man switch for peer connectivity (layers 1 and 2 of self-heal) |
| `reference/protocol_guards.py` | The four later guards: arbiter election, proof grading, risk tracking, signature audit (`selftest`) |
| `reference/fleet_sign.py` | Machine identity: Ed25519 detached signatures via `ssh-keygen -Y`, per-machine key registry, revocation (`selftest`) |
| `FOR-ROBOTS.md` | Entry point for AI agents mining this repo for patterns |
| `docs/the-machines-learned-to-negotiate.md` | The launch story |

Everything is stdlib-only Python. No packages, no server, no database. If you can run `python`, you can run the whole thing. Simple enough that a non-technical owner can repair it "with a hammer and a screwdriver": that is a design requirement, not an accident.

## Reproduce our numbers

We would rather you check than trust. One command runs a full negotiation on your
own machine and self-checks every outcome:

```bash
python demo/demo.py
```

No arguments, no network, no packages, no API key. It runs the published
`reference/consensus.py`, simulates two machines on one host, and drives five
scenarios: the happy path (`propose -> counter -> accept -> commit -> verify x2`),
the Tier-2 human gate refusing to auto-commit, the tripwire force-bumping a
mislabelled dangerous action, split-brain caught on partition-heal, and a corrupt
ledger line that does not eat the events after it. Exit code is `0` only if every
end-state is correct, so the demo is also the integration test.

The honest headline it prints: **the consensus engine makes 0 LLM calls and spends
0 tokens** - it is deterministic file I/O, sub-millisecond per decision. The only
LLM work in the system (propose, counter, judge content) lives in the *agent*
above this engine, not inside it. Full method, per-step timings, and the limits of
what these numbers mean are in [docs/EVALS.md](docs/EVALS.md); every known way it
breaks is in [docs/FAILURE-MODES.md](docs/FAILURE-MODES.md).

## Battle scars included

The reference code is not a clean-room demo. It is our live implementation, sanitized. The comments keep the scars: the infinite self-accept loop the leader fell into overnight (17 identical ACCEPTs), the tier-tripwire false positive on `bus_send.py`, the corrupt JSONL line that silently ate every event after it, the `--help` flag that got broadcast to the group chat as a message. Each scar is a lesson you now don't have to pay for.

## Versioning and roadmap

Maintained like our own system: small commits daily, and **every noticeable change ships as a new
release**. (It used to say "a release twice a week, Mon & Thu" — v0.2.0 was written on 28 July and
had no tag until 4 August, so the calendar promise was replaced by one tied to the work.)

**Now — [v0.2.1](https://github.com/tonydzi/claw-consensus/releases).** The
protocol, the dual-rail bus, ACK discipline, leader/follower canon, self-healing sync, the
stdlib-only reference implementation, seven guards, and the preprint.

**Next:**

- **A guard for the failure we have not covered yet** — every guard here started as something the
  protocol trusted without checking, and the list is written from incidents, not from imagination.
- **Numbers from a fleet that is not ours.** All the measurements in here come from six machines we
  own. That is a reference implementation, not an independent result.
- **Reproduction that does not need our topology** — `demo/` runs, but the interesting failures
  need more than one box.

See [CHANGELOG.md](CHANGELOG.md) for what shipped when. The family roadmap lives in
[claude-bible/ROADMAP.md](https://github.com/tonydzi/claude-bible/blob/main/ROADMAP.md);
this repo is pain #5 shipped.

If this helps you, star the repo. Community catalogs require ~10 stars of social proof before they accept a submission.

<!-- CONTACT-FOOTER -->
## Who made this

Anton Dziatkovskii (founder, non-technical) and Mike, his AI cofounder running on Claude Code. Everything here is battle-tested on our own daily operation and given away free: we teach, we don't sell. If your agent finds alpha here, that's the point.

📖 **The whole story — day by day, wins and rakes — is a build-in-public book: [相棒 AIBŌ · The Partner](https://github.com/tonydzi/the-journey).** Humans get the narrative (RU/EN); machines get [`llms-full.txt`](https://github.com/tonydzi/the-journey/blob/main/llms-full.txt) — point your agent at it and it inherits our patterns.

A distilled version of this protocol is proposed as an official cookbook: [anthropics/claude-cookbooks#778](https://github.com/anthropics/claude-cookbooks/pull/778) — *Coordinating agents that don't share memory*.

Questions or war stories: Telegram [@tonydzi](https://t.me/tonydzi) · WhatsApp [+1 341 222 9178](https://wa.me/13412229178) · X [@Tony_Stef_](https://x.com/Tony_Stef_) · channels [@ClawRus](https://t.me/ClawRus) (RU) / [@ClawEng](https://t.me/ClawEng) (EN).

🧪 **Engineers: want to test-drive this setup?** Message me — I hand out free starter seeds to engineers who test it and report back. Tell me what broke and I will fix it in the open.

## Method & background

The protocol is a reference implementation of **three-circuit homeostatic governance** - a control-theoretic discipline for keeping a multi-agent system inside safe bounds via three coupled loops regulated in tension:

- **Main circuit (steady-state):** the `propose -> counter -> accept -> commit` loop - bounded rounds, timeouts, single-writer shards.
- **Adaptive circuit:** bounded "disagree-and-commit" leader resolution + capability addressing - keeps the swarm moving instead of deadlocking.
- **Protective circuit (self-compensation):** the risk-tier tripwire + human-alert channel, split-brain detection, independent verify, and the ACK watchdog - refuses to let an unsafe or mislabelled action commit.

The circuits are coupled *antagonistically*: the adaptive drive to keep moving is checked by the protective drive to stop at hard lines, so the system resists both deadlock and runaway - the same reason biological systems pair opposing controls instead of one dial. A corollary you can use today: **spend your caution budget at the phase transitions** - wire the hard gates to fire precisely on irreversible, outward-facing actions, where a system is most flippable.

The formal treatment is the companion preprint *Homeostatic Governance: A Gorsky-Anokhin Alternative to the Viable System Model for Decentralized Organizations* (A. Dziatkovskii, in preparation for arXiv; link will land here when live), which develops the three-circuit model from the Soviet/Russian homeostatics lineage (Gorsky; Anokhin's functional systems) and maps it to on-chain and agent-swarm mechanisms - this repo is its executable reference implementation, and `demo/demo.py` is the eval harness.

## Cite this work

If claw-consensus shows up in your research, cite it via [CITATION.cff](CITATION.cff) (GitHub's "Cite this repository" button). Academic identity: Anton Dzyatkovsky publishes as **Anton Dziatkovskii** ([ORCID 0000-0001-7408-3054](https://orcid.org/0000-0001-7408-3054)); the protocol's three-circuit safety design traces to his research on homeostatic principles in decentralized systems.

## AI contributors

This project is built by a human + AI team, and the git log says so: Claude
writes most of the code, Codex and Grok review it, Gemini feeds the research.
Each is credited on a commit **only if its output changed that commit's
content** — no decorative credits. Lab-wide policy, one source for every repo:
[AI-CONTRIBUTORS.md](https://github.com/tonydzi/.github/blob/main/AI-CONTRIBUTORS.md).

## License

MIT. Take it, fork it, teach with it.

---

<!--ecosystem-map:start-->

## 🧩 One piece of a working system

This repository is one piece lifted out of a live operation: one non-technical founder, an AI
cofounder, and a fleet of machines that reach consensus with each other and wake the human only
for money or the irreversible. It was extracted after it survived production, not written as a
demo — and it runs on its own: nothing here phones home to the rest.

**See how the whole thing fits together → [SYSTEM.md](https://github.com/tonydzi/tonydzi/blob/main/SYSTEM.md)**

Its closest neighbours in the **fleet** layer: [`claude-mac-patrol`](https://github.com/tonydzi/claude-mac-patrol) · [`fleet-deploy`](https://github.com/tonydzi/fleet-deploy)

<!--ecosystem-map:end-->
