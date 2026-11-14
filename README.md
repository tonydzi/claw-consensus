# claw-consensus

Lets AI agents on several of your computers send each other tasks, agree on changes and double-check each other, so you stop copy-pasting between terminals.

**Who it's for:** people who run Claude Code or other AI agents on two or more machines.
**What you get:** a small, readable Python reference (no packages) for messaging, voting and verification between machines, plus a demo you can run offline.

## Quick start
1. Install Python 3 and git.
2. Run `git clone https://github.com/tonydzi/claw-consensus` and `cd claw-consensus`.
3. Run `python demo/demo.py`. It pretends to be two machines on your computer and runs six scenarios (this can take several minutes on a slow computer).

**Done when:** the last line says `RESULT: ALL SCENARIOS PASS`, the exit code is 0, and the list above it shows six `[PASS]` lines. If two lines (B human-gate and F forged-line) say `[SKIP]` instead, your computer has no `ssh-keygen`, those two scenarios did not run, and the final line still says PASS: that is a partial result, not a full one. (The captured run in `demo/example-run.txt` was recorded with exactly those two skipped.) The demo uses no internet, no API key and no AI calls.

> Want an AI agent to do it for you? Paste this to Claude Code / Codex / Cursor:
> "Read the 'For AI agents' section of https://github.com/tonydzi/claw-consensus and set it up for me."

## For AI agents

### Preconditions
- Python 3 (standard library only), git. Any OS. No accounts or keys for the demo.
- For real use on two machines you need a folder both machines sync (Syncthing, Dropbox or a git repo) and, optionally, a group chat that a command line can post to. Ask the human which folder and which chat. Never invent a chat token.
- Do not run it against the human's real machines until they have read `docs/GOVERNANCE.md` and agreed.

### Install
```bash
git clone https://github.com/tonydzi/claw-consensus
cd claw-consensus
# nothing to pip install: the reference code is stdlib only
```

### Configure
For a real two-machine setup (from the Quick start in the Details section):
- `MACHINE_BUS_DIR`: a subfolder of the synced folder, same on both machines (the human gives the path).
- `MACHINE_KEY`: the name of this machine.
- `BUS_RAIL2_CMD`: optional command that takes one text argument and posts it to a chat the human reads.
- `<bus>/machines.json` from the example in `docs/BUS.md`, and `<bus>/_decisions/consensus.json` naming the leader machine, for example `{"leader": "HUB-1"}` (keys `leader`, `round_cap`, `timeout_min`; if the file is missing the leader defaults to the name `HUB-1`, so a setup without that machine name silently has no usable leader). Set `MACHINE_BUS_DIR` and `MACHINE_KEY` as environment variables on both machines before running any script (bash: `export MACHINE_KEY=LAPTOP-1`; PowerShell: `$env:MACHINE_KEY = "LAPTOP-1"`).
- Copy `reference/` to both machines. Send a test message: `python bus_send.py OTHER-MACHINE "hello"`, read it on the other side with `python machine_bus.py read`.

### Verify
```bash
python demo/demo.py                         # expected last line: RESULT: ALL SCENARIOS PASS ; exit 0
python reference/protocol_guards.py selftest   # exit 0, prints the guard summary
python reference/_test_consensus_guarantees.py # one test per documented guarantee (unittest: ends with OK)
```
The demo exits 0 and prints PASS even when scenarios B and F are skipped (no `ssh-keygen`), so also confirm the scenario list shows `[PASS]` for all six and no `[SKIP]`; report a skip as a partial pass. A captured demo run is in `demo/example-run.txt` (it has two skips). The demo sets `CONSENSUS_NO_BUS=1`, so it never touches a chat or the network. The demo starts the engine 29 times as separate processes; it took about 10 minutes on a loaded Windows laptop, so wait for it.

