# node-plane-agent

Rust node-local executor for runtime and host operations.

Current scope:

- loads node-local config;
- exposes a small gRPC surface;
- reports runtime facts and local health;
- lists AWG/Xray remote profiles from local files;
- checks node-local port availability for requested runtime ports;
- opens firewall rules for requested runtime ports via `ufw`;
- writes `node.env` from central-driver payloads;
- writes arbitrary runtime file bundles from central-driver payloads;
- runs local `sync-xray.sh` and returns generated Xray settings;
- runs local Xray/AWG init and deploy scripts for bootstrap orchestration;
- installs Docker on Debian/Ubuntu hosts when requested by the central driver;
- deletes managed runtime containers/images and optionally removes runtime config;
- removes a requested public key from the local `authorized_keys`;
- creates and deletes Xray/AWG profile runtime entries through local helper scripts;
- runs a heartbeat loop.

The agent runs privileged local runtime actions requested by the central
driver. Protect node access and keep its gRPC port restricted to the controller.

## Run

```bash
scripts/run_node_agent.sh
```

The agent requires a TLS server certificate, private key and client CA
certificate. It rejects connections without a client certificate issued by the
configured CA. The rollout script installs these files under
`/etc/node-plane/tls/`; startup fails when any required file is missing.

Optional environment variables:

- `NODE_AGENT_CONFIG_PATH`
- `NODE_AGENT_NODE_KEY`
- `NODE_AGENT_LISTEN_ADDR`
- `NODE_AGENT_HEARTBEAT_SECONDS`

Default config path:

- `/etc/node-plane/agent.toml`

TLS certificate paths can be set in `agent.toml` with `tls_certificate_path`,
`tls_key_path` and `tls_client_ca_path`. Defaults point to `/etc/node-plane/tls/`.

Current RPC surface:

- `GetRuntimeFacts`
- `GetNodeHealth`
- `ListRemoteProfiles`
- `RunDiagnostics`
- `CheckPorts`
- `OpenPorts`
- `SyncNodeEnv`
- `SyncRuntimeFiles`
- `SyncXray`
- `InstallDocker`
- `DeleteRuntime`
- `InitXray`
- `DeployXray`
- `InitAwg`
- `DeployAwg`
- `PathExists`
- `RemoveAuthorizedKey`
- `AddXrayUser`
- `DeleteXrayUser`
- `AddAwgUser`
- `DeleteAwgUser`
