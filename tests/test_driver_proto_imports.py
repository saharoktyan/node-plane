import importlib
import sys
import unittest
from pathlib import Path

try:
    import grpc  # noqa: F401
    import google.protobuf  # noqa: F401
except ModuleNotFoundError:
    DRIVER_PROTO_RUNTIME_AVAILABLE = False
else:
    DRIVER_PROTO_RUNTIME_AVAILABLE = True


APP_DIR = Path(__file__).resolve().parents[1] / "app"
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))


class DriverProtoImportTests(unittest.TestCase):
    @unittest.skipUnless(
        DRIVER_PROTO_RUNTIME_AVAILABLE,
        "grpcio and protobuf runtime dependencies are not installed",
    )
    def test_generated_driver_stubs_import_with_runtime_dependencies(self):
        modules = (
            "driver.v1.types_pb2",
            "driver.v1.node_service_pb2",
            "driver.v1.provisioning_service_pb2",
            "driver.v1.runtime_service_pb2",
            "driver.v1.telemetry_service_pb2",
            "driver.v1.operation_service_pb2",
            "driver.v1.types_pb2_grpc",
            "driver.v1.node_service_pb2_grpc",
            "driver.v1.provisioning_service_pb2_grpc",
            "driver.v1.runtime_service_pb2_grpc",
            "driver.v1.telemetry_service_pb2_grpc",
            "driver.v1.operation_service_pb2_grpc",
        )

        for module in modules:
            with self.subTest(module=module):
                importlib.import_module(module)


if __name__ == "__main__":
    unittest.main()
