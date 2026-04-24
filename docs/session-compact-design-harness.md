# Session Compact Design Harness

Date: 2026-04-23
Status: ACTIVE design spec
Implementation base: `/Users/florianliao/Documents/Playground/codex-upstream-latest`
Reference-only repo: `/Users/florianliao/Documents/Playground/codex-v122-compat-merge`

## Objective

Upgrade the internals of latest-upstream `session compact` without changing its
external product identity.

This means:

- keep the name `session compact`
- keep `compact_strategy = session`
- keep manual `/compact` and auto compact as the only triggers
- keep `replacement_history` as the durable carrier

What changes is the compact core. `session compact` should become the internal
compact harness for long-running sessions rather than a thin
`state summary + tiny user tail` path.

## Why This Spec Exists

The current latest-upstream session route and the old collapse route optimize
for different things:

- current `session compact`
  - durable state first
  - tiny raw tail
  - good route/config separation
  - insufficient recent working-state retention for the intended product target
- legacy `collapse`
  - recent structured working state first
  - better assistant/tool continuity
  - weak durable session merge semantics
  - not the desired latest-upstream product identity

The intended target is not "bring back collapse as a top-level mode". It is:

`session compact` should subsume the important `collapse` behavior inside one
stable session-level compact framework.

For the oversize case where a single compact turn cannot faithfully cover the
whole pre-compaction thread, see:

- `docs/session-compact-staged-orchestration.md`

## Non-goals

- do not rename the route
- do not add a new session-memory subsystem or DB
- do not add a second trigger surface beyond `/compact` and auto compact
- do not turn images into the architecture center
- do not regress replay/resume compatibility
- do not bloat the read path with an ever-growing free-form blob

## Core Definition

The new `session compact` should be a four-layer harness:

1. `capture`
   - classify the raw session segment into durable state candidates, recent
     working-state candidates, sidecar evidence, and discardable material
2. `merge`
   - merge durable session state across compactions
   - refresh recent structured working state from the newest segment
3. `assembly`
   - project the merged result into the existing compact artifact boundary:
     `CompactedItem.message`, `CompactedItem.replacement_history`, and bounded
     sidecars
4. `resume`
   - define exactly what later turns reconsume automatically and what stays
     dormant unless explicitly needed

This is the "design harness" for session compact. Benchmark and test harnesses
are downstream validation of this shape, not the harness itself.

## Retention Model

The upgraded route should have three retention lanes.

### 1. Durable state lane

This is the current session-compact strength and should remain.

It carries information that must survive many compactions:

- objective and current acceptance target
- active constraints and policies
- exact bindings such as paths, runtimes, env names, or required services
- user overrides and update precedence
- explicit withdrawals and obsolete directions
- current handoff state and next-step intent
- evidence pointers to the source of truth

This lane should continue to live in the normalized
`<session_compact_state>...</session_compact_state>` block.

### 2. Recent structured working-state lane

This is the missing capability that should be absorbed from legacy collapse.

It carries the recent task frontier rather than only recent user text:

- latest user turns that still define the active frontier
- assistant messages tied to those turns
- reasoning items
- function calls and function-call outputs
- custom-tool calls and outputs
- recent failure surfaces, retries, and unfinished substeps
- compact-relevant contextual messages that are part of the active work surface

This lane should be bounded, structured, and locally derived from history. It
should not depend on the model to rewrite the recent tail correctly.

### 3. Evidence sidecar lane

This lane remains subordinate.

For V1.5-style session-compact upgrade it should include:

- bounded image continuity via `image_sidecar`
- room for future bounded non-text evidence metadata

It should not automatically replay old evidence into later prompts unless a
future targeted rehydration policy explicitly says so.

## How This Differs From Current Session Compact

Current latest-upstream behavior is effectively:

- merge durable state from the compact turn
- keep the last 2 user messages within about 4k tokens
- rebuild `replacement_history` from that tiny user tail plus the normalized
  state block

That is too thin for long coding threads where the current working frontier is
often encoded in assistant/tool structure rather than user prose alone.

The target behavior is:

- durable state still comes from session-level merge
- recent tail is no longer "last few user messages"
- recent tail becomes a bounded structured frontier
- the route remains one `session compact` route rather than splitting back into
  `session` versus `collapse`

## How This Differs From Legacy Collapse

Legacy collapse should be treated as a source of useful internal policy, not as
the desired latest-upstream product surface.

What to keep from collapse:

- split on real turn/frontier boundaries
- preserve a recent structured tail rather than only a prose handoff
- sanitize and microcompact preserved tool payloads instead of dropping them
- keep the recent tail locally computed and deterministic

