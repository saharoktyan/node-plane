from __future__ import annotations

from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from tests.test_backend_identity import Database
from backend.admin_cli import bootstrap_admin
from backend.authorization import AccessDenied, PrincipalKind, require_permission, resolve_actor
from backend.credentials import CredentialService
from backend.identity_repository import SQLIdentityRepository
from backend.workstation_cli import (
    LIFETIME, SCOPES, LEGACY_SCOPES, WorkstationError, WorkstationService, main, validate_request,
)


class WorkstationCredentialTests(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        self.addCleanup(self.db.connection.close)
        self.identities = SQLIdentityRepository(self.db)
        self.identities.initialize_schema()
        self.credentials = CredentialService(self.db)
        self.credentials.initialize_schema()
        self.admin = bootstrap_admin(self.identities, 101)
        self.identities.update_telegram_details(101, username='operator')
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'sessions'
        self.service = WorkstationService(self.db, self.path)
        self.session_id = str(uuid4())
        self.now = datetime.now(timezone.utc)
        self.attribution = {'ssh_user': 'root', 'device_fingerprint': 'SHA256:' + 'a' * 43}
        self.db.connection.execute('''CREATE TABLE backend_update_jobs (
            id TEXT PRIMARY KEY, actor_id TEXT, command_key TEXT, kind TEXT, intent_json TEXT)''')
        for table in ('backend_node_jobs', 'backend_agent_rollouts'):
            self.db.connection.execute(f'''CREATE TABLE {table} (
                id TEXT PRIMARY KEY, actor_id TEXT, command_key TEXT, node_key TEXT, status TEXT)''')

    def authenticate(self, **selectors):
        request = {'version': 1, 'action': 'authenticate', 'session_id': self.session_id,
            'attribution': self.attribution, 'account_id': self.admin.id, **selectors}
        if 'telegram_id' in selectors:
            request.pop('account_id')
        return self.service.handle(request, now=self.now)

    def assert_denied(self, code, call):
        with self.assertRaises(WorkstationError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)

    def test_issue_short_lived_least_scoped_account_credential(self):
        result = self.authenticate()
        self.assertTrue(result['ok'])
        self.assertEqual(result['account_id'], self.admin.id)
        self.assertEqual(result['scopes'], sorted(SCOPES))
        self.assertEqual(datetime.fromisoformat(result['expires_at']) - self.now, LIFETIME)
        principal = self.credentials.authenticate('Bearer ' + result['token'], now=self.now)
        self.assertEqual(principal.kind, PrincipalKind.ACCOUNT)
        self.assertNotIn('delegate.telegram', principal.scopes)
        actor = resolve_actor(principal, self.identities)
        for permission in SCOPES:
            require_permission(actor, permission)
        with self.assertRaises(AccessDenied):
            require_permission(actor, 'profiles.manage')
        row = self.db.connection.execute('SELECT * FROM backend_credentials').fetchone()
        self.assertNotIn(result['token'], str(dict(row)))

    def test_first_key_requires_selection_even_with_one_admin(self):
        self.assert_denied('admin_selection_required', lambda: self.service.handle({
            'version': 1, 'action': 'authenticate', 'session_id': self.session_id,
            'attribution': self.attribution}))

    def test_new_session_resolves_permanent_binding_without_selector(self):
        self.authenticate()
        bootstrap_admin(self.identities, 102)
        result = WorkstationService(self.db, self.path).handle({'version': 1,
            'action': 'authenticate', 'session_id': str(uuid4()), 'attribution': self.attribution})
        self.assertEqual(result['account_id'], self.admin.id)
        other = self.identities.find_telegram_account(102)
        self.assert_denied('workstation_account_conflict', lambda: self.service.handle({
            'version': 1, 'action': 'authenticate', 'session_id': str(uuid4()),
            'attribution': self.attribution, 'account_id': other.id}))

    def test_key_revocation_denies_live_token_and_new_session_until_explicit_restore(self):
        first = self.authenticate()
        request = {'version': 1, 'action': 'revoke-access', 'session_id': str(uuid4()),
            'attribution': self.attribution}
        self.service.handle(request)
        with self.assertRaises(AccessDenied):
            self.credentials.authenticate('Bearer ' + first['token'])
        self.assert_denied('workstation_access_revoked', lambda: self.service.handle({
            **request, 'action': 'authenticate', 'session_id': str(uuid4()), 'account_id': self.admin.id}))
        self.service.handle({**request, 'action': 'restore-access', 'account_id': self.admin.id})
        with self.assertRaises(AccessDenied):
            self.credentials.authenticate('Bearer ' + first['token'])
        self.assertEqual(self.authenticate()['account_id'], self.admin.id)

    def test_deleted_account_tombstone_prevents_rebinding_to_remaining_admin(self):
        self.authenticate()
        self.db.connection.execute('DELETE FROM backend_credentials')
        self.db.connection.execute('DELETE FROM backend_external_identities')
        self.db.connection.execute('DELETE FROM backend_accounts')
        other = bootstrap_admin(self.identities, 102)
        self.assert_denied('approved_admin_required', lambda: self.service.handle({
            'version': 1, 'action': 'authenticate', 'session_id': str(uuid4()),
            'attribution': self.attribution}))
        self.assert_denied('workstation_account_conflict', lambda: self.service.handle({
            'version': 1, 'action': 'authenticate', 'session_id': str(uuid4()),
            'attribution': self.attribution, 'account_id': other.id}))

    def test_each_key_has_its_own_binding_and_restore_cannot_change_owner(self):
        self.authenticate()
        other = bootstrap_admin(self.identities, 102)
        other_context = {**self.attribution, 'device_fingerprint': 'SHA256:' + 'b' * 43}
        other_request = {'version': 1, 'action': 'authenticate', 'session_id': str(uuid4()),
            'attribution': other_context, 'account_id': other.id}
        other_token = self.service.handle(other_request)['token']
        self.service.handle({'version': 1, 'action': 'revoke-access', 'session_id': str(uuid4()),
            'attribution': self.attribution})
        self.assertEqual(self.credentials.authenticate('Bearer ' + other_token).account_id, other.id)
        self.assert_denied('workstation_account_conflict', lambda: self.service.handle({
            'version': 1, 'action': 'restore-access', 'session_id': str(uuid4()),
            'attribution': self.attribution, 'account_id': other.id}))

    def test_retry_returns_same_secret_and_credential(self):
        first = self.authenticate()
        second_service = WorkstationService(self.db, self.path)
        second = second_service.handle({'version': 1, 'action': 'authenticate',
            'session_id': self.session_id, 'attribution': self.attribution}, now=self.now + timedelta(minutes=5))
        self.assertEqual(first, second)
        self.assertEqual(self.db.connection.execute('SELECT COUNT(*) FROM backend_credentials').fetchone()[0], 1)

    def test_old_session_scopes_upgrade_without_changing_account(self):
        first = self.authenticate()
        old_id, old_token = self.credentials.issue(PrincipalKind.ACCOUNT, LEGACY_SCOPES,
            account_id=self.admin.id, now=self.now)
        old = {**first, 'credential_id': old_id, 'token': old_token, 'scopes': sorted(LEGACY_SCOPES)}
        old.pop('ok')
        with self.service.store.locked() as directory:
            self.service.store.write(directory, self.session_id, old)
        result = self.authenticate()
        self.assertEqual(result['account_id'], self.admin.id)
        self.assertEqual(result['scopes'], sorted(SCOPES))
        with self.assertRaises(AccessDenied):
            self.credentials.authenticate('Bearer ' + old_token, now=self.now)

    def test_embedded_helper_initializes_binding_schema_on_older_controller(self):
        self.db.connection.execute('DROP TABLE backend_workstation_keys')
        with patch('backend.workstation_audit.WorkstationAudit.initialize_schema'):
            self.service = WorkstationService(self.db, self.path)
        self.assertEqual(self.authenticate()['account_id'], self.admin.id)

    def test_expired_session_renews_same_actor_and_revokes_old_credential(self):
        first = self.authenticate()
        second = self.service.handle({'version': 1, 'action': 'authenticate',
            'session_id': self.session_id, 'attribution': self.attribution}, now=self.now + LIFETIME)
        self.assertEqual(second['account_id'], first['account_id'])
        self.assertNotEqual(second['credential_id'], first['credential_id'])
        with self.assertRaises(AccessDenied):
            self.credentials.authenticate('Bearer ' + first['token'], now=self.now)

    def test_multiple_admins_require_explicit_choice_without_credential_creation(self):
        second = bootstrap_admin(self.identities, 102)
        self.identities.update_telegram_details(102, first_name='Another', last_name='Admin')
        with self.assertRaises(WorkstationError) as caught:
            self.service.handle({'version': 1, 'action': 'authenticate',
                'session_id': self.session_id, 'attribution': self.attribution})
        self.assertEqual(caught.exception.code, 'admin_selection_required')
        self.assertEqual({choice['label'] for choice in caught.exception.choices}, {'101 · @operator', '102'})
        self.assertEqual(self.db.connection.execute('SELECT COUNT(*) FROM backend_credentials').fetchone()[0], 0)
        self.assertEqual(self.authenticate(account_id=second.id)['account_id'], second.id)

    def test_explicit_telegram_selection_does_not_create_unknown_identity(self):
        self.assert_denied('approved_admin_required', lambda: self.authenticate(telegram_id=999))
        self.assertIsNone(self.identities.find_telegram_account(999))
        self.assertEqual(self.authenticate(telegram_id=101)['account_id'], self.admin.id)

    def test_existing_session_cannot_change_account_even_with_explicit_choice(self):
        self.authenticate()
        other = bootstrap_admin(self.identities, 102)
        self.assert_denied('session_account_conflict', lambda: self.authenticate(account_id=other.id))
        self.assertEqual(self.authenticate()['account_id'], self.admin.id)

    def test_disabled_pending_or_demoted_accounts_cannot_authenticate(self):
        self.authenticate()
        for role, status in (('admin', 'pending'), ('admin', 'disabled'), ('member', 'approved')):
            with self.subTest(role=role, status=status):
                self.db.connection.execute('UPDATE backend_accounts SET role=?, status=? WHERE id=?',
                                           (role, status, self.admin.id))
                self.assert_denied('approved_admin_required', self.authenticate)

    def test_list_excludes_non_admins_and_contains_no_credentials(self):
        self.identities.resolve_telegram(103)
        self.authenticate()
        result = self.service.handle({'version': 1, 'action': 'list'})
        self.assertEqual(result['accounts'], [{'account_id': self.admin.id, 'label': '101 · @operator'}])
        self.assertNotIn('token', result)
        self.assertNotIn('np_', json.dumps(result))

    def test_revoke_is_scoped_to_session_and_is_terminal(self):
        owned = self.authenticate()
        other_id, other_token = self.credentials.issue(PrincipalKind.ACCOUNT, SCOPES, account_id=self.admin.id)
        request = {'version': 1, 'action': 'revoke', 'session_id': self.session_id}
        self.assertTrue(self.service.handle(request)['revoked'])
        self.assertTrue(self.service.handle(request)['revoked'])
        with self.assertRaises(AccessDenied):
            self.credentials.authenticate('Bearer ' + owned['token'])
        self.assertEqual(self.credentials.authenticate('Bearer ' + other_token).id, other_id)
        self.assert_denied('session_revoked', self.authenticate)
        self.assertFalse(self.service.handle({**request, 'session_id': str(uuid4())})['revoked'])

    def test_secrets_are_private_and_existing_unsafe_storage_is_refused(self):
        self.authenticate()
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o700)
        for file in self.path.iterdir():
            self.assertEqual(stat.S_IMODE(file.stat().st_mode), 0o600)
        self.path.chmod(0o755)
        self.assert_denied('unsafe_session_storage', self.authenticate)

    def test_symlink_directory_is_refused_without_writing_target(self):
        target = Path(self.directory.name) / 'target'
        target.mkdir(mode=0o700)
        self.path.symlink_to(target, target_is_directory=True)
        self.assert_denied('unsafe_session_storage', self.authenticate)
        self.assertEqual(list(target.iterdir()), [])

    def test_hard_linked_session_file_is_refused(self):
        self.authenticate()
        source = self.path / (self.session_id + '.json')
        os.link(source, self.path / 'duplicate.json')
        self.assert_denied('unsafe_session_storage', self.authenticate)

    def test_corrupt_session_is_not_silently_replaced(self):
        self.authenticate()
        file = self.path / (self.session_id + '.json')
        file.write_text('{"token":"broken"}')
        self.assert_denied('session_state_invalid', self.authenticate)
        self.assertEqual(self.db.connection.execute('SELECT COUNT(*) FROM backend_credentials').fetchone()[0], 1)

    def test_storage_failure_revokes_untracked_credential(self):
        with patch.object(self.service.store, 'write', side_effect=OSError('sensitive failure')):
            with self.assertRaises(OSError):
                self.authenticate()
        row = self.db.connection.execute('SELECT revoked_at FROM backend_credentials').fetchone()
        self.assertIsNotNone(row['revoked_at'])

    def test_lookup_recovers_own_accepted_command_and_exact_intent(self):
        self.authenticate()
        job_id, command_id = str(uuid4()), str(uuid4())
        self.db.connection.execute('INSERT INTO backend_update_jobs VALUES (?,?,?,?,?)',
            (job_id, self.admin.id, command_id, 'stack', json.dumps({'target_ref': 'v0.4.3-alpha.49', 'branch': 'dev'})))
        result = self.service.handle({'version': 1, 'action': 'lookup-update',
            'session_id': self.session_id, 'command_id': command_id}, now=self.now)
        self.assertEqual(result['job'], {'id': job_id, 'kind': 'stack', 'target_ref': 'v0.4.3-alpha.49', 'branch': 'dev'})

    def test_lookup_does_not_reveal_another_admin_job(self):
        self.authenticate()
        other = bootstrap_admin(self.identities, 102)
        command_id = str(uuid4())
        self.db.connection.execute('INSERT INTO backend_update_jobs VALUES (?,?,?,?,?)',
            (str(uuid4()), other.id, command_id, 'stack', json.dumps({'target_ref': 'v0.4.3-alpha.49', 'branch': 'dev'})))
        result = self.service.handle({'version': 1, 'action': 'lookup-update',
            'session_id': self.session_id, 'command_id': command_id}, now=self.now)
        self.assertIsNone(result['job'])

    def test_node_lookup_recovers_only_own_dispatch_without_replaying(self):
        self.authenticate()
        command_id, job_id = str(uuid4()), str(uuid4())
        self.db.connection.execute('INSERT INTO backend_agent_rollouts VALUES (?,?,?,?,?)',
            (job_id, self.admin.id, command_id, 'lv1', 'running'))
        request = {'version': 1, 'action': 'lookup-node', 'session_id': self.session_id,
            'command_id': command_id}
        self.assertEqual(self.service.handle(request)['job'], {
            'id': job_id, 'node_key': 'lv1', 'status': 'running', 'kind': 'agent-rollouts'})
        other = bootstrap_admin(self.identities, 102)
        self.db.connection.execute('UPDATE backend_agent_rollouts SET actor_id=?', (other.id,))
        self.assertIsNone(self.service.handle(request)['job'])
        self.assertEqual(self.db.connection.execute('SELECT COUNT(*) FROM backend_agent_rollouts').fetchone()[0], 1)

    def test_creation_lookup_returns_only_original_admin_result(self):
        self.authenticate()
        self.db.connection.execute('CREATE TABLE backend_node_commands (actor_account_id TEXT, command_key TEXT, result_json TEXT)')
        command = str(uuid4())
        self.db.connection.execute('INSERT INTO backend_node_commands VALUES (?,?,?)',
            (self.admin.id, command, json.dumps({'key': 'lv1'})))
        request = {'version': 1, 'action': 'lookup-node-create', 'session_id': self.session_id, 'command_id': command}
        self.assertEqual(self.service.handle(request)['node'], {'key': 'lv1'})
        other = bootstrap_admin(self.identities, 102)
        self.db.connection.execute('UPDATE backend_node_commands SET actor_account_id=?', (other.id,))
        self.assertIsNone(self.service.handle(request)['node'])
        self.assert_denied('session_reauthentication_required',
            lambda: self.service.handle(request, now=self.now + LIFETIME))

    def test_lookup_requires_unexpired_session_and_current_approved_admin(self):
        self.authenticate()
        request = {'version': 1, 'action': 'lookup-update', 'session_id': self.session_id, 'command_id': str(uuid4())}
        self.assert_denied('session_reauthentication_required',
            lambda: self.service.handle(request, now=self.now + LIFETIME))
        self.db.connection.execute("UPDATE backend_accounts SET role='member' WHERE id=?", (self.admin.id,))
        self.assert_denied('session_reauthentication_required', lambda: self.service.handle(request, now=self.now))


