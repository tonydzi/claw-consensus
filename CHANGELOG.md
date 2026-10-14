# Changelog

All notable changes to this project. Small commits land daily as work happens; **every noticeable
change ships as a release**. (This line used to promise a release twice a week; v0.2.0 below was
written on 2026-07-28 and carried no tag until 2026-08-04, so the promise was replaced with a rule
tied to the work.) Format: what shipped, in plain words.

## v0.5.0 - 2026-10-14

**The trust boundary is now in the code, not in the honesty of whoever writes a ledger line**
(issue #9, rows A and B). Changes the event format, hence the minor bump.

- **B. Human approval is a signed envelope.** `approve <id> "<proof>"` is gone: a free-text
  proof is refused (exit 2). New verbs: `approval-request <id>` prints the envelope
  (`human_principal`, `proposal_id`, `action_digest`, `expires_at`, `nonce`), the human signs it
  on their own device with `sign-approval ... --key`, and `approve <id> --envelope` records it.
  Refused, with the reason printed: unknown principal, bad or missing signature, another
  proposal, changed action (a COUNTER after the approval voids it), expired, replayed nonce.
  New config `human_principals`; human keys pinned with `CONSENSUS_HUMAN_SIGNERS_DIR`. **Migration:** a legacy free-text `HUMAN_APPROVED` no longer
  unlocks `commit`; re-approve open Tier-2 proposals with a signed envelope.
- **A. Signatures are checked on load.** `consensus.py` signs every event it writes when the
  machine has a fleet key (`fleet_sign.py init`) and checks every line before projection: bad
  signature or a key signing for another machine → quarantine always; unsigned → quarantine
  once `sig_enforce_after` is set (`"*"` = strict, recommended once every machine signs).
  `status`/`list` print a `ledger trust:` line and every `QUARANTINE` row; unparsable lines are
  now listed too instead of skipped silently. Good verdicts are cached locally per registry
  fingerprint. Default stays **dark** (unsigned trusted, and the status line says `DARK`), per
  the documented rollout order; existing ledgers read as before.
- `fleet_sign.py`: `sign_bytes`/`verify_bytes` take a namespace (`fleet-approval` for humans)
  and `sign_bytes` an explicit key; `registry_fingerprint()`.
- `protocol_guards.sig_enforced`: `"*"` = strict, and a malformed `ts` is never grandfathered
  (a forged line could set `ts: "!"`, which sorts before any real timestamp).
- Env pins for machines that trust the shared config less than themselves:
  `CONSENSUS_SIG_ENFORCE_AFTER`, `CONSENSUS_HUMAN_PRINCIPALS`.
- `demo/demo.py`: scenario B now goes through the signed envelope and shows the free-text
  approve refused; new scenario F forges a line under strict mode and shows it quarantined. Each
  simulated machine gets its own key dir inside the temp bus.
- **Hardened after an adversarial review of the first cut** (each attack reproduced first, each
  now a test that was red on that cut): a bus writer could swap `signers/OWNER.pub` and sign as
  the owner → `CONSENSUS_HUMAN_SIGNERS_DIR` pins human keys locally (`FLEET_SIGNERS_DIR` does the
  same for machine keys), and `approve` says when the key is unpinned; an expired envelope
  appended by hand as `HUMAN_APPROVED` unlocked commit → expiry and nonce are re-checked on the
  ledger at commit time, so an approval must be used before it expires; `sig_enforce_after:
  true` disabled enforcement → unreadable values fail closed as strict. Malformed lines now go
  to the quarantine (`malformed`) instead of being skipped.
- `reference/_test_consensus_guarantees.py`: 31 tests. Against v0.4.1 the 15 new ones fail;
  all 31 pass now. New failure mode **N**.

## v0.4.1 - 2026-10-14

**Three documented guarantees of the reference engine are now enforced by code** (issue #9,
rows D, C and E; found by an outside audit, re-checked by hand against `main`):

- **D. The rubber-stamp guard refuses.** `verify` with a proof copied verbatim from a prior
  VERIFY now exits 1 and records nothing. Before, it printed a warning and appended the VERIFY
  anyway, which then counted toward "done". `--advisory` keeps the old behaviour, by name.
- **C. A leader's unilateral call is `LEADER_OVERRIDE`, not `COMMIT`.** New event type and
  status `overridden`. `commit` writes `COMMIT` only on `agreed` (or a human-approved Tier-2);
  the leader committing anything else, and `tick`'s disagree-and-commit on its own proposal,
  write `LEADER_OVERRIDE`. Split-brain detection counts both.
- **E. Order is logical, not wall-clock.** Every event carries `seq` (a Lamport counter);
  projection sorts by `(seq, ts, actor, event_id)`. A machine whose clock runs behind can no
  longer write an ACCEPT that sorts before the proposal it accepts. Old events without `seq`
  keep their `ts` order.
- **Hostile lines** (found by an adversarial review of the fix above, each reproduced first):
  a copied proof with a trailing "." passed the guard; a hand-appended duplicate VERIFY counted
  toward done; a second PROPOSE with `seq: -1` took a Tier-2 proposal over as Tier-0; `seq:
  Infinity` crashed every reader; another machine reusing my `event_id` erased my vote. Now:
  proofs compared on their words and counted once in the projection; malformed lines refused;
  dedup per `(actor, event_id)`; more than one PROPOSE = `conflict`, highest tier wins.
- New `reference/_test_consensus_guarantees.py`: 16 tests. Against v0.4.0 11 fail, against the
  first cut of this fix the 6 hostile-line tests fail; all 16 pass now. `demo/demo.py` is
  unchanged and still passes all five scenarios.

Not in this release: rows A (signature check on load) and B (signed human approval); they change
the event format and ship separately.

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
