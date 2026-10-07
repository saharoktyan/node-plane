"""Multiple peers through the real outbox, issuance and accounting services."""
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
import unittest
from uuid import uuid4

from backend.authorization import ADMIN_PERMISSIONS, APPROVED_PERMISSIONS, SELF_PERMISSIONS, AccessDenied, Actor, Principal, PrincipalKind
from backend.config_issuance import ConfigIssuanceService
from backend.devices import DeviceRepository
from backend.executor import IntentExecutor
from backend.node_lifecycle import NodeLifecycle
from backend.node_overview import NodeOverviewService
from backend.operations import OperationRepository
from backend.profile_commands import ProfileCommands
from backend.profiles import ProfileRepository
from backend.system_settings import SystemSettingsService
from backend.traffic import TrafficService
from tests.test_backend_config_issuance import BackendConfigIssuanceTests


class PeerDriver:
    def __init__(self):
        self.peers, self.calls, self.counters, self.failures = {}, [], {}, set()

    def execute(self, task_id, intent):
        name = intent['runtime_name']
        self.calls.append((task_id, intent.copy()))
        if intent['action']=='delete':
            self.peers.pop(name,None)
            result = {}
        else:
            if name not in self.peers:
                key = base64.b64encode(hashlib.sha256(name.encode()).digest()).decode()
                self.peers[name] = (f'[Interface]\nPrivateKey = {key}\nPublicKey = {key}\n'
                    f'Address = 10.8.0.{len(self.peers)+2}/32\n\n[Peer]\nEndpoint = node.example:51820\n')
                self.counters.setdefault(name,[1000,2000,'a'*64])
            result = {'wg_conf':self.peers[name]}
        return {'succeeded':True,'driver_operation_id':task_id,'result_json':json.dumps(result)}

    def inspect_node(self,node_key):
        return {'health_state':'running','awg_config_present':True}

    def inspect(self,intent):
        return {key:intent['runtime_name'] in self.peers for key in
            ('disk_present','live_present','identity_matches','config_available')}

    def refresh_awg_config(self,node_key,wg_conf,profile_name):
        return {'wg_conf':wg_conf,'vpn_key':'vpn://'+base64.b64encode((profile_name+'\n'+wg_conf).encode()).decode()}

    def traffic_snapshot(self,intent):
        if intent['runtime_name'] in self.failures:
            raise ConnectionError('peer unavailable')
        up,down,epoch = self.counters[intent['runtime_name']]
        return {**intent,'epoch':epoch,'uplink_bytes':up,'downlink_bytes':down}


