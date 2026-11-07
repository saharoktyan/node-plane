import os
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import grpc
from fastapi.testclient import TestClient

from backend.agent_inspection import inspection_error
from backend.driver_transport import LocalDriverTokenMissing
from backend.http_api import create_app
from tests import test_backend_http as fixture


class RpcFailure(grpc.RpcError):
    def __init__(self, code, detail):
        self.status, self.detail = code, detail

    def code(self):
        return self.status

    def details(self):
        return self.detail


class AgentInspectionTests(unittest.TestCase):
    def test_errors_separate_missing_route_identity_runtime_and_connection(self):
        for status, detail, expected, hint in (
                (grpc.StatusCode.FAILED_PRECONDITION, 'no node-agent target configured', 'node_agent_unconfigured', 'missing_route'),
                (grpc.StatusCode.FAILED_PRECONDITION, 'agent node identity mismatch', 'node_agent_identity_mismatch', 'wrong_node_key'),
                (grpc.StatusCode.FAILED_PRECONDITION, 'runtime failure SECRET', 'node_agent_inspection_failed', 'runtime_inspection'),
                (grpc.StatusCode.UNAVAILABLE, 'transport error: invalid peer certificate SECRET', 'node_agent_unavailable', 'tls'),
                (grpc.StatusCode.UNAVAILABLE, 'connection refused SECRET', 'node_agent_unavailable', 'connection_refused'),
                (grpc.StatusCode.DEADLINE_EXCEEDED, 'SECRET', 'node_agent_unavailable', 'timeout')):
            with self.subTest(expected=expected, hint=hint), self.assertLogs('backend.agent_inspection') as logs:
                error = inspection_error('n1', RpcFailure(status, detail))
                self.assertEqual(error.code, expected)
                output = ' '.join(logs.output)
                self.assertIn('cause=' + hint, output)
                self.assertNotIn('SECRET', output)

    def test_missing_local_token_classified_as_driver_unavailable_without_secret(self):
        for fallback in ('driver_unavailable', 'node_agent_unavailable'):
            with self.subTest(fallback=fallback), self.assertLogs('backend.agent_inspection') as logs:
                error = inspection_error(
                    'n1',
                    LocalDriverTokenMissing('local token missing SECRET_LOCAL_TOKEN'),
                    fallback=fallback,
                )
                self.assertEqual(error.code, 'driver_unavailable')
                self.assertEqual(error.status, 503)
                output = ' '.join(logs.output)
                self.assertIn('cause=missing_local_token', output)
                self.assertIn('rpc_status=non_rpc_error', output)
                self.assertNotIn('SECRET_LOCAL_TOKEN', output)
                self.assertNotIn('node_agent_unconfigured', output)
                self.assertNotIn('missing_route', output)
                self.assertNotIn('node_agent_unavailable', error.code)


class AgentInspectionHTTPTests(unittest.TestCase):
    setUp = fixture.BackendHTTPTests.setUp

    def test_services_runtime_error_is_not_reported_as_missing_agent(self):
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO backend_nodes(key,title,region,enabled,protocols_json,xray_transports_json) VALUES ('n1','Node','EU',0,'[]','[]')")
        headers = {**self.headers, 'X-Node-Plane-Telegram-User-ID': '101'}
        driver = SimpleNamespace(inspect_node_services=Mock(side_effect=RpcFailure(
            grpc.StatusCode.FAILED_PRECONDITION, 'runtime failure SECRET')))
        with TestClient(create_app(self.db, node_driver=driver)) as client, \
                self.assertLogs('backend.agent_inspection') as logs:
            result = client.get('/api/v1/nodes/n1/services', headers=headers)
        self.assertEqual(result.status_code, 502)
        self.assertEqual(result.json()['error']['code'], 'node_agent_inspection_failed')
        self.assertNotIn('SECRET', result.text + ' '.join(logs.output))

    def test_services_and_probe_http_missing_local_token_returns_503_driver_unavailable(self):
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO backend_nodes(key,title,region,enabled,protocols_json,xray_transports_json,applied_revision) "
                "VALUES ('n1','Node','EU',0,'[]','[]',1)"
            )
        headers = {**self.headers, 'X-Node-Plane-Telegram-User-ID': '101'}

        # Production create_app without injected fake driver
        prod_app = create_app(self.db)  # node_driver is None
        secret_leak = 'SECRET_LOCAL_AUTH_KEY_987'
        with TestClient(prod_app) as client:
            for path in (
                '/api/v1/nodes/n1/services',
                '/api/v1/nodes/n1/probe',
                '/api/v1/nodes/n1/runtime',
                '/api/v1/nodes/n1/diagnostics',
            ):
                with self.subTest(path=path), \
                     patch('backend.driver_transport.local_channel',
                           side_effect=LocalDriverTokenMissing(f'local Driver token missing: {secret_leak}')), \
                     self.assertLogs('backend.agent_inspection') as logs:
                    result = client.get(path, headers=headers)

                self.assertEqual(result.status_code, 503)
                self.assertEqual(result.json()['error']['code'], 'driver_unavailable')
                log_output = ' '.join(logs.output)
                self.assertIn('cause=missing_local_token', log_output)
                self.assertIn('rpc_status=non_rpc_error', log_output)
                self.assertNotIn(secret_leak, result.text + log_output)
                self.assertNotIn('node_agent_unconfigured', result.text + log_output)
                self.assertNotIn('missing_route', log_output)
                self.assertNotIn('node_agent_unavailable', result.text)

            # Also verify unpatched local_channel acquisition with empty environment token
            with patch.dict(os.environ, {'NODE_PLANE_DRIVER_LOCAL_TOKEN': ''}, clear=False):
                for path in ('/api/v1/nodes/n1/services', '/api/v1/nodes/n1/probe'):
                    with self.subTest(path=path, env_empty=True), \
                         self.assertLogs('backend.agent_inspection') as logs:
                        result = client.get(path, headers=headers)

                    self.assertEqual(result.status_code, 503)
                    self.assertEqual(result.json()['error']['code'], 'driver_unavailable')
                    log_output = ' '.join(logs.output)
                    self.assertIn('cause=missing_local_token', log_output)
                    self.assertIn('rpc_status=non_rpc_error', log_output)
                    self.assertNotIn('node_agent_unconfigured', result.text + log_output)
