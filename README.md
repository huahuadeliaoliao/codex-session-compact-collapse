# Codex Compact Collapse

Codex Compact Collapse is a Codex-derived experimental source release focused on
conversation compaction for long-running coding sessions.

It adds an opt-in `collapse` compaction mode on top of the upstream summary
path, with the goal of preserving recent working-state structure more faithfully
while keeping default behavior unchanged unless the new mode is explicitly
enabled.

> [!WARNING]
> This repository is an independent derivative of the open-source OpenAI Codex
> project. It is not an official OpenAI release. This first public release is
> intentionally source-only: benchmark corpora, copied session artifacts, and
> private lab outputs are omitted pending sanitization.

## What Changed

- Added `compact_mode = "summary" | "collapse"` as an additive configuration
  switch.
- Added `compact_preserve_turns` for controlling how much recent context is
  preserved in collapse mode.
- Set the current collapse-mode default preserve window to `5` turns.
- Kept the default upstream behavior unchanged unless `compact_mode = "collapse"`
  is explicitly configured.
- Preserved the existing replacement-history compatibility path rather than
  introducing a new public session format.

## Quickstart

Build from source using the existing Codex build instructions:

- [Installing and building](./docs/install.md)
- [Configuration docs](./docs/config.md)
- [Contributing](./docs/contributing.md)

After building, run the local binary as usual.

To enable the experimental compaction path, add the following to
`~/.codex/config.toml`:

```toml
compact_mode = "collapse"
compact_preserve_turns = 5
```

If these keys are absent, Codex keeps the default summary compaction behavior.

## Repository Scope

This public snapshot includes:

- the modified Codex source tree
- config and protocol changes needed to expose the new compaction mode
- tests and general build documentation needed to inspect or build the change

This public snapshot intentionally does not include:

- benchmark case corpora
- copied session data
- isolated lab manifests
- benchmark result artifacts
- private execution journals

## Upstream Lineage

This repository is derived from the open-source
[`openai/codex`](https://github.com/openai/codex) codebase and remains under the
same Apache-2.0 licensing terms for the upstream material.

See [DERIVATION.md](./DERIVATION.md) for a short summary of the source lineage
and the public-release scope of this snapshot.

## License

This repository includes upstream Codex material under the
[Apache-2.0 License](./LICENSE). Additional attribution notices remain in
[NOTICE](./NOTICE).
