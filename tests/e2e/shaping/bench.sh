#!/bin/sh
# Runs inside the bench container. The root namespace plays the node: Xray
# with its uplink veth0, and awg0 standing for the AWG tunnel (the fake
# `docker inspect` points the agent at this namespace). "inet" is the
# Internet with an iperf3 server; "peers" holds two AWG peers.
set -eu
TEST="$1"
ip link set lo up
ip netns add inet
ip link add veth0 type veth peer name veth1
ip link set veth1 netns inet
ip addr add 93.184.216.1/24 dev veth0; ip link set veth0 up
ip route add default via 93.184.216.34
ip netns exec inet sh -c 'ip link set lo up; ip addr add 93.184.216.34/24 dev veth1; ip link set veth1 up
  ip route add 10.8.1.0/24 via 93.184.216.1; iperf3 -s -D'
sysctl -qw net.ipv4.ip_forward=1
ip netns add peers
ip link add awg0 type veth peer name tun
ip link set tun netns peers
ip addr add 10.8.1.1/24 dev awg0; ip link set awg0 up
ip netns exec peers sh -c 'ip link set lo up; ip addr add 10.8.1.2/24 dev tun; ip addr add 10.8.1.3/24 dev tun
  ip link set tun up; ip route add default via 10.8.1.1'
mkdir -p /fake /state
printf '#!/bin/sh\necho 1\n' > /fake/docker; chmod +x /fake/docker
export PATH="/fake:$PATH"
printf '[Interface]\nAddress = 10.8.1.1/24\n\n# node-phone\n[Peer]\nPublicKey = a\nAllowedIPs = 10.8.1.2/32\n\n# node-tablet\n[Peer]\nPublicKey = b\nAllowedIPs = 10.8.1.3/32\n' > /state/awg0.conf
xray run -c /bench/server.json >/tmp/server.log 2>&1 &
xray run -c /bench/client.json >/tmp/client.log 2>&1 &
sleep 1.5

pass() { # $1 revision, $2 clients JSON, $3 uplinks JSON
  NP_TEST_SHAPING="{\"state_dir\":\"/state\",\"revision\":$1,\"clients\":$2,\"uplinks\":${3:-[]},\"xray_config\":\"/bench/server.json\",\"awg_config\":\"/state/awg0.conf\"}" \
    "$TEST" --ignored --exact shaping::e2e::shaping_pass --nocapture 2>&1 | sed -n 's/^NP_SHAPING_RESULT //p'
}
mbit() { jq '(.end.sum_received.bits_per_second // 0) / 1000000 | . * 10 | floor / 10'; }
xray_rate() { # $1 port, $2 extra
  timeout 15 iperf3 -c 127.0.0.1 -p "$1" -t 3 -O 1 --connect-timeout 3000 ${2:-} -J 2>/dev/null | mbit || echo 0
}
awg_rate() { # $1 peer, $2 extra
  ip netns exec peers timeout 15 iperf3 -B "$1" -c 93.184.216.34 -t 3 -O 1 ${2:-} -J | mbit
}
rules() { xray api lsrules --server=127.0.0.1:10085 | jq -c '[.rules[].ruleTag // "static"]'; }
FAILED=0
check() { # $1 label, $2 actual, $3 min, $4 max (Mbit/s or exact text when $4 is empty)
  if [ -z "${4:-}" ]; then ok=$([ "$2" = "$3" ] && echo 1 || echo 0); else ok=$(awk -v v="$2" -v a="$3" -v b="$4" 'BEGIN{print (v>=a && v<=b)}'); fi
  if [ "$ok" = 1 ]; then echo "ok   $1: $2"; else echo "FAIL $1: $2 (want $3${4:+..$4})"; FAILED=1; fi
}
APPLIED='[["xray","applied",""],["awg","applied",""]]'
FREE=1000

check "first pass" "$(pass 1 '[["xray","alice",5000,8000],["awg","phone",5000,8000]]')" "$APPLIED"
check "xray alice upload" "$(xray_rate 2101)" 3 5
check "xray alice download" "$(xray_rate 2101 -R)" 6 8
check "xray bob upload" "$(xray_rate 2102)" $FREE 1000000
check "xray bob download" "$(xray_rate 2102 -R)" $FREE 1000000
check "awg phone upload" "$(awg_rate 10.8.1.2)" 3 5
check "awg phone download" "$(awg_rate 10.8.1.2 -R)" 6 8
check "awg tablet download" "$(awg_rate 10.8.1.3 -R)" $FREE 1000000
check "same snapshot again" "$(pass 1 '[["xray","alice",5000,8000],["awg","phone",5000,8000]]')" "$APPLIED"
# Changed limits are applied to the classes in place: the ifb devices stay.
ifindex() { cat /sys/class/net/np-xup/ifindex /sys/class/net/np-xdown/ifindex /sys/class/net/np-aup/ifindex | tr '\n' ' '; }
BEFORE="$(ifindex)"
CHANGED='[["xray","alice",2000,8000],["awg","phone",0,3000],["awg","tablet",3000,1000]]'
check "in-place pass" "$(pass 2 "$CHANGED" '["veth0"]')" "$APPLIED"
check "ifb devices kept" "$(ifindex)" "$BEFORE"
check "xray alice new upload" "$(xray_rate 2101)" 1 2.2
check "awg phone upload now free" "$(awg_rate 10.8.1.2)" $FREE 1000000
check "awg phone new download" "$(awg_rate 10.8.1.2 -R)" 2 3.2
check "awg tablet upload" "$(awg_rate 10.8.1.3)" 2 3.2
check "awg tablet download" "$(awg_rate 10.8.1.3 -R)" 0.5 1.1
# An eviction rule added after the rate rule must still block alice.
/state/agent-cli add-rule --rule-tag=np-evict-1 --outbound=node-plane-blocked --user=alice --source=127.0.0.1 >/dev/null
check "eviction appended last" "$(rules | jq -c 'map(select(. != "static"))')" '["np-rate-1","np-evict-1"]'
pass 3 "$CHANGED" >/dev/null
check "rate rule moved behind it" "$(rules | jq -c 'map(select(. != "static"))')" '["np-evict-1","np-rate-1"]'
check "evicted alice is blocked" "$(xray_rate 2101 -R 2>/dev/null)" 0 0
/state/agent-cli remove-rule --rule-tag=np-evict-1 >/dev/null
# A lost tree (reboot) is rebuilt; changed limits take effect.
ip link del np-xup
check "rebuild pass" "$(pass 4 '[["xray","alice",2000,0],["awg","phone",0,2000]]')" "$APPLIED"
check "xray alice upload after rebuild" "$(xray_rate 2101)" 1 2.2
check "xray alice download now free" "$(xray_rate 2101 -R)" $FREE 1000000
check "awg phone download after rebuild" "$(awg_rate 10.8.1.2 -R)" 1 2.2
check "awg phone upload free after rebuild" "$(awg_rate 10.8.1.2)" $FREE 1000000
check "lift pass" "$(pass 5 '[]')" "$APPLIED"
check "no rate rules left" "$(rules | jq -c 'map(select(. != "static"))')" '[]'
check "no rate outbounds left" "$(xray api lso --server=127.0.0.1:10085 | jq '[.outbounds[] | select(.tag | startswith("np-rate"))] | length')" 0
check "no devices, tables or qdiscs left" "$(ip -o link show | grep -c np- || true)$(nft list tables | grep -c node_plane || true)$(tc qdisc show dev awg0 | grep -c htb || true)" 000
check "xray alice free again" "$(xray_rate 2101 -R)" $FREE 1000000
exit $FAILED
