from concurrent.futures import ThreadPoolExecutor
import importlib.util
import unittest


@unittest.skipUnless(importlib.util.find_spec('grpc'), 'grpc runtime unavailable')
class BackendDriverTransportTests(unittest.TestCase):
    def test_explicit_intent_and_command_identity_cross_grpc_boundary(self):
        import grpc
        from backend.driver_transport import GrpcIntentDriver
        from driver.v1 import provisioning_service_pb2_grpc, operation_service_pb2_grpc, types_pb2

        received = []
        lookups = []

        class Provisioning(provisioning_service_pb2_grpc.ProvisioningServiceServicer):
            def ResolveProfileIntent(self, request, context):
                from driver.v1 import provisioning_service_pb2
                lookups.append('resolve:' + dict(context.invocation_metadata())['x-node-plane-command-id'])
                return provisioning_service_pb2.ProfileInspection(disk_present=True,
                    live_present=False, identity_matches=False, config_available=False)

            def RecoverProfileIntent(self, request, context):
                from driver.v1 import provisioning_service_pb2
                lookups.append('agent:' + dict(context.invocation_metadata())['x-node-plane-command-id'])
                return provisioning_service_pb2.RecoverProfileIntentResponse(payload_json='{"config":"recovered"}')
            def InspectProfileIntent(self, request, context):
                from driver.v1 import provisioning_service_pb2
                return provisioning_service_pb2.ProfileInspection(disk_present=True,
                    live_present=True, identity_matches=True, config_available=True)

            def ApplyProfileIntent(self, request, context):
                received.append((request, dict(context.invocation_metadata())))
                return types_pb2.StartOperationResponse(operation_id='driver-operation')

        class Operations(operation_service_pb2_grpc.OperationServiceServicer):
            def GetOperationByCommand(self, request, context):
                lookups.append(request.command_id)
                if request.command_id == 'missing-driver-result':
                    context.abort(grpc.StatusCode.NOT_FOUND, 'missing')
                if request.command_id == 'driver-timeout':
                    return types_pb2.Operation(operation_id='failed-driver-operation', kind='apply_profile_intent',
                        node_key='node', profile_name='p_test', status='FAILED')
                return types_pb2.Operation(operation_id='driver-operation', kind='apply_profile_intent',
                    node_key='node', profile_name='p_test', status='SUCCEEDED', result_json='{"config":"private"}')

            def GetOperation(self, request, context):
                self_id = request.operation_id
                return types_pb2.Operation(operation_id=self_id, status='SUCCEEDED', result_json='{"config":"private"}')

        server = grpc.server(ThreadPoolExecutor(max_workers=2))
        provisioning_service_pb2_grpc.add_ProvisioningServiceServicer_to_server(Provisioning(), server)
        operation_service_pb2_grpc.add_OperationServiceServicer_to_server(Operations(), server)
        port = server.add_insecure_port('127.0.0.1:0')
        server.start()
        self.addCleanup(lambda: server.stop(0).wait())
        with grpc.insecure_channel(f'127.0.0.1:{port}') as channel:
            driver = GrpcIntentDriver(channel)
            intent = {'node_key': 'node', 'runtime_name': 'p_test', 'protocol': 'xray',
                      'action': 'ensure', 'desired_revision': 2,
                      'xray': {'uuid': '12345678-1234-1234-1234-123456789abc', 'short_id': '0123456789abcdef'}}
            result = driver.execute('stable-task-id', intent)
            inspected = driver.inspect(intent)
            resolved = driver.resolve('stable-task-id', intent)
            recovered = driver.lookup('stable-task-id', intent)
            mismatched = driver.lookup('stable-task-id', {**intent, 'runtime_name': 'another'})
            delayed = driver.lookup('driver-timeout', intent)
            missing = driver.lookup('missing-driver-result', intent)
        self.assertTrue(recovered['succeeded'])
        self.assertEqual(inspected, {'disk_present': True, 'live_present': True,
                                     'identity_matches': True, 'config_available': True})
        self.assertEqual(resolved, {'disk_present': True, 'live_present': False,
                                    'identity_matches': False, 'config_available': False})
        self.assertFalse(mismatched['succeeded'])
        self.assertIsNone(mismatched['result_json'])
        self.assertEqual(lookups, ['resolve:stable-task-id', 'stable-task-id', 'stable-task-id', 'driver-timeout',
                                 'agent:driver-timeout', 'missing-driver-result', 'agent:missing-driver-result'])
        self.assertTrue(delayed['succeeded'])
        self.assertEqual(delayed['result_json'], '{"config":"recovered"}')
        self.assertTrue(missing['succeeded'])
        self.assertIsNone(missing['driver_operation_id'])
        self.assertEqual(len(received), 1)
        self.assertTrue(result['succeeded'])
        self.assertEqual(result['driver_operation_id'], 'driver-operation')
        request, metadata = received[0]
        self.assertEqual(metadata['x-node-plane-command-id'], 'stable-task-id')
        self.assertEqual(request.desired_revision, 2)
        self.assertEqual(request.xray.profile_name, 'p_test')
        self.assertEqual(request.xray.uuid, intent['xray']['uuid'])

    def test_remote_driver_requires_explicit_secure_transport(self):
        from backend.driver_transport import local_channel
        with self.assertRaises(ValueError):
            local_channel('remote.example:50051')
