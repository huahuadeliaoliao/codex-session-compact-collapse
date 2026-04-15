# Derivation Notes

This repository is derived from the open-source
[`openai/codex`](https://github.com/openai/codex) project.

## Scope of This Public Snapshot

This first public release is intentionally limited to the source changes needed
to inspect, build, and review the compaction implementation itself.

Included:

- Codex source changes related to the additive `collapse` compaction mode
- configuration and protocol plumbing for enabling the mode
- unit and integration tests that belong to the source tree
- general build and usage documentation already suitable for public release

Omitted for now:

- benchmark corpora derived from real long sessions
- copied session rollouts and isolated lab homes
- local benchmark result JSON files
- private execution journals and environment manifests

## Positioning

This repository is not an official OpenAI release. It is a derived,
independently published source snapshot intended to make the implementation
itself reviewable before the evaluation assets are sanitized for public release.