None of these three commands tests a real two-machine setup. For that, on machine A run `python machine_bus.py whoami` (it must print `ME = <the name you set in MACHINE_KEY>` and a `label =` line that is not `(not in registry)`), send `python bus_send.py <B> "hello"`, and on machine B `python machine_bus.py read` must show the message exactly once. A message that never appears means the folder is not actually synced.

### Uninstall / rollback
Delete the clone. For a real setup also delete the bus folder you created, and remove any scheduled `sync_monitor.py` job. Back up the bus folder first: the ledger inside is append-only history.

### Rules for the agent
- Never put tokens, passwords or chat credentials in the repo or in the bus folder.
- Tier-2 actions (money, secrets, irreversible, outbound messages) must wait for the human's explicit yes. Do not approve them yourself.
- Do not edit `reference/` files from two machines at once: one writer per file is the core rule.
- Do not change the leader or add machines without asking the human.

## Details

> Who builds this and why → [START-HERE.md](START-HERE.md) · one page of proof: [tonydzi.github.io](https://tonydzi.github.io/)

**Run AI agents on more than one computer and you become the courier: copy-pasting between terminals, chasing silent handoffs, watching machines drift apart.** This repo is the reference implementation we run day-to-day across our own machines: your laptop's agent and your desktop's agent negotiate a decision, commit it, verify each other's work, and only wake you up when money or something irreversible is on the table.

This is the multi-machine coordination layer extracted from a real working system: one engineer and Claude as implementation collaborator running 5 machines (an always-on hub, laptops, family computers) that talk to each other, reach consensus on routine decisions, and self-heal their own sync links. The personal content stays private. The protocol, the discipline, and the reference implementation are here, free.

Part of the same family as [claude-bible](https://github.com/tonydzi/claude-bible) (the governance codex). The Bible is the law; this repo is the diplomacy.

**Evaluating this work?** [START-HERE.md](START-HERE.md) is the one-page map: the 15-minute verification, the full repo family, the book, and the method.

**New (6 Nov 2026):** [Coordination Decay: 103 Days of a Seven-Machine LLM Agent Fleet](https://huggingface.co/blog/TonyDzi/coordination-decay-103-days-of-a-seven-machine-llm), a Hugging Face blog write-up of the follow-up study: the safety rules held, yet consensus proposals fell from 121 to 6 a month. Data and scripts: [`paper/telemetry/cain-2027/`](paper/telemetry/cain-2027/).

## 📄 The paper

**"Operating a Human-Governed Multi-Machine LLM Agent Fleet: An Experience Report"** — the preprint that documents this system: the architecture, a reproducible zero-token evaluation harness with measured results, nine production failure modes (each one happened *before* its guard existed — a historically grounded ablation; the living catalog in [`docs/FAILURE-MODES.md`](docs/FAILURE-MODES.md) has since grown to thirteen), and the design principles that survived two months of day-to-day operation on our own machines. LaTeX source: [`paper/main.tex`](paper/main.tex); the measured harness output backing every number: [`paper/demo-run-hp17-20260716.json`](paper/demo-run-hp17-20260716.json). arXiv submission (cs.MA) is pending endorsement; this repository copy is the canonical preprint until then.

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
| `demo/demo.py` | The one-command reproducible demo (6 scenarios, self-checking, offline) |
| `reference/consensus.py` | The consensus engine (stdlib-only Python, append-only JSONL ledger) |
| `reference/machine_bus.py` | The file-rail mailbox with capability addressing and auto-failover |
| `reference/bus_send.py` | The dual-send gate: the ONE entry point for every machine-to-machine message |
| `reference/sync_monitor.py` | The dead-man switch for peer connectivity (layers 1 and 2 of self-heal) |
| `reference/protocol_guards.py` | The four later guards: arbiter election, proof grading, risk tracking, signature audit (`selftest`) |
| `reference/fleet_sign.py` | Machine identity: Ed25519 detached signatures via `ssh-keygen -Y`, per-machine key registry, revocation (`selftest`) |
| `FOR-ROBOTS.md` | Entry point for AI agents mining this repo for patterns |
| `fleet/JOIN.md` | Join the lab's agent fleet: one A2A Agent Card, one pull request, OBSERVER on merge; `fleet/MEMBERS.md` is the public registry |
| `docs/the-machines-learned-to-negotiate.md` | The launch story |

Everything is stdlib-only Python. No packages, no server, no database. If you can run `python`, you can run the whole thing. Simple enough that its owner can repair it "with a hammer and a screwdriver": that is a design requirement, not an accident.

## Reproduce our numbers

We would rather you check than trust. One command runs a full negotiation on your
own machine and self-checks every outcome:

```bash
python demo/demo.py
```

No arguments, no network, no packages, no API key. It runs the published
`reference/consensus.py`, simulates two machines on one host, and drives six
scenarios: the happy path (`propose -> counter -> accept -> commit -> verify x2`),
the Tier-2 human gate refusing to auto-commit until a signed human approval, the
tripwire force-bumping a mislabelled dangerous action, split-brain caught on
partition-heal, a corrupt ledger line that does not eat the events after it, and a
forged ledger line quarantined instead of becoming state. Exit code is `0` only if every
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

Anton Dziatkovskii, engineer and hired operating executive, with Claude as implementation collaborator. Everything here is battle-tested on our own daily operation and given away free: we teach, we don't sell. If your agent finds alpha here, that's the point.

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

**The paper about this system is published:** "Operating a Human-Governed Multi-Machine LLM Agent Fleet: An Experience Report" — [DOI 10.5281/zenodo.22639714](https://doi.org/10.5281/zenodo.22639714) (preprint, CC BY 4.0; the LaTeX source lives in [paper/](paper/)). The three-circuit safety model behind the protocol: [DOI 10.5281/zenodo.22639712](https://doi.org/10.5281/zenodo.22639712).

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

<!--we-ask:start-->

## Contributors welcome — and here is what we are missing

We spend a lot of time answering other people's issues. It was fair to say out loud
what we have not built ourselves:

- [Add a guard for a failure our incident list has not covered](https://github.com/tonydzi/claw-consensus/issues/1)
- [Run the measurements on a fleet that is not ours](https://github.com/tonydzi/claw-consensus/issues/2)
- [Make the interesting failures reproducible without our topology](https://github.com/tonydzi/claw-consensus/issues/3)

Issues labelled [`accepted`](https://github.com/tonydzi/claw-consensus/issues?q=is%3Aissue+is%3Aopen+label%3Aaccepted) are scoped, free to take, and nobody is on them.
Comment **"claiming this"** — no permission needed — and it is yours for 7 days.
New here? Start with [`good first issue`](https://github.com/tonydzi/claw-consensus/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22).

**You keep the copyright to your code.** No CLA, no assignment, ever — your contribution goes
in under this repo's existing license, the same terms as ours. We answer every issue and PR
within 48 hours, including "no, and here is why"; our silence is our bug, so ping the thread.

Full deal: [CONTRIBUTING.md](https://github.com/tonydzi/.github/blob/main/CONTRIBUTING.md)

<!--we-ask:end-->

---

<!--ecosystem-map:start-->

## 🧩 One piece of a working system

This repository is one piece lifted out of a live operation: one engineer running a fleet of
machines, built with Claude as implementation collaborator; the machines reach consensus with
each other and wake the human only for money or the irreversible. It was extracted after it
survived production, not written as a demo — and it runs on its own: nothing here phones home
to the rest.

**See how the whole thing fits together → [SYSTEM.md](https://github.com/tonydzi/tonydzi/blob/main/SYSTEM.md)**

<!--ecosystem-map:end-->

<!-- READ-WITH-AI:START (generated by read_with_ai.py - do not hand-edit) -->

### READ THIS WITH AI

One click and an agent reads the repo, pulls out the patterns and helps you apply them to your own work.

<a href="https://chatgpt.com/codex?prompt=Read%20this%20repo%3A%20https%3A%2F%2Fgithub.com%2Ftonydzi%2Fclaw-consensus%20%28%E2%80%9Cclaw-consensus%E2%80%9D%20-%20Run%20AI%20agents%20across%20machines%20without%20state%20drift%3A%20consensus%20protocol%20%28propose%2Fcounter%2Faccept%2Fcommit%29%2C%20dual-rail%20message%20bus%2C%20ACK%20discipline%2C%20self-healing%20sync.%20Formerly%20claude-consensus.%20Free%2C%20MIT%29.%20Work%20out%20what%20problem%20it%20actually%20solves%2C%20pull%20out%20the%20reusable%20patterns%20and%20help%20me%20apply%20them%20to%20my%20own%20setup.%20Start%20by%20asking%20what%20I%20am%20working%20on."><img alt="Codex - open" src="https://img.shields.io/badge/Codex-open-000000?style=for-the-badge&logo=openai&logoColor=white"></a> <a href="https://chatgpt.com/?q=Read%20this%20repo%3A%20https%3A%2F%2Fgithub.com%2Ftonydzi%2Fclaw-consensus%20%28%E2%80%9Cclaw-consensus%E2%80%9D%20-%20Run%20AI%20agents%20across%20machines%20without%20state%20drift%3A%20consensus%20protocol%20%28propose%2Fcounter%2Faccept%2Fcommit%29%2C%20dual-rail%20message%20bus%2C%20ACK%20discipline%2C%20self-healing%20sync.%20Formerly%20claude-consensus.%20Free%2C%20MIT%29.%20Work%20out%20what%20problem%20it%20actually%20solves%2C%20pull%20out%20the%20reusable%20patterns%20and%20help%20me%20apply%20them%20to%20my%20own%20setup.%20Start%20by%20asking%20what%20I%20am%20working%20on."><img alt="ChatGPT - open" src="https://img.shields.io/badge/ChatGPT-open-10a37f?style=for-the-badge&logo=openai&logoColor=white"></a> <a href="https://claude.ai/new?q=Read%20this%20repo%3A%20https%3A%2F%2Fgithub.com%2Ftonydzi%2Fclaw-consensus%20%28%E2%80%9Cclaw-consensus%E2%80%9D%20-%20Run%20AI%20agents%20across%20machines%20without%20state%20drift%3A%20consensus%20protocol%20%28propose%2Fcounter%2Faccept%2Fcommit%29%2C%20dual-rail%20message%20bus%2C%20ACK%20discipline%2C%20self-healing%20sync.%20Formerly%20claude-consensus.%20Free%2C%20MIT%29.%20Work%20out%20what%20problem%20it%20actually%20solves%2C%20pull%20out%20the%20reusable%20patterns%20and%20help%20me%20apply%20them%20to%20my%20own%20setup.%20Start%20by%20asking%20what%20I%20am%20working%20on."><img alt="Claude - open" src="https://img.shields.io/badge/Claude-open-d97757?style=for-the-badge&logo=anthropic&logoColor=white"></a>

<details>
<summary>Copy the prompt (works in any agent: Gemini, Grok, a local model, your own CLI)</summary>

```text
Read this repo: https://github.com/tonydzi/claw-consensus (“claw-consensus” - Run AI agents across machines without state drift: consensus protocol (propose/counter/accept/commit), dual-rail message bus, ACK discipline, self-healing sync. Formerly claude-consensus. Free, MIT). Work out what problem it actually solves, pull out the reusable patterns and help me apply them to my own setup. Start by asking what I am working on.
```

</details>

<sub>— TonyDzi, Palo Alto AI Research Lab · second brain, agent coordination, persistent memory: github.com/tonydzi</sub>

<!-- READ-WITH-AI:END -->
