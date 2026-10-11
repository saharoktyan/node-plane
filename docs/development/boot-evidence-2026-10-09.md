# Systemd boot test evidence: 2026-10-09

Recorded in a disposable Debian 12 QEMU guest inside a disposable Docker container. Guest SSH was forwarded only to host loopback. No host systemd services were installed or restarted.

Guest: kernel 6.1.0-53-cloud-amd64, systemd 252.39-1~deb12u2, Docker 20.10.24+dfsg1. Agent: portable v0.4.3-alpha.59 Linux amd64 release. Runtime helpers/supervisors came from the checkout under test. Xray: 26.3.27; AWG: project wrapper with the pinned amneziavpn/amneziawg-go:3.1.20260828 base.

| Scenario | Observed result |
| --- | --- |
| Kernel reboot | Agent, supervisors and expiry timer started; overdue AWG/VLESS identities removed; permanent/valid temporary identities retained |
| Agent stopped | Independent expiry removed overdue identities while the agent remained stopped |
| Docker restart | Supervisors restored containers and enforcement health |
| Reboot with timer disabled | Startup guards removed overdue identities before protocol startup; enforcement health remained false until timer enablement/reconciliation |

Two reboots changed the kernel boot ID. Checks inspected the live Xray API for TCP/XHTTP, AWG config/peer count, journal state and service/enforcement health. Only disposable deadlines were shortened; the system clock was unchanged.

These results cover node-local enforcement on this fixture. They do not establish full controller installation, Ubuntu boot, Internet REALITY handshakes or established VLESS tunnel termination. The VM/container, disks, exports and temporary SSH material were removed and the forwarded port was confirmed closed.

Reproduction: [Development and testing](testing.md#actual-systemd-boot).
