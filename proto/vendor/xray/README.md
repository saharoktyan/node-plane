# Vendored Xray-core protobuf definitions

Unmodified copies from [XTLS/Xray-core](https://github.com/XTLS/Xray-core),
tag `v26.10.21` (commit `1b1647cd9ddf0abc34b981d4611fc03bcac1ca74`), licensed
under MPL-2.0 (see `LICENSE`). Paths match the upstream repository.

The node agent compiles them into a gRPC client only (`rust/node-agent/build.rs`)
and talks to the pinned runtime image `ghcr.io/xtls/xray-core:26.10.29`. That
image has no matching upstream tag; it lies between `v26.10.21` and `v26.10.31`,
and these files are identical in both tags except for comments.

Only the import closure of the services the agent uses is vendored:
`HandlerService`, `StatsService` and `RoutingService`. The VLESS account
(`proxy/vless/account.proto`) pulls in the whole transport tree, so the agent
declares the fields it needs (`id`, `flow`, `encryption`) by hand with the same
field numbers.

To update: copy the same files from the new tag, keep this list and the commit
above in sync (also in `licenses/vendored/manifest.json`), regenerate
`THIRD_PARTY_LICENSES` and run the Xray API tests in `tests/test_runtime_docker.py`.
