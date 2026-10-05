#!/usr/bin/env bash
# Durable presentation state and local controller snapshot for stack updates.

stack_progress() {
  [[ -n "${STACK_PROGRESS_FILE:-}" ]] || return 0
  python3 - "$STACK_PROGRESS_FILE" "$1" "$2" <<'PY'
import json, os, pathlib, sys, tempfile
path, key, status = pathlib.Path(sys.argv[1]), sys.argv[2], sys.argv[3]
path.parent.mkdir(parents=True, exist_ok=True)
value = json.loads(path.read_text()) if path.exists() else {'components': {
    kind: 'waiting' for kind in ('backend', 'worker', 'driver', 'telegram')}}
if key in value['components']:
    value['components'][key] = status
else:
    value[key] = status
fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.progress-')
try:
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream)
    os.replace(temporary, path)
finally:
    if os.path.exists(temporary):
        os.unlink(temporary)
PY
}

stack_files() {
  printf '%s\n' /usr/local/bin/node-plane-driver \
      /etc/systemd/system/node-plane-driver.service \
      /etc/systemd/system/node-plane-backend.service \
      /etc/systemd/system/node-plane-backend-worker.service \
      /etc/systemd/system/node-plane-backend-worker.timer \
      /etc/systemd/system/node-plane-telegram.service "$STACK_ENV_FILE"
}

stack_snapshot() {
  STACK_SNAPSHOT="$(mktemp -d)"
  chmod 700 "$STACK_SNAPSHOT"
  STACK_ENV_FILE="$1"
  local item
  while IFS= read -r item; do
    if sudo test -f "$item"; then
      sudo cp -a --parents "$item" "$STACK_SNAPSHOT"
    fi
  done < <(stack_files)
}

stack_restore() {
  local item failed=0
  while IFS= read -r item; do
    if sudo test -f "${STACK_SNAPSHOT}${item}"; then
      if sudo cp -a "${STACK_SNAPSHOT}${item}" "${item}.rollback.$$"; then
        sudo mv -f "${item}.rollback.$$" "$item" || failed=1
      else
        failed=1
      fi
    else
      sudo rm -f "$item" || failed=1
    fi
  done < <(stack_files)
  return "$failed"
}
