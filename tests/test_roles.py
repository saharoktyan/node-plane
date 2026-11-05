import unittest

from backend.authorization import (
    AccessDenied, Account, Actor, Principal, PrincipalKind, ADMIN_PERMISSIONS, ROLE_PERMISSIONS,
    MEMBER_PERMISSIONS, account_permissions, require_permission,
)
from tests.test_backend_identity import Database
from backend.identity_repository import SQLIdentityRepository


def actor(*roles, role='admin'):
    return Actor(Principal('t', PrincipalKind.ACCOUNT, MEMBER_PERMISSIONS | ADMIN_PERMISSIONS | ROLE_PERMISSIONS, 'a'),
                 Account('a', role if roles else 'member', 'approved', frozenset(roles)))


class PresetTests(unittest.TestCase):
    def allowed(self, current, permission):
        try:
            require_permission(current, permission)
        except AccessDenied:
            return False
        return True

    def test_presets_grant_their_permissions_on_top_of_a_members(self):
        self.assertTrue(self.allowed(actor('owner'), 'roles.manage'))
        self.assertFalse(self.allowed(actor('admin'), 'roles.manage'))
        self.assertTrue(self.allowed(actor('admin'), 'accounts.manage'))
        self.assertTrue(self.allowed(actor('operator'), 'nodes.execute'))
        self.assertFalse(self.allowed(actor('operator'), 'accounts.manage'))
        self.assertTrue(self.allowed(actor('support'), 'access_requests.manage'))
        self.assertFalse(self.allowed(actor('support'), 'nodes.manage'))
        for current in (actor('operator'), actor('support'), actor()):
            self.assertTrue(self.allowed(current, 'profiles.self.read'))
        self.assertEqual(account_permissions(actor().account), MEMBER_PERMISSIONS)

    def test_roles_combine(self):
        current = actor('operator', 'support')
        self.assertTrue(self.allowed(current, 'nodes.manage'))
        self.assertTrue(self.allowed(current, 'access_requests.manage'))

    def test_an_administrator_of_an_older_release_is_an_owner(self):
        legacy = Actor(actor().principal, Account('a', 'admin', 'approved', None))
        self.assertTrue(self.allowed(legacy, 'roles.manage'))


class AssignmentTests(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        self.addCleanup(self.db.connection.close)
        self.identities = SQLIdentityRepository(self.db)
        self.identities.initialize_schema()

    def test_assignments_load_with_the_account_and_mirror_the_role(self):
        from backend.admin_cli import bootstrap_admin
        from backend.roles import assign, owners
        first = bootstrap_admin(self.identities, 101)
        self.assertEqual(first.roles, frozenset({'owner'}))
        second = self.identities.resolve_telegram(102)
        with self.db.transaction() as conn:
            assign(conn, second.id, {'operator'})
            self.assertEqual(owners(conn), 1)
        loaded = self.identities.get_account(second.id)
        self.assertEqual((loaded.role, loaded.roles), ('admin', frozenset({'operator'})))
        with self.db.transaction() as conn:
            assign(conn, second.id, set())
        loaded = self.identities.get_account(second.id)
        self.assertEqual((loaded.role, loaded.roles), ('member', frozenset()))
        with self.db.transaction() as conn:
            with self.assertRaises(AccessDenied):
                assign(conn, second.id, {'superuser'})

    def test_a_demotion_written_to_the_mirror_takes_every_role(self):
        from backend.admin_cli import bootstrap_admin
        first = bootstrap_admin(self.identities, 101)
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_accounts SET role='member' WHERE id=?", (first.id,))
        self.assertEqual(self.identities.get_account(first.id).roles, frozenset())
