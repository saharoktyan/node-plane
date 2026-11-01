#!/usr/bin/env bash
# Safety checks for the existing shell deploy paths; mutations use captured IDs.
# The caller supplies docker_cmd and has already loaded the trusted node.env.
container_guard_init() {
  NP_PROTOCOL="$1"
  NP_CONTAINER="$2"
  NP_SOURCE="$(realpath -e -- "$3")" || return 1
  NP_DESTINATION="$4"
  if [[ ! "$NP_CONTAINER" =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]*$ ||
        ! "${SERVER_KEY:-}" =~ ^[a-zA-Z0-9_-]{1,64}$ ]]; then
    echo 'Invalid managed container or node identity' >&2
    return 1
  fi
  NP_INSTALLATION="$(cat /var/lib/node-plane-agent/controller-installation-id)" || return 1
  if [[ ! "$NP_INSTALLATION" =~ ^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$ ]]; then
    echo 'Agent installation identity is unavailable' >&2
    return 1
  fi
  NP_DEPLOYMENT="$(python3 -c 'import uuid; print(uuid.uuid4())')" || return 1
  NP_GUARD_DIR="$(mktemp -d)" || return 1
  chmod 0700 "$NP_GUARD_DIR" || return 1
  NP_CIDFILE="${NP_GUARD_DIR}/container-id"
  NP_CONTAINER_LABELS=(--label org.node-plane.managed=1
    --label "org.node-plane.installation=${NP_INSTALLATION}"
    --label "org.node-plane.node=${SERVER_KEY}"
    --label "org.node-plane.service=${NP_PROTOCOL}"
    --label "org.node-plane.deployment=${NP_DEPLOYMENT}")
}

container_guard_validate() {
  local identity="$1" expected_name="$2" deployment="${3:-}"
  if [[ ! "$identity" =~ ^[0-9a-f]{64}$ ]]; then
    echo 'Invalid Docker container identity' >&2
    return 1
  fi
  local record
  record="$(docker_cmd container inspect "$identity")" || return 1
  NP_ID="$identity" NP_NAME="$expected_name" NP_MOUNT="$NP_SOURCE" \
  NP_DEST="$NP_DESTINATION" NP_OWNER="$NP_INSTALLATION" NP_NODE="$SERVER_KEY" \
  NP_SERVICE="$NP_PROTOCOL" NP_TOKEN="$deployment" \
    python3 -c '
import json, os, sys
records = json.load(sys.stdin)
if not isinstance(records, list) or len(records) != 1:
    raise SystemExit("Invalid Docker container inventory")
record = records[0]
if record.get("Id") != os.environ["NP_ID"] or record.get("Name") != "/" + os.environ["NP_NAME"]:
    raise SystemExit("Container identity changed during deployment")
mounts = record.get("Mounts", [])
expected = [m for m in mounts if m.get("Destination") == os.environ["NP_DEST"]]
if (len(expected) != 1 or expected[0].get("Type") != "bind"
        or expected[0].get("Source") != os.environ["NP_MOUNT"]
        or (os.environ["NP_SERVICE"] == "xray" and expected[0].get("RW") is not False)):
    raise SystemExit("Container mount ownership could not be verified")
labels = record.get("Config", {}).get("Labels") or {}
required = {"org.node-plane.managed": "1",
            "org.node-plane.installation": os.environ["NP_OWNER"],
            "org.node-plane.node": os.environ["NP_NODE"],
            "org.node-plane.service": os.environ["NP_SERVICE"]}
# Pre-label installations use the same exact bind-mount evidence as cleanup.
# Any Node Plane label activates the full label contract; partial labels fail.
tagged = any(key.startswith("org.node-plane.") for key in labels)
if (tagged or os.environ["NP_TOKEN"]) and any(labels.get(k) != v for k, v in required.items()):
    raise SystemExit("Container installation ownership could not be verified")
if os.environ["NP_TOKEN"] and labels.get("org.node-plane.deployment") != os.environ["NP_TOKEN"]:
    raise SystemExit("Container belongs to another deployment")
print(record["Id"])
' <<< "$record"
}

container_guard_existing() {
  local inventory identity name found=""
  inventory="$(docker_cmd ps -a --no-trunc --format '{{.ID}} {{.Names}}')" || return 1
  while read -r identity name; do
    if [[ "$name" == "$NP_CONTAINER" ]]; then
      [[ -z "$found" ]] || return 1
      found="$(container_guard_validate "$identity" "$NP_CONTAINER")" || return 1
    fi
  done <<< "$inventory"
  printf '%s' "$found"
}

container_guard_new() {
  if [[ ! -s "$NP_CIDFILE" ]]; then
    return 0
  fi
  container_guard_validate "$(cat "$NP_CIDFILE")" "$NP_CONTAINER" "$NP_DEPLOYMENT"
}

container_guard_remove_new() {
  local identity
  identity="$(container_guard_new)" || return 1
  if [[ -n "$identity" ]]; then
    docker_cmd rm -f "$identity" >/dev/null || return 1
  fi
}