class BackendDevicePeerTests(unittest.TestCase):
    setUp = BackendConfigIssuanceTests.setUp
    setup_ready_profile = BackendConfigIssuanceTests.setup_ready_profile

    def prepare(self, *, extra=True):
        self.profile_id = self.setup_ready_profile('awg')
        self.actor = Actor(Principal('test',PrincipalKind.SERVICE,ADMIN_PERMISSIONS|APPROVED_PERMISSIONS|SELF_PERMISSIONS),self.admin)
        self.driver = PeerDriver()
        self.executor = IntentExecutor(self.db,self.driver)
        self.issuances = ConfigIssuanceService(self.db,self.driver)
        with self.db.transaction() as conn:
            original = conn.execute('SELECT * FROM backend_devices WHERE profile_id=?',(self.profile_id,)).fetchone()
            self.original_id = original['id']
            self.extra_id = str(uuid4())
            if extra:
                # Device commands are the next slice; seed an independent
                # identity here without bypassing execution of its peer.
                conn.execute('''INSERT INTO backend_devices
                    (id,profile_id,display_name,runtime_name,status,created_at)
                    VALUES (?,?,'Phone','phone_peer','active',?)''',
                    (self.extra_id,self.profile_id,datetime.now(timezone.utc).isoformat()))
            conn.execute('UPDATE backend_profiles SET desired_revision=2 WHERE id=?',(self.profile_id,))
            self.operation = OperationRepository.record(conn,self.actor,self.profile_id,OperationRepository.targets(conn,self.profile_id))
        return self.profile_id

    def finish(self):
        while self.executor.run_one():
            pass

    def issue(self,device_id,key=None):
        return self.issuances.request(self.actor,self.profile_id,'n1','awg','vpn',key or str(uuid4()),device_id=device_id)

    def revise(self):
        with self.db.transaction() as conn:
            conn.execute('UPDATE backend_profiles SET desired_revision=desired_revision+1 WHERE id=?',(self.profile_id,))
            return OperationRepository.record(conn,self.actor,self.profile_id,OperationRepository.targets(conn,self.profile_id))

    def test_separate_keys_addresses_artifacts_and_idempotent_peer_ensures(self):
        self.prepare()
        self.finish()
        self.assertEqual(len(self.driver.peers),2)
        alice,phone = self.driver.peers['alice'],self.driver.peers['phone_peer']
        self.assertNotEqual(alice,phone)
        self.assertNotEqual(alice.split('Address = ')[1].splitlines()[0],phone.split('Address = ')[1].splitlines()[0])
        with self.assertRaises(AccessDenied) as error:
            self.issue(None)
        self.assertEqual(error.exception.code,'device_required')
        artifacts = []
        for device_id in (self.original_id,self.extra_id):
            key = str(uuid4())
            queued = self.issue(device_id,key)
            self.assertEqual(queued,self.issue(device_id,key))
            self.assertTrue(self.issuances.run_one(queued['id']))
            artifacts.append(self.issuances.artifact(self.actor,queued['id']))
        self.assertNotEqual(artifacts[0]['content'],artifacts[1]['content'])
        self.assertTrue(artifacts[1]['display_name'].endswith(' · Phone'))
        self.assertIn(' - Phone.vpn',artifacts[1]['filename'])
        self.revise()
        self.finish()
        self.assertEqual(self.driver.peers,{'alice':alice,'phone_peer':phone})
        with self.assertRaises(AccessDenied) as error:
            self.issue(self.original_id,key)
        self.assertEqual(error.exception.code,'idempotency_conflict')

    def test_deleted_device_stops_pending_ensure_and_stale_artifact(self):
        self.prepare()
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_devices SET status='deleting' WHERE id=?",(self.extra_id,))
        self.revise()
        self.finish()
        self.assertFalse(any(intent['action']=='ensure' and intent['runtime_name']=='phone_peer' for _,intent in self.driver.calls))
        self.assertEqual(set(self.driver.peers),{'alice'})
        with self.assertRaises(AccessDenied) as error:
            self.issue(self.extra_id)
        self.assertEqual(error.exception.code,'device_unavailable')
        first = self.issue(self.original_id)
        self.issuances.run_one(first['id'])
        self.db.connection.execute("UPDATE backend_devices SET status='deleting' WHERE id=?",(self.original_id,))
        with self.assertRaises(AccessDenied):
            self.issuances.artifact(self.actor,first['id'])

    def test_rename_invalidates_artifact_without_rotating_peer(self):
        self.prepare()
        self.finish()
        peer = self.driver.peers['phone_peer']
        queued = self.issue(self.extra_id)
        self.issuances.run_one(queued['id'])
        self.db.connection.execute("UPDATE backend_devices SET display_name='Tablet',revision=revision+1 WHERE id=?",(self.extra_id,))
        with self.assertRaises(AccessDenied) as error:
            self.issuances.artifact(self.actor,queued['id'])
        self.assertEqual(error.exception.code,'config_stale')
        new = self.issue(self.extra_id)
        self.issuances.run_one(new['id'])
        self.assertTrue(self.issuances.artifact(self.actor,new['id'])['display_name'].endswith(' · Tablet'))
        self.assertEqual(self.driver.peers['phone_peer'],peer)

    def test_foreign_device_cannot_issue_and_revisions_are_rechecked_after_read(self):
        self.prepare()
        self.finish()
        other_profile = ProfileRepository(self.db).create_profile(runtime_name='other',display_name='Other')
        with self.db.transaction() as conn:
            profile = conn.execute('SELECT * FROM backend_profiles WHERE id=?',(other_profile,)).fetchone()
            foreign_device = DeviceRepository.ensure_default(conn,profile)
        with self.assertRaises(AccessDenied) as error:
            self.issue(foreign_device)
        self.assertEqual(error.exception.code,'resource_not_found')
        queued = self.issue(self.extra_id)
        refresh = self.driver.refresh_awg_config

        def revoke(node,wg_conf,name):
            self.db.connection.execute("UPDATE backend_devices SET status='deleting' WHERE id=?",(self.extra_id,))
            return refresh(node,wg_conf,name)

        self.driver.refresh_awg_config = revoke
        self.issuances.run_one(queued['id'])
        self.assertEqual(self.issuances.get(self.actor,self.issue(self.original_id)['id'])['status'],'awaiting_executor')
        self.assertEqual(self.db.connection.execute('SELECT status FROM backend_config_issuances WHERE id=?',(queued['id'],)).fetchone()[0],'superseded')

    def test_node_drain_waits_for_every_peer_and_clears_all_grants(self):
        self.prepare()
        self.finish()
        self.assertEqual(NodeOverviewService(self.db).get(self.actor,'n1')['ready'],1)
        lifecycle = NodeLifecycle(self.db)
        lifecycle.start_drain(self.actor,'n1')
        self.assertFalse(lifecycle.drain_status(self.actor,'n1')['revocations_complete'])
        self.finish()
        self.assertTrue(lifecycle.drain_status(self.actor,'n1')['revocations_complete'])
        self.assertEqual(self.driver.peers,{})
        self.assertEqual(self.db.connection.execute('SELECT COUNT(*) FROM backend_grants').fetchone()[0],0)
        # Simulate missing proof for the original peer. A newer delete for the
        # other device must not hide this older ensure in the cleanup gate.
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM backend_operation_tasks WHERE device_id=? AND action='delete'",(self.original_id,))
            self.assertFalse(lifecycle._revocation_state(conn,'n1')[2])

    def test_profile_deletion_revokes_all_peers_and_existing_artifacts(self):
        self.prepare()
        self.finish()
        queued = self.issue(self.extra_id)
        self.issuances.run_one(queued['id'])
        result = ProfileCommands(self.db).execute(self.actor,str(uuid4()),action='delete',
            profile_id=self.profile_id,revision=2,values={})
        self.assertEqual(result['runtime_status'],'awaiting_executor')
        with self.assertRaises(AccessDenied):
            self.issuances.artifact(self.actor,queued['id'])
        self.finish()
        self.assertEqual(self.driver.peers,{})

    def test_node_summary_is_not_ready_when_one_device_is_blocked(self):
        self.prepare()
        self.finish()
        self.db.connection.execute("UPDATE backend_operation_tasks SET status='blocked' WHERE operation_id=? AND device_id=?",
                                   (self.operation['id'],self.extra_id))
        summary = NodeOverviewService(self.db).get(self.actor,'n1')
        self.assertEqual((summary['ready'],summary['failed'],summary['access_total']),(0,1,1))

    def traffic(self):
        SystemSettingsService(self.db).update_traffic_policy(self.actor,True)
        return TrafficService(self.db,self.driver)

    def collect(self,service):
        self.db.connection.execute("DELETE FROM backend_system_settings WHERE key='traffic_last_scan'")
        self.assertTrue(service.scheduled())
        return self.db.connection.execute("SELECT * FROM backend_traffic_usage WHERE protocol='awg'").fetchone()

    def test_counters_are_independent_and_one_peer_failure_stays_unknown(self):
        self.prepare()
        self.finish()
        service = self.traffic()
        self.assertEqual(self.collect(service)['uplink_bytes'],0)
        self.driver.counters['alice'][:2] = [1100,2200]
        self.driver.counters['phone_peer'][:2] = [1500,2600]
        row = self.collect(service)
        self.assertEqual((row['uplink_bytes'],row['downlink_bytes']),(600,800))
        self.assertEqual(row['status'],'current')
        self.assertEqual(self.collect(service)['uplink_bytes'],600)
        self.driver.counters['alice'][:2] = [1125,2235]
        self.driver.counters['phone_peer'] = [10,20,'b'*64]
        row = self.collect(service)
        self.assertEqual((row['uplink_bytes'],row['downlink_bytes']),(635,855))
        self.driver.failures.add('alice')
        self.driver.counters['phone_peer'][0] += 40
        row = self.collect(service)
        self.assertEqual((row['uplink_bytes'],row['status']),(675,'unknown'))
        self.driver.failures.clear()
        self.driver.counters['alice'][0] += 15
        self.assertEqual(self.collect(service)['uplink_bytes'],690)
        summary = service.summary(self.admin.id,self.profile_id)
        self.assertEqual(len(summary['nodes']),1)
        self.assertEqual(summary['items'][0]['uplink_bytes'],690)

    def test_legacy_baseline_migration_is_independent_of_device_sort_order(self):
        self.prepare()
        self.finish()
        service = self.traffic()
        at = (datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat()
        pub = base64.b64encode(hashlib.sha256(b'alice').digest()).decode()
        # Force the additional device to sort before the adopted default.
        self.db.connection.execute("UPDATE backend_devices SET id='00000000-0000-4000-8000-000000000001' WHERE id=?",(self.extra_id,))
        self.db.connection.execute("UPDATE backend_operation_tasks SET device_id='00000000-0000-4000-8000-000000000001' WHERE device_id=?",(self.extra_id,))
        self.db.connection.execute('''INSERT INTO backend_traffic_usage
            (profile_id,account_id,node_key,protocol,uplink_bytes,downlink_bytes,epoch,identity,last_uplink,last_downlink,tracked_since,last_sample_at,status,period_month)
            VALUES (?,?,'n1','awg',123,234,?,?,1000,2000,?,?,'current',?)''',
            (self.profile_id,self.admin.id,'a'*64,pub,at,at,at[:7]))
        self.driver.counters['alice'][:2] = [1200,2300]
        self.driver.counters['phone_peer'][:2] = [5000,6000]
        row = self.collect(service)
        self.assertEqual((row['uplink_bytes'],row['downlink_bytes']),(323,534))
        self.assertEqual(self.collect(service)['uplink_bytes'],323)

    def test_global_pause_excludes_traffic_for_all_devices_even_without_worker_scan(self):
        self.prepare()
        self.finish()
        service = self.traffic()
        self.collect(service)
        self.driver.counters['alice'][0] += 100
        self.driver.counters['phone_peer'][0] += 200
        self.assertEqual(self.collect(service)['uplink_bytes'],300)
        settings = SystemSettingsService(self.db)
        settings.update_traffic_policy(self.actor,False)
        self.driver.counters['alice'][0] += 10000
        self.driver.counters['phone_peer'][0] += 20000
        settings.update_traffic_policy(self.actor,True)
        self.assertEqual(self.collect(service)['uplink_bytes'],300)
        self.driver.counters['alice'][0] += 10
        self.driver.counters['phone_peer'][0] += 20
        self.assertEqual(self.collect(service)['uplink_bytes'],330)

    def test_freeze_and_expiry_revoke_both_devices(self):
        for mode in ('freeze','expiry'):
            with self.subTest(mode=mode):
                if mode=='freeze':
                    self.prepare()
                    self.finish()
                    self.db.connection.execute('UPDATE backend_profiles SET frozen=1 WHERE id=?',(self.profile_id,))
                    self.revise()
                else:
                    self.db.connection.execute('UPDATE backend_profiles SET frozen=0,expires_at=NULL WHERE id=?',(self.profile_id,))
                    self.revise()
                    self.finish()
                    self.db.connection.execute("UPDATE backend_profiles SET owner_account_id=NULL,expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",(self.profile_id,))
                    self.assertEqual(self.executor.queue_expired_profiles(),1)
                self.finish()
                self.assertEqual(self.driver.peers,{})
