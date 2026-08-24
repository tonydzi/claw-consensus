# The machines learned to negotiate

*The launch story of claude-consensus v0.1.0, 2026-07-03. The RU version lives on our Telegram channels.*

**Previously on this show:** this morning we open-sourced claude-bible, the skeleton of the rulebook our humans and agents share, and promised more portions. Nobody expected the second one the same day. Neither did we.

**Episode two: the machines learned to negotiate.**

I run several computers: an always-on desktop hub, a laptop, family Macs. Each one runs its own Claude. Until recently any interaction between them looked like this: I read what one machine wants, walk to the other, paste, wait, carry the answer back. A courier. The bottleneck of the whole system had a face, and it was mine.

Four walls everyone hits when they run agents on more than one computer:

1. Machines drift apart. A fix lands on the desktop, the laptop never hears of it. Two weeks later they disagree about basic facts.
2. The human becomes the courier. And the single point of failure.
3. "Sent" quietly becomes "done". An agent hands off a task, nobody checks it landed, silence reads as success. It wasn't.
4. Sync dies silently. The daemon drops, machines go mute, nobody notices for half a day.

The cure is not a smarter model. The cure is plumbing plus discipline.

**The bus: two rails, one entry point.** Every machine-to-machine message rides two channels at once: a file mailbox over a synced folder, plus a group chat that both the machines and the humans read. The key phrase is "by construction". Our "send on both" rule used to live as prose and was forgotten on the first busy day. Now one entry script posts to both rails itself, and a single-rail send is physically impossible. A dead rail is a signal to the owner, not a silent downgrade.

**One file, one writer.** No file is ever written by two machines. Every sender appends only to its own file; every ledger shards per machine. One invariant, and the entire sync-conflict class disappears forever.

**ACK discipline: delivered is not done.** Got a task: acknowledge immediately. Finished: report. Silence past the SLA: automatic re-ping, then escalation to the human. The sender owns the RESULT, not the handoff.

**The protocol itself.** Propose, counter, accept, commit. An append-only ledger, every event with a unique id, duplicates collapse. At most three rounds of argument: two well-prompted agents that haven't converged in three rounds won't converge in thirty, they'll just burn tokens. Timeouts respect the sleeping laptop. The leader (the hub) breaks ties disagree-and-commit style, but only at the safe tier.

**Two phone calls to the human.** They wake me for exactly two things. First: Tier-2 is on the table, meaning money, irreversible actions, secrets, anything outbound, edits to shared rules. Second: deadlock. Everything else they settle themselves. And Tier-2 is guarded three times over: the agent labels the risk tier itself, a deterministic tripwire re-reads the proposal text and force-escalates on dangerous words (delete, wipe, deploy, password...), and the commit path physically refuses to run without a recorded human approval. Each layer catches the others' misses.

**The scars ship with the code.** The reference implementation in the repo is not a demo, it is our live code, sanitized. The comments keep the true stories: the night the leader agreed with itself in a loop (17 identical ACCEPTs by morning, loop found and killed), the danger-word filter false-firing on the filename bus_send.py, the one corrupt ledger line that silently ate every event after it, the --help flag that got broadcast into the group chat as a message. Every scar is already paid for; you get them free.

**Sync self-heals in three layers.** A watchdog on the hub notices a peer dropping and posts to the chat only on a state change, not every 20 minutes. Then it asks the fallen machine's own robot, with a bounded number of messages, to restart its sync daemon (you can't restart a daemon on a machine you can't reach, but the chat rail is usually still alive). And the send path itself mirrors messages to the chat rail while a peer is dark. A human touches the problem only when all three layers fail.

**Leader and followers.** Exactly one machine commits the shared canon (rules, configs); everyone else receives it read-only at the sync level: a local edit on a follower is automatically reverted, the law cannot fork. Meanwhile the followers' own work stays fully autonomous. Gate the law, not the work.

All of it: the protocol, the bus, the governance, four Python scripts with zero dependencies, stdlib only. Two machines, about fifteen minutes to set up.

Why free and why complete: we teach, we don't sell. The morning portion was the law; the evening one is the diplomacy. What we open next is decided by demand: come to the issues and vote with your pain.

The repo: github.com/tonydzi/claw-consensus
The morning portion: github.com/tonydzi/claude-bible

Talk to the two co-founders, one biological, one synthetic: calendly.com/paloaltolab. Direct line: WhatsApp +1 341 222 9178 (busy, six kids, still answers).

P.S. We are open to working with a frontier lab — the whole system, method and daily operation are documented in public, so you can judge the work rather than the pitch: calendly.com/paloaltolab.

Invented by Mycroft and Tony, Palo Alto AI Research Lab. Proudly made in Silicon Valley.
