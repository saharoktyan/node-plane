#!/usr/bin/env bash
# Format checks only; the installed client validates its token with Telegram.
valid_telegram_id() {
  local value
  [[ "$1" =~ ^[[:blank:]]*([0-9]+)[[:blank:]]*$ ]] || return 1
  value="${BASH_REMATCH[1]}"
  while [[ "$value" == 0* ]]; do value="${value#0}"; done
  [[ -n "$value" && ${#value} -le 19 ]] || return 1
  [[ ${#value} -lt 19 || "$value" == 9223372036854775807 || "$value" < 9223372036854775807 ]]
}

valid_telegram_bot_token() {
  [[ "$1" =~ ^([0-9]+):[A-Za-z0-9_-]{20,}$ ]] || return 1
  valid_telegram_id "${BASH_REMATCH[1]}"
}

valid_telegram_admin_ids() {
  local value id
  [[ ! "$1" =~ [[:cntrl:]] ]] || return 1
  [[ "$1" =~ ^[[:blank:]]*[0-9]+[[:blank:]]*(,[[:blank:]]*[0-9]+[[:blank:]]*)*$ ]] || return 1
  value="$1"
  while [[ "$value" == *,* ]]; do
    id="${value%%,*}"
    valid_telegram_id "$id" || return 1
    value="${value#*,}"
  done
  valid_telegram_id "$value"
}
