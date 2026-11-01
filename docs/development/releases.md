# Releases

Release channels are separate:

| Source branch | Channel | Version/tag | Workstation command |
| --- | --- | --- | --- |
| `dev` | `dev` | `vX.Y.Z-alpha.N` | `node-plane-dev` |
| `beta` | `beta` | `vX.Y.Z-beta.N`, `vX.Y.Z-rc.N` | `node-plane-beta` |
| `main` | `stable` | `vX.Y.Z` | `node-plane` |

The public-beta baseline is `0.5.0-beta.1`. Historical `1.0.0-alpha.*` and
`1.0.0-beta.*` builds precede that baseline; they are not supported upgrade
sources. Reinstall those experimental installations. Archival aliases use
`legacy/<original-tag>` and are never active release candidates.

`RELEASE_CHANNEL` declares the source channel. Its value must agree with the tag
stage. `VERSION`, `project.version` in `pyproject.toml`, the Workstation Cargo
version and their lockfiles must agree. Driver and Agent artifacts are built
from the same commit and selected through the controller release; they do not
independently follow a latest release.

Every eligible release publishes `RELEASE_METADATA.txt` with `format=2`,
`channel`, exact `tag`, `version` and full 40-character `commit`. The server
checksums include this file. Update discovery requires matching metadata;
older releases without explicit channel membership are excluded even when
their version sorts higher. Explicit tags follow the same checks. Source
installs additionally require matching channel/version files and branch ancestry.
Only `dev` supports an explicitly selected current branch head.

Promote changes by merging/cherry-picking the intended commits, then update the
version and `RELEASE_CHANNEL` on the target branch before tagging. Do not retag
the same artifact under a different version/channel. Channel switching is an
explicit operation and retains the existing compatibility/downgrade checks.


## Artifacts and CI