What not to copy directly:

- reintroducing `collapse` as the headline mode identity
- relying only on preserved recent structure with weak session-level merge
- treating preserved turns as the whole compact answer

## Artifact Contract

The durable artifact boundary should stay inside existing protocol fields.

### `CompactedItem.message`

Keep using the summary prefix plus the normalized session state block. This
remains the canonical durable session summary.

### `CompactedItem.replacement_history`

This should evolve from:

- `tiny recent user tail`
- plus `session_compact_state`

into:

- `bounded recent structured frontier`
- plus `session_compact_state`

Preferred ordering:

1. bounded recent structured frontier items
2. final normalized session-compact state message

That preserves the current "summary/state as final handoff" shape while letting
later turns still see the most recent agent/tool structure before it.

### `image_sidecar`

Keep the current bounded image continuity contract:

- persist recent user image references
- keep them out of follow-up prompts by default
- treat them as dormant evidence, not active prompt cargo

## Capture Layer Requirements

The capture layer should stop using "collect only user messages" as the main
session-tail strategy.

It should instead classify raw history into:

- durable-state candidates
- working-frontier candidates
- sidecar evidence candidates
- excluded artifacts

Excluded artifacts still include at least:

- prior compaction artifacts
- ghost snapshots where they do not help replay/resume
- stale duplicated state that should remain only in the normalized state block

The working-frontier selector should start from legacy collapse heuristics:

- split on real user-turn boundaries
- step backward from the most recent task frontier
- preserve a bounded number of frontier turns or frontier-adjacent items
- run microcompact on large tool payloads before dropping structure

## Merge Layer Requirements

The merge layer is where `session compact` stays meaningfully different from
plain collapse.

Durable merge semantics should remain explicit:

- `retain`
- `update`
- `supersede`
- `withdraw`

Recent structured frontier should not be recursively merged forever. It should
refresh from the newest raw segment on each compaction so the route does not
degrade into summary-of-summary-of-summary behavior.

In short:

- durable lane is recursive
- working-state lane is refreshed

## Assembly Layer Requirements

The assembly layer should be deterministic and protocol-preserving.

Implementation direction:

- keep `compact_session::select_compact_route(...)` as-is
- keep `run_session_compact_task_inner_impl(...)` as the route entry
- replace the current `collect_user_messages(...) ->
  build_session_compacted_history(...)` path with a richer assembly pipeline
- factor the preserved-tail selection and microcompact logic into shared helper
  code rather than cloning old collapse behavior inline

The assembly layer should also emit diagnostics that are cheap to inspect from
rollouts and tests, such as:

- how many frontier items were preserved by type
- whether tool payloads were microcompacted
- how many durable state entries were retained or superseded

These diagnostics exist to stabilize implementation behavior. They are not a
new user-facing product surface.

## Resume Layer Requirements

The resume policy should stay intentionally small and predictable.

Automatic prompt reconsumption should include:

- the normalized durable session state
- the bounded structured frontier

Automatic prompt reconsumption should not include:

- dormant image evidence
- arbitrarily old tool payloads
- a second parallel session-memory source

This keeps the read path bounded while still being materially richer than the
current "2 user messages + state" path.

## Code Touchpoints

The primary latest-upstream touchpoints for this upgrade are:

- `codex-rs/core/src/compact_session.rs`
  - current route logic and current tiny-tail assembly
- `codex-rs/core/templates/session_compact/prompt.md`
  - durable state generation prompt
- shared compact helpers that should absorb preserved-tail logic from older
  collapse code
- staged-orchestration helpers for the oversized-thread escalation path
- request-level tests around manual and auto session compact
- replay/resume tests that rely on `replacement_history`

Legacy reference logic worth reusing conceptually:

- `codex/codex-rs/core/src/compact.rs`
  - turn-boundary splitting
  - preserved-tail sanitization
  - microcompact of tool payloads

## Acceptance Direction

The acceptance target for this design is not "smallest possible retained
history". It is:

- durable session continuity remains strong
- recent working-state continuity improves materially
- replay/resume compatibility stays intact
- repeated compactions do not collapse the route into a giant blob or a barren
  user-only tail

Benchmark consequences still matter, but they are downstream. The design
harness must exist first so later evaluation is measuring the right compact.

## One-line Summary

Keep `session compact` as the public route, but redesign its internals as a
session-level compact harness with:

- recursive durable state merge
- bounded structured working-state retention
- subordinate evidence sidecars
- stable replay/resume assembly through `replacement_history`
