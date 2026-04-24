# Session-Compact Retention Benchmark

This document defines the migrated retention benchmark spine for the
latest-upstream `session compact` implementation.

It is intentionally narrower than the old compat-repo lab:

- the benchmark target is `session_compact`, not legacy `collapse`
- the first stable layer preserves the rare-critical retention corpus
- the chained execution layer now also scores recent working-state continuity

## Purpose

The benchmark exists to answer two closely related questions:

1. Does a thread that has gone through session-level compaction still behave as
   if it remembers low-frequency but high-impact project requirements?
2. After a continued-auto hop, does the post-compact thread still recover the
   most recent active work bundle well enough to keep working?

This is a behavior benchmark, not a string-similarity benchmark.

## Migrated Assets

The latest-upstream repo now contains:

- `benchmarks/session_compact_rcr_cases.json`
- `scripts/session_compact_rcr_benchmark.py`

These assets were migrated from the old compat-repo benchmark corpus and then
retargeted at the new `session compact` route.

The benchmark script now covers:

- deterministic manifest/probe/schema/scoring helpers
- isolated copied-session lab setup
- single-hop copied-session `source` vs `session` execution
- minimal chained `run-chain` execution for `chain_control` vs `session`
- optional cross-repo `legacy_collapse` chained comparison via the old compat repo
- per-hop working-state continuity probes during chained execution

## Current Corpus

The migrated manifest currently preserves:

- 15 rare-critical retention cases
- 3 copied source threads
- 5 cases per source thread
- mixed `single_choice` and `short_answer` probes

The cases continue to cover:

- `forbidden_action`
- `latent_constraint_application`
- `update_precedence`
- `exact_binding`
- `scope_lock`

The chained runner now layers an additional deterministic working-state probe on
top of those cases. Each hop injects a synthetic active work bundle containing:

- one file path
- one next-step instruction
- one blocker
- one command

The post-hop working-state probe then asks the model to recover that bundle
exactly from the most recent thread state.

## Corpus Provenance

This benchmark is grounded in copied local Codex sessions, not synthetic chat
transcripts alone.

Each case in `benchmarks/session_compact_rcr_cases.json` should remain traceable
to a copied source thread and include:

- `source_thread_id`
- `evidence_summary`
- `evidence_timestamps`
- `category`
- `priority`
- `response_kind`

This provenance requirement matters because the benchmark is meant to measure
real session-level behavioral continuity after compaction, not only abstract
memory recall.

## Variant Naming

The benchmark naming in latest upstream should now be:

- `source`
  un-compacted copied-session reference
- `session`
  the new session-level compact route
- `chain_control`
  continued-chain control without session compaction
- `legacy_collapse`
  optional cross-repo donor baseline run through the old compat repo to measure
  how close the upgraded session route gets to collapse-style recent
  working-state retention

Do not keep using `collapse` as the headline benchmark variant name in this
repo. That name belongs to the old compat framing.

When `legacy_collapse` is present, treat it as an auxiliary comparison lane
rather than the primary product identity. The benchmark question is still
whether `session compact` closes the working-state gap while preserving the
latest-upstream session-route contract.

## Scaffold Scope

The current migrated script intentionally supports only the stable benchmark
substrate:

- manifest validation
- deterministic probe rendering
- deterministic schema generation
- deterministic scoring
- aggregate corpus summaries

This is enough to unblock:

- probe wording review
- score-function review
- case-corpus maintenance
- future copied-session runner integration

## Next Migration Layer

The next benchmark step after the new single-hop runner is chained execution.

The new single-hop execution layer now already uses the latest-upstream app
server SDK and the new `compact_strategy = session` configuration to:

1. prepare a copied source thread
2. optionally compact it through the session route
3. fork an isolated probe thread
4. run the deterministic probe
5. score the structured result with the migrated scorer

What remains is to extend the same runner into chained continued-auto execution.

## Isolation Contract

The execution harness should preserve the old copied-session lab boundary:

- create an isolated lab root with separate `home/` and `user-home/`
- copy only the needed `config.toml`, `auth.json`, `hooks.json`, `AGENTS.md`,
  and selected `sessions/rollout-*.jsonl`
- point `CODEX_HOME`, `HOME`, and `XDG_*` at the lab before running Codex

This copied-session isolation rule is part of the benchmark contract. Live
`~/.codex` data must stay untouched.

## Execution Spine To Preserve

The migrated online runner should keep the same behavioral shape as the old
compat benchmark:

