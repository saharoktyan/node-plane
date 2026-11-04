"""Token installation: the command, the worker's enrollment and server setup
waiting for it."""
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
import re
from types import SimpleNamespace
from uuid import uuid4
import unittest

import grpc

from backend import agent_enrollment
from backend.agent_enrollment import AgentEnrollmentService, command, release_assets, target
from backend.authorization import AccessDenied
from backend.node_bootstrap import NodeBootstrapService
from tests import test_backend_node_jobs as fixtures

ROOT = Path(__file__).resolve().parents[1]
RELEASE = {'tag': 'v1.2.3', 'base': 'https://github.com/saharoktyan/node-plane/releases/download/v1.2.3',
           'agent_sha256': 'a' * 64, 'installer_sha256': 'b' * 64}


class Failure(grpc.RpcError):
    def __init__(self, code):
        self._code = code

    def code(self):
        return self._code

    def details(self):
        # What the driver says before it knows the agent.
        return 'no node-agent target configured'


class Driver:
    def __init__(self):
        self.enrolled = False
        self.failure = grpc.StatusCode.UNAVAILABLE
        self.calls = []

    def enroll_agent(self, node_key, target, token, controller, retired, restrict_to_controller):
        self.calls.append((node_key, target, token, controller, retired, restrict_to_controller))
        if self.failure:
            raise Failure(self.failure)
        self.enrolled = True
        return {'firewall': 'agent port 50061 open; mutual TLS guards it', 'journal_archive': ''}

    def inspect_node_services(self, node_key):
        if not self.enrolled:
            raise Failure(grpc.StatusCode.FAILED_PRECONDITION)
        return {'docker': True}


