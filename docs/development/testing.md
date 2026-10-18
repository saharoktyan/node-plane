# Development and testing

Use a source checkout and isolated data. Do not point tests at production databases, journals or Docker resources.

## Python

Use Python 3.11 or 3.12:

```sh
uv sync --locked --python 3.12
uv run --locked python scripts/run_ci_tests.py unit
```

Use uv 0.12.23, the same version as CI and the controller installer. `pyproject.toml` declares the common dependencies and the `backend`, `telegram` and `dev` groups; `uv.lock` pins the full dependency graph for Python 3.11/3.12. A normal `uv sync` includes all three groups. Deployment explicitly selects only `backend` and `telegram`, and creates a separate `.venv` for each release.

To change dependencies, edit `pyproject.toml`, then run:

```sh
uv lock --python 3.12
uv run --locked python scripts/export_python_requirements.py
uv run --locked python scripts/export_python_requirements.py --check
```

Commit the manifest, lockfile and generated exports together. The `requirements*.txt` files are compatibility exports for already published installers; do not edit them by hand. Install/update and CI use `uv sync --locked`, which rejects a stale lockfile instead of selecting new versions during deployment. Source files still run through `PYTHONPATH=app`; this project is not installed as a Python package.

Select narrower test modules while developing. PostgreSQL integration requires a disposable DSN and a separate process; see [Database](database.md).

## Continuous integration

Every pull request and push to `dev`/`main` runs the server/Python and Workstation workflows. Tag releases call the same workflows and require both to succeed before packaging and publication.

- Python unit/contract tests run on Python 3.11 and 3.12, with all runtime/development dependencies and uv installed.
- All `test_*_postgres.py` modules run separately on both Python versions against a disposable PostgreSQL 16 service. They cover migrations, races, recovery, delivery, workers and process interruption. Unit tests' database doubles never share that process.
- Docker tests run against the pinned Xray/AWG images and a freshly built agent: protocol traffic, expiry/revocation, cleanup, mTLS and SIGKILL recovery. Only disposable test resources are used.
- Driver and agent run format, clippy and their full Rust tests. Existing server clippy warnings remain visible; Workstation clippy rejects warnings.
- Workstation runs its existing native tests and installer checks on Linux, macOS and Windows, on every change because backend contracts can affect it too.

The Python CI runner discovers every `test_*.py` module. PostgreSQL modules use the `_postgres` suffix; Docker modules use `_docker` or the explicit agent-fault module. Missing dependencies, empty suites and unexpected skipped tests fail the check. Run each suite in a fresh process:

```sh
.venv/bin/python scripts/run_ci_tests.py unit
NODE_PLANE_TEST_POSTGRES_DSN=postgresql://... .venv/bin/python scripts/run_ci_tests.py postgres
NODE_PLANE_TEST_DOCKER=1 .venv/bin/python scripts/run_ci_tests.py docker
```

The Docker suite requires the cached pinned images, `node-plane-release-builder:bookworm` built from `scripts/Dockerfile.release`, and a current debug agent binary. Real QEMU boot checks below and interactive Telegram/terminal acceptance remain separate release acceptance steps; automated CI does not certify them.

## Rust

```sh
cargo test --locked --manifest-path rust/node-plane-cli/Cargo.toml
cargo clippy --locked --manifest-path rust/node-plane-cli/Cargo.toml --all-targets -- -D warnings
cargo test --locked --manifest-path rust/node-driver/Cargo.toml
cargo test --locked --manifest-path rust/node-agent/Cargo.toml
```

Socket/wire tests need permission to create local listeners. A restricted environment may prevent those tests; record exclusions rather than describing a partial run as full integration acceptance.

## Local backend

Load a private development environment with PostgreSQL settings and initialize the schema:

```sh
PYTHONPATH=app .venv/bin/python -m backend.admin_cli init-schema
PYTHONPATH=app .venv/bin/python -m uvicorn backend.http_api:application --factory --host 127.0.0.1 --port 8080 --no-access-log
```

Read `/health/live`, `/health/ready` and `/openapi.json`. Readiness validates schema compatibility; it does not prove every node is reachable. Run `backend.admin_cli --help` for trusted account/token setup commands.

## Failure scenarios

Check duplicate requests, stale revisions, interrupted driver/agent calls, worker restarts, missing runtime resources, agent startup delays and unknown removal outcomes. Confirm unrelated profiles/containers survive. Inspect both backend state and node journals; a UI label alone is not execution evidence.

For temporary access, shorten only disposable fixture deadlines. Preserve product durations and the host clock. Test expiry with the agent/controller stopped, failed deletion and guarded protocol startup.

## Actual systemd boot

Use a disposable Debian 12 QEMU guest. Do not mount the host Docker socket or production runtime directories. Forward guest SSH only to host loopback. Prepare the explicit fixture hostname `np-boot-test` and `/root/boot-fixture-ready` marker.

Copy these files into `/root` in the guest:

- [`systemd_boot_prepare.sh`](../../tests/systemd_boot_prepare.sh) and [`systemd_boot_guest.py`](../../tests/systemd_boot_guest.py).
- The release `node-plane-agent` binary.
- `runtime.tar`, with current runtime assets under `node-plane-runtime`.
- `xray-image.tar` and `awg-image.tar`, exported from the release's pinned runtime images; inspect the preparation script for required base-image retagging.

Inside the guest:

```sh
sh /root/systemd_boot_prepare.sh
python3 /root/systemd_boot_guest.py arm
systemctl reboot
```

Reconnect, then run:

```sh
python3 /root/systemd_boot_guest.py verify
python3 /root/systemd_boot_guest.py offline
python3 /root/systemd_boot_guest.py docker-restart
systemctl start node-plane-agent
python3 /root/systemd_boot_guest.py verify
python3 /root/systemd_boot_guest.py arm-guard
systemctl reboot
```

Reconnect and run `python3 /root/systemd_boot_guest.py verify-guard`. Record boot-ID changes, service states, live peer/authorization state and preserved permanent identities.

Power off and delete the guest/container, disks, exported images and temporary SSH material. Verify the forwarded port is closed. This checks real boot and node-local enforcement; test Internet tunnels and established VLESS sessions separately.

## Interface acceptance

Test keyboard focus, mouse hover, scrolling/resizing, drafts and back navigation in a small and large terminal. Test Linux, macOS and Windows independently. Check Telegram on Android, iOS and desktop, including copyable artifacts and notifications that preserve the current menu.