Publication first validates the channel, versions and tagged commit ancestry on its matching source branch. It then requires the reusable server/Python checks and the native Workstation matrix to pass for the tagged commit. Unit, real PostgreSQL and Docker suites run separately; unexpected Python test skips fail CI. See [Testing](testing.md#continuous-integration). All three Rust binaries have committed Cargo lockfiles; build and test with `--locked`. Python installation and CI use the committed `pyproject.toml` and `uv.lock` through `uv sync --locked`. Controller archives must contain both files; installers reject incomplete dependency manifests.

`dist-workspace.toml` configures cargo-dist for Workstation. The release workflow builds Linux x64, macOS Intel/Apple Silicon and Windows x64 archives, shell/PowerShell installers and checksums.

The server job builds the controller archive plus Linux amd64 driver/agent archives. Server artifacts run alongside Workstation builds and join the same GitHub release. Rust dependency caches are shared across tag builds. Pull requests run release planning; pushing a version tag triggers publication.

The controller archive contains the Python application, operational scripts and runtime assets. Its extraction policy validates paths/types and required resources without rejecting additional safe files merely because the installer does not know their names. Do not use a developer source archive as an installed controller runtime.

The supported Python dependency layout is `pyproject.toml` plus `uv.lock`. Older installed updaters that require the removed dependency exports cannot validate this archive layout; those pre-beta installations require a fresh installation. Supported update acceptance starts from the public-beta baseline.

Release archives package legal notices alongside the binaries and controller runtime. The root `LICENSE` covers Node Plane itself, while `THIRD_PARTY_LICENSES` contains third-party notices generated from locked Rust crate graphs using `cargo-about` (primary tool version 0.9.2). All pre-releases and stable releases carry these notices. These notices apply to the compiled Rust binaries and repository distribution; they do not cover Python packages or host system libraries downloaded and installed separately by the runtime environment.

Refresh notices whenever Rust dependencies or supported targets change:

```sh
cargo install cargo-about --version 0.9.2 --locked
python3 scripts/generate_third_party_licenses.py
python3 scripts/generate_third_party_licenses.py --check
```

Commit the generated root file with the dependency changes. `--offline` skips Cargo downloads when dependencies are cached. Generation preserves bundled notices and verified upstream supplements in `licenses/upstream/`; a missing license requires a reviewed supplement rather than a placeholder. Standard-library notices in `licenses/` must follow the pinned Rust version. Release CI checks freshness before publication.

For the first stable release, use the [1.0.0 acceptance checklist](release-1.0.0-acceptance.md). Automated results and manual acceptance are recorded separately.

## Prepare a release

1. Finish and commit the intended changes on the correct branch.
2. Update `VERSION`, `project.version` in `pyproject.toml`, affected Cargo manifests/lockfiles and `CHANGELOG.md`. Run `uv lock --python 3.12` and `uv run --locked python scripts/check_tls_dependencies.py`; commit the updated manifest and lockfiles.
3. Run relevant Python/Rust tests and platform checks. Include migration/rollback validation for schema changes.
4. Review install/update scripts and ensure required runtime resources are packaged.
5. Commit the version changes with a clean working tree.
6. Create the tag with the release helper, then push the branch and tag.

```sh
./scripts/tag_release.sh vX.Y.Z-alpha.N --no-build
git push origin dev
git push origin vX.Y.Z-alpha.N
```

Use the actual version instead of the placeholder. `--no-build` lets CI produce artifacts; omit it for local packaging. Do not use `--skip-tests` as the normal release path. `--no-tag` prepares without creating a tag; `--publish` uploads local artifacts to an existing CI-created release.

## Verify publication

- All Workstation targets and server jobs passed.
- The GitHub release has platform installers/archives, controller and server binaries, checksums and metadata.
- Installer scripts resolve the new release in its channel.
- A clean controller install and an update from the supported retained release pass health checks.
- The bot and Workstation can open status, create a node and perform an authorized mutation.

A successful compile is not Windows/macOS interactive acceptance. Check PATH, non-admin local execution, SSH, clipboard and terminal behavior on the actual platform.

## Compatibility and rollback

Published tags must keep pointing to the commit used to build their artifacts. Rewriting authors or ancestry changes commit IDs even when files stay the same. Moving a published tag without rebuilding its artifacts causes controller archive verification to refuse the release. Import older history on a separate branch or merge it without rewriting published commits; publish a new version after any necessary rewrite.

On older controllers, a completed transient update unit removed by systemd's `--collect` can leave an unfinished update gate. `scripts/recover_collected_update.py` is a standalone repair for a failed core update with a successful rollback receipt. Copy it to the host and run it from the installed controller directory using `PYTHONPATH=app .venv/bin/python /path/to/recover_collected_update.py OPERATION_UUID --shared-dir /opt/node-plane/shared`. Use the actual shared directory of that installation. The utility selects its runtime paths before loading the installed configuration; an interactive shell does not inherit systemd's environment. It takes the worker lock, checks the exact job/unit/gate, refuses an existing unit or incomplete rollback, records the recovery, and never runs the installer. It does not repair mismatched release artifacts.

Use matching release artifacts unless a combination has explicit acceptance evidence. An application rollback does not downgrade PostgreSQL. Respect migration `minimum_reader` and driver/agent journal format compatibility. Keep journals and the actual previous working release.

Record tested OS, component versions, failure scenarios and limits in release evidence. Changelog entries describe user-visible changes; user guides describe the current behavior without release-history narration.

## Workflow maintenance

Release dependency caches are workflow artifacts named `rust-deps-*`, not release assets. They expire after seven days and are restored from successful push-triggered Release runs. Platform, runner architecture, toolchain and target distinguish them. Project binaries, incremental output and release archives are excluded; cache failure must not fail publication.

GitHub's normal branch-scoped cache does not share entries between sibling tag refs, so release workflows use cross-run artifacts. Branch checks can use rust-cache.

When upgrading cargo-dist, generate a baseline separately and retain the custom cache steps and `actions: read` permission. `allow-dirty = ["ci"]` allows the customized workflow. Keep the `local-artifacts-jobs` server job in the dependencies of global packaging and publication. CI's `scripts/build_release_stack.sh --from-ci` consumes fetched server binaries; local builds compile them.

## Archiving experimental release tags

Prepare an inventory of remote refs and GitHub release IDs, bodies and asset
URLs before changing anything. Preserve each original tag object/commit and
keep a local backup of the old refs. The archive name is
`legacy/v1.0.0-alpha.N` or `legacy/v1.0.0-beta.N`; embedded artifact versions stay
unchanged. Update the associated GitHub Release tag and mark it as a prerelease;
renaming only the visible title does not remove the old release from catalogs.
Verify assets and new refs before deleting the old refs.

A historical tag push can run the workflow stored in that historical commit.
The current workflow excludes `legacy/**`, but that exclusion does not protect
old commits. During an authorized remote archival, temporarily disable the
Release workflow, create and verify archive refs, move releases, remove the
original refs and restore the workflow's previous state. Do not perform this
alongside an active release run. Immutable releases require a different archival
procedure and must not be force-moved. Record each completed step so an
interrupted migration can resume safely.
