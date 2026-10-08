from types import SimpleNamespace
import tempfile
import sys
from unittest import TestCase
from unittest.mock import patch, Mock
from uuid import uuid4

from tests import test_workstation_cli as fixture
from tests import test_backend_http as http_fixture
from backend.workstation_cli import WorkstationService, WorkstationError, validate_request


ATTRIBUTION = {'ssh_user': 'deploy', 'device_fingerprint': 'SHA256:' + 'a' * 43}


class CredentialAttributionTests(TestCase):
    setUp = fixture.WorkstationCredentialTests.setUp

    def issue(self):
        return self.service.handle({'version': 1, 'action': 'authenticate',
            'session_id': self.session_id, 'attribution': ATTRIBUTION, 'account_id': self.admin.id}, now=self.now)

    def test_device_admin_and_ssh_user_are_bound_and_retry_does_not_duplicate_event(self):
        first = self.issue()
        self.assertEqual(self.issue()['credential_id'], first['credential_id'])
        context = self.service.audit.context(first['credential_id'])
        self.assertEqual(context['account_label'], '@operator')
        self.assertEqual(context['ssh_user'], 'deploy')
        self.assertEqual(context['device_fingerprint'], ATTRIBUTION['device_fingerprint'])
        rows = self.db.connection.execute('SELECT * FROM backend_workstation_audit').fetchall()
        self.assertEqual(len(rows), 2)
        self.assertNotIn(first['token'], str([dict(row) for row in rows]))
        with self.assertRaises(WorkstationError):
            self.service.handle({'version': 1, 'action': 'authenticate', 'session_id': self.session_id,
                'attribution': {**ATTRIBUTION, 'ssh_user': 'another'}}, now=self.now)

    def test_revocation_keeps_attributed_history_without_token(self):
        first = self.issue()
        request = {'version': 1, 'action': 'revoke', 'session_id': self.session_id}
        self.service.handle(request)
        self.service.handle(request)
        rows = self.db.connection.execute('SELECT * FROM backend_workstation_audit').fetchall()
        self.assertEqual([row['phase'] for row in rows], ['issued', 'issued', 'revoked'])
        self.assertTrue(all(row['account_id'] == self.admin.id for row in rows))
        self.assertNotIn(first['token'], str([dict(row) for row in rows]))

    def test_audit_snapshots_survive_account_deletion(self):
        self.issue()
        self.db.connection.execute('DELETE FROM backend_credentials WHERE account_id=?', (self.admin.id,))
        self.db.connection.execute('DELETE FROM backend_external_identities WHERE account_id=?', (self.admin.id,))
        self.db.connection.execute('DELETE FROM backend_accounts WHERE id=?', (self.admin.id,))
        row = self.db.connection.execute('SELECT account_id, account_label FROM backend_workstation_audit WHERE credential_id IS NOT NULL').fetchone()
        self.assertEqual(dict(row), {'account_id': self.admin.id, 'account_label': '@operator'})

    def test_enrollment_records_target_key_and_actor_without_key_material(self):
        self.issue()
        command_id = str(uuid4())
        request = {'version': 1, 'action': 'audit-enrollment', 'session_id': self.session_id,
            'command_id': command_id, 'target': 'root@[vps.example]:22',
            'key_fingerprint': 'SHA256:' + 'b' * 43, 'outcome': 'admitted'}
        self.service.handle(request)
        self.service.handle({**request, 'outcome': 'succeeded'})
        rows = self.db.connection.execute('''SELECT a.account_label,a.command_id,e.*
            FROM backend_workstation_audit a JOIN backend_workstation_enrollment e ON e.event_id=a.id
            ORDER BY a.occurred_at''').fetchall()
        self.assertEqual([row['outcome'] for row in rows], ['admitted', 'succeeded'])
        self.assertTrue(all(row['account_label'] == '@operator' and row['command_id'] == command_id for row in rows))
        self.assertEqual(rows[0]['target'], request['target'])
        self.assertEqual(rows[0]['key_fingerprint'], request['key_fingerprint'])
        self.service.handle({'version': 1, 'action': 'revoke', 'session_id': self.session_id})
        with self.assertRaises(WorkstationError):
            self.service.handle(request)

    def test_enrollment_rejects_missing_identity_and_free_form_payloads(self):
        request = {'version': 1, 'action': 'audit-enrollment', 'session_id': self.session_id,
            'command_id': str(uuid4()), 'target': 'root@[vps.example]:22',
            'key_fingerprint': 'SHA256:' + 'b' * 43, 'outcome': 'admitted'}
        with self.assertRaises(WorkstationError):
            self.service.handle(request)
        for extra in ({'target': 'secret\nserver'}, {'outcome': {}}, {'outcome': 'raw-exception'},
                      {'key_fingerprint': 'ssh-ed25519 actual-key'}, {'private_key': 'secret'}):
            with self.assertRaises(WorkstationError):
                validate_request({**request, **extra})
        with self.assertRaises(WorkstationError):
            self.service.handle({'version': 1, 'action': 'authenticate', 'session_id': self.session_id})
        with self.assertRaises(WorkstationError):
            self.service.handle(request)


