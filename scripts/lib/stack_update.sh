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
  # Publish only a complete inventory. A failed copy must never turn files
  # that have not been visited yet into "originally absent" rollback targets.
  STACK_SNAPSHOT=""
  local candidate item target
  candidate="$(mktemp -d)" || return 1
  chmod 700 "$candidate" || { rm -rf -- "$candidate"; return 1; }
  STACK_ENV_FILE="$1"
  while IFS= read -r item; do
    if sudo test -e "$item" || sudo test -L "$item"; then
      target="${candidate}/files${item}"
      if ! sudo test -d "$item" || sudo test -L "$item"; then
        if sudo mkdir -p -- "$(dirname "$target")" && sudo cp -a -- "$item" "$target"; then
          continue
        fi
      fi
    else
      target="${candidate}/absent${item}"
      if sudo mkdir -p -- "$(dirname "$target")" && sudo touch -- "$target"; then
        continue
      fi
    fi
    sudo rm -rf -- "$candidate"
    return 1
  done < <(stack_files)
  if ! sudo touch -- "${candidate}/.complete"; then
    sudo rm -rf -- "$candidate"
    return 1
  fi
  STACK_SNAPSHOT="$candidate"
}

stack_restore() {
  local item failed=0
  [[ -n "${STACK_SNAPSHOT:-}" ]] && sudo test -f "${STACK_SNAPSHOT}/.complete" || return 1
  # Validate the entire inventory before changing any live file. Missing
  # snapshot data is an error, never permission to delete the original.
  while IFS= read -r item; do
    if sudo test -e "${STACK_SNAPSHOT}/files${item}" || sudo test -L "${STACK_SNAPSHOT}/files${item}"; then
      sudo test ! -e "${STACK_SNAPSHOT}/absent${item}" || return 1
    else
      sudo test -f "${STACK_SNAPSHOT}/absent${item}" || return 1
    fi
  done < <(stack_files)
  while IFS= read -r item; do
    if sudo test -e "${STACK_SNAPSHOT}/files${item}" || sudo test -L "${STACK_SNAPSHOT}/files${item}"; then
      if sudo cp -a -- "${STACK_SNAPSHOT}/files${item}" "${item}.rollback.$$"; then
        sudo mv -fT -- "${item}.rollback.$$" "$item" || failed=1
      else
        failed=1
      fi
    else
      sudo rm -f -- "$item" || failed=1
    fi
  done < <(stack_files)
  return "$failed"
}
