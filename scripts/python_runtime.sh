#!/usr/bin/env bash

# Select a tested interpreter without changing the host's default python3.
select_python_runtime() {
  local requested="${NODE_PLANE_PYTHON_BIN:-}"
  local candidate resolved version
  local -a candidates
  if [[ -n "$requested" ]]; then
    candidates=("$requested")
  else
    candidates=(python3 python3.12 python3.11)
  fi

  for candidate in "${candidates[@]}"; do
    if ! resolved="$(command -v "$candidate" 2>/dev/null)"; then
      continue
    fi
    if ! version="$("$resolved" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null)"; then
      continue
    fi
    case "$version" in
      3.12|3.11)
        printf '%s\n' "$resolved"
        return 0
        ;;
    esac
  done

  if [[ -n "$requested" ]]; then
    echo "NODE_PLANE_PYTHON_BIN=${requested} is unavailable or is not Python 3.11/3.12." >&2
  else
    echo "No supported Python runtime found (requires Python 3.11 or 3.12)." >&2
    if command -v python3 >/dev/null 2>&1; then
      python3 --version >&2 || true
    fi
    echo "Install python3.12 and python3.12-venv (or their 3.11 equivalents); python3 may remain at its system version." >&2
  fi
  return 1
}
