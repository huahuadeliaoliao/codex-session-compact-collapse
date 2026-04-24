# Derivation Notes

This repository is derived from the open-source
[`openai/codex`](https://github.com/openai/codex) project.

## Source Lineage

- Upstream base: OpenAI Codex `rust-v0.124.0`
- Published focus: an additive `compact_strategy = "session"` route for
  session-level compact-collapse
- Repository status: independent experimental source release, not an official
  OpenAI distribution

## Scope of This Public Snapshot

Included:

- Codex source changes for the opt-in session compact route
- configuration and protocol plumbing for `compact_strategy`
- prompt contract and bounded frontier logic for durable session state
- source-tree tests covering route selection, remote bypass, replay behavior,
  and image sidecar boundaries
- docs and a small benchmark harness manifest suitable for public review

Omitted:

- private copied-session rollouts
- local benchmark result JSON files
- isolated lab homes
- private execution journals and environment manifests

## Positioning

The implementation is designed to keep default Codex behavior unchanged unless
the session compact strategy is enabled. It is intended as a reviewable source
snapshot for the compact-collapse experiment rather than a packaged end-user
release.
