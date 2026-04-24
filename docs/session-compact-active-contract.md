# Session Compact Active Contract

Date: 2026-04-22
Status: ACTIVE
Implementation base: `/Users/florianliao/Documents/Playground/codex-upstream-latest`
Reference-only repo: `/Users/florianliao/Documents/Playground/codex-v122-compat-merge`

## Objective

Finish the session-level compact route in the latest upstream Codex. The route
must preserve durable session continuity across repeated compactions while
bypassing provider-based remote compact whenever this route is selected.

This is not a new memory subsystem. It is a new compact route that still uses
the existing compact triggers (`/compact` and auto compact), the existing
compaction pipeline, and `replacement_history` as the durable artifact carrier.

## Commitments

- Treat session compact as a new compact route or strategy, not as a patch on
  legacy collapse.
- Do not describe session compact as "covering local collapse" in latest
  upstream. In this
  repo, session compact is a new route layered onto the current
  local-vs-remote compact split.
- Keep `replacement_history` as the continuity and replay carrier.
- Keep the read path small. Put most complexity in the write path merge logic,
  the compact prompt contract, and the benchmark harness.
- Preserve room for image continuity, but keep images subordinate to the main
  session-compact design.
- Keep the implementation additive and upstream-friendly.
- Keep repo-local markdown updated so long-running work stays anchored to direct
  evidence.

## Non-goals

- Do not implement a separate session-memory DB or background memory module.
- Do not depend on remote compact parity.
- Do not rewrite the general session architecture.
- Do not optimize primarily for smaller summaries at the expense of behavioral
  continuity.
- Do not treat images as the central architecture.
- Do not modify live `~/.codex` session files during analysis or testing.

## Current Upstream Evidence

- Latest upstream no longer exposes the old `CompactMode::{Summary, Collapse}`
  split. The current compact flow is effectively a local-vs-remote routing
  choice.
- Current remote compact routing is still provider-capability-based through
  `compact::should_use_remote_compact_task(provider)`, which currently delegates
  to `provider.supports_remote_compaction()`. In current upstream that provider
  gate is still hardcoded around OpenAI- and Azure-compatible provider identity
  checks, so those providers can claim the compact path unless a
  higher-priority route intercepts first.
- Current local compact still produces a single handoff summary and rebuilt
  `replacement_history`; it is not yet session-level compact.
- `CompactedItem` still exposes `message` and `replacement_history`, so the new
  route should keep using this artifact boundary.
- Upstream also contains a separate `memories` pipeline, but that is a
  startup-time, cross-session shared-memory workflow. It is not the active
  session compaction path and is not a dependency for session compact.

## Route Design

### Routing shape

Introduce a new explicit compact route ahead of the current remote/local split:

1. `session_compact`
2. `remote_compact`
3. `legacy_local_compact`

The route gate should be explicit and configuration-driven. The intent is that
when `session_compact` is enabled, the provider capability check for remote
compact must not win.

### Config shape

Do not revive the old `Summary/Collapse` framing in the latest upstream repo.
Add a new strategy or route selector instead. The desired semantics are:

- default behavior remains current upstream behavior
- explicit opt-in enables `session_compact`
- when enabled, both manual `/compact` and auto compact take the new route

Current Phase 1 implementation decision:

- config key: `compact_strategy`
- values: `default`, `session`
- shared selector: `compact_session::select_compact_route(...)`

### Routing invariants

These invariants are part of the contract, not optional implementation style:

- `compact_strategy = session` must short-circuit before the provider-based
  remote gate.
- manual `/compact` and auto compact must share the same route selector so they
  cannot drift apart.
- config propagation must preserve `compact_strategy` through
  `Config -> SessionConfiguration -> TurnContext`, including forked/resumed
  thread flows.
- the remote bypass must be validated black-box at the request level, not only
  by unit tests on the selector function.

### Artifact shape

Keep `replacement_history` as the durable artifact carrier.

