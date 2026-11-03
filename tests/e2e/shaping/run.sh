#!/bin/sh
# Speed-limit bench: builds the agent's tests, then runs bench.sh in a
# privileged container with no network, so the host is never touched.
set -eu
HERE="$(cd "$(dirname "$0")" && pwd)"
AGENT="$HERE/../../../rust/node-agent"
docker build -q --network host -t node-plane-shaping-bench "$HERE" >/dev/null
TEST="$(cd "$AGENT" && cargo test --no-run --message-format=json 2>/dev/null \
  | jq -r 'select(.profile.test == true and .target.name == "node-plane-agent") | .executable' | tail -1)"
cargo build --quiet --manifest-path "$AGENT/Cargo.toml"
cat > "$HERE/.agent-cli" <<'CLI'
#!/bin/sh
exec /agent xray-api --server=127.0.0.1:10085 "$@"
CLI
chmod +x "$HERE/.agent-cli"
trap 'rm -f "$HERE/.agent-cli"' EXIT
docker run --rm --privileged --network none \
  -v "$HERE:/bench:ro" -v "$TEST:/test:ro" -v "$AGENT/target/debug/node-plane-agent:/agent:ro" \
  --entrypoint sh node-plane-shaping-bench -c 'mkdir -p /state && cp /bench/.agent-cli /state/agent-cli && /bench/bench.sh /test'
