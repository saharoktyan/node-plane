#!/usr/bin/env bash
set -euo pipefail
source /etc/node-plane/node.env

XRAY_CONFIG_PATH="${1:-}"
XRAY_SNI_VALUE="${2:-}"
XRAY_TCP_VALUE="${3:-}"
XRAY_XHTTP_VALUE="${4:-}"
XRAY_PATH_VALUE="${5:-/assets}"
APPLY_XRAY="${6:-false}"
APPLY_AWG="${7:-false}"

if [[ "$APPLY_XRAY" == true ]]; then
  [[ -n "$XRAY_SNI_VALUE" && "$XRAY_TCP_VALUE" =~ ^[0-9]+$ && "$XRAY_XHTTP_VALUE" =~ ^[0-9]+$ ]] || { echo "Xray SNI or ports are invalid" >&2; exit 1; }
  [[ -f "$XRAY_CONFIG_PATH" ]] || { echo "Xray config missing: $XRAY_CONFIG_PATH" >&2; exit 1; }
  XRAY_BACKUP="$(mktemp "$(dirname "$XRAY_CONFIG_PATH")/.xray-config-backup-XXXXXX")"
  cp -p "$XRAY_CONFIG_PATH" "$XRAY_BACKUP"
  XRAY_CONFIG_ENV="$XRAY_CONFIG_PATH" XRAY_SNI_ENV="$XRAY_SNI_VALUE" XRAY_TCP_ENV="$XRAY_TCP_VALUE" XRAY_XHTTP_ENV="$XRAY_XHTTP_VALUE" XRAY_PATH_ENV="$XRAY_PATH_VALUE" python3 - <<'PY'
import json
import os
import tempfile
from pathlib import Path

path = Path(os.environ['XRAY_CONFIG_ENV'])
data = json.loads(path.read_text(encoding='utf-8'))
inbounds = {item.get('tag'): item for item in data.get('inbounds', [])}
if 'reality-tcp' not in inbounds or 'reality-xhttp' not in inbounds:
    raise SystemExit('Xray Reality inbounds are missing')
for tag, port in [('reality-tcp', 'XRAY_TCP_ENV'), ('reality-xhttp', 'XRAY_XHTTP_ENV')]:
    inbound = inbounds[tag]
    inbound['port'] = int(os.environ[port])
    reality = inbound['streamSettings']['realitySettings']
    reality['serverNames'] = [os.environ['XRAY_SNI_ENV']]
    reality['dest'] = os.environ['XRAY_SNI_ENV'] + ':443'
inbounds['reality-xhttp']['streamSettings'].setdefault('xhttpSettings', {})['path'] = os.environ['XRAY_PATH_ENV']
with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as tmp:
    json.dump(data, tmp, ensure_ascii=False, indent=2)
    tmp.write('\n')
    staged = Path(tmp.name)
staged.chmod(0o600)
os.replace(staged, path)
PY
  if ! /opt/node-plane-runtime/deploy-xray.sh; then
    cp -p "$XRAY_BACKUP" "$XRAY_CONFIG_PATH"
    /opt/node-plane-runtime/deploy-xray.sh >&2 || true
    echo "Xray deployment failed; previous config restored" >&2
    exit 1
  fi
  rm -f "$XRAY_BACKUP"
fi

if [[ "$APPLY_AWG" == true ]]; then
  CFG="${AWG_CONFIG:-/opt/node-plane-runtime/amnezia-awg/data/wg0.conf}"
  if [[ ! -f "$CFG" ]]; then
    mapfile -t OLD_CFGS < <(find "$(dirname "$CFG")" -maxdepth 1 -type f -name '*.conf' ! -name "$(basename "$CFG")")
    if [[ "${#OLD_CFGS[@]}" -ne 1 ]]; then
      echo "AWG config missing: $CFG; cannot identify a unique existing interface config" >&2
      exit 1
    fi
    cp -p "${OLD_CFGS[0]}" "$CFG"
  fi
  AWG_BACKUP="$(mktemp "$(dirname "$CFG")/.awg-config-backup-XXXXXX")"
  cp -p "$CFG" "$AWG_BACKUP"
  PRESET_FILE="$(dirname "$CFG")/.node-plane-i1-preset"
  if [[ -f "$PRESET_FILE" ]]; then
    CURRENT_PRESET="$(cat "$PRESET_FILE")"
  else
    CURRENT_I1="$(awk -F ' = ' '$1 == "I1" {print $2; exit}' "$CFG")"
    case "$CURRENT_I1" in
      '<b 0xc000000001>'*) CURRENT_PRESET=quic ;;
      '<rc 2><b 0x01000001000000000000>'*) CURRENT_PRESET=dns ;;
      *) CURRENT_PRESET=chaos ;;
    esac
  fi
  if [[ "$CURRENT_PRESET" != "${AWG_I1_PRESET:-quic}" ]]; then
    if ! python3 /opt/node-plane-runtime/awg_profile.py regenerate "$CFG" "${AWG_I1_PRESET:-quic}" >/dev/null; then
      cp -p "$AWG_BACKUP" "$CFG"
      echo "AWG entropy update failed; previous config restored" >&2
      exit 1
    fi
  fi
  AWG_CONFIG_ENV="$CFG" AWG_PORT_ENV="${AWG_SERVER_PORT:-51820}" python3 - <<'PY'
import os
import re
from pathlib import Path

path = Path(os.environ['AWG_CONFIG_ENV'])
text = path.read_text(encoding='utf-8')
updated, count = re.subn(r'(?m)^ListenPort\s*=\s*\d+\s*$', 'ListenPort = ' + os.environ['AWG_PORT_ENV'], text, count=1)
if count != 1:
    raise SystemExit('AWG ListenPort is missing')
path.write_text(updated, encoding='utf-8')
path.chmod(0o600)
PY
  if ! /opt/node-plane-runtime/deploy-awg.sh; then
    cp -p "$AWG_BACKUP" "$CFG"
    /opt/node-plane-runtime/deploy-awg.sh >&2 || true
    echo "AWG deployment failed; previous config restored" >&2
    exit 1
  fi
  rm -f "$AWG_BACKUP"
  printf '%s\n' "${AWG_I1_PRESET:-quic}" > "$PRESET_FILE"
fi
echo "Node settings applied"
