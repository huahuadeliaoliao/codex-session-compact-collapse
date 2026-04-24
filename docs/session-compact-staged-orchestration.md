# Session Compact Staged Orchestration

Date: 2026-04-23
Status: ACTIVE design spec
Implementation base: `/Users/florianliao/Documents/Playground/codex-upstream-latest`
Parent spec: `docs/session-compact-design-harness.md`

## Objective

Define how `session compact` should escalate internally when one compact turn
cannot faithfully absorb the full pre-compaction thread.

This is an internal orchestration upgrade only. It does not change:

- the public name `session compact`
- `compact_strategy = session`
- `/compact` and auto compact as the only triggers
- `replacement_history` as the durable carrier

## Why This Exists

The upgraded design harness already distinguishes two different questions:

1. what the compact model sees
2. what later turns automatically reconsume

For normal compactions, the answer is already correct:

- the compact turn should see the whole pre-compaction thread while it fits
- later turns should only reconsume bounded structured frontier plus durable
  session state

But an additional case still exists:

- the pre-compaction thread is too large to fit in one compact turn even before
  considering the compact prompt budget

When that happens, oldest-first dropping inside a single compact turn becomes a
graceful fallback, but not the desired long-term architecture.

The desired next step is to keep `session compact` as one route while letting
its write path escalate into a staged internal compact flow.

## Non-goals

- do not expose `staged compact` as a separate strategy
- do not rebrand the feature as `agent compact`
- do not broaden the design into a general memory platform
- do not add cross-session storage or a background daemon
- do not make later turns replay every staged artifact

## Core Definition

`Staged session compact` is a multi-step internal orchestration that activates
only when a single compact turn cannot cover the full pre-compaction thread
within budget.

Externally it is still one `session compact` event that emits one normal
compaction artifact.

Internally it behaves like a bounded compact worker with explicit phases:

1. `snapshot`
   - freeze the full pre-compaction thread before any replacement happens
2. `partition`
   - split the oversized history into ordered chunks on real turn boundaries
3. `chunk compact`
   - compact older chunks into durable state deltas
4. `merge`
   - merge chunk deltas into one normalized session state
5. `assemble`
   - emit one final compact artifact with:
     - normalized durable state
     - bounded recent structured frontier
     - bounded sidecars

This is "agent-compact-like" in implementation shape, but it remains session
compact in product identity.

## Activation Rule

The default path should remain single-turn session compact.

Staged orchestration should activate only when the system determines that the
full pre-compaction thread cannot be preserved inside one compact turn without
dropping older history items.

V1 rule:

- attempt normal single-turn session compact input construction first
- if the compact request overflows and would require trimming one or more
  pre-compaction history items, switch from trim-and-retry to staged compact

This keeps the current fast path intact and makes staged compact an explicit
escalation rather than the default.

## Capture Model

Staged compact should still follow the same harness lanes defined in the parent
design:

- durable state lane
- recent structured working-state lane
- evidence sidecar lane

The difference is how those lanes are produced.

### Durable lane in staged compact

The durable lane should absorb information from the full oversized segment, not
just the suffix that happened to fit inside one compact turn.

### Working-state lane in staged compact

The recent structured frontier should still come from the newest raw segment
rather than from recursively summarized chunk outputs.

This prevents the route from degrading into summary-of-summary frontier replay.

### Evidence sidecar lane in staged compact

Evidence sidecars should continue to be bounded and subordinate.

Images remain the primary V1 sidecar. Older chunks may contribute evidence
pointers, but staged compact should not auto-reinject old evidence into follow
up prompts.

## Partition Strategy

Chunking should follow session semantics rather than arbitrary byte slicing.

V1 partition rules:

- split on real user-turn boundaries
- keep pre-turn contextual messages attached to the following user turn
- avoid splitting inside a tool exchange when it belongs to one active turn
- reserve the newest raw frontier outside the chunk loop so it can remain
  locally preserved rather than recursively summarized

This yields two regions:

1. `historical merge region`
   - older turns that need staged durable-state absorption
2. `recent frontier region`
   - newest bounded raw frontier that should survive as structure

## Chunk Compact Semantics

Each historical chunk should be compacted oldest to newest.

The output of a chunk compaction is not a user-facing artifact. It is an
internal durable-state delta that can be merged into an accumulator.

The chunk compact prompt should be narrower than the normal route prompt:

- prioritize durable state extraction
- preserve update precedence
- explicitly mark superseded or withdrawn instructions
- avoid pretending to preserve a raw recent frontier for historical chunks

This means chunk compaction is allowed to be more state-centric than the final
route-level artifact.

## Merge Semantics

Chunk outputs should merge into one durable accumulator using the same session
state rules already intended for the main route:

- retain
- update
- supersede
- withdraw

The accumulator should be normalized after the last historical chunk so the
route still emits one canonical `<session_compact_state>` block.

## Final Assembly

The final artifact contract stays unchanged.

The route should emit one `CompactedItem` with:

- `message`
  - summary prefix plus final normalized session state
- `replacement_history`
  - bounded recent structured frontier from the newest raw region
  - then the final normalized session state
- `image_sidecar`
  - bounded recent image continuity only

Chunk-local intermediate state should not leak into `replacement_history`.

## Why This Is Not A New Product Mode

The user-visible questions remain unchanged:

- manual `/compact`
- auto compact near the model context ceiling
- resume from one compacted thread

Staged compact only changes how the write path reaches the final artifact when
the thread is too large for one compact turn.

So the right framing is:

- `session compact` is the product mode
- `staged compact` is an internal escalation path inside session compact

## V1 Implementation Shape

The first implementation should stay narrow.

Recommended sequence:

1. keep the current single-turn path as the default fast path
2. detect compact-turn overflow before oldest-first trimming would discard
   source history
3. select the recent structured frontier from the frozen pre-compaction history
4. partition the remaining older history into chunk groups
5. compact those chunk groups into a durable accumulator
6. normalize the accumulator into the final session state
7. assemble the final artifact using the current replacement-history and
   sidecar machinery

## Diagnostics

The staged path should emit cheap diagnostics for rollouts and tests:

- whether staged compact activated
- how many source items were chunked
- how many chunks were processed
- how much history stayed raw as structured frontier
- whether any chunk merge produced withdrawals or supersedes

These diagnostics should remain implementation-facing rather than becoming a
new user-facing surface.

## Relationship To Benchmarking

This spec exists before benchmark work, but it clarifies what later benchmark
shapes must verify.

The important downstream questions become:

- when staged compact activates, does the final state still preserve low
  frequency but critical constraints from older chunks
- does the final artifact still preserve recent working-state continuity from
  the newest raw frontier
- after repeated compactions, does the route still behave like one coherent
  session compact rather than a pile of recursive summaries

## One-line Summary

When one compact turn cannot faithfully cover the full pre-compaction thread,
`session compact` should escalate internally into a staged chunk-and-merge
write path, while still emitting the same public artifact shape and preserving
the same route/config/name surface.
