from concurrent.futures import ThreadPoolExecutor
import os
import threading
import unittest
from uuid import uuid4

from backend import vpn_policies
from backend.authorization import AccessDenied
from backend.profile_commands import ProfileCommands
from backend.vpn_policies import PolicyKey, VpnPolicies, effective, integer, register
from tests import test_backups_postgres as fixture


@unittest.skipUnless(os.environ.get('NODE_PLANE_TEST_POSTGRES_DSN'), 'requires disposable PostgreSQL')
class VpnPoliciesPostgresTests(unittest.TestCase):
    drop_schema = fixture.BackupsPostgresTests.drop_schema

    def setUp(self):
        fixture.BackupsPostgresTests.setUp(self)
        saved = dict(vpn_policies.REGISTRY)
        self.addCleanup(lambda: (vpn_policies.REGISTRY.clear(), vpn_policies.REGISTRY.update(saved)))
        register(PolicyKey('test.limit', frozenset({'installation', 'profile'}),
                           frozenset({'global', 'node'}), integer(1, 10), default=5))
        self.service = VpnPolicies(self.db)
        self.profile = ProfileCommands(self.db).execute(self.actor, str(uuid4()), action='create',
            values={'display_name': 'Alice'})['profile']['id']

    def change(self, value, revision=0):
        return [{'key': 'test.limit', 'value': value, 'revision': revision}]

    def test_values_resolve_and_competing_writers_keep_one_revision(self):
        self.service.set(self.actor, str(uuid4()), 'installation', None, self.change(2))
        start = threading.Barrier(2)

        def write(value):
            start.wait(timeout=5)
            try:
                return self.service.set(self.actor, str(uuid4()), 'profile', self.profile, self.change(value))
            except AccessDenied as error:
                return error.code

        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(write, (3, 4)))
        self.assertEqual(sum(result == 'revision_conflict' for result in results), 1)
        with self.db.connect() as conn:
            value = effective(conn, profile_id=self.profile, keys=['test.limit'])['test.limit']
            self.assertIn(value, (3, 4))
            self.assertEqual(effective(conn, keys=['test.limit'])['test.limit'], 2)
            self.assertEqual(conn.execute('SELECT COUNT(*) AS n FROM backend_vpn_policies').fetchone()['n'], 2)


if __name__ == '__main__':
    unittest.main()