1. prepare one variant thread from one copied source session
2. for single-hop evaluation, compact that prepared thread at most once
3. fork isolated case threads from the prepared state
4. run deterministic probes in the forked case threads
5. score against the migrated deterministic scorer

The important contract here is "prepare once, fork many probes". Do not let the
benchmark quietly mutate the source baseline differently for each case.

The current script entry points are:

- `setup-lab`
  create an isolated copied-session lab
- `run`
  execute single-hop `source` vs `session` prepare/probe evaluation
- `run-chain`
  execute chained continued-auto `chain_control` vs `session` evaluation

## Continued-Auto Target

Single-hop prepare-and-probe is only the first acceptance layer.

The real sign-off target remains a continued long-running thread that:

1. accumulates fresh work
2. auto-compacts again
3. keeps going
4. still retains rare-critical requirements
5. still remembers the immediately preceding active work surface

When the execution layer is migrated, the main acceptance family should be
named `continued_auto_session`.

The long-lived prepared-thread shape from the old runner should be preserved:

1. keep one prepared thread alive across hops
2. add one deterministic continuation stimulus on that same thread
3. observe a real auto-compaction event on that same thread
4. fork isolated probe threads only after the hop settles
5. continue from that same prepared thread into the next hop

The current chain runner therefore now emits two parallel probe families after
every probe-ready hop:

- rare-critical retention probes from the migrated corpus
- one working-state continuity probe for the hop-local active work bundle

## First Real Chain Smoke

The migrated latest-upstream runner has now completed one real copied-session
continued-auto smoke on a local thread copy.

Observed result on `2026-04-22` local time:

- source thread: `019d4e04-eaa2-7311-a0e2-ad430b478151`
- variant: `session`
- case: `s4e04_real_assets_before_benchmark`
- chain hops: `1`
- turn status: `midturn_compaction_observed`
- compact observation: `1` new compacted item observed on the prepared thread
- prepared-thread shape check: `shape_valid = true`
- retained probe verdict after the hop: `pass`

Observed compact delta on that smoke:

- prepared thread replacement history shrank from `325` items /
  `25,350` estimated total tokens
- to `3` items / `1,495` estimated total tokens
- final compact signature kept exactly `1` summary message at
  `summary_positions = [2]`

This smoke does not replace the full sign-off matrix, but it does prove that
the new latest-upstream `run-chain` path can:

1. fork a copied prepared thread
2. trigger session-route auto compaction on that same thread
3. observe the compact event from rollout-native diagnostics
4. interrupt after detection
5. fork a hop-local probe thread from the post-compact state
6. retain a rare-critical requirement on the probe

## First Parity Smoke

The migrated latest-upstream runner has also completed a first copied-session
parity comparison between `chain_control` and `session`.

Observed result on `2026-04-22` local time:

- source thread: `019d4e04-eaa2-7311-a0e2-ad430b478151`
- compared variants: `chain_control`, `session`
- chain hops: `1`
- comparable cases: `1`
- session retained-vs-baseline rate: `1.0`
- chain-control reference pass rate: `1.0`
- session pass rate: `1.0`

This is only a one-case parity smoke, but it proves the migrated
cross-variant summary is now anchored to `chain_control` rather than the old
useless source-comparison logic for chained runs.

## First 2-Hop Session Smoke

The migrated latest-upstream runner has also completed a first copied-session
2-hop `session` smoke on the same source thread.

Observed result on `2026-04-22` local time:

- source thread: `019d4e04-eaa2-7311-a0e2-ad430b478151`
- variant: `session`
- chain hops: `2`
- hop 1 status: `midturn_compaction_observed`
- hop 2 status: `task_completed_without_target_compaction`
- chain diagnostics: `total_hops = 2`, `compact_observed_hops = 1`
- hop 1 compact delta:
  replacement history shrank from `325` items / `25,350` estimated total
  tokens to `3` items / `1,507` estimated total tokens
- hop 2 compact delta:
  no new compacted item was observed; replacement history stayed at `3` items /
  `1,507` estimated total tokens
- retained probe verdicts after both hops: `pass`

This result is encouraging but not complete sign-off. It currently proves that
the post-compaction prepared thread can continue and still answer the retained
probe, but it does not yet prove reliable repeated auto-compaction on hop 2+.

## First 3-Hop Session Smoke

The migrated latest-upstream runner has now also completed a fresh copied-session
3-hop `session` smoke on the same source thread.

Observed result on `2026-04-22` local time:

- source thread: `019d4e04-eaa2-7311-a0e2-ad430b478151`
- variant: `session`
- chain hops: `3`
- hop status sequence:
  `midturn_compaction_observed`,
  `midturn_compaction_observed`,
  `task_completed_without_target_compaction`