class WorkstationProtocolTests(unittest.TestCase):
    def request(self, text, *, uid=0):
        output = io.StringIO()
        with patch('backend.workstation_cli.os.geteuid', return_value=uid):
            code = main(io.StringIO(text), output)
        return code, json.loads(output.getvalue())

    def test_malformed_oversized_duplicate_or_unknown_requests_are_rejected(self):
        for encoded in ('{}', 'null', '[]', '{', ' ' * 16_385,
            '{"version":1,"version":1,"action":"list"}',
            '{"version":1,"action":"list","token":"secret"}'):
            with self.subTest(encoded=encoded[:60]):
                code, result = self.request(encoded)
                self.assertEqual(code, 1)
                self.assertEqual(result['error']['code'], 'invalid_request')

    def test_invalid_selectors_scope_injection_and_path_traversal_are_rejected(self):
        base = {'version': 1, 'action': 'authenticate', 'session_id': str(uuid4())}
        for changed in ({'session_id': '../../credentials'}, {'session_id': base['session_id'].upper()},
            {'account_id': str(uuid4()), 'telegram_id': 101}, {'telegram_id': True},
            {'telegram_id': 0}, {'telegram_id': '101'}, {'scopes': ['profiles.manage']},
            {'version': True}, {'credential_id': 'arbitrary'}):
            with self.subTest(changed=changed):
                with self.assertRaises(WorkstationError):
                    validate_request({**base, **changed})

    def test_non_root_is_rejected_before_database_or_input_processing(self):
        code, result = self.request('{"version":1,"action":"list"}', uid=1000)
        self.assertEqual(code, 1)
        self.assertEqual(result['error']['code'], 'root_required')

    def test_database_error_never_exposes_exception_or_connection_secret(self):
        with patch('db.get_db', side_effect=RuntimeError('postgres://private-user:private-pass@host/database')):
            code, result = self.request('{"version":1,"action":"list"}')
        self.assertEqual(code, 1)
        self.assertEqual(result['error']['code'], 'workstation_unavailable')
        self.assertNotIn('private', json.dumps(result))
