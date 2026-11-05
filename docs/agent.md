[Русская версия](agent.ru.md)

# Node agent on the server

The node agent is the only Node Plane component on a VPN server. The controller manages the server only through the agent's API over mutual TLS. This page describes what the install command does, how the agent meets the controller, and why it runs as root.

## Requirements

- Linux x86_64 with systemd.
- `curl` or `wget`, `tar` and `sha256sum`.
- Debian or Ubuntu for automatic installation of Docker and the speed limit tools (`iproute2`, `nftables`, `util-linux`).
- The controller must reach the agent port, TCP 50061 by default (`NODE_AGENT_PORT` on the controller). If the provider has its own firewall, allow that port. The controller connects to the node's client address.

## What the install command does

**Server setup** shows a one-time command; Workstation's **Auto** or the bot's **Install over SSH** can run it for you over one SSH login. Run it as root. The command:

1. Downloads `node-plane-agent-installer.sh` from the GitHub release of the controller's version and checks its SHA-256. The controller refuses to issue a command when the released installer differs from its own copy.
2. The installer downloads the agent archive of the same release, checks the SHA-256 given in the command and installs `/usr/local/bin/node-plane-agent`.
3. Installs missing speed limit tools with `apt-get`. Without them the node only reports that it cannot limit speed.
4. Writes `/etc/node-plane/agent.toml` and the token to `/etc/node-plane/tls/enrollment.token`. A previous agent key and certificate are removed.
5. If `ufw` is active, opens the agent port.
6. Installs and starts the `node-plane-agent` systemd service.

The controller gets no SSH access, no SSH key and no sudo. Workstation's **Auto** logs in once with your key or password, uses `sudo` when the login is not root, and saves nothing. The bot's **Install over SSH** logs in once with the controller's key, which you added to the server beforehand, and needs root or passwordless sudo; after that the controller uses SSH for nothing on that server.

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

- runs the Xray and AmneziaWG containers through Docker; access to the Docker socket is already equivalent to root. Images are pulled by digest and tagged locally as `node-plane-xray:<version>` and `node-plane-awg:<version>`; nothing is built on the server. The containers are named `node-plane-xray` and `node-plane-awg`, so they do not collide with an Xray or AmneziaWG installed separately;
- installs Docker and system packages with `apt-get` during server setup;
- opens VPN ports in `ufw`;
- limits speed with `tc` and `nft`, and enters the AmneziaWG container's network namespace with `nsenter`;
- writes the IPv6 setting to `/etc/sysctl.d/70-node-plane-ipv6.conf`;
- manages protocol files under `/opt/node-plane-runtime`, `/etc/xray` and `/opt/amnezia/awg`;
- removes itself and its resources during node removal.

Because of Docker alone, a separate unprivileged user would not limit what a compromised agent could do. Rootless containers do not support the kernel networking that AmneziaWG needs.

## What the controller can do on the server

The controller calls the agent's API: protocol deployment, profile access, settings, diagnostics, speed and connection limits, removal. Changes are journaled on the server.

The controller sends nothing the agent executes: the runtime entry points, the AWG profile generator and the AmneziaWG container's start script are built into the agent, and the controller delivers only data such as `node.env`. A compromised controller can therefore do only what the agent's API allows, unless you keep the controller's SSH key on the server after **Install over SSH**; removing the node takes that key off. Python is not needed on the server.

## Service hardening

The agent keeps `/etc/systemd/system/node-plane-agent.service.d/hardening.conf` in place and rewrites it if it changes: home directories are read-only, so the agent cannot add an SSH key; it cannot load kernel modules (the kernel still loads the ones `tc`, `nft` and Docker need), read kernel logs, change cgroups or the hostname; and it runs only native 64-bit system calls. Agents of earlier releases add it on their next start; it applies from the restart after that, for example the next update.

This limits mistakes, not a compromised agent. The agent needs the Docker socket, writes systemd units for temporary access and starts transient units; each of these is enough to leave any sandbox. A stricter sandbox (`ProtectSystem=strict`, fewer capabilities) would therefore not stop an attacker, but could break Docker, apt, ufw or speed limits. What limits a compromised controller is above: the agent's API, nothing executable delivered and signed agent releases.

## Relay

A node can run the web cabinet's relay. The agent starts it from its own binary (`node-plane-agent relay-serve`), so the relay updates with the agent and restarts into the new binary after an update. It runs as the `node-plane-relay` user in a sandbox: it may bind its port and reach the network, write only `/var/lib/node-plane-relay/`, and has no other privileges, because it faces the internet. Its key stays on the server; the controller pins the public half, and the relay pins the controller's link key, which comes with the relay settings. With an active ufw the agent opens the relay's port (not behind a reverse proxy). Removing the relay or the agent removes its unit, settings, keys and certificates.

## Agent updates

**Updates** updates agents together with the controller. The controller names its release; the agent downloads it from GitHub itself and checks it:

- the release workflow signs a statement with the version and the SHA-256 of the agent archive. The agent accepts only a statement signed with the release key built into it, so neither the controller nor a changed download can give it another binary;
- the version must be newer than the running one; older releases are refused;
- the new binary must start on this server and report the signed version.

The agent keeps the current binary as `node-plane-agent.previous`, replaces itself and restarts. Before the restart it arms a systemd timer: unless the new agent is serving within three minutes, the previous binary is put back and the service restarted. Updates then shows that the update was rolled back, and the agent does not retry that version on its own. After fixing the cause, run on the server:

```sh
sudo node-plane-agent self-update https://github.com/saharoktyan/node-plane/releases/download/vX.Y.Z X.Y.Z
```

Agents from before self-update, and agents built without a release key, cannot update themselves, and the controller does not update agents over SSH: reinstall such an agent with a new install command once.

## Removal

Full removal of a server revokes access and asks the agent to remove the runtime. Before it removes itself, the agent checks the server: the runtime, `node.env`, the protocol configs and the managed containers must be gone. It reports that, with the server's `/etc/machine-id`, which the controller recorded before it started. The agent then removes its service, binary, configuration, keys, journals and lease units, and the controller's SSH key if the controller has one. The controller retires the node once the agent, having reported the same machine-id, stops answering.

The agent's own removal is not verified: the controller cannot look at the server afterwards, and an agent that stopped answering for another reason looks the same. The node record and the bot say so. If the agent still answers ten minutes later, removal stops with `agent_still_running`; check the `node-plane-agent` service on the server. Agents from before this check report no machine-id; update them first. The controller's own node is still inspected by the controller after removal.

## Files on the server

| Path | Contents |
|---|---|
| `/usr/local/bin/node-plane-agent` | Agent binary |
| `/usr/local/bin/node-plane-agent.previous` | Previous binary after a self-update |
| `/etc/systemd/system/node-plane-agent.service` | Service |
| `/etc/systemd/system/node-plane-agent.service.d/hardening.conf` | Service hardening |
| `/etc/node-plane/agent.toml` | Node key, listen address, TLS paths |
| `/etc/node-plane/tls/` | Agent key and certificate, controller CA; the token until enrollment |
| `/etc/node-plane/node.env` | Node settings |
| `/var/lib/node-plane-agent/` | Command journals, the controller installation ID, self-update state in `update/` |
| `/opt/node-plane-runtime/` | Protocol runtime files and client configurations |
| `/etc/systemd/system/node-plane-relay.service`, `/etc/node-plane/relay.env` | Relay service and settings, when the node runs the relay |
| `/var/lib/node-plane-relay/` | Relay key, the controller's pinned key, link state, ACME certificates |

Full node removal deletes the agent and its resources and verifies the host. See [Nodes](nodes.md#remove-a-node).
