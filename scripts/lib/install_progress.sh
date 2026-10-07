#!/usr/bin/env bash
# Opt-in workstation installer protocol. Labels describe phases, never commands
# or configuration values. A saved descriptor keeps events out of $(...) output.

INSTALL_EVENT_TOTAL=7
INSTALL_EVENT_COMPLETED=0
INSTALL_EVENT_ACTIVE=""
INSTALL_EVENT_LABEL=""
INSTALL_EVENT_FAILED=0
INSTALL_EVENT_FD=""

install_progress_init() {
  [[ "${NODE_PLANE_INSTALL_EVENTS:-0}" == "1" ]] || return 0
  if [[ -n "${NODE_PLANE_INSTALL_EVENT_FD:-}" ]]; then
    if [[ ! "$NODE_PLANE_INSTALL_EVENT_FD" =~ ^[0-9]+$ ]] \
      || ! { true >&"$NODE_PLANE_INSTALL_EVENT_FD"; } 2>/dev/null; then
      echo "NODE_PLANE_INSTALL_EVENT_FD must name an open output descriptor." >&2
      return 1
    fi
    INSTALL_EVENT_FD="$NODE_PLANE_INSTALL_EVENT_FD"
  else
    exec {INSTALL_EVENT_FD}>&1
  fi
}

install_progress_json_string() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  value="${value//$'\n'/\\n}"
  value="${value//$'\r'/\\r}"
  value="${value//$'\t'/\\t}"
  printf '"%s"' "$value"
}

install_progress_emit() {
  [[ -n "$INSTALL_EVENT_FD" ]] || return 0
  local event="$1" state="$2" label="$3"
  printf 'NODE_PLANE_EVENT {"version":1,"event":"%s","id":"%s","label":%s,"state":"%s","completed":%s,"total":%s}\n' \
    "$event" "$INSTALL_EVENT_ACTIVE" "$(install_progress_json_string "$label")" \
    "$state" "$INSTALL_EVENT_COMPLETED" "$INSTALL_EVENT_TOTAL" >&"$INSTALL_EVENT_FD"
}

install_progress_begin() {
  INSTALL_EVENT_ACTIVE="$1"
  case "$1" in
    configuration) INSTALL_EVENT_LABEL="Configure installation" ;;
    release) INSTALL_EVENT_LABEL="Prepare release files" ;;
    python) INSTALL_EVENT_LABEL="Install backend runtime dependencies" ;;
    database) INSTALL_EVENT_LABEL="Prepare PostgreSQL and backend schema" ;;
    identity) INSTALL_EVENT_LABEL="Prepare administrator and Telegram credentials" ;;
    services) INSTALL_EVENT_LABEL="Install and start systemd services" ;;
    driver) INSTALL_EVENT_LABEL="Prepare driver and managed agents" ;;
    *) echo "Unknown installer phase." >&2; return 1 ;;
  esac
  INSTALL_EVENT_FAILED=0
  install_progress_emit step running "$INSTALL_EVENT_LABEL"
}

install_progress_done() {
  [[ -n "$INSTALL_EVENT_ACTIVE" ]] || return 0
  INSTALL_EVENT_COMPLETED=$((INSTALL_EVENT_COMPLETED + 1))
  install_progress_emit step done "$INSTALL_EVENT_LABEL"
  INSTALL_EVENT_ACTIVE=""
}

install_progress_fail() {
  [[ -n "$INSTALL_EVENT_ACTIVE" && "$INSTALL_EVENT_FAILED" == "0" ]] || return 0
  INSTALL_EVENT_FAILED=1
  install_progress_emit step failed "$INSTALL_EVENT_LABEL"
}

install_progress_detail() {
  [[ -n "$INSTALL_EVENT_ACTIVE" ]] || return 0
  install_progress_emit detail running "$1"
}
