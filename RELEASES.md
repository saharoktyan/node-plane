# Releases

This document defines the release and versioning policy for Node Plane.

The goal is to keep the process simple:
- one stable branch
- one integration branch
- predictable version numbers
- clear rules for alpha builds and stable releases

## Branches

### `main`

Stable branch.

Rules:
- only stable releases live here
- versions on `main` use `x.y.z`
- production should track `main`

Examples:
- `0.1.0`
- `0.1.1`
- `0.2.0`

### `dev`

Integration branch.

Rules:
- new work is merged here first
- versions on `dev` use `x.y.z-alpha.N`
- staging or test deployments should track `dev`

Examples:
- `0.2.0-alpha.1`
- `0.2.0-alpha.2`
- `0.2.0-alpha.3`

### `feature/*`

Short-lived working branches.

Rules:
- branch from `dev`
- merge back into `dev`
- do not release directly from `feature/*`

Examples:
- `feature/xray-traffic-fix`
- `feature/admin-ux-cleanup`
- `feature/server-metrics`

## Version Format

Node Plane uses Semantic Versioning with prerelease labels.

Stable releases:

```text
x.y.z
```

Alpha releases:

```text
x.y.z-alpha.N
```

Examples:
- `0.1.0`
- `0.1.1`
- `0.2.0-alpha.1`
- `0.2.0-alpha.2`
- `0.2.0`

The project version is stored in:

- `VERSION`

## How To Increment Versions

### Patch: `x.y.z`

Increase `z` for:
- bug fixes
- small UX fixes
- minor internal improvements
- low-risk maintenance changes

Examples:
- `0.1.0` -> `0.1.1`
- `0.1.1` -> `0.1.2`

### Minor: `x.y.z`

Increase `y` for:
- new features
- new screens
- new admin actions
- meaningful workflow improvements

Examples:
- `0.1.2` -> `0.2.0`
- `0.2.0` -> `0.3.0`

### Major: `x.y.z`

Increase `x` for:
- breaking changes
- incompatible config changes
- incompatible storage or deployment changes
- anything that requires explicit migration or coordination

Examples:
- `0.9.4` -> `1.0.0`
- `1.3.2` -> `2.0.0`

## Alpha Version Rules

Alpha builds are used on `dev`.

Rules:
- start a new development cycle with `x.y.z-alpha.1`
- bump the alpha counter after a meaningful batch of changes
- keep the base version stable until release

Example:

```text
main: 0.1.0
dev:  0.2.0-alpha.1
dev:  0.2.0-alpha.2
dev:  0.2.0-alpha.3
main: 0.2.0
```

Recommended interpretation:
- `alpha.1` — first testable build for the next release
- `alpha.2+` — additional testable builds after fixes or new changes

## Release Tags

Stable releases should always be tagged.

Format:

```text
vX.Y.Z
```

Examples:
- `v0.1.0`
- `v0.1.1`
- `v0.2.0`

Alpha tags are optional.

If used, keep the same format:
- `v0.2.0-alpha.1`
- `v0.2.0-alpha.2`

Recommended minimum:
- tag every stable release
- alpha tags only when they add real value

## Release Workflow

### Normal feature work

1. Create a branch from `dev`.
2. Implement the change in `feature/*`.
3. Merge the change into `dev`.
4. If the result should be tested as a new build, bump `VERSION` on `dev`.
5. Deploy or test from `dev`.

### Starting a new release cycle

After shipping a stable release from `main`:

1. Decide the next target version.
2. Update `VERSION` on `dev` to the next alpha.

Example:
- current stable: `0.1.0`
- next cycle on `dev`: `0.2.0-alpha.1`

### Publishing a stable release

1. Ensure `dev` is in a releasable state.
2. Update `VERSION` from `x.y.z-alpha.N` to `x.y.z`.
3. Merge the release into `main`.
4. Create a git tag `vX.Y.Z`.
5. Deploy production from `main`.

## Tagging Rule

Release tags and `VERSION` must always match.

Examples:
- tag `v0.3.1-alpha.3` requires `VERSION=0.3.1-alpha.3`
- tag `v0.4.0` requires `VERSION=0.4.0`

This repository includes a helper:

- `scripts/tag_release.sh`

The helper:
- validates the tag format
- checks that `VERSION` matches the tag without the `v` prefix
- refuses to tag a dirty tracked worktree
- refuses to overwrite an existing tag

Example:

```bash
./scripts/tag_release.sh v0.3.1-alpha.3
```

Recommended release order:

1. Update `VERSION`.
2. Commit the version bump.
3. Run `./scripts/tag_release.sh vX.Y.Z[-alpha.N]`.
4. Push the branch and tag.

## Recommended Branch Targets

Use this as the default:

- production: `main`
- staging or test bot: `dev`
- daily work: `feature/*`

## Practical Examples

### Example 1: small bugfix after `0.1.0`

If the next release is only a fix release:

- `main`: `0.1.0`
- `dev`: `0.1.1-alpha.1`
- after testing:
  - `main`: `0.1.1`
  - tag: `v0.1.1`

### Example 2: next feature release after `0.1.0`

- `main`: `0.1.0`
- `dev`: `0.2.0-alpha.1`
- more changes:
  - `0.2.0-alpha.2`
  - `0.2.0-alpha.3`
- release:
  - `main`: `0.2.0`
  - tag: `v0.2.0`

## Minimal Policy

If you want the short version, it is this:

- `main` is stable
- `dev` is prerelease
- `feature/*` branches merge into `dev`
- `main` uses `x.y.z`
- `dev` uses `x.y.z-alpha.N`
- stable releases are tagged as `vX.Y.Z`
- `VERSION` is the source of truth for the app version
