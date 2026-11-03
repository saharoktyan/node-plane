# Disposable bench for speed limits (VPN plan, section 11d). Ubuntu 24.04 for
# a glibc that runs the agent's test binary built on the developer host.
FROM ghcr.io/xtls/xray-core:26.10.29@sha256:c9443f3ee297f6481bc245269d61720c6f989195ee9b58fa90fdd99f03c21044 AS xray
FROM ubuntu:24.04
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    iproute2 nftables iperf3 jq util-linux && rm -rf /var/lib/apt/lists/*
COPY --from=xray /usr/local/bin/xray /usr/local/bin/xray
