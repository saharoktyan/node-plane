import json
from datetime import datetime, timedelta, timezone
import unittest
from uuid import uuid4

from backend.authorization import Actor, Principal, PrincipalKind, ADMIN_PERMISSIONS, AccessDenied
from backend.backups import BackupService
from backend.temporary_configs import TemporaryConfigService
from tests.test_backend_http import BackendHTTPTests
from tests.test_backend_config_issuance import BackendConfigIssuanceTests


class TemporaryConfigTests(unittest.TestCase):
    setUp = BackendHTTPTests.setUp
    setup_ready_profile = BackendConfigIssuanceTests.setup_ready_profile

    def prepare(self, protocol='xray'):
        self.setup_ready_profile(protocol)
        self.actor = Actor(Principal('workstation',PrincipalKind.ACCOUNT,ADMIN_PERMISSIONS,self.admin.id),self.admin)
        base = BackendConfigIssuanceTests.driver(self)
        base.calls, base.receipts, base.states = [], {}, {}
        def action(identity, action, intent, recover=False):
            base.calls.append((identity,action,recover))
            if action == 'temporary_status':
                return base.states[identity]
            if action == 'temporary_revoke':
                return {'revocation_mode':'new_connections_only' if intent['protocol']=='xray' else 'peer_removed'}
            if identity not in base.receipts:
                if recover:
                    raise ValueError('not recorded')
                expiry = (datetime.now(timezone.utc)+timedelta(seconds=intent['lease_seconds'])).isoformat()
                base.receipts[identity] = {'expires_at':expiry,
                    'revocation_mode':'new_connections_only' if intent['protocol']=='xray' else 'peer_removed',
                    'xray_uuid':intent['uuid'],'wg_conf':'[Interface]\nPrivateKey = secret\n','vpn_key':'vpn://secret'}
                base.states[identity] = {'status':'active','expires_at':expiry,'enforcement_ready':True}
            return base.receipts[identity]
        base.node_action = action
        base.refresh_awg_config = lambda *args: {'wg_conf':'[Interface]\nPrivateKey = secret\n','vpn_key':'vpn://secret'}
        self.driver = base
        self.service = TemporaryConfigService(self.db,base)

    def create(self, protocol='xray', duration=86400, key=None):
        return self.service.create(self.actor,'n1',protocol,'tcp' if protocol=='xray' else 'vpn',duration,key or str(uuid4()))

    def test_durations_idempotency_and_independent_identities(self):
        self.prepare()
        key = str(uuid4())
        first = self.create(key=key)
        self.assertEqual(self.create(key=key),first)
        with self.assertRaises(AccessDenied):
            self.create(duration=43200,key=key)
        for invalid in (3600,259201,True):
            with self.assertRaises(AccessDenied):
                self.create(duration=invalid)
        second = self.create(duration=259200)
        intents = [json.loads(r[0]) for r in self.db.connection.execute('SELECT intent_json FROM backend_temporary_configs')]
        self.assertEqual(len({i['uuid'] for i in intents}),2)
        self.assertEqual(len({i['runtime_name'] for i in intents}),2)
        self.assertNotEqual(first['id'],second['id'])
        self.assertNotIn('runtime_name',first)

    def test_stopped_or_unsupported_enforcement_blocks_issuance_and_recovers_read_only(self):
        self.prepare()
        original = self.driver.node_action
        def unhealthy(identity, action, intent, recover=False):
            result = original(identity,action,intent,recover)
            if action=='temporary_status':
                return {k:v for k,v in result.items() if k!='enforcement_ready'}
            return result
        self.driver.node_action = unhealthy
        config = self.create()
        self.service.run_one()
        self.assertEqual(self.service.get(self.actor,config['id'])['status'],'blocked')
        self.driver.node_action = original
        self.service.reconcile()
        self.assertEqual(self.service.get(self.actor,config['id'])['status'],'active')
        self.assertEqual(len([c for c in self.driver.calls if c[1]=='temporary_ensure' and not c[2]]),1)
        self.driver.states[config['id']]['enforcement_ready'] = False
        with self.assertRaises(AccessDenied) as failure:
            self.service.artifact(self.actor,config['id'])
        self.assertEqual(failure.exception.code,'config_unavailable')

    def test_issue_download_revoke_for_both_protocols_and_backup_guard(self):
        for protocol in ('xray','awg'):
            with self.subTest(protocol=protocol):
                if protocol=='xray':
                    self.prepare(protocol)
                else:
                    self.db.connection.execute("UPDATE backend_nodes SET protocols_json='[\"awg\"]'")
                config = self.create(protocol)
                self.assertTrue(self.service.run_one())
                active = self.service.get(self.actor,config['id'])
                self.assertEqual(active['status'],'active')
                expiry = active['expires_at']
                artifact = self.service.artifact(self.actor,config['id'])
                self.assertTrue(artifact['content'].startswith('vless://' if protocol=='xray' else 'vpn://'))
                self.assertEqual(len(artifact['files']),1 if protocol=='xray' else 2)
                self.assertTrue(BackupService.busy(self.db.connection))
                self.assertEqual(self.service.get(self.actor,config['id'])['expires_at'],expiry)
                self.assertEqual(self.service.revoke(self.actor,config['id'])['status'],'revoking')
                with self.assertRaises(AccessDenied):
                    self.service.artifact(self.actor,config['id'])
                self.service.run_one()
                self.assertEqual(self.service.get(self.actor,config['id'])['status'],'revoked')
                self.assertEqual(self.service.list(self.actor)['total'],0)
                self.assertFalse(BackupService.busy(self.db.connection))
        events = [dict(r) for r in self.db.connection.execute('SELECT * FROM backend_temporary_config_events')]
        self.assertTrue(events)
        self.assertTrue(all(r['principal_id']=='workstation' and r['actor_id']==self.admin.id for r in events))
        self.assertNotIn('secret',json.dumps(events))

    def test_unknown_issue_is_only_read_back_and_local_expiry_settles_it(self):
        self.prepare()
        config = self.create()
        action = self.driver.node_action
        def lose_response(*args,**kwargs):
            result = action(*args,**kwargs)
            if not kwargs.get('recover'):
                raise TimeoutError('lost response')
            return result
        self.driver.node_action = lose_response
        self.service.run_one()
        self.assertEqual(self.service.get(self.actor,config['id'])['status'],'blocked')
        self.service.reconcile()
        self.assertEqual(self.service.get(self.actor,config['id'])['status'],'active')
        self.assertEqual(sum(not c[2] for c in self.driver.calls),1)
        self.db.connection.execute("UPDATE backend_temporary_configs SET status='blocked' WHERE id=?",(config['id'],))
        self.driver.states[config['id']]['status']='expired'
        self.service.reconcile()
        self.assertEqual(self.service.get(self.actor,config['id'])['status'],'expired')

    def test_queued_cancel_and_node_changes_never_issue(self):
        self.prepare()
        first = self.create()
        self.assertEqual(self.service.revoke(self.actor,first['id'])['status'],'cancelled')
        second = self.create()
        self.db.connection.execute('UPDATE backend_nodes SET desired_revision=2')
        self.assertTrue(self.service.run_one())
        self.assertEqual(self.service.get(self.actor,second['id'])['status'],'cancelled')
        self.assertEqual(self.driver.calls,[])

    def test_expiry_denies_download_before_worker_and_queues_revocation(self):
        self.prepare()
        config = self.create()
        self.service.run_one()
        self.db.connection.execute('UPDATE backend_temporary_configs SET expires_at=? WHERE id=?',
            ((datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(),config['id']))
        self.assertEqual(self.service.get(self.actor,config['id'])['status'],'expiry_pending')
        with self.assertRaises(AccessDenied) as error:
            self.service.artifact(self.actor,config['id'])
        self.assertEqual(error.exception.code,'config_expired')
        self.service.scheduled()
        self.service.run_one()
        self.assertEqual(self.service.get(self.actor,config['id'])['status'],'expired')

    def test_demoted_admin_cannot_read_or_create_even_with_stale_actor(self):
        self.prepare()
        config = self.create()
        self.db.connection.execute("UPDATE backend_accounts SET role='member' WHERE id=?",(self.admin.id,))
        self.db.connection.commit()
        for operation in (lambda:self.create(),lambda:self.service.list(self.actor),
                          lambda:self.service.get(self.actor,config['id']),
                          lambda:self.service.revoke(self.actor,config['id'])):
            with self.assertRaises(AccessDenied):
                operation()

    def test_http_registry_queues_work_and_checks_permissions(self):
        self.prepare()
        from backend.http_api import create_app
        from fastapi.testclient import TestClient
        headers = {**self.headers,'X-Node-Plane-Telegram-User-ID':'101','Idempotency-Key':str(uuid4())}
        body = {'node_key':'n1','protocol':'xray','transport':'tcp','duration_seconds':43200}
        with TestClient(create_app(self.db,node_driver=self.driver)) as client:
            path = '/api/v1/system/temporary-configs'
            response = client.post(path,headers=headers,json=body)
            self.assertEqual(response.status_code,202,response.text)
            config = response.json()
            self.assertEqual(config['status'],'queued')
            self.assertEqual(client.post(path,headers=headers,json=body).json(),config)
            self.assertEqual(self.driver.calls,[])
            self.service.run_one()
            self.assertEqual(client.get(path,headers=headers).json()['total'],1)
            self.assertEqual(client.get(path+'/'+config['id']+'/artifact',headers=headers).status_code,200)
            self.assertEqual(client.get(path).status_code,401)
            self.assertEqual(client.post(path+'/'+config['id']+'/revoke',headers=headers).status_code,202)
            self.service.run_one()
            self.assertEqual(client.get(path,headers=headers).json()['total'],0)

    def test_revoke_failure_can_be_retried_without_renewing_lease(self):
        self.prepare()
        config = self.create()
        self.service.run_one()
        expiry = self.service.get(self.actor,config['id'])['expires_at']
        self.service.revoke(self.actor,config['id'])
        action = self.driver.node_action
        self.driver.node_action = lambda *args,**kwargs: (_ for _ in ()).throw(TimeoutError())
        self.service.run_one()
        self.assertEqual(self.service.get(self.actor,config['id'])['status'],'revoke_blocked')
        self.driver.node_action = action
        self.service.revoke(self.actor,config['id'])
        self.service.run_one()
        result = self.service.get(self.actor,config['id'])
        self.assertEqual(result['status'],'revoked')
        self.assertEqual(result['expires_at'],expiry)
