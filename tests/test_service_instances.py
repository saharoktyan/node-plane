import unittest

from backend.service_instances import changed_engines


class ChangedEnginesTests(unittest.TestCase):
    def setUp(self):
        self.applied = {'protocols': ['awg', 'xray'],
                        'settings': {'public_host': 'node.example', 'awg_port': 51820, 'xray_tcp_port': 443}}

    def test_an_engine_setting_touches_only_that_engine(self):
        settings = {**self.applied['settings'], 'awg_port': 51821}
        self.assertEqual(changed_engines(self.applied, ['awg', 'xray'], settings), {'awg'})
        settings = {**self.applied['settings'], 'xray_tcp_port': 8443}
        self.assertEqual(changed_engines(self.applied, ['awg', 'xray'], settings), {'xray'})

    def test_a_shared_setting_touches_every_engine(self):
        settings = {**self.applied['settings'], 'public_host': 'other.example'}
        self.assertEqual(changed_engines(self.applied, ['awg', 'xray'], settings), {'awg', 'xray'})

    def test_added_and_removed_engines_are_touched(self):
        self.assertEqual(changed_engines(self.applied, ['xray'], self.applied['settings']), {'awg'})
        applied = {'protocols': ['xray'], 'settings': self.applied['settings']}
        self.assertEqual(changed_engines(applied, ['awg', 'xray'], self.applied['settings']), {'awg'})

    def test_unknown_applied_state_touches_every_configured_engine(self):
        self.assertEqual(changed_engines(None, ['xray'], {}), {'xray'})

    def test_compared_settings_can_be_limited(self):
        settings = {**self.applied['settings'], 'policy_value': 'x'}
        self.assertEqual(changed_engines(self.applied, ['awg', 'xray'], settings, keys={'awg_port'}), set())


class ServicePermissionTests(unittest.TestCase):
    from tests.test_backend_node_jobs import BackendNodeJobTests
    setUp = BackendNodeJobTests.setUp
    del BackendNodeJobTests

    def edit(self, actor, settings):
        from uuid import uuid4
        from backend.nodes import NodeService
        revision = self.db.connection.execute("SELECT desired_revision FROM backend_nodes WHERE key='n1'").fetchone()[0]
        return NodeService(self.db).command(actor, action='edit', node_key='n1', revision=revision,
                                            command_key=str(uuid4()), values={'settings': settings})

    def test_a_change_needs_the_permission_of_each_engine_it_touches(self):
        from unittest.mock import patch
        from backend.authorization import AccessDenied, Account, Actor, PRESETS
        operator = Actor(self.actor.principal, Account(self.actor.account.id, 'admin', 'approved', frozenset({'operator'})))
        limited = {**PRESETS, 'operator': PRESETS['operator'] - {'services.awg.manage'}}
        with patch.dict('backend.authorization.PRESETS', limited):
            with self.assertRaises(AccessDenied) as denied:
                self.edit(operator, {'awg_port': 51999})
            self.assertEqual(denied.exception.code, 'permission_denied')
            with self.assertRaises(AccessDenied):
                self.edit(operator, {'public_host': 'other.example'})
            self.edit(operator, {'xray_tcp_port': 2053})
        self.edit(operator, {'awg_port': 51999})

    def test_service_permissions_ride_on_the_nodes_scope(self):
        from backend.authorization import account_permissions, service_permission
        self.assertNotIn('services.xray.manage', self.actor.principal.scopes)
        self.assertIn(service_permission('xray'), account_permissions(self.actor.account))

    def test_a_grant_names_the_instance_of_its_engine(self):
        from backend.profiles import ProfileRepository
        repository = ProfileRepository(self.db)
        profile = repository.create_profile(runtime_name='granted', display_name='Granted')
        self.db.connection.execute("INSERT INTO backend_grants(profile_id,node_key,protocol) VALUES (?,'n1','awg')",
                                   (profile,))
        instance = self.db.connection.execute(
            "SELECT id FROM backend_service_instances WHERE node_key='n1' AND service='awg'").fetchone()[0]
        self.assertEqual(repository.grants(profile),
                         [{'node_key': 'n1', 'protocol': 'awg', 'service_instance_id': instance}])
