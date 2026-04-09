#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "$REPO_ROOT"

PYTHON_BIN="${PYTHON_BIN:-}"
if [[ -z "$PYTHON_BIN" ]]; then
  if [[ -x "$REPO_ROOT/.venv/bin/python" ]]; then
    PYTHON_BIN="$REPO_ROOT/.venv/bin/python"
  else
    PYTHON_BIN="python3"
  fi
fi

if ! "$PYTHON_BIN" -c 'import grpc_tools.protoc' >/dev/null 2>&1; then
  echo "grpcio-tools is not installed. Install requirements-dev.txt first." >&2
  exit 1
fi

mkdir -p app/driver/v1

"$PYTHON_BIN" -m grpc_tools.protoc \
  -I proto \
  --python_out=app \
  --grpc_python_out=app \
  proto/driver/v1/types.proto \
  proto/driver/v1/node_service.proto \
  proto/driver/v1/provisioning_service.proto \
  proto/driver/v1/runtime_service.proto \
  proto/driver/v1/telemetry_service.proto \
  proto/driver/v1/operation_service.proto

touch app/driver/__init__.py
touch app/driver/v1/__init__.py

echo "Generated Python gRPC stubs under app/driver"
