#!/usr/bin/env bash
# Run agent installer commands as root or with noninteractive sudo. Keep stdin
# untouched so callers can stream installation scripts through `bash -s`.
agent_ssh() {
  local count=$# target command payload quoted
  (( count >= 2 )) || return 2
  target="${@:count-1:1}"
  command="${@:count:1}"
  payload='sudo() {
    if [ "$EUID" -eq 0 ]; then
      "$@"
    else
      command sudo -n "$@"
    fi
  }
  export -f sudo
  '
  payload+="$command"
  # POSIX shell quoting works even when the remote login shell is not bash.
  quoted="${payload//\'/\'\\\'\'}"
  ssh "${@:1:count-2}" "$target" "bash -c '$quoted'"
}