class APIAuditTests(TestCase):
    setUp = http_fixture.BackendHTTPTests.setUp

    def test_workstation_reads_node_registry_without_telegram_delegation(self):
        with tempfile.TemporaryDirectory() as directory:
            service = WorkstationService(self.db, __import__('pathlib').Path(directory) / 'sessions')
            credential = service.handle({'version': 1, 'action': 'authenticate',
                'session_id': str(uuid4()), 'attribution': ATTRIBUTION, 'account_id': self.admin.id})
            headers = {'Authorization': 'Bearer ' + credential['token']}
            response = self.client.get('/api/v1/nodes?order=region&include_summary=true', headers=headers)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()['items'], [])
            self.assertEqual(self.client.get('/api/v1/profiles', headers=headers).status_code, 403)

    def test_outcome_logging_failure_does_not_invite_replay_of_committed_action(self):
        import app.services
        from backend.workstation_audit import WorkstationAudit
        with tempfile.TemporaryDirectory() as directory:
            service = WorkstationService(self.db, __import__('pathlib').Path(directory) / 'sessions')
            credential = service.handle({'version': 1, 'action': 'authenticate',
                'session_id': str(uuid4()), 'attribution': ATTRIBUTION, 'account_id': self.admin.id})
            original = WorkstationAudit.record
            def record(audit, context, action, phase, **kwargs):
                if phase == 'completed':
                    raise RuntimeError('result journal unavailable')
                return original(audit, context, action, phase, **kwargs)
            updater = SimpleNamespace(check_for_updates=Mock(return_value={'status': 'ok'}))
            with patch.dict(sys.modules, {'app.services.updates': updater}), \
                    patch.object(app.services, 'updates', updater, create=True), \
                    patch.object(WorkstationAudit, 'record', record):
                response = self.client.post('/api/v1/system/updates/check',
                    headers={'Authorization': 'Bearer ' + credential['token']})
            self.assertEqual(response.status_code, 200, response.text)
            updater.check_for_updates.assert_called_once()
            rows = self.db.connection.execute("SELECT phase FROM backend_workstation_audit WHERE action LIKE 'POST%'").fetchall()
            self.assertEqual([r['phase'] for r in rows], ['admitted'])

    def test_mutation_result_is_audited_from_credential_not_headers_or_body(self):
        with tempfile.TemporaryDirectory() as directory:
            service = WorkstationService(self.db, __import__('pathlib').Path(directory) / 'sessions')
            credential = service.handle({'version': 1, 'action': 'authenticate',
                'session_id': str(uuid4()), 'attribution': ATTRIBUTION, 'account_id': self.admin.id})
            headers = {'Authorization': 'Bearer ' + credential['token'], 'Idempotency-Key': str(uuid4()),
                       'X-Workstation-User': 'forged-person'}
            import app.services
            updater = SimpleNamespace(check_for_updates=lambda: {'status': 'ok'})
            with patch.dict(sys.modules, {'app.services.updates': updater}), \
                    patch.object(app.services, 'updates', updater, create=True):
                response = self.client.post('/api/v1/system/updates/check', headers=headers,
                    json={'token': 'SECRET-PAYLOAD-NEVER-LOG'})
            self.assertEqual(response.status_code, 200, response.text)
            audit = self.client.get('/api/v1/system/workstation-audit', headers=headers)
            self.assertEqual(audit.status_code, 200, audit.text)
            rows = audit.json()['items']
            mutation = [item for item in rows if item['action'].startswith('POST')]
            self.assertEqual({item['phase'] for item in mutation}, {'admitted', 'completed'})
            completed = next(item for item in mutation if item['phase'] == 'completed')
            self.assertEqual(completed['http_status'], 200)
            self.assertEqual(completed['request_id'], response.headers['X-Request-ID'])
            self.assertEqual(completed['command_id'], headers['Idempotency-Key'])
            self.assertEqual(completed['account_id'], self.admin.id)
            self.assertEqual(completed['ssh_user'], 'deploy')
            self.assertNotIn('SECRET-PAYLOAD', audit.text)
            self.assertNotIn('forged-person', audit.text)
            self.assertNotIn(credential['token'], audit.text)
            self.assertEqual(self.client.get('/api/v1/system/workstation-audit').status_code, 401)
            service.handle({'version': 1, 'action': 'audit-enrollment',
                'session_id': service.audit.context(credential['credential_id'])['session_id'],
                'command_id': str(uuid4()), 'target': 'root@[vps.example]:22',
                'key_fingerprint': 'SHA256:' + 'b' * 43, 'outcome': 'succeeded'})
            events = self.client.get('/api/v1/system/workstation-audit', headers=headers).json()['items']
            enrolled = next(event for event in events if event['target'])
            self.assertEqual(enrolled['outcome'], 'succeeded')
            self.assertEqual(enrolled['key_fingerprint'], 'SHA256:' + 'b' * 43)