- chain diagnostics:
  `total_hops = 3`, `compact_observed_hops = 2`,
  `hop_status_counts = {"midturn_compaction_observed": 2, "task_completed_without_target_compaction": 1}`
- compact-observed hop indexes: `[1, 2]`
- completed-without-target-compaction hop indexes: `[3]`
- retained probe verdicts after all three hops: `pass`

This fresh run matters because it proves repeated compaction can in fact be
observed again on hop 2 in the current harness. At the same time, it also means
the hop-level status pattern is still varying across runs, so the remaining
question is no longer "can repeated compaction happen at all?" but rather "how
stable and interpretable is the repeated-compaction pattern across copied
threads and reruns?"

## Second Normal-Source 3-Hop Confirmation

The migrated latest-upstream runner has now also completed a 3-hop `session`
smoke on a second structurally normal copied source thread.

Observed result on `2026-04-22` local time:

- source thread: `019d6961-5785-7520-903b-e4567b6e96a0`
- case: `s6961_benchmark_panel_scope_lock`
- variant: `session`
- chain hops: `3`
- hop status sequence:
  `midturn_compaction_observed`,
  `midturn_compaction_observed`,
  `midturn_compaction_observed`
- per-hop retained probe verdicts: `pass`, `pass`, `pass`
- hop 1 compact delta:
  replacement history shrank from `17` items / `6,975` estimated total tokens
  to `3` items / `1,776` estimated total tokens
- hop 2 compact delta:
  replacement history changed from `3` items / `1,776` estimated total tokens
  to `5` items / `4,895` estimated total tokens
- hop 3 compact delta:
  replacement history remained `5` items but still observed a fresh compact
  event, moving from `4,895` to `4,907` estimated total tokens

This second-source confirmation changes the acceptance posture materially. The
benchmark no longer has only a single copied-thread proof that repeated session
compaction can happen. It now has direct multi-hop positive evidence on two
different copied source threads, with the cleaner `019d6961` run showing fresh
compaction on hop 1, hop 2, and hop 3.

## Second Normal-Source Hop-1 Parity Breadth

The migrated latest-upstream runner has now also completed a broader hop-1
`chain_control` vs `session` parity sweep on the same second normal source
thread.

Observed result on `2026-04-22` local time:

- source thread: `019d6961-5785-7520-903b-e4567b6e96a0`
- compared variants: `chain_control`, `session`
- chain hops: `1`
- local cases covered: `5`
- cross-variant summary:
  `cross_variant_summary["1"]["session"] = {"baseline_variant": "chain_control", "comparable_cases": 5, "retained_vs_baseline_rate": 1.0, "baseline_reference_pass_rate": 1.0, "variant_pass_rate": 1.0}`
- per-hop combined verdicts:
  `case_count = 10`, `exact_pass_rate = 1.0`, `hard_fail_count = 0`
- category coverage in that one-hop breadth sweep:
  `exact_binding`, `latent_constraint_application`, `scope_lock`,
  `update_precedence`

Variant-specific hop diagnostics on that sweep:

- `chain_control`
  hop status `completed_without_target_compaction_tracking`, as expected for
  the non-session control
- `session`
  hop status `midturn_compaction_observed`, with `shape_valid = true` and
  compact-observed hop indexes `[1]`

This parity run matters because it upgrades the current evidence from a
single-case parity smoke to a full five-case hop-1 parity confirmation on a
second structurally normal copied source thread.

## Second Normal-Source Hop-2 Parity Breadth

The migrated latest-upstream runner has now also completed a clean all-case
hop-2 `chain_control` vs `session` parity sweep on the same second normal
source thread using the updated long-text continuation prompt.

Observed result on `2026-04-22` local time:

- source thread: `019d6961-5785-7520-903b-e4567b6e96a0`
- compared variants: `chain_control`, `session`
- chain hops: `2`
- local cases covered: `5`
- cross-variant summary:
  `cross_variant_summary["1"]["session"] = {"baseline_variant": "chain_control", "comparable_cases": 5, "retained_vs_baseline_rate": 1.0, "baseline_reference_pass_rate": 1.0, "variant_pass_rate": 1.0}`
  and
  `cross_variant_summary["2"]["session"] = {"baseline_variant": "chain_control", "comparable_cases": 5, "retained_vs_baseline_rate": 1.0, "baseline_reference_pass_rate": 1.0, "variant_pass_rate": 1.0}`
