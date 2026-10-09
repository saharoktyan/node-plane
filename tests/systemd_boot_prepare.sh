#!/bin/sh
set -eu
test "$(systemd-detect-virt)" = qemu
test "$(cat /etc/hostname)" = np-boot-test
test -f /root/boot-fixture-ready
test ! -e /etc/node-plane/profile-intents.sqlite3
tar -xf /root/runtime.tar -C /opt
python3 -c 'from pathlib import Path; [p.chmod(0o755) for p in Path("/opt/node-plane-runtime").rglob("*") if p.is_file() and p.suffix in (".py", ".sh")]'

docker load -i /root/xray-image.tar > /root/xray-load.log
docker load -i /root/awg-image.tar > /root/awg-load.log
base=$(sed -n 's/Loaded image ID: //p' /root/awg-load.log | tail -1)
if [ -z "$base" ]; then base=$(docker images -q amneziavpn/amneziawg-go | head -1); fi
test -n "$base"
docker tag "$base" node-plane-awg-base:boot-test
sed -i '1cFROM node-plane-awg-base:boot-test' /opt/node-plane-runtime/amnezia-awg/Dockerfile
docker build -t node-plane-awg:boot-test /opt/node-plane-runtime/amnezia-awg > /root/awg-build.log 2>&1
install -m 0755 /root/node-plane-agent /usr/local/bin/node-plane-agent
mkdir -p /etc/node-plane/tls
cd /etc/node-plane/tls
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj '/CN=Boot test CA' -keyout ca.key -out ca.crt >/dev/null 2>&1
openssl req -newkey rsa:2048 -nodes -subj '/CN=bootnode' -keyout server.key -out server.csr >/dev/null 2>&1
printf '%s\n' 'basicConstraints=critical,CA:FALSE' 'keyUsage=critical,digitalSignature,keyEncipherment' 'extendedKeyUsage=serverAuth' 'subjectAltName=IP:127.0.0.1' > server.ext
openssl x509 -req -in server.csr -CA ca.crt -CAkey ca.key -CAcreateserial -days 1 -extfile server.ext -out server.crt >/dev/null 2>&1
chmod 0600 *.key
cat > /etc/node-plane/agent.toml <<'EOF'
node_key = "bootnode"
listen_addr = "127.0.0.1:50061"
heartbeat_seconds = 2
runtime_root = "/opt/node-plane-runtime"
state_dir = "/var/lib/node-plane-agent"
log_dir = "/var/log/node-plane-agent"
node_env_path = "/etc/node-plane/node.env"
xray_config_path = "/opt/node-plane-runtime/xray/config.json"
awg_config_path = "/opt/node-plane-runtime/amnezia-awg/data/wg0.conf"
EOF
cat > /etc/systemd/system/node-plane-agent.service <<'EOF'
[Unit]
Description=Node Plane Agent boot fixture
After=network.target
[Service]
Type=simple
ExecStart=/usr/local/bin/node-plane-agent
Restart=on-failure
RestartSec=2
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now node-plane-agent
python3 /root/systemd_boot_guest.py setup