The compact summary inside that artifact should evolve from a generic prose
handoff into a stable `session_compact_state`. The state should remain text
first, short, and recursively mergeable.

Current favored sections:

- `Objective`
- `Active Memory`
- `Inactive Changes`
- `Current Handoff`
- `Evidence Pointers`

Current Phase 2 implementation decision:

- prompt template lives at `codex-rs/core/templates/session_compact/prompt.md`
- model output is normalized into one stable
  `<session_compact_state>...</session_compact_state>` block
- legacy session-compact tags from earlier draft runs are still parsed for
  compatibility, but new output is versionless
- `replacement_history` for the session route now keeps a bounded structured
  frontier plus the normalized state, instead of the legacy broad user-message
  carry forward

### Merge semantics

Each compaction should merge:

- `previous_session_compact_state`
- `newly_compacted_segment`

Allowed update actions:

- `retain`
- `update`
- `supersede`
- `withdraw`

This is the core behavioral change. The new route should stop treating compact
as "summarize the old span" and instead treat it as "update the durable
session-compact state."

### Read path

The read path should stay small:

- inject the newest compact state
- inject only a bounded recent structured frontier
- let the workspace be re-explored when detailed evidence is needed

The design should avoid ever-growing summary blobs.

Current Phase 2 implementation decision:

- bounded structured frontier: keep the most recent 2 real turns by default,
  but allow the frontier selector to expand backward across nearby active work
  turns
- frontier token budget: dynamic, based on the full current context length,
  capped at 10% of the model context window, with a 4k floor when the cap
  allows it
- summary prefix remains the existing compact summary prefix for compatibility
- analytics now distinguish `session_compact` from legacy local and remote
  compact paths

Current execution evidence:

- the latest-upstream copied-session benchmark runner now supports both
  single-hop `run` and minimal chained `run-chain` execution
- a real copied-session `run-chain --variant session --chain-hops 1` smoke has
  already completed successfully on source thread
  `019d4e04-eaa2-7311-a0e2-ad430b478151`
- that smoke observed a real mid-turn compact event, reduced the prepared
  thread's retained replacement history from `325` items / `25,350` estimated
  total tokens to `3` items / `1,495` estimated total tokens, and then passed
  the post-hop rare-critical probe
- a copied-session parity smoke for `chain_control` vs `session` on the same
  source thread now also passes with
  `cross_variant_summary["1"]["session"]["retained_vs_baseline_rate"] = 1.0`
- a copied-session `run-chain --variant session --chain-hops 2` smoke now shows
  both hop probes passing, but only hop 1 observed a fresh compact event; hop 2
  completed as `task_completed_without_target_compaction`, so repeated-compact
  sign-off remains open rather than implicitly complete
- a fresh copied-session `run-chain --variant session --chain-hops 3` smoke on
  the same source thread has now completed with all three hop probes passing;
  in that run, hop statuses were
  `[midturn_compaction_observed, midturn_compaction_observed, task_completed_without_target_compaction]`,
  so repeated compaction is now directly observed on hop 2
- a second copied-session `run-chain --variant session --chain-hops 3` smoke on
  structurally normal source thread
  `019d6961-5785-7520-903b-e4567b6e96a0` has now also completed with all three
  hop probes passing and hop statuses
  `[midturn_compaction_observed, midturn_compaction_observed, midturn_compaction_observed]`
- a broader copied-session `run-chain` parity sweep on the same second normal
  source thread has now completed across all 5 local cases at hop 1 with
  `cross_variant_summary["1"]["session"]["retained_vs_baseline_rate"] = 1.0`,
  `baseline_reference_pass_rate = 1.0`, and `variant_pass_rate = 1.0`
- this moved the acceptance focus from "can hop 2+ repeated compaction happen
  at all?" to "what clean parity breadth and reporting boundary are sufficient
  to close the route end-to-end?"

## Images And Other Non-text Evidence

