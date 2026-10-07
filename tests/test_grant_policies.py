import json
from types import SimpleNamespace
import unittest
from uuid import uuid4

from backend.authorization import AccessDenied
from backend.grant_policies import GrantPolicies, read
from backend.node_lifecycle import NodeLifecycle
from backend.node_operations import NodeOperations
from backend.nodes import NodeService
from backend.profile_commands import ProfileCommands
from tests import test_backend_node_jobs as fixture


class GrantPolicyTests(unittest.TestCase):
    setUp = fixture.BackendNodeJobTests.setUp

    def profile(self, grants=None):
        return ProfileCommands(self.db).execute(self.actor, str(uuid4()), action='create',
            values={'display_name': 'Alice', 'grants': grants or []})['profile']['id']

    def values(self, rules=None, explicit=None, exclusions=None):
        return {'explicit_grants': explicit or [], 'rules': rules or [], 'exclusions': exclusions or []}

    def policy(self, profile, values, key=None, revision=None):
        revision = revision or self.db.connection.execute('SELECT desired_revision FROM backend_profiles WHERE id=?', (profile,)).fetchone()['desired_revision']
        return ProfileCommands(self.db).execute(self.actor, key or str(uuid4()), action='policy',
            profile_id=profile, revision=revision, values=values)

    def grants(self, profile):
        return {(g['node_key'], g['protocol']) for g in self.db.connection.execute('SELECT node_key,protocol FROM backend_grants WHERE profile_id=?', (profile,)).fetchall()}

    def rule(self, scope='all', region=None, protocols=None):
        return {'scope': scope, 'region_id': region, 'protocols': protocols or ['awg']}

    def new_node(self, key='n2', region='EU', protocols=None):
        return NodeService(self.db).command(self.actor, action='create', command_key=str(uuid4()),
            values={'key': key, 'title': key, 'region': region, 'protocols': protocols or ['awg'],
                    'transport': 'ssh', 'ssh_target': 'root@' + key + '.example',
                    'settings': {'public_host': key + '.example'}})

    def bootstrap(self, key):
        NodeOperations(self.db).queue(self.actor, key, 'bootstrap', revision=1, command_key=str(uuid4()))
        driver = SimpleNamespace(node_action=lambda task, action, intent: {
            'node_key': key, 'action': action, 'revision': intent['revision'], 'result': {'verified': True}})
        self.assertTrue(NodeOperations(self.db, driver).run_one())

    def test_future_node_grants_start_after_confirmed_bootstrap_and_are_idempotent(self):
        profile = self.profile()
        values = self.values([self.rule(protocols=['awg', 'xray'])])
        key = str(uuid4())
        first = self.policy(profile, values, key=key, revision=1)
        self.assertEqual(self.policy(profile, values, key=key, revision=1), first)
        self.assertEqual(self.grants(profile), {('n1', 'awg'), ('n1', 'xray')})
        self.new_node()
        self.assertNotIn(('n2', 'awg'), self.grants(profile))
        self.bootstrap('n2')
        self.assertEqual(self.grants(profile), {('n1', 'awg'), ('n1', 'xray'), ('n2', 'awg')})
        operation = self.db.connection.execute('SELECT desired_revision FROM backend_operations WHERE profile_id=? ORDER BY desired_revision DESC LIMIT 1', (profile,)).fetchone()
        self.assertEqual(operation['desired_revision'], 3)
        self.assertEqual(len(self.db.connection.execute('SELECT * FROM backend_devices WHERE profile_id=?', (profile,)).fetchall()), 1)

    def test_exclusion_and_policy_removal_preserve_independent_manual_access(self):
        profile = self.profile()
        explicit = [{'node_key': 'n1', 'protocol': 'xray'}]
        self.policy(profile, self.values([self.rule(protocols=['awg', 'xray'])], explicit,
                                        [{'node_key': 'n1', 'protocol': 'awg'}]))
        self.assertEqual(self.grants(profile), {('n1', 'xray')})
        self.policy(profile, self.values(explicit=explicit))
        self.assertEqual(self.grants(profile), {('n1', 'xray')})
        self.policy(profile, self.values())
        self.assertEqual(self.grants(profile), set())
        latest = self.db.connection.execute('SELECT id FROM backend_operations WHERE profile_id=? ORDER BY desired_revision DESC LIMIT 1', (profile,)).fetchone()
        self.assertTrue(all(t['action'] == 'delete' for t in self.db.connection.execute('SELECT action FROM backend_operation_tasks WHERE operation_id=?', (latest['id'],)).fetchall()))

    def test_snapshot_grant_edit_does_not_erase_policy(self):
        profile = self.profile()
        self.policy(profile, self.values([self.rule()]))
        ProfileCommands(self.db).execute(self.actor, str(uuid4()), action='grants', profile_id=profile,
            revision=2, values={'grants': [{'node_key': 'n1', 'protocol': 'xray'}]})
        self.assertEqual(self.grants(profile), {('n1', 'awg'), ('n1', 'xray')})
        self.assertEqual(GrantPolicies(self.db).get(self.actor, profile)['explicit_grants'], [{'node_key': 'n1', 'protocol': 'xray'}])

    def test_region_move_requires_confirmation_and_revokes_only_inherited_access(self):
        node = NodeService(self.db).get(self.actor, 'n1')
        region_id = node['region_id']
        self.assertTrue(region_id)
        profile = self.profile()
        self.policy(profile, self.values([self.rule('region', region_id)], [{'node_key': 'n1', 'protocol': 'xray'}]))
        with self.assertRaises(AccessDenied) as error:
            NodeService(self.db).command(self.actor, action='edit', node_key='n1', revision=1,
                command_key=str(uuid4()), values={'region': 'Asia'})
        self.assertEqual(error.exception.code, 'region_policy_review_required')
        NodeService(self.db).command(self.actor, action='edit', node_key='n1', revision=1,
            command_key=str(uuid4()), values={'region': 'Asia', 'confirm_access_change': True})
        self.assertEqual(self.grants(profile), {('n1', 'xray')})
        old_region = next(r for r in GrantPolicies(self.db).regions(self.actor)['items'] if r['id'] == region_id)
        self.assertEqual(old_region['title'], 'EU')
        self.new_node('n2', '  eu  ')
        self.assertEqual(NodeService(self.db).get(self.actor, 'n2')['region_id'], region_id)
        self.bootstrap('n2')
        self.assertEqual(self.grants(profile), {('n1', 'xray'), ('n2', 'awg')})

    def test_frozen_and_expired_profiles_never_receive_ensure_tasks(self):
        for fields in ({'frozen': True}, {'expires_at': '2000-01-01T00:00:00+00:00'}):
            profile = self.profile()
            self.policy(profile, self.values([self.rule()]))
            ProfileCommands(self.db).execute(self.actor, str(uuid4()), action='edit', profile_id=profile,
                                            revision=2, values=fields)
        self.new_node()
        self.bootstrap('n2')
        tasks = self.db.connection.execute("SELECT action FROM backend_operation_tasks WHERE node_key='n2'").fetchall()
        self.assertTrue(tasks)
        self.assertEqual({t['action'] for t in tasks}, {'delete'})

    def test_drain_clears_manual_sources_and_keeps_future_rules(self):
        profile = self.profile()
        self.policy(profile, self.values([self.rule()], [{'node_key': 'n1', 'protocol': 'xray'}]))
        NodeLifecycle(self.db).start_drain(self.actor, 'n1')
        self.assertEqual(self.grants(profile), set())
        with self.db.connect() as conn:
            values = read(conn, profile)
            self.assertEqual(values['explicit_grants'], [])
            self.assertEqual(values['rules'], [self.rule()])
        self.new_node()
        self.bootstrap('n2')
        self.assertEqual(self.grants(profile), {('n2', 'awg')})

    def test_deleting_profile_drops_policies_and_future_node_never_grants_it_access(self):
        profile = self.profile()
        self.policy(profile, self.values([self.rule()]))
        ProfileCommands(self.db).execute(self.actor, str(uuid4()), action='delete', profile_id=profile, revision=2)
        self.assertIsNone(self.db.connection.execute('SELECT 1 FROM backend_grant_policies WHERE profile_id=?', (profile,)).fetchone())
        self.new_node()
        self.bootstrap('n2')
        self.assertEqual(self.grants(profile), set())

    def test_validation_is_atomic_and_rejects_unknown_region_and_conflicting_replay(self):
        profile = self.profile()
        with self.assertRaises(AccessDenied) as error:
            self.policy(profile, self.values([self.rule('region', str(uuid4()))]))
        self.assertEqual(error.exception.code, 'region_not_found')
        self.assertEqual(self.grants(profile), set())
        key = str(uuid4())
        self.policy(profile, self.values([self.rule()]), key=key, revision=1)
        with self.assertRaises(AccessDenied) as error:
            self.policy(profile, self.values(), key=key, revision=1)
        self.assertEqual(error.exception.code, 'idempotency_conflict')
        self.assertEqual(self.grants(profile), {('n1', 'awg')})

    def test_reconciliation_keeps_access_during_temporary_node_maintenance(self):
        from backend.grant_policies import reconcile
        profile = self.profile()
        self.policy(profile, self.values([self.rule()]))
        # Runtime availability gates issuance, not the stored access policy.
        self.db.connection.execute("UPDATE backend_nodes SET enabled=0 WHERE key='n1'")
        with self.db.transaction() as conn:
            self.assertEqual(reconcile(conn, self.actor), set())
        self.assertEqual(self.grants(profile), {('n1', 'awg')})

    def test_region_review_includes_access_after_future_bootstrap(self):
        self.new_node()
        node = NodeService(self.db).get(self.actor, 'n2')
        profile = self.profile()
        self.policy(profile, self.values([self.rule('region', node['region_id'])]))
        preview = GrantPolicies(self.db).preview_region(self.actor, 'n2', 'Asia')
        self.assertEqual(preview['affected_profiles'], 1)
        self.assertEqual(preview['profiles'][0]['removed_protocols'], ['awg'])
        self.assertNotIn(('n2', 'awg'), self.grants(profile))

    def test_overlapping_rules_revoke_only_when_last_source_is_removed(self):
        profile = self.profile()
        region = NodeService(self.db).get(self.actor, 'n1')['region_id']
        self.policy(profile, self.values([self.rule(), self.rule('region', region)]))
        self.policy(profile, self.values([self.rule('region', region)]))
        self.assertEqual(self.grants(profile), {('n1', 'awg')})
        self.policy(profile, self.values())
        self.assertEqual(self.grants(profile), set())

    def test_region_move_cannot_use_node_only_scope_to_change_profile_access(self):
        from backend.authorization import Actor, Principal, PrincipalKind
        profile = self.profile()
        region = NodeService(self.db).get(self.actor, 'n1')['region_id']
        self.policy(profile, self.values([self.rule('region', region)]))
        limited = Actor(Principal('node-only', PrincipalKind.SERVICE, frozenset({'nodes.manage'})), self.actor.account)
        with self.assertRaises(AccessDenied) as error:
            NodeService(self.db).command(limited, action='edit', node_key='n1', revision=1,
                command_key=str(uuid4()), values={'region': 'Asia', 'confirm_access_change': True})
        self.assertEqual(error.exception.code, 'permission_denied')
        self.assertEqual(NodeService(self.db).get(self.actor, 'n1')['region_id'], region)
        self.assertEqual(self.grants(profile), {('n1', 'awg')})

    def test_policy_revocation_covers_every_device_and_preexisting_pending_ensure(self):
        from backend.device_commands import DeviceCommands
        profile = self.profile()
        self.policy(profile, self.values([self.rule()]))
        DeviceCommands(self.db).execute(self.actor, str(uuid4()), action='create', profile_id=profile,
                                       revision=2, display_name='Laptop')
        result = self.policy(profile, self.values())
        tasks = self.db.connection.execute('SELECT device_id,action FROM backend_operation_tasks WHERE operation_id=?', (result['operation_id'],)).fetchall()
        self.assertEqual(len(tasks), 2)
        self.assertEqual({t['action'] for t in tasks}, {'delete'})
        self.assertEqual(len({t['device_id'] for t in tasks}), 2)
        self.assertEqual(self.grants(profile), set())
