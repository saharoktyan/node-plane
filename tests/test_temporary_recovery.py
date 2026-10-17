from unittest import TestCase
from unittest.mock import Mock
from backend.authorization import AccessDenied
from backend.recovery import act, overview, history
from tests import test_backend_temporary_configs as fixture


class TemporaryRecoveryTests(TestCase):
    Fixture = fixture.TemporaryConfigTests
    setUp = Fixture.setUp
    setup_ready_profile = Fixture.setup_ready_profile
    prepare = Fixture.prepare
    create = Fixture.create

    def blocked(self):
        self.prepare()
        row = self.create()
        self.db.connection.execute("UPDATE backend_temporary_configs SET status='blocked',error_code='outcome_unknown' WHERE id=?", (row['id'],))
        return row

    def test_inventory_includes_secret_free_blocked_and_revocation_states(self):
        row = self.blocked()
        item = next(i for i in overview(self.db,self.actor)['items'] if i['id']==row['id'])
        self.assertEqual((item['kind'],item['node_key'],item['error_code'],item['actions']), ('temporary','n1','outcome_unknown',['recheck','resolve']))
        self.assertNotIn('tmp_',str(item))
        headers={**self.headers,'X-Node-Plane-Telegram-User-ID':'101'}
        response=self.client.get('/api/v1/system/recovery',headers=headers)
        self.assertEqual(response.status_code,200,response.text)
        self.assertTrue(any(i['kind']=='temporary' for i in response.json()['items']))
        self.db.connection.execute("UPDATE backend_temporary_configs SET status='revoke_blocked'")
        item = next(i for i in overview(self.db,self.actor)['items'] if i['id']==row['id'])
        self.assertEqual(item['actions'], ['recheck'])

    def test_recheck_reads_exact_command_without_reissuing(self):
        row = self.blocked()
        self.driver.node_action = Mock(return_value={'status':'expired'})
        result = act(self.db,self.actor,self.driver,'temporary',row['id'],'recheck')
        self.assertEqual(result['status'],'expired')
        self.assertEqual(self.driver.node_action.call_args.args[:2],(row['id'],'temporary_status'))
        self.assertTrue(self.driver.node_action.call_args.kwargs['recover'])
        self.driver.node_action.assert_called_once()
        self.assertEqual(history(self.db,self.actor)['items'][0]['kind'],'temporary')

    def test_unknown_evidence_preserves_block_and_audit(self):
        row = self.blocked()
        self.driver.node_action = Mock(side_effect=TimeoutError('private secret'))
        with self.assertRaises(AccessDenied):
            act(self.db,self.actor,self.driver,'temporary',row['id'],'recheck')
        self.assertEqual(self.service.get(self.actor,row['id'])['status'],'blocked')
        self.assertEqual(history(self.db,self.actor)['items'][0]['outcome'],'unconfirmed')
        self.assertNotIn('private secret',str(history(self.db,self.actor)))

    def test_resolution_requires_explicit_fenced_absence_and_is_terminal(self):
        row = self.blocked()
        empty=dict.fromkeys(('disk_present','live_present','identity_matches','config_available'),False)
        self.driver.node_action=Mock(return_value={'unused_confirmed':True,'observation':{**empty,'live_present':0}})
        with self.assertRaises(AccessDenied):
            act(self.db,self.actor,self.driver,'temporary',row['id'],'resolve')
        self.assertEqual(self.service.get(self.actor,row['id'])['status'],'blocked')
        self.driver.node_action.return_value={'unused_confirmed':True,'observation':empty}
        result=act(self.db,self.actor,self.driver,'temporary',row['id'],'resolve')
        self.assertEqual(result['status'],'cancelled')
        self.assertEqual(self.driver.node_action.call_args.args[:2],(row['id'],'temporary_resolve'))
        self.assertTrue(self.driver.node_action.call_args.kwargs['recover'])
        before=self.driver.node_action.call_count
        with self.assertRaises(AccessDenied):
            act(self.db,self.actor,self.driver,'temporary',row['id'],'resolve')
        self.assertEqual(self.driver.node_action.call_count,before)
