import json
import os
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4
from tempfile import TemporaryDirectory
from pathlib import Path

from backend.agent_rollout import AgentRolloutService
from tests import test_backend_http as fixture_http


class BackendAgentRolloutTests(TestCase):
    setUp = fixture_http.BackendHTTPTests.setUp

    def test_agent_only_node_can_be_created_and_later_receive_protocols(self):
        headers = {**self.headers, 'X-Node-Plane-Telegram-User-ID': '101',
                   'Idempotency-Key': str(uuid4())}
        created = self.client.post('/api/v1/nodes', headers=headers, json={
            'key': 'agent1', 'title': 'Agent server', 'region': 'Europe', 'transport': 'local',
            'protocols': [], 'xray_transports': [], 'settings': {'public_host': 'agent.example'}})
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(created.json()['protocols'], [])
        rollout = self.client.post('/api/v1/nodes/agent1/agent-rollouts',
            headers={**headers, 'Idempotency-Key': str(uuid4())}, json={'transport': 'local'})
        self.assertEqual(rollout.status_code, 202, rollout.text)
        with patch.dict(os.environ, NODE_PLANE_APP_DIR='/opt/node-plane/current'):
            self.assertTrue(AgentRolloutService(self.db, runner=lambda args: True).run_one())
        with self.db.connect() as conn:
            self.assertFalse(conn.execute('SELECT 1 FROM backend_node_jobs').fetchone())
        updated = self.client.patch('/api/v1/nodes/agent1', headers={**headers,
            'Idempotency-Key': str(uuid4()), 'If-Match': '"1"'}, json={'protocols': ['awg']})
        self.assertEqual(updated.status_code, 200, updated.text)
        summary = self.client.get('/api/v1/nodes/agent1/overview', headers=headers).json()
        self.assertTrue(summary['settings_complete'])
        self.assertEqual(summary['state'], 'not_installed')

    def test_node_card_reports_agent_installation_until_it_finishes(self):
        self.node()
        headers={**self.headers,'X-Node-Plane-Telegram-User-ID':'101','Idempotency-Key':str(uuid4())}
        queued=self.client.post('/api/v1/nodes/lv1/agent-rollouts',headers=headers,json={'transport':'local'}).json()
        summary=self.client.get('/api/v1/nodes/lv1/overview',headers=headers).json()
        self.assertEqual(summary['state'],'applying')
        self.assertEqual(summary['agent_rollout']['id'],queued['id'])
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_agent_rollouts SET status='succeeded' WHERE id=?",(queued['id'],))
        summary=self.client.get('/api/v1/nodes/lv1/overview',headers=headers).json()
        self.assertEqual(summary['state'],'agent_only')
        self.assertIsNone(summary['agent_rollout'])

    def test_archived_journal_notice_survives_rollout_and_reaches_api(self):
        self.node()
        headers = {**self.headers, 'X-Node-Plane-Telegram-User-ID': '101',
                   'Idempotency-Key': str(uuid4())}
        queued = self.client.post('/api/v1/nodes/lv1/agent-rollouts', headers=headers,
                                 json={'transport': 'local'}).json()
        archive = '/var/lib/node-plane-agent/journal-archives/previous-controller-legacy-20261005T170000Z-abcd1234'
        output = f'AGENT_JOURNAL_ARCHIVE|lv1|{archive}\nAGENT_JOURNAL_ARCHIVE|lv1|{archive}\n'
        with patch.dict(os.environ, {'NODE_PLANE_APP_DIR': '/opt/node-plane/current'}), \
                patch('backend.agent_rollout.subprocess.run', return_value=SimpleNamespace(
                    returncode=0, stdout=output, stderr='')):
            self.assertTrue(AgentRolloutService(self.db).run_one())
        result = self.client.get(f'/api/v1/agent-rollouts/{queued["id"]}', headers=headers).json()
        self.assertEqual(result['journal_archives'], [archive])
        self.assertEqual(result['status'], 'succeeded')

    def test_rollout_exposes_live_stage_and_hides_it_after_completion(self):
        from backend.installation_progress import progress_path, write_progress
        self.node()
        headers = {**self.headers, 'X-Node-Plane-Telegram-User-ID': '101',
                   'Idempotency-Key': str(uuid4())}
        queued = self.client.post('/api/v1/nodes/lv1/agent-rollouts', headers=headers,
                                 json={'transport': 'local'}).json()
        with TemporaryDirectory() as shared, patch.dict(os.environ, NODE_PLANE_SHARED_DIR=shared):
            with self.db.transaction() as conn:
                conn.execute("UPDATE backend_agent_rollouts SET status='running' WHERE id=?", (queued['id'],))
            write_progress(progress_path(queued['id']), queued['id'], 'verify backend agent route for lv1')
            result = self.client.get(f'/api/v1/agent-rollouts/{queued["id"]}', headers=headers).json()
            self.assertEqual(result['progress']['stage'], 'verify_agent')
            summary = self.client.get('/api/v1/nodes/lv1/overview', headers=headers).json()
            self.assertEqual(summary['agent_rollout']['progress']['stage'], 'verify_agent')
            with self.db.transaction() as conn:
                conn.execute("UPDATE backend_agent_rollouts SET status='succeeded' WHERE id=?", (queued['id'],))
            result = self.client.get(f'/api/v1/agent-rollouts/{queued["id"]}', headers=headers).json()
            self.assertIsNone(result['progress'])

    def node(self):
        with self.db.transaction() as conn:
            conn.execute('''INSERT INTO backend_nodes(key, title, region, enabled,
                protocols_json, xray_transports_json)
                VALUES ('lv1', 'Latvia', 'EU', 0, '[]', '[]')''')

    def test_ssh_prerequisite_failure_is_exposed_without_console_output(self):
        service = AgentRolloutService(self.db)
        with patch.dict(os.environ, {'NODE_PLANE_APP_DIR': '/opt/node-plane/current'}), \
                patch('backend.agent_rollout.subprocess.run', return_value=SimpleNamespace(
                    returncode=1, stdout='', stderr='SSH_PREREQUISITES_FAILED: lv1 requires root or passwordless sudo')):
            self.assertFalse(service._execute({'node_key': 'lv1', 'intent_json': json.dumps({
                'transport': 'ssh', 'ssh_target': 'root@lv1.example', 'ssh_port': 22})}))
        self.assertEqual(service._failure_code, 'ssh_prerequisites')

    def test_release_binary_unavailable_failure_is_exposed_from_script_output(self):
        service = AgentRolloutService(self.db)
        for stderr in (
            'RELEASE_BINARY_UNAVAILABLE: Driver release asset node-plane-driver-linux-amd64.tar.gz for v0.4.1 is missing or inaccessible.',
            'Agent release asset node-plane-agent-linux-amd64.tar.gz for v0.4.1 is missing or inaccessible. Publish both release assets or configure authenticated binary URLs.',
        ):
            with patch.dict(os.environ, {'NODE_PLANE_APP_DIR': '/opt/node-plane/current'}), \
                    patch('backend.agent_rollout.subprocess.run', return_value=SimpleNamespace(
                        returncode=1, stdout='', stderr=stderr)):
                self.assertFalse(service._execute({'node_key': 'lv1', 'intent_json': json.dumps({
                    'transport': 'ssh', 'ssh_target': 'root@lv1.example', 'ssh_port': 22})}))
            self.assertEqual(service._failure_code, 'release_binary_unavailable')

    def test_legacy_rust_required_and_build_resources_failures_are_preserved(self):
        for stderr, expected in (
            ('RUST_INSTALL_REQUIRED: release binaries and local Rust build tools are unavailable.', 'rust_required'),
            ('Not enough free memory for local Rust build: 512 MiB available', 'build_resources'),
            ('CPU is too busy for local Rust build: 95% busy', 'build_resources'),
        ):
            service = AgentRolloutService(self.db)
            with patch.dict(os.environ, {'NODE_PLANE_APP_DIR': '/opt/node-plane/current'}), \
                    patch('backend.agent_rollout.subprocess.run', return_value=SimpleNamespace(
                        returncode=1, stdout='', stderr=stderr)):
                self.assertFalse(service._execute({'node_key': 'lv1', 'intent_json': json.dumps({
                    'transport': 'ssh', 'ssh_target': 'root@lv1.example', 'ssh_port': 22})}))
            self.assertEqual(service._failure_code, expected)

    def test_install_rust_parameter_is_rejected_without_execution(self):
        self.node()
        headers = {**self.headers, 'X-Node-Plane-Telegram-User-ID': '101',
                   'Idempotency-Key': str(uuid4())}
        path = '/api/v1/nodes/lv1/agent-rollouts'
        response = self.client.post(path, headers=headers,
            json={'transport': 'local', 'install_rust': True})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()['error']['code'], 'install_rust_unsupported')
        service = AgentRolloutService(self.db)
        from backend.authorization import AccessDenied, Actor, Principal, PrincipalKind
        actor = Actor(Principal('admin', PrincipalKind.ACCOUNT, frozenset({'nodes.manage'})), self.admin)
        with self.assertRaises(AccessDenied) as ctx:
            service.request(actor, 'lv1', str(uuid4()), transport='local', install_rust=True)
        self.assertEqual(ctx.exception.code, 'install_rust_unsupported')

    def test_known_preinstall_failure_allows_recovery_retry_but_timeout_and_forged_output_do_not(self):
        from backend.authorization import AccessDenied, Actor, Principal, PrincipalKind
        from backend.recovery import overview
        actor = Actor(Principal('admin', PrincipalKind.ACCOUNT, frozenset({'nodes.manage', 'maintenance.manage'})), self.admin)
        self.node()
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO backend_node_connections(node_key, transport, ssh_target) VALUES ('lv1', 'ssh', 'root@lv1.example')")
        service = AgentRolloutService(self.db)

        # 1. Known pre-install failure: release_binary_unavailable
        req1 = service.request(actor, 'lv1', str(uuid4()), transport='ssh', ssh_target='root@lv1.example')
        with patch.dict(os.environ, {'NODE_PLANE_APP_DIR': '/opt/node-plane/current'}), \
                patch('backend.agent_rollout.subprocess.run', return_value=SimpleNamespace(
                    returncode=1, stdout='', stderr='RELEASE_BINARY_UNAVAILABLE: Driver release asset node-plane-driver-linux-amd64.tar.gz for v0.4.1 is missing or inaccessible.')):
            service.run_one()
        row1 = service.get(actor, req1['id'])
        self.assertEqual(row1['status'], 'blocked')
        self.assertTrue(service.preflight_failed(row1))
        items1 = overview(self.db, actor)['items']
        item1 = next(i for i in items1 if i['id'] == req1['id'])
        self.assertIn('resume', item1['actions'])
        resumed1 = service.resume_preflight(actor, req1['id'])
        self.assertEqual(resumed1['status'], 'awaiting_executor')

        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_agent_rollouts SET status='cancelled' WHERE id=?", (req1['id'],))

        # 2. Known pre-install failure: legacy rust_required
        req2 = service.request(actor, 'lv1', str(uuid4()), transport='ssh', ssh_target='root@lv1.example')
        with patch.dict(os.environ, {'NODE_PLANE_APP_DIR': '/opt/node-plane/current'}), \
                patch('backend.agent_rollout.subprocess.run', return_value=SimpleNamespace(
                    returncode=1, stdout='', stderr='RUST_INSTALL_REQUIRED: release binaries and local Rust build tools are unavailable.')):
            service.run_one()
        row2 = service.get(actor, req2['id'])
        self.assertEqual(row2['status'], 'blocked')
        self.assertTrue(service.preflight_failed(row2))
        items2 = overview(self.db, actor)['items']
        item2 = next(i for i in items2 if i['id'] == req2['id'])
        self.assertIn('resume', item2['actions'])
        resumed2 = service.resume_preflight(actor, req2['id'])
        self.assertEqual(resumed2['status'], 'awaiting_executor')

        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_agent_rollouts SET status='cancelled' WHERE id=?", (req2['id'],))

        # 3. TimeoutError / no receipt: does NOT authorize retry
        req3 = service.request(actor, 'lv1', str(uuid4()), transport='ssh', ssh_target='root@lv1.example')
        with patch.dict(os.environ, {'NODE_PLANE_APP_DIR': '/opt/node-plane/current'}), \
                patch('backend.agent_rollout.subprocess.run', side_effect=TimeoutError("installer timeout")):
            service.run_one()
        row3 = service.get(actor, req3['id'])
        self.assertEqual(row3['status'], 'blocked')
        self.assertFalse(service.preflight_failed(row3))
        items3 = overview(self.db, actor)['items']
        item3 = next(i for i in items3 if i['id'] == req3['id'])
        self.assertNotIn('resume', item3['actions'])
        with self.assertRaises(AccessDenied) as ctx:
            service.resume_preflight(actor, req3['id'])
        self.assertEqual(ctx.exception.code, 'recovery_unconfirmed')

        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_agent_rollouts SET status='cancelled' WHERE id=?", (req3['id'],))

        # 4. Forged generic output: does NOT authorize retry
        req4 = service.request(actor, 'lv1', str(uuid4()), transport='ssh', ssh_target='root@lv1.example')
        with patch.dict(os.environ, {'NODE_PLANE_APP_DIR': '/opt/node-plane/current'}), \
                patch('backend.agent_rollout.subprocess.run', return_value=SimpleNamespace(
                    returncode=1, stdout='Something unexpected occurred', stderr='ERROR: unrelated file is missing or inaccessible.')):
            service.run_one()
        row4 = service.get(actor, req4['id'])
        self.assertEqual(row4['status'], 'blocked')
        self.assertFalse(service.preflight_failed(row4))
        items4 = overview(self.db, actor)['items']
        item4 = next(i for i in items4 if i['id'] == req4['id'])
        self.assertNotIn('resume', item4['actions'])
        with self.assertRaises(AccessDenied) as ctx:
            service.resume_preflight(actor, req4['id'])
        self.assertEqual(ctx.exception.code, 'recovery_unconfirmed')

    def test_ssh_access_failures_are_not_mislabeled_as_host_prerequisites(self):
        for detail, expected in (
            ('Permission denied (publickey,password).', 'ssh_authentication'),
            ('Host key verification failed.', 'ssh_host_key'),
            ('REMOTE HOST IDENTIFICATION HAS CHANGED!', 'ssh_host_key'),
        ):
            service = AgentRolloutService(self.db)
            with patch.dict(os.environ, {'NODE_PLANE_APP_DIR': '/opt/node-plane/current'}), \
                    patch('backend.agent_rollout.subprocess.run', return_value=SimpleNamespace(
                        returncode=1, stdout='', stderr=detail + '\nSSH_PREREQUISITES_FAILED: lv1')):
                self.assertFalse(service._execute({'node_key': 'lv1', 'intent_json': json.dumps({
                    'transport': 'ssh', 'ssh_target': 'root@lv1.example', 'ssh_port': 22})}))
            self.assertEqual(service._failure_code, expected)

    def test_rollout_is_queued_for_admin_and_executed_without_shell_input(self):
        self.node()
        key = str(uuid4())
        headers = {**self.headers, 'X-Node-Plane-Telegram-User-ID': '101',
                   'Idempotency-Key': key}
        body = {'transport': 'ssh', 'ssh_target': 'root@lv1.example', 'ssh_port': 2222}
        path = '/api/v1/nodes/lv1/agent-rollouts'
        queued = self.client.post(path, headers=headers, json=body)
        self.assertEqual(queued.status_code, 202, queued.text)
        self.assertEqual(self.client.post(path, headers=headers, json=body).json(), queued.json())
        self.assertEqual(self.client.post(path, headers=headers,
            json={**body, 'ssh_target': 'root@other.example'}).status_code, 409)
        seen = []
        with patch.dict(os.environ, {'NODE_PLANE_APP_DIR': '/opt/node-plane/current'}):
            service = AgentRolloutService(self.db, lambda args: seen.append(args) or True)
            self.assertTrue(service.run_one())
        self.assertEqual(seen[0][-4:], ['--backend-ssh-target', 'root@lv1.example',
                                      '--backend-ssh-port', '2222'])
        status = self.client.get(f'/api/v1/agent-rollouts/{queued.json()["id"]}',
                                 headers=headers)
        self.assertEqual(status.json()['status'], 'succeeded')

    def test_untrusted_target_and_non_admin_are_rejected(self):
        self.node()
        path = '/api/v1/nodes/lv1/agent-rollouts'
        headers = {**self.headers, 'X-Node-Plane-Telegram-User-ID': '101',
                   'Idempotency-Key': str(uuid4())}
        response = self.client.post(path, headers=headers,
            json={'transport': 'ssh', 'ssh_target': 'root@host;id'})
        self.assertEqual(response.status_code, 422)
        member = self.identities.resolve_telegram(102)
        self.db.connection.execute("UPDATE backend_accounts SET status = 'approved' WHERE id = ?", (member.id,))
        headers['X-Node-Plane-Telegram-User-ID'] = '102'
        self.assertEqual(self.client.post(path, headers=headers,
            json={'transport': 'local'}).status_code, 403)

    def test_interrupted_rollout_is_not_replayed_automatically(self):
        self.node()
        headers = {**self.headers, 'X-Node-Plane-Telegram-User-ID': '101',
                   'Idempotency-Key': str(uuid4())}
        queued = self.client.post('/api/v1/nodes/lv1/agent-rollouts', headers=headers,
            json={'transport': 'local'})
        self.assertEqual(queued.status_code, 202)
        self.db.connection.execute("UPDATE backend_agent_rollouts SET status = 'running'")
        service = AgentRolloutService(self.db, lambda args: self.fail('must not replay'))
        service.recover()
        self.assertFalse(service.run_one())
        self.assertEqual(self.client.get(f'/api/v1/agent-rollouts/{queued.json()["id"]}',
            headers=headers).json()['status'], 'blocked')

    def test_completed_rollout_history_does_not_pin_removed_node(self):
        self.node()
        headers = {**self.headers, 'X-Node-Plane-Telegram-User-ID': '101',
                   'Idempotency-Key': str(uuid4())}
        response = self.client.post('/api/v1/nodes/lv1/agent-rollouts', headers=headers,
                                    json={'transport': 'local'})
        self.assertEqual(response.status_code, 202)
        self.db.connection.execute("UPDATE backend_agent_rollouts SET status = 'succeeded'")
        self.db.connection.execute("DELETE FROM backend_nodes WHERE key = 'lv1'")
        self.assertEqual(self.db.connection.execute(
            'SELECT node_key FROM backend_agent_rollouts').fetchone()['node_key'], 'lv1')

    def test_registry_only_removal_api_is_explicit_and_idempotent(self):
        self.node()
        headers = {**self.headers, 'X-Node-Plane-Telegram-User-ID': '101'}
        path = '/api/v1/nodes/lv1/retire-registry-only'
        self.assertEqual(self.client.get('/api/v1/nodes/lv1/maintenance',
                                        headers=headers).json()['status'], 'active')
        self.assertEqual(self.client.post(path, headers=headers,
            json={'reason': 'VPS unreachable for a long time'}).status_code, 422)
        body = {'reason': 'VPS unreachable for a long time',
                'accept_unverified_runtime': True}
        with TemporaryDirectory() as directory:
            Path(directory, 'data').mkdir()
            with patch.dict(os.environ, {'NODE_PLANE_SHARED_DIR': directory}):
                response = self.client.post(path, headers=headers, json=body)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(self.client.post(path, headers=headers,
                    json=body).json(), response.json())
        self.assertEqual(response.json()['mode'], 'registry_only')
        self.assertEqual(self.client.get('/api/v1/nodes/lv1/maintenance',
                                         headers=headers).status_code, 404)

    def test_verified_cleanup_routes_preserve_host_verification_gate(self):
        from backend.removal_inventory import inventory_digest
        resources = {'paths': ['/opt/custom-runtime'], 'containers': ['custom-xray', 'custom-awg']}
        self.node()
        self.db.connection.execute("UPDATE backend_nodes SET enabled = 1 WHERE key = 'lv1'")
        headers = {**self.headers, 'X-Node-Plane-Telegram-User-ID': '101'}
        base = '/api/v1/nodes/lv1'
        with TemporaryDirectory() as directory:
            Path(directory, 'data').mkdir()
            public_key = Path(directory, 'bot.pub')
            public_key.write_text('ssh-ed25519 ' + 'A' * 40)
            with patch.dict(os.environ, {'NODE_PLANE_SHARED_DIR': directory,
                                          'NODE_PLANE_BOT_PUBLIC_KEY_FILE': str(public_key)}):
                self.assertEqual(self.client.post(base + '/drain',
                    headers=headers).json()['error']['code'],
                    'verification_target_required')
                with patch('backend.removal_verifier.RemovalVerifier.capture_identity',
                           return_value='a' * 64), patch('backend.removal_verifier.RemovalVerifier.capture_resources',
                           return_value=resources):
                    bound = self.client.post(base + '/bind-verification-target',
                        headers=headers, json={'transport': 'local'})
                self.assertEqual(bound.status_code, 200, bound.text)
                self.assertEqual(self.client.post(base + '/drain',
                    headers=headers).json()['status'], 'draining')
                with patch('backend.node_lifecycle.NodeLifecycle.cleanup',
                           return_value={'node_key': 'lv1', 'phase': 'prepared',
                                         'command_id': str(uuid4())}):
                    cleanup = self.client.post(base + '/cleanup-step',
                        headers=headers, json={'expected_phase': 'not_started'})
                self.assertEqual(cleanup.status_code, 200, cleanup.text)
                self.assertEqual(self.client.post(base + '/verify-and-retire',
                    headers=headers).json()['error']['code'], 'agent_uninstall_not_requested')
                self.db.connection.execute('''INSERT INTO backend_node_cleanup
                    (node_key, actor_id, command_id, phase, started_at)
                    VALUES ('lv1', ?, ?, 'uninstall_scheduled', 'now')''',
                    (self.admin.id, str(uuid4())))
                with patch('backend.removal_verifier.RemovalVerifier.verify',
                           return_value={'method': 'local', 'target': 'local',
                               'inventory_digest': inventory_digest(resources),
                               'host_fingerprint': 'a' * 64, 'checked_at': 'now',
                               'result': 'agent_and_standard_artifacts_absent'}):
                    result = self.client.post(base + '/verify-and-retire', headers=headers)
                self.assertEqual(result.status_code, 200, result.text)
                self.assertEqual(result.json()['mode'], 'verified')
