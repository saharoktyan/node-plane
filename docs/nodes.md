[Русская версия](nodes.ru.md)

# Nodes

A node is a Linux host running a Node Plane agent and optional VPN protocols. A local node runs on the controller; an SSH node runs on another host. An installation can have one local node and multiple remote nodes.

## Create a node

In Workstation, open **Nodes** and choose the creation action. In Telegram, open the administrator's server list and add a server.

1. Choose local or SSH. Local is offered only when no local node exists.
2. Select a region/template or enter the server details.
3. Enter the name, region, flag and unique code. Template codes use a sequence such as `lv1`, `lv2`.
4. For SSH, enter the management host, port and login.
5. Enter the address VPN clients connect to: a public IP or DNS name, for example `203.0.113.10` or `vpn.example.com`.
6. Select protocols and Xray transports. Selecting no protocols creates an agent-only node.
7. Review and create. Open the server card to continue setup.

The SSH management address and client address can differ. A DNS client address must resolve to the intended server.

## Set up the server

On a node with protocols selected, **Server setup** installs the agent, installs Docker and deploys the selected protocols in one operation. When the server has no agent yet, choose how to install it:

- **Command for the server** in Telegram, **Manual** in Workstation: Node Plane shows a one-time command. Copy it and run it on the server as root, for example in your own SSH session. The controller needs no SSH access or sudo. The command works once and expires in an hour; a new command replaces an unused one. Once the agent answers, setup continues by itself: Telegram shows the command in the setup progress, which updates by itself, and Workstation's node card follows the progress. **Install command** on the card shows the command again.
- **Auto** in Workstation: Workstation runs the same command over one SSH login with an existing key or a password. When the login is not root, it uses sudo and asks for the sudo password if needed. Nothing is saved and no key is added to the server.
- **Install over SSH** in Telegram, when the controller already has SSH access to the server: the controller installs the agent over SSH. A local node is always set up this way.

Before the command, choose access to the agent port: an open port protected by mutual TLS (recommended; works when the controller's address changes) or the controller's address only (for a static controller address). The controller connects to the node's client address on TCP 50061; allow it in the provider's firewall if there is one. See [Node agent](agent.md) for what the command does and why the agent runs as root.

On an agent-only node, **Setup agent** installs the agent. Add protocols later in the node settings and run their installation action.

Review the confirmation and start setup. Check the current step and final result. A running agent does not by itself mean protocols are installed. Missing means no installed runtime; stopped means an installed runtime is not running.

## Edit settings

Open the server settings to edit general information, protocols or advanced options. Review the draft and save. Leaving without saving asks whether to discard the changes.

Installation defaults supply initial values for new deployments; server settings control that specific node. See [Protocols](protocols.md) for ports, SNI, fingerprint and AWG parameters. Runtime changes can require users to obtain updated configurations.

Server cards include monthly traffic when available, with total, VLESS and AWG usage. See [Settings](settings.md) for the collection policy.

## Diagnose and recover

Use the node's diagnostics/recovery actions for agent connectivity, runtime health and blocked operations. **Refresh** refreshes the card and current operation. Accepted work continues after you leave the screen.

When the agent answers, the checks are followed by the live server observation — the same data as **Probe** on the Telegram server card: Docker, each protocol's installed/running/config state and ports, CPU, RAM, disk, load and uptime, and the agent, runtime and expected runtime versions. In Workstation it is under the checks in **Diagnostic & recovery** on the server card.

A blocked outcome may be uncertain. Read the error and recovery choices before starting another setup or reinstall. Keep the existing command journals; they help determine what already ran.

## Remove a node

Open node maintenance and review full cleanup. The confirmation lists affected profiles and resources. Full cleanup revokes access, removes managed protocol resources and the agent, verifies the host and retires the node record.

Registry-only removal retires the record without confirming host cleanup. Configurations on an unreachable host may still work. Docker, unrelated containers, host packages and unrelated SSH keys are shared resources and are retained.