Image continuity is now explicitly scoped as compact-owned sidecar metadata, not
as part of the main `session_compact_state` body and not as automatic prompt
replay.

Implemented boundary:

- session compact persists a bounded `image_sidecar` on the compacted artifact
- the sidecar stores only recent user image references plus a short associated
  user-text preview
- the sidecar is bounded and dormant: it is carried for continuity and possible
  future rehydration, but it is not automatically reinjected into later prompts
- ordinary follow-up turns therefore keep old images out of model-visible input
  unless the user provides new images again

Acceptance evidence:

- black-box test
  `session_compact_persists_image_sidecar_without_reinjecting_old_images`
  verifies that session compact persists image-sidecar data while keeping a
  later no-image follow-up request free of the old image payload

## Benchmark Spine

### Primary acceptance spine

Use the old compat repo as the benchmark source of truth for first migration.
The primary benchmark spine is:

- copied-session rare-critical retention
- continued auto-compact chaining
- replay and resume compatibility

Current local anchors in the reference repo:

- `docs/compact-rcr-benchmark.md`
- `benchmarks/compact_rcr_cases.json`
- `scripts/compact_collapse_rcr_benchmark.py`
- `scripts/compact_collapse_real_benchmark.py`
- `scripts/setup_compact_collapse_lab.py`

Known current state in the reference corpus:

- `RCR-Bench`
- 15 runnable cases
- 3 copied source threads with 5 cases each
- continued-auto acceptance criteria already framed around chained hops

### Local benchmark contract

The main benchmark truth source comes from copied local Codex sessions, not
synthetic-only corpora.

Each benchmark case should stay traceable to a copied source thread and keep:

- `source_thread_id`
- `evidence_summary`
- `evidence_timestamps`
- `category`
- `priority`
- `response_kind`

Current migrated corpus shape in latest upstream:

- 15 migrated cases
- 3 copied source threads:
  `019d4e04-eaa2-7311-a0e2-ad430b478151`,
  `019d5980-4f20-7861-9083-e3527d20a3d2`,
  `019d6961-5785-7520-903b-e4567b6e96a0`
- 5 cases per source thread
- category coverage:
  `forbidden_action`, `exact_binding`, `latent_constraint_application`,
  `update_precedence`, `scope_lock`
- response kinds:
  `single_choice`, `short_answer`

The canonical execution spine remains:

1. create an isolated lab `CODEX_HOME`
2. copy only selected rollout/config/auth assets into that lab
3. fork one prepared thread from the copied source session
4. optionally compact that prepared thread through the target route
5. fork isolated probe threads from the prepared state
6. score post-compact behavior, not summary text similarity

For chained acceptance, keep one prepared thread alive across hops and evaluate
continued behavior after another real compact event on that same thread.

Current migrated runner status:

- copied-session lab setup: implemented
- single-hop `source` vs `session` prepare/probe execution: implemented
- rollout-native compact diagnostics and compaction delta reporting: implemented
- minimal continued-auto `run-chain` scaffold for `chain_control` vs `session`:
  implemented
- first real `chain_control` vs `session` parity smoke on one copied source
  thread: completed and retained parity is currently `1.0`
- first real 2-hop `session` smoke on one copied source thread: completed, with
  hop 1 compact observed and hop 2 finishing without a new compact event
- first real 3-hop `session` smoke on one copied source thread: completed, with
  hop 1 and hop 2 compact observed, hop 3 finishing without a new compact
  event, and all three hop probes passing
- second real 3-hop `session` smoke on a different structurally normal copied
  source thread: completed, with hop 1, hop 2, and hop 3 all directly
  observing a fresh compact event and all three hop probes passing
- second normal-source hop-1 parity breadth sweep against `chain_control`:
  completed across all 5 cases with retained-vs-baseline parity currently `1.0`
- source thread `019d5980-4f20-7861-9083-e3527d20a3d2` is currently treated as
  a shape/harness outlier rather than a primary repeated-compaction oracle,
  because its latest replacement history is structurally abnormal for the
  current session benchmark path
