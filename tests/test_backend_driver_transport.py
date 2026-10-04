from concurrent.futures import ThreadPoolExecutor
import importlib.util
import unittest


@unittest.skipUnless(importlib.util.find_spec('grpc'), 'grpc runtime unavailable')
class BackendDriverTransportTests(unittest.TestCase):
    def test_explicit_intent_and_command_identity_cross_grpc_boundary(self):
        import grpc
        from backend.driver_transport import GrpcIntentDriver
        from driver.v1 import provisioning_service_pb2_grpc, operation_service_pb2_grpc, node_service_pb2_grpc, runtime_service_pb2_grpc, types_pb2

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
                if self_id == 'node-settings-operation':
                    return types_pb2.Operation(operation_id=self_id, kind='apply_backend_node_settings',
                        node_key='node', status='SUCCEEDED', result_json='{"node_key":"node"}')
                return types_pb2.Operation(operation_id=self_id, status='SUCCEEDED', result_json='{"config":"private"}')

        class Runtime(runtime_service_pb2_grpc.RuntimeServiceServicer):
            def GetBackendXrayPublic(self, request, context):
                from driver.v1 import runtime_service_pb2
                return runtime_service_pb2.BackendXrayPublicResult(node_key=request.node_key,
                    metadata_json='{"sni":"www.cloudflare.com","public_key":"' + 'a' * 43 +
                    '","short_id":"0123456789abcdef","tcp_port":443,"xhttp_port":8443,'
                    '"xhttp_path":"/assets","flow":"xtls-rprx-vision","fingerprint":"chrome"}')

            def PrepareBackendNode(self, request, context):
                from driver.v1 import runtime_service_pb2
                return runtime_service_pb2.BackendNodeSettingsResult(
                    result_json='{"node_key":"node","prepared":true}')

            def ApplyBackendNodeSettings(self, request, context):
                received.append((request, dict(context.invocation_metadata())))
                return types_pb2.StartOperationResponse(operation_id='node-settings-operation')

            def RecoverBackendNodeSettings(self, request, context):
                from driver.v1 import runtime_service_pb2
                return runtime_service_pb2.BackendNodeSettingsResult(result_json='{"node_key":"node"}')

            def ResolveBackendNodeSettings(self, request, context):
                from driver.v1 import runtime_service_pb2
                received.append((request, dict(context.invocation_metadata())))
                return runtime_service_pb2.BackendNodeSettingsResult(
                    result_json='{"config_matches":false,"containers_running":true}')

        class Nodes(node_service_pb2_grpc.NodeServiceServicer):
            def InspectBackendNode(self, request, context):
                from driver.v1 import node_service_pb2
                return node_service_pb2.BackendNodeObservation(node_key=request.node_key,
                    health_state='running', runtime_version='test', runtime_commit='abc',
                    agent_version='0.4.3-alpha.20', agent_commit='binary-sha',
                    xray_config_present=True, awg_config_present=False)

            def GetNodeDiagnostics(self, request, context):
                from driver.v1 import node_service_pb2
                return node_service_pb2.GetNodeDiagnosticsResponse(node_key=request.node_key,
                    items=[types_pb2.DiagnosticItem(kind=kind, status=status,
                        summary=summary) for kind, status, summary in (
                            ('docker', 'ok', 'Docker daemon available'),
                            ('runtime_root', 'ok', '/opt/node-plane-runtime'),
                            ('xray_config', 'ok', '/private/xray/config.json'),
                            ('awg_config', 'missing', '/private/awg/wg0.conf'),
                            ('runtime_version', 'ok', '0.4.3'))])

        server = grpc.server(ThreadPoolExecutor(max_workers=2))
        provisioning_service_pb2_grpc.add_ProvisioningServiceServicer_to_server(Provisioning(), server)
        operation_service_pb2_grpc.add_OperationServiceServicer_to_server(Operations(), server)
        node_service_pb2_grpc.add_NodeServiceServicer_to_server(Nodes(), server)
        runtime_service_pb2_grpc.add_RuntimeServiceServicer_to_server(Runtime(), server)
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
            node = driver.inspect_node('node')
            diagnostics = driver.diagnose_node('node')
            driver.prepare_node('node')
            public = driver.read_xray_public('node')
            node_intent = {'node_key': 'node', 'revision': 2, 'protocols': ['awg'],
                           'settings': {'public_host': 'node.example', 'awg_port': 51820}}
            applied = driver.apply_node_settings('node-settings-task', node_intent)
            node_recovered = driver.recover_node_settings('node-settings-task', node_intent)
            node_resolved = driver.resolve_node_settings('node-settings-task', node_intent)
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
        self.assertEqual(len(received), 3)
        self.assertTrue(result['succeeded'])
        self.assertEqual(result['driver_operation_id'], 'driver-operation')
        request, metadata = received[0]
        self.assertEqual(metadata['x-node-plane-command-id'], 'stable-task-id')
        self.assertEqual(request.desired_revision, 2)
        self.assertEqual(request.xray.profile_name, 'p_test')
        self.assertEqual(request.xray.uuid, intent['xray']['uuid'])
        self.assertEqual(applied, '{"node_key":"node"}')
        self.assertEqual(node_recovered, applied)
        self.assertEqual(node_resolved, {'config_matches': False, 'containers_running': True})
        node_request, node_metadata = received[1]
        self.assertEqual(node_metadata['x-node-plane-command-id'], 'node-settings-task')
        self.assertEqual(node_request.desired_revision, 2)
        self.assertEqual(node_request.settings_json, '{"awg_port":51820,"public_host":"node.example"}')
        self.assertEqual(received[2][1]['x-node-plane-command-id'], 'node-settings-task')
        self.assertEqual(node, {'node_key': 'node', 'health_state': 'running',
                                'runtime_version': 'test', 'runtime_commit': 'abc',
                                'agent_version': '0.4.3-alpha.20', 'agent_commit': 'binary-sha',
                                'xray_config_present': True, 'awg_config_present': False})
        self.assertEqual(diagnostics, {'node_key': 'node', 'docker': 'ok',
            'runtime_root': 'ok', 'xray_config': 'ok', 'awg_config': 'missing',
            'runtime_version': '0.4.3'})
        self.assertEqual(public['public_key'], 'a' * 43)

    def test_diagnostics_reject_legacy_response_without_agent_items(self):
        from backend.driver_transport import AgentNotConfigured, GrpcIntentDriver
        from driver.v1 import node_service_pb2
        from unittest.mock import Mock
        driver = GrpcIntentDriver.__new__(GrpcIntentDriver)
        driver.nodes = Mock()
        driver.timeout = 1
        driver.nodes.GetNodeDiagnostics.return_value = (
            node_service_pb2.GetNodeDiagnosticsResponse(node_key='node', summary='saved state'))
        with self.assertRaises(AgentNotConfigured):
            driver.diagnose_node('node')

    def test_remote_driver_requires_explicit_secure_transport(self):
        from backend.driver_transport import local_channel
        with self.assertRaises(ValueError):
            local_channel('remote.example:50051')
