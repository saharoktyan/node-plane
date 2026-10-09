# Isolated systemd boot checks

Executed on 2026-10-09 in a disposable Debian 12 QEMU guest. QEMU ran inside
a disposable Docker container; only the guest SSH port was forwarded, to
host loopback. No host systemd services were installed or restarted.

Guest: kernel `6.1.0-53-cloud-amd64`, systemd `252.39-1~deb12u2`, Docker
`20.10.24+dfsg1`. The agent was the portable `v0.4.3-alpha.59` Linux amd64
release binary. Runtime helpers and protocol supervisors came from the current
checkout. Xray was `26.3.27`; AWG used the production wrapper and pinned
`amneziavpn/amneziawg-go:3.1.20260828` base image.

## Results

| Case | Result |
| --- | --- |
| Actual guest kernel reboot | Agent, protocol supervisors and expiry timer started through systemd. Overdue AWG/VLESS identities were removed; permanent and valid temporary identities survived. |
| Agent stopped | Independent systemd expiry removed both protocols' overdue identities while the agent remained stopped. No controller/backend/driver process was installed in the guest. |
| Docker daemon restart | Protocol supervisors restored their containers and enforcement health without manual container starts. |
| Second kernel reboot, timer disabled | Startup guards removed overdue identities before any expiry scan. Permanent and valid identities survived. Enforcement health correctly remained false; enabling the timer completed journal reconciliation. |

Real kernel boot IDs changed from
`a8ecbb36-13fc-49ee-a552-241efeb940f7` to
`6992f686-bf85-448d-9976-47e44c94c957`, then to
`b1f6f52e-ff6a-426e-aa6a-cd01109a3ea3`.
Checks inspected the live Xray API for both TCP/XHTTP inbounds, AWG configuration
and live peer count, durable journal status, service state and enforcement health.

The real Debian Docker builder exposed an AWG Dockerfile failure:
`unknown instruction: FS.FILE-MAX`. Replacing Dockerfile heredoc RUN blocks with
portable `printf` commands fixed it; the image then built and ran in this guest.

The VM was powered off and its Docker container, disk, seed image, temporary SSH
keys, known-host file and image exports were removed. The forwarded port was
confirmed closed.

## Scope

This exercises node-local enforcement and real guest systemd/kernel startup.
It does not test a complete controller installer, Ubuntu boot, an Internet
REALITY handshake or an already established VLESS tunnel. Existing VLESS
connections may continue after authorization removal until they disconnect.
Live AWG traffic cutoff is covered separately by Docker runtime tests.

Only disposable journal deadlines were shortened to two seconds. Product
durations remain 12h / 1d / 3d; no system clock was changed.

## Manual reproduction

Use a fresh Debian 12 QEMU guest with hostname `np-boot-test`, Docker, Python 3,
OpenSSL and SSH. Create `/root/boot-fixture-ready` explicitly. Do not mount host
runtime directories or the host Docker socket into the guest. Use temporary
SSH credentials and forward SSH only to host loopback.

Copy these files to `/root` in the guest:

- `systemd_boot_prepare.sh` and `systemd_boot_guest.py` from this directory.
- `node-plane-agent`, extracted from the portable release binary archive.
- `runtime.tar`, containing current `runtime_assets` under the archive directory
  `node-plane-runtime` (matching production delivery).
- `xray-image.tar` and `awg-image.tar`, exported with `docker save` from the exact
  images above. Export the AWG base by image ID; its digest reference may not
  survive save/load. Preparation retags the loaded base locally without changing
  its contents.

Run inside the guest:

```sh
sh /root/systemd_boot_prepare.sh
python3 /root/systemd_boot_guest.py arm
systemctl reboot
```

Reconnect after boot, then run:

```sh
python3 /root/systemd_boot_guest.py verify
python3 /root/systemd_boot_guest.py offline
python3 /root/systemd_boot_guest.py docker-restart
systemctl start node-plane-agent
python3 /root/systemd_boot_guest.py verify
python3 /root/systemd_boot_guest.py arm-guard
systemctl reboot
```

Reconnect and run `python3 /root/systemd_boot_guest.py verify-guard`. This checks
the disabled-timer state, enables the timer and verifies final reconciliation.
Both scripts refuse execution outside the explicitly prepared QEMU guest.
Finally power off the guest and delete its container, disks, exported images and
temporary credentials; confirm its forwarded port is closed.
