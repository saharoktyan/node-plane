#!/usr/bin/env bash
set -euo pipefail
set -a
source /etc/node-plane/node.env
set +a
exec python3 "$(dirname "${BASH_SOURCE[0]}")/inspect-profile.py" "$@"
