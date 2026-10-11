# Development and testing

Use a source checkout and isolated data. Do not point tests at production databases, journals or Docker resources.

## Python

Use Python 3.11 or 3.12:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt -r requirements-dev.txt
PYTHONPATH=app .venv/bin/python -m unittest discover -s tests
```

Select narrower test modules while developing. PostgreSQL integration requires a disposable DSN and a separate process; see [Database](database.md).

## Rust

```sh
cargo test --manifest-path rust/node-plane-cli/Cargo.toml
cargo clippy --manifest-path rust/node-plane-cli/Cargo.toml --all-targets -- -D warnings
cargo test --manifest-path rust/node-driver/Cargo.toml
cargo test --manifest-path rust/node-agent/Cargo.toml
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
