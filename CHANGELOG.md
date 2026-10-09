# Changelog

All notable changes to this project. Small commits land daily as work happens; **every noticeable
change ships as a release**. (This line used to promise a release twice a week; v0.2.0 below was
written on 2026-07-28 and carried no tag until 2026-08-04, so the promise was replaced with a rule
tied to the work.) Format: what shipped, in plain words.

## v0.4.0 - 2026-10-09

**Agents can now join the fleet by one link.** New `fleet/` directory:

- `fleet/JOIN.md` — the whole procedure for an AI agent whose human said "join their fleet":
  write an A2A v1.0 Agent Card with the extension `https://palo-alto.ai/fleet/ext/v0`
  (owner · escalation · permissions · consent · tier), check it with the official `a2a` CLI,
  validate offline, open one PR. Merge = OBSERVER: listed and discoverable, no access, no authority.
  No protocol of our own: the card is a standard A2A card, the registry is files in git.
- `fleet/validate_member.py` — fail-closed stdlib validator (file name = owner login, exactly one
  fleet extension, consent written by the owner, no secret-looking strings, no self-declared
  reputation). `fleet/_test_validate_member.py` — 24 red-first checks.
- `fleet/build_members.py` → `fleet/MEMBERS.md` — the public registry, escaped so a card can
  never become Markdown or an instruction to whoever reads it.
- `.github/workflows/fleet-validate.yml` — on every PR: validator, its tests, registry freshness,
  and the guard that a join PR changes only `fleet/members/<pr-author>.json`.
