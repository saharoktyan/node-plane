#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

cd "$REPO_ROOT"

if ! python3 -c 'import grpc_tools.protoc' >/dev/null 2>&1; then
  echo "grpcio-tools is not installed. Install requirements-dev.txt first." >&2
  exit 1
fi

mkdir -p app/generated

python3 -m grpc_tools.protoc \
  -I proto \
  --python_out=app/generated \
  --grpc_python_out=app/generated \
  proto/driver/v1/types.proto \
  proto/driver/v1/node_service.proto \
  proto/driver/v1/provisioning_service.proto \
  proto/driver/v1/runtime_service.proto \
  proto/driver/v1/telemetry_service.proto \
  proto/driver/v1/operation_service.proto

touch app/generated/__init__.py

echo "Generated Python gRPC stubs under app/generated"