- full multi-hop sign-off matrix and broader chain-control parity runs: still
  pending, but repeated-compaction existence and first broader parity on normal
  copied threads are no longer the primary open question

### Secondary external references

External benchmark families were searched as secondary inspiration, not as the
primary acceptance source. The current externally verified references worth
tracking later are:

- `LongBench` / `LongBench v2`
  broad long-context task coverage, including long dialogue and code-repository
  understanding; useful as the general long-context stress umbrella
  source: `https://github.com/THUDM/LongBench`
- `BABILong`
  distributed-fact retrieval and reasoning under very long noisy documents;
  useful for "needle stays recoverable after noise" stress patterns
  source: `https://github.com/booydar/babilong`
- `NoLiMa`
  long-context retrieval without lexical-overlap shortcuts; useful as an
  anti-literal-matching stress test so copied-session probes do not overfit to
  surface wording
  source: `https://github.com/adobe-research/NoLiMa`
- `LongMemEval`
  long-term conversational memory benchmark with update, temporal, multi-hop,
  and abstention-style settings; the closest external reference for chat-memory
  evaluation vocabulary
  source: `https://github.com/xiaowu0162/LongMemEval`
- `MemoryAgentBench`
  incremental multi-turn agent-memory benchmark spanning retrieval, test-time
  learning, long-range understanding, and conflict resolution
  source: `https://github.com/HUST-AI-HYZ/MemoryAgentBench`
- `LoCoMo`
  long conversational memory benchmark centered on multi-session dialogue
  recall, temporal reasoning, and multi-hop questions
  source: `https://github.com/snap-research/locomo`
- `AMA-Bench`
  agent-memory benchmark focused on how memory is constructed and retrieved
  across longer agent trajectories; useful as a future reference for
  future typed-memory discussions, not for replacing copied-session acceptance
  source: `https://github.com/AMA-Bench/AMA-Bench`

These should inform evaluation vocabulary and failure-mode coverage, but not
replace the copied-session Codex-specific acceptance spine.

## Phase Plan

### Phase 1: route and config

- add the new session-compact route gate
- wire it into both manual `/compact` and auto compact
- guarantee it bypasses provider-based remote compact when selected

Current status:

- implemented
- config plumbing now carries `compact_strategy` from TOML through
  `Config -> SessionConfiguration -> TurnContext`
- manual `/compact` and auto compact now share one explicit route selector
- request-level tests now verify that `compact_strategy = session` bypasses
  remote compact for both manual and auto-compaction paths

### Phase 2: state contract

- define the exact `session_compact_state` format
- update the compact prompt to emit that format
- implement parser, normalizer, and merge rules
- preserve fallback behavior for malformed outputs

Current status:

- first-pass implementation is in place
- normalized state sections are `Objective`, `Active Memory`,
  `Inactive Changes`, `Current Handoff`, and `Evidence Pointers`
- session route replacement history now keeps the normalized state plus a very
  small bounded raw tail

### Phase 3: behavior and harness

- migrate the RCR and continued-auto benchmark assets into the latest upstream
  repo
- add route coverage, parser coverage, replay/resume compatibility coverage, and
  repeated-compaction continuity coverage
- keep live `~/.codex` sessions out of destructive testing

Current status:

- deterministic corpus/scoring scaffold is migrated
- isolated copied-session lab setup and single-hop `source` vs `session`
  prepare/probe execution are now wired into
  `scripts/session_compact_rcr_benchmark.py`
- chained diagnostics now explicitly expose hop-status counts,
  compact-observed hop indexes, completed-without-target-compaction hop
  indexes, and per-source-thread hop-status breakdowns
- continued-auto-session remains the final sign-off family, not yet fully
  signed off

## Acceptance Criteria