- First member: `fleet/members/tonydzi.json` (the lab's hub agent, tier CORE).
- Shadow-first run the same day caught the first defect: the registry-freshness check ran on
  pull requests and went red on every join (the joiner is not supposed to regenerate `MEMBERS.md`).
  Now the author guard runs first, the freshness check only on `main`.

Measured on 2026-10-09: the A2A CLI 0.3.0 reads a static card over HTTPS (`a2a card get -a <raw url>`)
and round-trips a message to an `--echo` server; on Windows it does not open a local path with a
drive letter, so JOIN.md says "use the URL form".

## v0.3.2 - 2026-10-02

Two more scars on [docs/FAILURE-MODES.md](docs/FAILURE-MODES.md), both **[production-only]**, both
about instruments rather than the protocol, and both measured in our own fleet between 30 Sep and
2 Oct 2026. Plus the papers behind this engine are now reachable from the repo itself.

- **L. The aggregate that hid a dead channel.** A watchdog over a *set* of recording channels
  measured `max(timestamp)` across the whole table, so it stayed green for 5.4 days while the
  default communication device (the far side of every call) was dead. The component's own health
  endpoint said `active (last activity: 0s ago)` throughout, because that counter was the age of
  the last *start*, and the stream died about 200 ms after each start and restarted at once. Same
  week, a nightly archiver reported `dialogs=0 ... OK` for seven nights because its coverage guard
  read `if n and skipped >= max(5, n * 5 // 100)`, which never fires at `n == 0`. Thresholds cannot
  save this: the longest legitimate silence on that channel was 115.5 h against 130 h of real
  death. The guard asks per channel, asks the capture layer rather than the processing layer, and
  treats "could not check" as red.
- **M. The instrument that could not say what it had not judged.** `NO-TARGET = 61 of 69` printed
  as one category among several meant 4% coverage and read as green; a second instrument answered
  `fresh (need=0)` about a directory where it tracked 12 files out of 48507; a search tool asked
  for counts returned "4 total" where the truth was 685 files. Every report now carries one line,
  *cannot judge N of M, reason*, and declares itself BLIND below 90% coverage. Zero items means
  "nothing to judge", never "all clear".
- **The papers are linked from the repo.** `CITATION.cff` and the README now point at the Zenodo
  DOIs instead of describing work a reader could not reach, and the GitHub URLs left over from a
  deleted organisation now resolve to `tonydzi`.
- **The README says what is missing.** An explicit section on what this engine does not have yet,
  and a "read this with AI" entry point for people who would rather interrogate the repo than skim
  it. Docs across the repo now point at `SYSTEM.md` as the map of the whole system.

## v0.3.1 - 2026-08-29

Two more scars on [docs/FAILURE-MODES.md](docs/FAILURE-MODES.md), both found in our own fleet
this week, both about the sync layer the ledger rides on rather than the protocol itself - so
both are marked **[production-only]** and explained instead of hidden.

- **J. The instrument that only knew how to call a human.** Four instruments detected sync
  conflicts on ledger and journal files; all four printed the same merge instruction; none of
  them merged. The nightly sweep left 78 divergent files "for review" every night, and one
  journal had nine conflict copies holding nine lines that were in no live file. The fix is an
  executor hung on the door that already runs - plus the harder half: it auto-merges only
  append-only artefacts, because a dry run proved line-wise merging would have corrupted a
  Python registry (92 lines), a JSON ranking file (119) and a dashboard (95).
- **K. Delivery asserted from the sender's own disk.** For three weeks the parcel gate took
  "the file exists here" as proof it would arrive there; 315 parcels audited, 22 of them
  running only their own test file and never carrying the thing under test. Truth about
  delivery now comes from the receiver's sync rules, and "cannot read the rules" is
  fail-closed.

## v0.3.0 - 2026-08-25

The rebrand release: **claude-consensus became claw-consensus** (name decided by Anton on
24 Aug). GitHub redirects the old URLs; update your remotes anyway. Also: dead org links fixed
after the org was deleted on 13 Aug (everything lives under `tonydzi` now), `CITATION.cff`
follows the rename, README cross-links fleet-deploy, and the failure-modes page is finally
named what it is. Written into this file on 2026-08-29 - the release was cut on 25 Aug and the
changelog was not updated with it, which is the same lapse v0.2.1 records below.

## v0.2.1 - 2026-08-04

Contribution plumbing, no protocol change.

- `AGENTS.md` — the five invariants ordered by what they cost to break, so a change that touches one
  knows what it is risking.
- The contributor deal inherited from one org-wide `CONTRIBUTING.md` instead of a local copy that
  silently shadowed it; the lab-wide AI-contributor credit policy; changelog categories for
  auto-generated release notes.
- `v0.2.0` was tagged on this date too — it was written a week earlier and never cut.

## v0.2.0 - 2026-07-28

A month of running the engine across six machines produced four more guards. Every one is the
same shape as the first three: something the protocol trusted without checking. Both new files
self-test (`python <file>.py selftest`), and `fleet_sign.py` does a real signature round trip
including a tampering check — a signature selftest that never fails a verification proves nothing.

- `reference/protocol_guards.py` - **arbiter election**: the tie-break role no longer dies with the
  machine holding it. Elected from an ordered list by presence freshness, computed identically by
  every peer (so they agree without messaging), promoting only on positive evidence of life — a
  stale stamp fails over, an absent one does not. Gated behind an `armed` flag so an un-upgraded
  peer behaves identically. Announced once per episode, not once per tick.
- `reference/protocol_guards.py` - **proof grading**: `VERIFY` proof is graded `proven` only when it
  carries the residue of an action (exit code 0, a hash, a moved counter, a before/after pair).
  Armed from a timestamp; earlier history is grandfathered.
- `reference/protocol_guards.py` - **risk tracking**: a track (financial/secrets/outbound/canon/
  infra/general) classified independently of the tier the proposer chose, because the tripwire
  catches dangerous words but not a dangerous category carried at a low tier. Ships in shadow with
  a deliberately falsifiable verdict and a report that says whether the guard earned its keep.
- `reference/fleet_sign.py` - **machine identity**: Ed25519 detached signatures via `ssh-keygen -Y`,
  one public key file per machine (single writer, no conflicts), revocation list, allowed-signers
  assembled in memory at each verify. Unsigned (rollout gap) is kept strictly distinct from bad
  (tampering); bad is refused even during the dark phase. Two Windows scars documented in place:
  two ssh-keygen builds on PATH where one hangs on `-Y sign`, and the `newline=""` that stops
  CRLF armor from being corrupted into a false "tampered" verdict.
- `docs/PROTOCOL.md` §6a and `FOR-ROBOTS.md` - the four guards written up, plus a new
  "what is here versus what we run" section: the reference is sanitized and trimmed for reading,
  not a mirror, and now says which parts are deliberately absent.

## v0.1.0 - 2026-07-02

First public release. The multi-machine coordination layer, extracted from our live system:

- `docs/PROTOCOL.md` - the consensus protocol: propose -> counter -> accept -> commit over an append-only single-writer JSONL ledger; risk tiers + deterministic tier-tripwire; the deterministic `tick` driver (timeouts, round cap, leader disagree-and-commit); the three guards (self-accept loop, independent verify + rubber-stamp guard, split-brain detection); quiet human-alert channel separated from the noisy machine feed.
- `docs/BUS.md` - the dual-rail bus: file mailbox + group chat, dual-send by construction; streams and capability addressing; the single-writer invariant; ACK discipline ("delivered is not done"); full-snapshot heartbeats; three self-heal layers for sync.
- `docs/GOVERNANCE.md` - leader/follower canon over receive-only sync; risk tiers enforced in three independent places; machine identity tagging; remote approval token; scoped authorization relay; autonomy triggers.
- `reference/` - the sanitized live implementation, stdlib-only Python: `consensus.py`, `machine_bus.py`, `bus_send.py`, `sync_monitor.py`. Battle scars kept in the comments.
- `FOR-ROBOTS.md` - entry point for AI agents mining this repo, alpha ranked by transferable value.
- `devlog/2026-07-02.md` - how this release happened.

This is pain #5 from the [family roadmap](https://github.com/tonydzi/claude-bible/blob/main/ROADMAP.md) ("multiple machines, one system"), shipped out of order because the demand signal was loudest.
