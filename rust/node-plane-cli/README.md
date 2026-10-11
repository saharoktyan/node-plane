# Workstation development

Rust terminal/command-line administrator client with embedded SSH. User instructions: [Workstation](../../docs/workstation.md) ([Russian](../../docs/workstation.ru.md)).

## Build and run

```sh
cargo build --manifest-path rust/node-plane-cli/Cargo.toml --bin node-plane
rust/node-plane-cli/target/debug/node-plane
```

Run `node-plane --help` for command-line options. `--state-dir` selects private local data; `--no-tui` uses concise terminal output. Never use production connection state in destructive test fixtures.

## Source layout

- `tui/`: application state, event loop, view/widgets/themes, screens, input and actions.
- `connections.rs`: saved installation profiles and preferences.
- `ssh.rs`: SSH transport and session behavior.
- Domain modules: controller, nodes, access, temporary configurations and operation models.
- `workflow.rs` and related modules: command execution and progress.

Themes are JSON files copied from `tui/themes/` to the local state directory without overwriting existing files. Keep keyboard and pointer focus separate from activation. Restore parent focus by control identity after dynamic lists redraw.

## Checks

```sh
cargo test --manifest-path rust/node-plane-cli/Cargo.toml
cargo clippy --manifest-path rust/node-plane-cli/Cargo.toml --all-targets -- -D warnings
```

See [Testing](../../docs/development/testing.md) for platform/terminal acceptance and [Releases](../../docs/development/releases.md) for cargo-dist packaging.
