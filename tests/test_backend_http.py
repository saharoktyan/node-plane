from __future__ import annotations

from uuid import uuid4
import unittest

from tests.test_backend_identity import Database
from backend.admin_cli import bootstrap_admin
from backend.authorization import PrincipalKind
from backend.credentials import CredentialService, ADAPTER_SCOPES
from backend.identity_repository import SQLIdentityRepository
from backend.profiles import ProfileRepository
from backend.profile_commands import ProfileCommands
from backend.http_api import create_app
from backend.access_requests import AccessRequestService
from backend.accounts import AccountService
from backend.nodes import NodeService
from backend.node_settings import NodeSettingsService
from backend.config_issuance import ConfigIssuanceService
from backend.agent_rollout import AgentRolloutService
from fastapi.testclient import TestClient


class BackendHTTPTests(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        self.addCleanup(self.db.connection.close)
        self.identities = SQLIdentityRepository(self.db)
        self.identities.initialize_schema()
        self.credentials = CredentialService(self.db)
        self.credentials.initialize_schema()
        ProfileRepository(self.db).initialize_schema()
        ProfileCommands(self.db).initialize_schema()
        AccessRequestService(self.db).initialize_schema()
        AccountService(self.db).initialize_schema()
        NodeService(self.db).initialize_schema()
        NodeSettingsService(self.db).initialize_schema()
        ConfigIssuanceService(self.db).initialize_schema()
        AgentRolloutService(self.db).initialize_schema()
        self.admin = bootstrap_admin(self.identities, 101)
        self.token_id, self.token = self.credentials.issue(PrincipalKind.ADAPTER, ADAPTER_SCOPES)
        self.headers = {'Authorization': 'Bearer ' + self.token}
        self.client = TestClient(create_app(self.db))
        self.addCleanup(self.client.close)

    def register(self, user_id, key=None, **fields):
        return self.client.post('/api/v1/integrations/telegram/identities/resolve',
            headers={**self.headers, 'Idempotency-Key': key or str(uuid4())},
            json={'telegram_user_id': user_id, **fields})

    def test_authenticated_me_and_no_auth_rejected(self):
        self.assertEqual(self.client.get('/api/v1/me').status_code, 401)
        response = self.client.get('/api/v1/me', headers={**self.headers, 'X-Node-Plane-Telegram-User-ID': '101'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['id'], self.admin.id)
        self.assertIn('nodes.execute', response.json()['permissions'])
        self.assertEqual(response.headers['Cache-Control'], 'no-store')

    def test_registration_deduplicates_and_rejects_key_reuse(self):
        key = str(uuid4())
        first = self.register(102, key)
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()['status'], 'pending')
        self.assertEqual(first.json()['role'], 'member')
        self.assertEqual(self.register(102, key).json()['id'], first.json()['id'])
        conflict = self.register(103, key)
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()['error']['code'], 'idempotency_conflict')
        self.assertIsNone(self.identities.find_telegram_account(103))

    def test_registration_rejects_role_assignment_and_bad_types(self):
        for body in ({'telegram_user_id': 102, 'role': 'admin'}, {'telegram_user_id': '102'},
                     {'telegram_user_id': True}, {'telegram_user_id': -1}):
            response = self.client.post('/api/v1/integrations/telegram/identities/resolve',
                headers={**self.headers, 'Idempotency-Key': str(uuid4())}, json=body)
            self.assertEqual(response.status_code, 422)
        self.assertIsNone(self.identities.find_telegram_account(102))

    def test_invalid_header_and_missing_key_are_rejected(self):
        response = self.client.post('/api/v1/integrations/telegram/identities/resolve', headers=self.headers,
                                    json={'telegram_user_id': 102})
        self.assertEqual(response.status_code, 422)
        for value in ('-1', '١٠١', '101.0', '9' * 20):
            # HTTP header values are ASCII; Unicode input exercised by policy tests.
            if not value.isascii():
                continue
            response = self.client.get('/api/v1/me', headers={**self.headers, 'X-Node-Plane-Telegram-User-ID': value})
            self.assertEqual(response.status_code, 422)

    def test_account_cannot_delegate_or_register(self):
        _, token = self.credentials.issue(PrincipalKind.ACCOUNT, frozenset({'account.self.read'}), account_id=self.admin.id)
        headers = {'Authorization': 'Bearer ' + token}
        self.assertEqual(self.client.get('/api/v1/me', headers=headers).status_code, 200)
        self.assertEqual(self.client.get('/api/v1/me', headers={**headers, 'X-Node-Plane-Telegram-User-ID': '101'}).status_code, 403)
        self.assertEqual(self.client.post('/api/v1/integrations/telegram/identities/resolve',
            headers={**headers, 'Idempotency-Key': str(uuid4())}, json={'telegram_user_id': 102}).status_code, 403)

    def test_revocation_and_error_redaction(self):
        self.credentials.revoke(self.token_id)
        response = self.client.get('/api/v1/me', headers=self.headers)
        self.assertEqual(response.status_code, 401)
        self.assertNotIn(self.token, response.text)
        self.assertIn('request_id', response.json()['error'])
        self.assertEqual(response.headers['WWW-Authenticate'], 'Bearer')
        self.assertEqual(response.headers['X-Request-ID'], response.json()['error']['request_id'])

    def test_duplicate_security_headers_rejected(self):
        response = self.client.get('/api/v1/me', headers=[('Authorization', 'Bearer ' + self.token),
            ('Authorization', 'Bearer invalid'), ('X-Node-Plane-Telegram-User-ID', '101')])
        self.assertEqual(response.status_code, 422)

    def test_readiness_requires_schema_and_openapi_exists(self):
        self.assertEqual(self.client.get('/health/ready').status_code, 200)
        empty = Database()
        self.addCleanup(empty.connection.close)
        with TestClient(create_app(empty)) as client:
            self.assertEqual(client.get('/health/live').status_code, 200)
            response = client.get('/health/ready')
            self.assertEqual(response.status_code, 503)
            self.assertNotIn('table', response.text)
        paths = self.client.get('/openapi.json').json()['paths']
        self.assertIn('/api/v1/me', paths)
