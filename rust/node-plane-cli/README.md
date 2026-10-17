# Workstation development

Rust terminal/command-line administrator client with embedded SSH. User instructions: [Workstation](../../docs/workstation.md) ([Russian](../../docs/workstation.ru.md)).

## Build and run

For a published alpha, use the [dev installation commands](../../README.md#development-builds). To install the current `dev` branch without a GitHub Release, follow [Build from source](../../docs/workstation.md#build-from-source).

```sh
cargo build --manifest-path rust/node-plane-cli/Cargo.toml --bin node-plane
rust/node-plane-cli/target/debug/node-plane
```

Run `node-plane --help` for command-line options. `--state-dir` selects private local data; `--no-tui` uses concise terminal output. Never use production connection state in destructive test fixtures.

Build channel follows the package version: prereleases are dev, stable versions are stable. Installation and controller operations enforce this channel. Installed commands are `node-plane-dev` and `node-plane`; local state and updater receipts are separate. See the user guide for profile copying and explicit controller transitions.

## Source layout

- `tui/`: application state, event loop, view/widgets/themes, screens, input and actions.
- `connections.rs`: saved installation profiles and preferences.
- `ssh.rs`: SSH transport and session behavior.
- Domain modules: controller, nodes, access, temporary configurations and operation models.
- `workflow.rs` and related modules: command execution and progress.

Custom themes are supported in the local state directory’s `themes/` subdirectory.

## Checks

```sh
cargo test --manifest-path rust/node-plane-cli/Cargo.toml
cargo clippy --manifest-path rust/node-plane-cli/Cargo.toml --all-targets -- -D warnings
```

See [Testing](../../docs/development/testing.md) for platform/terminal acceptance and [Releases](../../docs/development/releases.md) for cargo-dist packaging.
