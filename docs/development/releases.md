# Releases

`dev` publishes development releases (`X.Y.Z-alpha.N`); `main` publishes stable releases (`X.Y.Z`). Tags use the `v` prefix. `VERSION`, Rust package versions and the changelog must describe the same release.

## Artifacts and CI

Publication requires the reusable server/Python checks and the native Workstation matrix to pass for the tagged commit. Unit, real PostgreSQL and Docker suites run separately; unexpected Python test skips fail CI. See [Testing](testing.md#continuous-integration). All three Rust binaries have committed Cargo lockfiles; build and test with `--locked`.

`dist-workspace.toml` configures cargo-dist for Workstation. The release workflow builds Linux x64, macOS Intel/Apple Silicon and Windows x64 archives, shell/PowerShell installers and checksums.

The server job builds the controller archive plus Linux amd64 driver/agent archives. Server artifacts run alongside Workstation builds and join the same GitHub release. Rust dependency caches are shared across tag builds. Pull requests run release planning; pushing a version tag triggers publication.

The controller archive contains the Python application, operational scripts and runtime assets. Its extraction policy validates paths/types and required resources without rejecting additional safe files merely because the installer does not know their names. Do not use a developer source archive as an installed controller runtime.

For the first stable release, use the [1.0.0 acceptance checklist](release-1.0.0-acceptance.md). Automated results and manual acceptance are recorded separately.

## Prepare a release

1. Finish and commit the intended changes on the correct branch.
2. Update `VERSION`, affected Cargo manifests/lockfiles and `CHANGELOG.md`.
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

Use matching release artifacts unless a combination has explicit acceptance evidence. An application rollback does not downgrade PostgreSQL. Respect migration `minimum_reader` and driver/agent journal format compatibility. Keep journals and the actual previous working release.

Record tested OS, component versions, failure scenarios and limits in release evidence. Changelog entries describe user-visible changes; user guides describe the current behavior without release-history narration.

## Workflow maintenance

Release dependency caches are workflow artifacts named `rust-deps-*`, not release assets. They expire after seven days and are restored from successful push-triggered Release runs. Platform, runner architecture, toolchain and target distinguish them. Project binaries, incremental output and release archives are excluded; cache failure must not fail publication.

GitHub's normal branch-scoped cache does not share entries between sibling tag refs, so release workflows use cross-run artifacts. Branch checks can use rust-cache.

When upgrading cargo-dist, generate a baseline separately and retain the custom cache steps and `actions: read` permission. `allow-dirty = ["ci"]` allows the customized workflow. Keep the `local-artifacts-jobs` server job in the dependencies of global packaging and publication. CI's `scripts/build_release_stack.sh --from-ci` consumes fetched server binaries; local builds compile them.
