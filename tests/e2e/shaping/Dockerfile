# Disposable bench for speed limits (VPN plan, section 11d). Ubuntu 24.04 for
# a glibc that runs the agent's test binary built on the developer host.
FROM ghcr.io/saharoktyan/xray-core:v26.10.31-np.2@sha256:43c919899d9a7daf94f988552d65e98fb8abfcc7b804fb39f94a3812755c08c0 AS xray
FROM ubuntu:24.04
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    iproute2 nftables iperf3 jq util-linux && rm -rf /var/lib/apt/lists/*
COPY --from=xray /usr/local/bin/xray /usr/local/bin/xray
