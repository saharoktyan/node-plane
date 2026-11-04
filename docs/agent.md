[Русская версия](agent.ru.md)

# Node agent on the server

The node agent is the only Node Plane component on a VPN server. The controller manages the server only through the agent's API over mutual TLS. This page describes what the install command does, how the agent meets the controller, and why it runs as root.

## Requirements

- Linux x86_64 with systemd.
- `curl` or `wget`, `tar` and `sha256sum`.
- Debian or Ubuntu for automatic installation of Docker and the speed limit tools (`iproute2`, `nftables`, `util-linux`).
- The controller must reach the agent port, TCP 50061 by default (`NODE_AGENT_PORT` on the controller). If the provider has its own firewall, allow that port. The controller connects to the node's client address.

## What the install command does

**Server setup** shows a one-time command, or Workstation's **Auto** runs it over SSH. Run it as root. The command:

1. Downloads `node-plane-agent-installer.sh` from the GitHub release of the controller's version and checks its SHA-256. The controller refuses to issue a command when the released installer differs from its own copy.
2. The installer downloads the agent archive of the same release, checks the SHA-256 given in the command and installs `/usr/local/bin/node-plane-agent`.
3. Installs missing speed limit tools with `apt-get`. Without them the node only reports that it cannot limit speed.
4. Writes `/etc/node-plane/agent.toml` and the token to `/etc/node-plane/tls/enrollment.token`. A previous agent key and certificate are removed.
5. If `ufw` is active, opens the agent port.
6. Installs and starts the `node-plane-agent` systemd service.

The controller gets no SSH access, no SSH key and no sudo. Workstation's **Auto** logs in once with your key or password, uses `sudo` when the login is not root, and saves nothing.

## Enrollment

The agent generates its private key on the server; the key never leaves it. Until it has a certificate, the agent serves only enrollment on its port.

The controller connects to the agent; the agent never connects to the controller, so the controller can stay behind NAT. Both sides prove that they know the token with an HMAC over the exchanged data, including the agent's public key. The token itself never crosses the network, and a man in the middle cannot substitute another key. The controller then issues a certificate for the agent's key with its CA. The agent stores the CA and certificate, deletes the token, applies the port access rule and switches to mutual TLS.

The token works once and expires in an hour. The controller keeps it only until it is used or expires, and backups leave it out. A new command replaces an unused one.

If the server had an agent of another Node Plane installation, its command journals are archived. An agent that belongs to another active node of the same controller is refused.

## Agent port access

Choose before the command is shown:

- **Open port** (recommended). Anyone can reach the port, but only a client with the controller's certificate is served. It keeps working when the controller's address changes, for example behind a home NAT.
- **Controller address only**. The `ufw` rule allows only the address the controller connected from during enrollment. Use it only when that address is static: after a change the controller cannot reach the agent until you change the rule on the server, for example `sudo ufw allow 50061/tcp comment 'Node Plane agent'`.

Without an active `ufw`, the agent does not change the firewall; restrict the port in your own firewall if needed.

## Why the agent runs as root

The agent does work that requires root on the server:

- runs the Xray and AmneziaWG containers through Docker; access to the Docker socket is already equivalent to root;
- installs Docker and system packages with `apt-get` during server setup;
- opens VPN ports in `ufw`;
- limits speed with `tc` and `nft`, and enters the AmneziaWG container's network namespace with `nsenter`;
- writes the IPv6 setting to `/etc/sysctl.d/70-node-plane-ipv6.conf`;
- manages protocol files under `/opt/node-plane-runtime`, `/etc/xray` and `/opt/amnezia/awg`;
- removes itself and its resources during node removal.

Because of Docker alone, a separate unprivileged user would not limit what a compromised agent could do. Rootless containers do not support the kernel networking that AmneziaWG needs.

## What the controller can do on the server

The controller calls the agent's API: protocol deployment, profile access, settings, diagnostics, speed and connection limits, removal. Changes are journaled on the server.

Current limitation: the controller still sends `awg_profile.py`, which the agent runs as root, and the AmneziaWG container's `start.sh`, so a compromised controller can run code on its nodes. The other runtime entry points are built into the agent. Planned next: moving these into the agent, signed agent self-update instead of updates over SSH, and a systemd sandbox for the agent. Until signed self-update, agent updates of nodes installed with the command are not delivered from the controller.

## Files on the server

| Path | Contents |
|---|---|
| `/usr/local/bin/node-plane-agent` | Agent binary |
| `/etc/systemd/system/node-plane-agent.service` | Service |
| `/etc/node-plane/agent.toml` | Node key, listen address, TLS paths |
| `/etc/node-plane/tls/` | Agent key and certificate, controller CA; the token until enrollment |
| `/etc/node-plane/node.env` | Node settings |
| `/var/lib/node-plane-agent/` | Command journals and the controller installation ID |
| `/opt/node-plane-runtime/` | Protocol runtime files and client configurations |

Full node removal deletes the agent and its resources and verifies the host. See [Nodes](nodes.md#remove-a-node).
