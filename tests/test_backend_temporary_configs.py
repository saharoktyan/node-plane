import json
from datetime import datetime, timedelta, timezone
import unittest
from uuid import uuid4

from backend.authorization import Actor, Principal, PrincipalKind, ADMIN_PERMISSIONS, AccessDenied
from backend.backups import BackupService
from backend.temporary_configs import TemporaryConfigService
from tests import test_backend_http as http_fixture
from tests import test_backend_config_issuance as issuance_fixture


class TemporaryConfigTests(unittest.TestCase):
    setUp = http_fixture.BackendHTTPTests.setUp
    setup_ready_profile = issuance_fixture.BackendConfigIssuanceTests.setup_ready_profile

    def prepare(self, protocol='xray'):
        self.setup_ready_profile(protocol)
        self.actor = Actor(Principal('workstation',PrincipalKind.ACCOUNT,ADMIN_PERMISSIONS,self.admin.id),self.admin)
        base = issuance_fixture.BackendConfigIssuanceTests.driver(self)
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
        self.awg_refresh_labels = []
        def refresh_awg(node_key, wg_conf, profile_name):
            self.awg_refresh_labels.append(profile_name)
            return {'wg_conf':'[Interface]\nPrivateKey = secret\n\n[Peer]\nEndpoint = node.example:51820\n','vpn_key':'vpn://secret'}
        base.refresh_awg_config = refresh_awg
        self.driver = base
        self.service = TemporaryConfigService(self.db,base)

    def create(self, protocol='xray', duration=86400, key=None):
        return self.service.create(self.actor,'n1',protocol,'tcp' if protocol=='xray' else 'vpn',duration,key or str(uuid4()))

    def test_pending_settings_hold_new_leases_but_allow_revocation(self):
        self.prepare()
        active = self.create()
        self.service.run_one()
        queued = self.create()
        self.service.revoke(self.actor, active['id'])
        identity = str(uuid4())
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO backend_node_settings_tasks(id,node_key,actor_account_id,command_key,revision,intent_json,status) VALUES (?,'n1',?,?,1,'{}','awaiting_executor')",
                         (identity, self.admin.id, str(uuid4())))
        self.assertTrue(self.service.run_one(), 'revocation goes before settings cleanup')
        self.assertEqual(self.service.get(self.actor, active['id'])['status'], 'revoked')
        self.assertEqual(self.service.get(self.actor, queued['id'])['status'], 'queued')
        self.assertFalse(self.service.run_one(), 'retained queued lease is held, not cancelled')
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_node_settings_tasks SET status='succeeded' WHERE id=?", (identity,))
        self.assertTrue(self.service.run_one())
        self.assertEqual(self.service.get(self.actor, queued['id'])['status'], 'active')

    def test_retained_lease_waits_between_saving_protocol_change_and_apply(self):
        self.prepare()
        queued = self.create()
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_nodes SET desired_revision=desired_revision+1 WHERE key='n1'")
            conn.execute("UPDATE backend_temporary_configs SET node_revision=node_revision+1 WHERE id=?", (queued['id'],))
        self.assertFalse(self.service.run_one())
        self.assertEqual(self.service.get(self.actor, queued['id'])['status'], 'queued')
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_nodes SET applied_revision=desired_revision WHERE key='n1'")
        self.assertTrue(self.service.run_one())
        self.assertEqual(self.service.get(self.actor, queued['id'])['status'], 'active')

    def test_get_after_previous_revocation_repeatedly_checks_enforcement_without_reissuing(self):
        self.prepare()
        old = self.create()
        self.service.run_one()
        self.service.revoke(self.actor,old['id'])
        self.service.run_one()
        current = self.create()
        self.service.run_one()
        deadline = self.service.get(self.actor,current['id'])['expires_at']
        for ready in (True,False,True,False,True):
            self.driver.states[current['id']]['enforcement_ready'] = ready
            if ready:
                self.assertTrue(self.service.artifact(self.actor,current['id'])['content'].startswith('vless://'))
            else:
                with self.assertRaises(AccessDenied) as failed:
                    self.service.artifact(self.actor,current['id'])
                self.assertEqual(failed.exception.code,'config_unavailable')
        self.assertEqual(self.service.get(self.actor,current['id'])['expires_at'],deadline)
        self.assertEqual(sum(identity==current['id'] and action=='temporary_ensure' and not recover for identity,action,recover in self.driver.calls),1)
        with self.assertRaises(AccessDenied):
            self.service.artifact(self.actor,old['id'])

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

    def test_temporary_awg_download_rejects_a_stale_runtime_endpoint(self):
        self.prepare('awg')
        config = self.create('awg')
        self.service.run_one()
        self.driver.refresh_awg_config = lambda *args: {'wg_conf':
            '[Interface]\nPrivateKey = secret\n\n[Peer]\nEndpoint = old.example:51820\n',
            'vpn_key': 'vpn://secret'}
        with self.assertRaises(AccessDenied) as failure:
            self.service.artifact(self.actor, config['id'])
        self.assertEqual(failure.exception.code, 'config_stale')

    def test_temporary_vless_honors_fingerprint_and_rejects_stale_runtime_settings(self):
        self.prepare()
        with self.db.transaction() as conn:
            settings = json.loads(conn.execute("SELECT settings_json FROM backend_nodes WHERE key='n1'").fetchone()[0])
            settings['xray_fingerprint'] = 'firefox'
            conn.execute("UPDATE backend_nodes SET settings_json=? WHERE key='n1'", (json.dumps(settings),))
        config = self.create()
        self.service.run_one()
        with self.assertRaises(AccessDenied) as failure:
            self.service.artifact(self.actor, config['id'])
        self.assertEqual(failure.exception.code, 'config_stale')
        read_metadata = self.driver.read_xray_public
        self.driver.read_xray_public = lambda key: {**read_metadata(key), 'fingerprint': 'firefox'}
        self.assertIn('fp=firefox', self.service.artifact(self.actor, config['id'])['content'])

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
                from urllib.parse import unquote, urlsplit
                if protocol == 'xray':
                    self.assertEqual(artifact['files'][0]['filename'], 'Latvia #1 · VLESS · TCP · Temp.txt')
                    self.assertEqual(unquote(urlsplit(artifact['content']).fragment), 'VLESS · TCP · Latvia #1 · Temp')
                else:
                    self.assertEqual([f['filename'] for f in artifact['files']],
                                     ['Latvia #1 · AWG · Temp.vpn', 'Latvia #1 · AWG · Temp.conf'])
                    self.assertIn('AWG · Latvia #1 · Temp', self.awg_refresh_labels)
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

    def test_metadata_edits_preserve_active_lease_and_download(self):
        from backend.nodes import NodeService
        self.prepare()
        config = self.create()
        self.service.run_one()
        before = self.service.get(self.actor, config['id'])
        original = self.service.artifact(self.actor, config['id'])
        nodes = NodeService(self.db)
        current = nodes.get(self.actor, 'n1')
        changed = nodes.command(self.actor, action='edit', node_key='n1',
            revision=current['desired_revision'], command_key=str(uuid4()),
            values={'title':'New server name', 'flag':'🇱🇻', 'notes':'Edited metadata'})
        self.assertEqual(changed['desired_revision'], changed['applied_revision'])
        after = self.service.get(self.actor, config['id'])
        self.assertEqual(after['expires_at'], before['expires_at'])
        self.assertEqual(after['status'], 'active')
        refreshed = self.service.artifact(self.actor, config['id'])
        self.assertEqual(refreshed['content'].split('#')[0], original['content'].split('#')[0])
        self.assertEqual(sum(action == 'temporary_ensure' and not recover
            for _, action, recover in self.driver.calls), 1)

    def test_changed_connection_settings_are_explained_and_never_downloaded(self):
        from backend.nodes import NodeService
        self.prepare()
        config = self.create()
        self.service.run_one()
        nodes = NodeService(self.db)
        current = nodes.get(self.actor, 'n1')
        settings = {**current['settings'], 'xray_sni':'example.org'}
        nodes.command(self.actor, action='edit', node_key='n1', revision=current['desired_revision'],
            command_key=str(uuid4()), values={'settings':settings})
        item = self.service.get(self.actor, config['id'])
        self.assertFalse(item['artifact_available'])
        self.assertEqual(item['unavailable_reason'], 'node_changed')
        with self.assertRaises(AccessDenied):
            self.service.artifact(self.actor, config['id'])

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