- A new explicit session-compact route exists in latest upstream.
- The new route wins ahead of provider-based remote compact when enabled.
- `compact_strategy = session` does not issue a remote compact request even when
  provider capability would otherwise allow it.
- Black-box request-level coverage proves the remote bypass for both manual
  `/compact` and auto compact, not only selector-level unit tests.
- Repeated compactions preserve session objective and other durable constraints.
- Later instructions can supersede or withdraw earlier ones without stale memory
  leaking into future turns.
- Rare but critical facts survive multiple compactions.
- Compact artifacts remain replay and resume compatible through
  `replacement_history`.
- Benchmark migration produces a clear acceptance path in the latest upstream
  repo.
- Benchmark cases remain traceable to copied source threads rather than detached
  synthetic summaries.
- At least one copied-session parity run against `chain_control` shows retained
  post-compact behavior at hop 1 with
  `retained_vs_baseline_rate = 1.0` on comparable cases before broader sweeps.
- Repeated-compaction sign-off is not considered complete until hop 2+ either
  observes a fresh compact event or the benchmark documents a specific,
  accepted reason why a new compact should not have been expected.
- Direct fresh-run evidence must show not just that hop 2+ can compact once,
  but that hop-level status patterns are explainable and stable enough across
  more than one copied source thread.

## Acceptance Closure

The acceptance bar is now treated as closed for this route.

Closed acceptance items:

- route correctness is signed off:
  `compact_strategy=session` wins ahead of remote/local routing for both manual
  `/compact` and auto compact, and request-level tests prove that the session
  route does not issue a remote compact request on remote-capable providers
- repeated-compaction existence on normal copied threads is signed off:
  hop 2+ fresh compact events are directly observed on two structurally normal
  copied source threads, `019d4e04...` and `019d6961...`, while the retained
  rare-critical probes continue to pass
- parity breadth is signed off at the chosen threshold:
  `019d6961...` now has clean all-case `chain_control` vs `session` parity at
  hop 1 and hop 2, each with `retained_vs_baseline_rate = 1.0`,
  `baseline_reference_pass_rate = 1.0`, and `variant_pass_rate = 1.0`; this is
  paired with the existing `019d4e04...` hop-1 parity evidence and multi-hop
  repeated-compaction evidence on both normal copied threads
- outlier handling is signed off:
  the benchmark manifest and runner now classify `019d5980...` as a
  `shape_harness_outlier`, exclude it from the acceptance-oracle bucket, and
  keep it only for timeout/diagnostic reporting
- image continuity scope is signed off:
  the route now uses a compact-owned bounded `image_sidecar`, and black-box
  testing proves that old images are persisted for continuity without being
  automatically replayed into later no-image prompts

Working interpretation:

- do not reopen the already-settled question of whether session compact must
  avoid remote compact when selected; that behavior is established
- do not reopen the already-settled question of whether hop 2+ repeated session
  compaction can happen on normal copied threads; that behavior is established
- do not reopen the already-settled question of whether image continuity lives
  inside the main compact state body; it does not
- future benchmark expansion is optional confidence-building work, not part of
  the required acceptance bar for this route

## Resolved Decisions

- image continuity stays inside compact-owned sidecar metadata and does not
  share the main compact-state envelope
- the copied-session benchmark runner now has the required local CLI surface:
  manifest summary, isolated lab setup, single-hop run, and chained run-chain
- hop 2+ does not need forced threshold changes to satisfy acceptance; the
  acceptance language now distinguishes route correctness and retention parity
  from whether a given later hop observed a fresh compact event in that exact
  run
- the long-text continuation prompt is the accepted harness shape for clean
  parity work because it removes tool-startup noise from the acceptance path
- the minimum continued-auto-session evidence is now treated as satisfied by the
  current normal-thread oracle set and the clean parity threshold recorded above

## Working Rule

When tradeoffs appear, prefer the route that best preserves real long-session
behavior on copied-session evidence, even if that route is less elegant than a
purely aesthetic compact summary.
