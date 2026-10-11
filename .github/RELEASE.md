# Release workflow

The cargo-dist workflow builds Workstation for its configured targets and the reusable server workflow builds controller, driver and agent artifacts. Global packaging and publication require successful component builds.

See [Releases](../docs/development/releases.md) for versioning, tagging, artifact verification and dependency-cache maintenance. Distribution configuration is in [`dist-workspace.toml`](../dist-workspace.toml); workflows are [`release.yml`](workflows/release.yml) and [`release-server.yml`](workflows/release-server.yml).