class AgentEnrollmentTests(unittest.TestCase):
    def setUp(self):
        fixtures.BackendNodeJobTests.setUp(self)
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_nodes SET applied_revision=0,enabled=0 WHERE key='n1'")
            conn.execute("DELETE FROM backend_node_connections")
        self.driver = Driver()
        self.service = AgentEnrollmentService(self.db, self.driver, release=lambda: RELEASE)
        self.bootstraps = NodeBootstrapService(self.db, self.driver)

    def issue(self, **options):
        return self.service.issue(self.actor, 'n1', 1, str(uuid4()), **options)

    def row(self):
        with self.db.connect() as conn:
            return conn.execute('SELECT * FROM backend_agent_enrollments').fetchone()

    def test_the_command_checks_the_installer_and_pins_the_agent(self):
        issued = self.issue()
        token = self.row()['token']
        self.assertRegex(token, r'^\d+\.[0-9a-f]{64}$')
        self.assertEqual(issued['command'],
            f"curl -fsSLo /tmp/node-plane-agent-installer.sh {RELEASE['base']}/node-plane-agent-installer.sh && "
            f"echo '{'b' * 64}  /tmp/node-plane-agent-installer.sh' | sha256sum -c - && "
            f"sudo sh /tmp/node-plane-agent-installer.sh --node-key n1 --token {token} --release v1.2.3 "
            f"--agent-sha256 {'a' * 64} --port 50061")
        self.assertEqual(issued['target'], 'node.example:50061')
        # The same steps for Workstation to run over SSH.
        self.assertEqual(issued['installer'], {'url': f"{RELEASE['base']}/node-plane-agent-installer.sh",
                                               'sha256': 'b' * 64})
        self.assertEqual(issued['arguments'][:4], ['--node-key', 'n1', '--token', token])
        self.assertEqual(issued['bootstrap']['phase'], 'agent')
        self.assertEqual(self.service.get(self.actor, 'n1')['status'], 'waiting')
        self.assertNotIn('token', self.service.get(self.actor, 'n1'))

    def test_targets_are_hosts_or_addresses(self):
        self.assertEqual(target('2001:db8::1', 50061), '[2001:db8::1]:50061')
        self.assertEqual(target('203.0.113.5', 1), '203.0.113.5:1')
        for bad in ('', 'a b', '-x.example', 'x:1', 'x/y'):
            with self.assertRaises(AccessDenied):
                target(bad, 50061)

    def test_setup_waits_for_the_agent_then_continues_after_enrollment(self):
        issued = self.issue(restrict_to_controller=True)
        # Before the administrator ran the command: setup waits, no SSH rollout.
        self.assertFalse(self.service.run_one())
        self.assertFalse(self.bootstraps.run_one())
        self.assertEqual(self.bootstraps.get(self.actor, issued['bootstrap']['id'])['phase'], 'agent')
        self.assertIsNone(self.row()['error_code'])
        with self.db.connect() as conn:
            self.assertIsNone(conn.execute('SELECT 1 FROM backend_agent_rollouts').fetchone())
        # Retried only after a pause.
        self.assertFalse(self.service.run_one())
        self.assertEqual(len(self.driver.calls), 1)
        node_key, address, token, controller, retired, restrict = self.driver.calls[0]
        self.assertEqual((node_key, address, restrict), ('n1', 'node.example:50061', True))
        self.assertRegex(controller, r'^[0-9a-f-]{36}$')

        self.driver.failure = None
        with self.db.transaction() as conn:
            conn.execute('UPDATE backend_agent_enrollments SET attempted_at=NULL')
        self.assertTrue(self.service.run_one())
        state = self.service.get(self.actor, 'n1')
        self.assertEqual(state['status'], 'enrolled')
        self.assertEqual(state['firewall'], 'agent port 50061 open; mutual TLS guards it')
        self.assertIsNone(self.row()['token'])
        self.assertTrue(self.bootstraps.run_one())
        self.assertEqual(self.bootstraps.get(self.actor, issued['bootstrap']['id'])['phase'], 'docker')

    def test_an_agent_only_command_leaves_services_for_later(self):
        issued = self.issue(install_services=False)
        self.assertIsNone(issued['bootstrap'])
        with self.db.connect() as conn:
            self.assertIsNone(conn.execute('SELECT 1 FROM backend_node_bootstraps').fetchone())
        from backend.nodes import NodeService
        installed = lambda: NodeService(self.db).get(self.actor, 'n1')['agent_installed']
        self.assertFalse(installed())
        self.driver.failure = None
        self.assertTrue(self.service.run_one())
        # Enrolled: the node offers the services setup.
        self.assertTrue(installed())

    def test_a_rejected_token_is_shown_and_still_retried(self):
        self.issue()
        self.driver.failure = grpc.StatusCode.PERMISSION_DENIED
        self.service.run_one()
        self.assertEqual(self.service.get(self.actor, 'n1')['error_code'], 'agent_enrollment_rejected')
        self.assertEqual(self.service.get(self.actor, 'n1')['status'], 'waiting')

    def test_an_expired_command_blocks_setup_until_a_new_one(self):
        first = self.issue()
        with self.db.transaction() as conn:
            conn.execute('UPDATE backend_agent_enrollments SET expires_at=?',
                         ((datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(),))
        self.assertTrue(self.service.run_one())
        self.assertEqual(self.service.get(self.actor, 'n1')['status'], 'expired')
        self.assertIsNone(self.row()['token'])
        self.assertTrue(self.bootstraps.run_one())
        blocked = self.bootstraps.get(self.actor, first['bootstrap']['id'])
        self.assertEqual((blocked['status'], blocked['error_code']), ('blocked', 'agent_enrollment_expired'))
        # A new command replaces the token and resumes the same setup.
        second = self.issue()
        self.assertEqual(second['bootstrap']['id'], first['bootstrap']['id'])
        self.assertEqual(second['bootstrap']['status'], 'awaiting_executor')
        self.assertEqual(self.service.get(self.actor, 'n1')['status'], 'waiting')

    def test_a_new_command_replaces_the_unused_token(self):
        self.issue()
        old = self.row()['token']
        again = self.issue()
        self.assertNotEqual(self.row()['token'], old)
        self.assertIn(self.row()['token'], again['command'])


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        agent_enrollment._release_cache.clear()
        self.addCleanup(agent_enrollment._release_cache.clear)

    def fetch(self, installer):
        def fetch(url, limit):
            self.assertEqual(url, 'https://github.com/saharoktyan/node-plane/releases/download/'
                             f"v{(ROOT / 'VERSION').read_text().strip()}/SHA256SUMS.txt")
            return (f"{'a' * 64}  node-plane-agent-linux-amd64.tar.gz\n"
                    f"{installer}  node-plane-agent-installer.sh\n").encode()
        return fetch

    def test_the_installer_must_be_the_controllers_own(self):
        own = hashlib.sha256((ROOT / 'scripts/install_agent.sh').read_bytes()).hexdigest()
        release = release_assets(ROOT, fetch=self.fetch(own))
        self.assertEqual((release['agent_sha256'], release['installer_sha256']), ('a' * 64, own))
        agent_enrollment._release_cache.clear()
        with self.assertRaises(AccessDenied) as changed:
            release_assets(ROOT, fetch=self.fetch('c' * 64))
        self.assertEqual(changed.exception.code, 'agent_installer_mismatch')

    def test_an_unreachable_release_is_reported(self):
        def fetch(url, limit):
            raise OSError('offline')
        with self.assertRaises(AccessDenied) as offline:
            release_assets(ROOT, fetch=fetch)
        self.assertEqual(offline.exception.code, 'agent_release_unavailable')

    def test_the_command_quotes_nothing_unexpected(self):
        line = command(RELEASE, 'n1', '1.' + 'f' * 64, 50061)
        self.assertNotRegex(line, r'[`$;]')
        self.assertTrue(re.search(r'sudo sh /tmp/node-plane-agent-installer\.sh --node-key n1', line))


if __name__ == '__main__':
    unittest.main()
