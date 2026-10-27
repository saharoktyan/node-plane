from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import grpc
from fastapi.testclient import TestClient

from backend.agent_inspection import inspection_error
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