- per-hop combined verdicts:
  hop 1 `case_count = 10`, `exact_pass_rate = 1.0`, `hard_fail_count = 0`
  and hop 2 `case_count = 10`, `exact_pass_rate = 1.0`,
  `hard_fail_count = 0`
- hop diagnostics:
  `chain_control` remained the expected no-target-compaction control across
  both hops, while `session` observed a fresh compact event at hop 1 and then
  completed hop 2 as `task_completed_without_target_compaction`, with all hop-2
  retained probes still passing

This clean 2-hop breadth run closes the benchmark question that remained after
the earlier one-hop sweep: the session route now matches a clean
no-compaction control across all local cases at both hop 1 and hop 2 on a
normal copied source thread, without continuation-side tool noise.

## Acceptance Closure

At this benchmark stage, the acceptance story is treated as closed:

- repeated-compaction behavior is established on two normal copied source
  threads, `019d4e04...` and `019d6961...`
- parity against `chain_control` is established at the chosen breadth
  threshold: hop-1 parity exists on both normal copied threads, and `019d6961`
  now has clean all-case parity at both hop 1 and hop 2
- `019d5980...` is no longer an ambiguous blocker; it is explicitly treated as
  a `shape_harness_outlier` and kept in the diagnostic bucket rather than the
  acceptance-oracle bucket
- earlier continuation-side `exec_command` failures are now superseded by the
  clean long-text continuation harness, so they remain useful only as harness
  history, not as live evidence against the route

## Current Source-Thread Classification

The current copied-session sources should not be treated as equally trustworthy
for repeated-compaction sign-off:

- `019d4e04-eaa2-7311-a0e2-ad430b478151`
  current normal source-thread oracle; one-hop, parity, and repeated-compaction
  evidence all exist here
- `019d6961-5785-7520-903b-e4567b6e96a0`
  current normal source-thread oracle; three-hop run directly observed fresh
  compaction at hop 1, hop 2, and hop 3
- `019d5980-4f20-7861-9083-e3527d20a3d2`
  current shape/harness outlier; use it for timeout and diagnostics handling,
  not as the main repeated-compaction oracle or parity oracle

## Required Diagnostics

When the copied-session execution layer is migrated, the first-class diagnostics
should remain rollout-native:

- whether a target compact event was actually observed
- whether the turn completed without the target compact event
- whether the turn completed without usable turn artifacts
- compacted history shape validity
- retained replacement-history growth across hops
- per-hop status labels such as `midturn_compaction_observed` and
  `task_completed_without_target_compaction`
- hop-status counts and hop indexes for each source thread, so reruns can be
  compared without manually diffing raw rollout files
- source-thread shape classification, so structurally abnormal copied sessions
  are not mixed into the same acceptance bucket as normal repeated-compaction
  oracles
- when hop preparation has already failed or timed out, probe turns should be
  skipped and recorded as benchmark-harness `run_error` outputs rather than
  being executed as if the prepared thread were trustworthy
- whether a hop-local "no compact" outcome is considered an expected control
  result or an unsatisfied repeated-compaction target

These diagnostics are as important as case scoring because they distinguish
benchmark harness failures from real session-compact retention failures.

## External Reference Layer

External benchmarks are support references only. They help define vocabulary and
failure modes, but they do not replace copied-session acceptance.

- `LongBench` / `LongBench v2`
  broad long-context tasks, useful for general long-context and chain-stress
  vocabulary
  source: `https://github.com/THUDM/LongBench`
- `BABILong`
  noisy long-document retrieval/reasoning, useful for stress-style recall
  source: `https://github.com/booydar/babilong`
- `NoLiMa`
  non-literal long-context retrieval, useful against lexical-overlap shortcuts
  source: `https://github.com/adobe-research/NoLiMa`
- `LongMemEval`
  long-term chat memory, useful for update/temporal/abstention evaluation ideas
  source: `https://github.com/xiaowu0162/LongMemEval`
- `MemoryAgentBench`
  incremental agent memory, useful for conflict-resolution and long-range memory
  failure modes
  source: `https://github.com/HUST-AI-HYZ/MemoryAgentBench`
- `LoCoMo`
  long conversational memory, useful for multi-hop dialogue recall
  source: `https://github.com/snap-research/locomo`
- `AMA-Bench`
  long-horizon agent memory, useful for memory-construction and
  memory-retrieval failure modes in future work
  source: `https://github.com/AMA-Bench/AMA-Bench`

## Guardrails

- Never mutate live `~/.codex` session data while benchmarking.
- Use copied sessions only.
- Judge the new route by retained post-compact behavior, not by prose aesthetics.
- Treat hard failures as the first metric to minimize.
