# Codex Session Compact Collapse

This repository is a Codex-derived experimental source release for session-level
compact-collapse. It is based on OpenAI Codex `rust-v0.124.0` and adds an
opt-in compaction route designed for long-running coding sessions.

The goal is durable continuity after repeated `/compact` or auto-compaction
events: instead of producing a one-off handoff summary, the session route asks
the model to maintain a stable `<session_compact_state>` block with active
memory, inactive changes, current handoff, and evidence pointers.

> [!WARNING]
> This is an independent derivative of the open-source OpenAI Codex project.
> It is not an official OpenAI release.

## What Changed

- Added `compact_strategy = "session"` as an explicit opt-in route.
- Routed manual `/compact` and auto compact through the same selector.
- Ensured the session strategy wins before provider-based remote compaction.
- Added a dedicated session-compact prompt contract under
  `codex-rs/core/templates/session_compact/prompt.md`.
- Preserved continuity through `replacement_history` while keeping a bounded
  recent structured frontier.
- Added sidecar metadata for recent user images without replaying old images
  into later prompts.
- Added docs and benchmark harness material for repeated compact recovery
  checks.

Default Codex behavior remains unchanged unless the session strategy is
explicitly enabled.

## Enable It

Build from source using the existing Codex build flow:

- [Installing and building](./docs/install.md)
- [Configuration docs](./docs/config.md)
- [Contributing](./docs/contributing.md)

Then add this to `~/.codex/config.toml`:

```toml
compact_strategy = "session"
```

With this setting, both manual `/compact` and automatic compaction use the
session route. Without it, Codex keeps the upstream default compact behavior.

## Design Notes

- [Active contract](./docs/session-compact-active-contract.md)
- [Design harness](./docs/session-compact-design-harness.md)
- [RCR benchmark notes](./docs/session-compact-rcr-benchmark.md)
- [Staged orchestration notes](./docs/session-compact-staged-orchestration.md)

The benchmark runner is available at:

```shell
python3 scripts/session_compact_rcr_benchmark.py --help
```

The included benchmark manifest is intentionally small and source-reviewable.
Private copied-session artifacts and local result journals are not included.

## Upstream Lineage

This repository is derived from the open-source
[`openai/codex`](https://github.com/openai/codex) codebase and remains under the
same Apache-2.0 licensing terms for upstream material.

See [DERIVATION.md](./DERIVATION.md) for source lineage and public-release
scope.

## License

This repository includes upstream Codex material under the
[Apache-2.0 License](./LICENSE). Additional attribution notices remain in
[NOTICE](./NOTICE).
