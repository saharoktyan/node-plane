"""Policy/recipient races against disposable PostgreSQL, without external sends.

Run separately: other discovery modules install database doubles.
"""
import base64
import json
import os
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from uuid import uuid4

from backend.announcements import AnnouncementService
from backend.authorization import AccessDenied, Principal, PrincipalKind
from backend.identity_repository import SQLIdentityRepository
from backend.node_lifecycle import NodeLifecycle
from backend.operations import OperationRepository
from backend.profile_commands import ProfileCommands
from backend.profiles import ProfileRepository
from backend.system_settings import SystemSettingsService
from backend.traffic import TrafficService, MAX_COUNTER
from tests import test_backups_postgres as fixture
from tests.test_core_reliability_postgres import HookDatabase


class CounterDriver:
    def __init__(self):
        self.calls = []
        self.up, self.down, self.epoch = 1000, 2000, 'a' * 64
        self.entered, self.release = threading.Event(), threading.Event()
        self.pause = False
        self.failure = False

    def traffic_snapshot(self, intent):
        self.calls.append(intent)
        result = {**intent, 'epoch': self.epoch,
                  'uplink_bytes': self.up, 'downlink_bytes': self.down}
        if self.pause:
            self.entered.set()
            if not self.release.wait(timeout=5):
                raise RuntimeError('test did not release counter read')
        if self.failure:
            raise TimeoutError('simulated agent timeout')
        return result


@unittest.skipUnless(os.environ.get('NODE_PLANE_TEST_POSTGRES_DSN'), 'requires disposable PostgreSQL')
class TrafficPolicyPostgresTests(unittest.TestCase):
    drop_schema = fixture.BackupsPostgresTests.drop_schema

    def setUp(self):
        fixture.BackupsPostgresTests.setUp(self)
        self.backups = self.service
        self.profile = ProfileRepository(self.db).create_profile(runtime_name='traffic_test',
            display_name='Traffic test', owner_account_id=self.admin.id)
        with self.db.transaction() as conn:
            conn.execute("""INSERT INTO backend_nodes
                (key,title,region,protocols_json,xray_transports_json,applied_revision)
                VALUES ('node','Test node','Europe','["xray"]','["tcp","xhttp"]',1)""")
            conn.execute("INSERT INTO backend_grants VALUES (?,'node','xray')", (self.profile,))
            OperationRepository.record(conn, self.actor, self.profile, set())
            conn.execute("UPDATE backend_operation_tasks SET status='succeeded'")
            conn.execute("UPDATE backend_operations SET status='succeeded'")
        self.settings = SystemSettingsService(self.db)
        self.settings.update_traffic_policy(self.actor, True)
        self.driver = CounterDriver()
        self.service = TrafficService(self.db, self.driver)

    def collect(self):
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM backend_system_settings WHERE key='traffic_last_scan'")
        return self.service.scheduled()

    def row(self):
        with self.db.connect() as conn:
            return conn.execute('SELECT * FROM backend_traffic_usage WHERE profile_id=?', (self.profile,)).fetchone()

    def during_read(self, change):
        self.driver.pause = True
        with ThreadPoolExecutor(max_workers=1) as pool:
            sample = pool.submit(self.collect)
            try:
                self.assertTrue(self.driver.entered.wait(timeout=5))
                change()
            finally:
                self.driver.release.set()
            return sample.result(timeout=5)

    def command(self, action, values=None):
        return ProfileCommands(self.db).execute(self.actor, str(uuid4()), action=action,
            profile_id=self.profile, revision=1, values=values)

    def test_disabled_collection_makes_no_agent_calls_and_hides_summary(self):
        self.settings.update_traffic_policy(self.actor, False)
        self.assertFalse(self.collect())
        self.assertEqual(self.driver.calls, [])
        self.assertIsNone(self.service.summary(self.admin.id, self.profile))

    def test_demoted_administrator_cannot_change_policy_with_stale_actor(self):
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_accounts SET role='member' WHERE id=?", (self.admin.id,))
        with self.assertRaises(AccessDenied) as denied:
            self.settings.update_traffic_policy(self.actor, False)
        self.assertEqual(denied.exception.code, 'permission_denied')
        with self.db.connect() as conn:
            self.assertTrue(TrafficService._enabled(conn))

    def test_disabling_policy_during_read_discards_response_and_keeps_totals(self):
        self.collect()
        self.driver.up = 1100
        self.collect()
        self.driver.up = 2000
        self.assertFalse(self.during_read(lambda: self.settings.update_traffic_policy(self.actor, False)))
        self.assertEqual(self.row()['uplink_bytes'], 100)
        self.assertIsNone(self.row()['epoch'])
        self.assertIsNone(self.service.summary(self.admin.id, self.profile))

    def test_disable_enable_race_is_fenced_by_generation(self):
        def change():
            self.settings.update_traffic_policy(self.actor, False)
            self.settings.update_traffic_policy(self.actor, True)
        self.assertFalse(self.during_read(change))
        self.assertIsNone(self.row())

    def test_reenable_uses_new_baseline_without_counting_disabled_period(self):
        self.collect()
        self.driver.up = 1100
        self.collect()
        self.settings.update_traffic_policy(self.actor, False)
        self.settings.update_traffic_policy(self.actor, True)
        self.driver.up = 5000
        self.collect()
        self.assertEqual(self.row()['uplink_bytes'], 100)
        self.driver.up = 5050
        self.collect()
        self.assertEqual(self.row()['uplink_bytes'], 150)

    def test_revoked_grant_during_read_is_not_recorded(self):
        self.during_read(lambda: self.command('grants', {'grants': []}))
        self.assertIsNone(self.row())

    def test_profile_freeze_during_read_is_not_recorded(self):
        self.during_read(lambda: self.command('edit', {'frozen': True}))
        self.assertIsNone(self.row())

    def test_profile_deletion_during_read_is_not_recorded(self):
        self.during_read(lambda: self.command('delete'))
        self.assertIsNone(self.row())

    def test_node_drain_during_read_is_not_recorded(self):
        self.during_read(lambda: NodeLifecycle(self.db).start_drain(self.actor, 'node'))
        self.assertIsNone(self.row())

    def test_restore_admitted_during_read_prevents_accounting_commit(self):
        backup_id = self.backups.create_snapshot()['backup_id']
        checksum = self.backups.detail(self.actor, backup_id)['checksum']
        self.assertFalse(self.during_read(lambda: self.backups.queue(
            self.actor, str(uuid4()), 'restore', backup_id, checksum)))
        self.assertIsNone(self.row())

    def test_owner_transfer_during_read_purges_history_and_discards_old_owner_sample(self):
        self.collect()
        self.driver.up = 1100
        self.collect()
        new_owner = SQLIdentityRepository(self.db).create_account()
        def transfer():
            with self.db.transaction() as conn:
                conn.execute('UPDATE backend_account_guard SET revision=revision+1 WHERE id=1')
                conn.execute('UPDATE backend_profiles SET owner_account_id=? WHERE id=?', (new_owner.id, self.profile))
        self.during_read(transfer)
        self.assertIsNone(self.row())
        self.assertEqual(self.service.summary(self.admin.id, self.profile)['items'], [])

    def test_outage_preserves_baseline_and_epoch_reset_counts_only_new_counters(self):
        self.collect()
        self.driver.failure = True
        self.collect()
        self.assertEqual(self.row()['status'], 'unknown')
        self.assertEqual(self.row()['last_uplink'], 1000)
        self.driver.failure = False
        self.driver.up = 1200
        self.collect()
        self.assertEqual(self.row()['uplink_bytes'], 200)
        self.driver.epoch, self.driver.up, self.driver.down = 'b' * 64, 30, 40
        self.collect()
        self.assertEqual((self.row()['uplink_bytes'], self.row()['downlink_bytes']), (230, 40))

    def test_awg_identity_comes_from_confirmed_client_key(self):
        key = base64.b64encode(b'k' * 32).decode()
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_nodes SET protocols_json='[\"awg\"]'")
            conn.execute("UPDATE backend_grants SET protocol='awg'")
            conn.execute("UPDATE backend_operation_tasks SET protocol='awg',result_json=?", (
                json.dumps({'wg_conf': f'[Interface]\nPublicKey = {key}\n[Peer]\nPublicKey = other'}),))
        self.collect()
        self.assertEqual(self.driver.calls[0]['identity'], key)
        self.assertEqual(self.row()['protocol'], 'awg')

    def test_month_rollover_resets_month_totals_but_keeps_live_baseline(self):
        self.collect()
        self.driver.up = 1100
        self.collect()
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_traffic_usage SET period_month='2000-01'")
        self.driver.up = 1150
        self.collect()
        self.assertEqual(self.row()['uplink_bytes'], 50)
        self.assertEqual(self.row()['period_month'], datetime.now(timezone.utc).strftime('%Y-%m'))

    def test_overflow_does_not_corrupt_previous_baseline(self):
        self.collect()
        with self.db.transaction() as conn:
            conn.execute('UPDATE backend_traffic_usage SET uplink_bytes=?', (MAX_COUNTER,))
        self.driver.up = 1100
        self.collect()
        self.assertEqual(self.row()['uplink_bytes'], MAX_COUNTER)
        self.assertEqual(self.row()['last_uplink'], 1000)
        self.assertEqual(self.row()['status'], 'unknown')


@unittest.skipUnless(os.environ.get('NODE_PLANE_TEST_POSTGRES_DSN'), 'requires disposable PostgreSQL')
class AnnouncementPolicyPostgresTests(unittest.TestCase):
    drop_schema = fixture.BackupsPostgresTests.drop_schema

    def setUp(self):
        fixture.BackupsPostgresTests.setUp(self)
        self.backups = self.service
        self.identities = SQLIdentityRepository(self.db)
        self.member = self.identities.resolve_telegram(102)
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_accounts SET status='approved' WHERE id=?", (self.member.id,))
        self.profile = ProfileRepository(self.db).ensure_account_profile(self.member.id)
        self.transport = Principal('telegram-test', PrincipalKind.ADAPTER, frozenset({'settings.manage'}))
        self.service = AnnouncementService(self.db)

    def queue(self):
        return self.service.queue(self.actor, 'Test notice', str(uuid4()))

    def delete(self):
        ProfileCommands(self.db).execute(self.actor, str(uuid4()), action='delete',
            profile_id=self.profile, revision=1)

    def test_sender_and_pending_orphan_nontelegram_accounts_are_not_recipients(self):
        self.identities.resolve_telegram(103)
        self.identities.create_account()
        orphan = self.identities.resolve_telegram(104)
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_accounts SET status='approved' WHERE id=?", (orphan.id,))
            conn.execute('DELETE FROM backend_profiles WHERE owner_account_id=?', (orphan.id,))
        self.assertEqual(self.service.preview(self.actor, 'Test notice')['recipients'], 1)
        self.assertEqual(self.queue()['total'], 1)
        self.assertEqual(self.service.claim(self.transport, str(uuid4()))['telegram_user_id'], 102)

    def test_deleted_profile_before_claim_is_skipped_even_with_retained_approval(self):
        job = self.queue()
        self.delete()
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_accounts SET status='approved' WHERE id=?", (self.member.id,))
        self.assertIsNone(self.service.claim(self.transport, str(uuid4())))
        self.assertEqual(self.service.get(self.actor, job['id'])['counts']['skipped'], 1)

    def test_disabled_recipient_before_claim_is_skipped(self):
        job = self.queue()
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_accounts SET status='disabled' WHERE id=?", (self.member.id,))
        self.assertIsNone(self.service.claim(self.transport, str(uuid4())))
        self.assertEqual(self.service.get(self.actor, job['id'])['counts']['skipped'], 1)

    def test_sender_demotion_before_claim_stops_delivery(self):
        self.queue()
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_accounts SET role='member' WHERE id=?", (self.admin.id,))
        self.assertIsNone(self.service.claim(self.transport, str(uuid4())))

    def test_deletion_transaction_serializes_against_claim_authorization(self):
        job = self.queue()
        deleting, release, claiming = (threading.Event() for _ in range(3))
        def hold_delete(sql):
            if 'INSERT INTO backend_profile_deletions' in sql:
                deleting.set()
                if not release.wait(timeout=5):
                    raise RuntimeError('test did not release deletion')
        def claim():
            claiming.set()
            return self.service.claim(self.transport, str(uuid4()))
        with ThreadPoolExecutor(max_workers=2) as pool:
            deletion = pool.submit(ProfileCommands(HookDatabase(self.db, hold_delete)).execute,
                self.actor, str(uuid4()), action='delete', profile_id=self.profile, revision=1)
            try:
                self.assertTrue(deleting.wait(timeout=5))
                delivery = pool.submit(claim)
                self.assertTrue(claiming.wait(timeout=5))
            finally:
                release.set()
            deletion.result(timeout=5)
            self.assertIsNone(delivery.result(timeout=5))
        self.assertEqual(self.service.get(self.actor, job['id'])['counts']['skipped'], 1)

    def test_two_concurrent_same_key_announcements_create_one_delivery(self):
        key = str(uuid4())
        start = threading.Barrier(2)
        def queue():
            start.wait(timeout=5)
            return self.service.queue(self.actor, 'Test notice', key)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = pool.submit(queue), pool.submit(queue)
            self.assertEqual(first.result(timeout=5)['id'], second.result(timeout=5)['id'])
        with self.db.connect() as conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) AS count FROM backend_announcement_deliveries').fetchone()['count'], 1)

    def test_deleted_profile_between_preview_and_queue_is_not_admitted(self):
        self.assertEqual(self.service.preview(self.actor, 'Test notice')['recipients'], 1)
        self.delete()
        self.assertEqual(self.queue()['total'], 0)

    def test_lost_claim_response_can_retry_only_while_still_eligible(self):
        self.queue()
        key = str(uuid4())
        original = self.service.claim(self.transport, key)
        self.assertEqual(self.service.claim(self.transport, key), original)
        self.service.acknowledge(self.transport, original['id'], key, 'unknown')
        self.assertIsNone(self.service.claim(self.transport, key))

    def test_sound_preference_is_read_at_claim_not_composition_time(self):
        self.queue()
        with self.db.transaction() as conn:
            conn.execute('INSERT INTO backend_system_settings VALUES (?,?)',
                ('announcement_silent:' + self.member.id, 'true'))
        self.assertTrue(self.service.claim(self.transport, str(uuid4()))['silent'])

    def test_two_adapters_cannot_claim_same_recipient_with_different_keys(self):
        self.queue()
        start = threading.Barrier(2)
        def claim():
            start.wait(timeout=5)
            return self.service.claim(self.transport, str(uuid4()))
        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = pool.submit(claim), pool.submit(claim)
            results = [first.result(timeout=5), second.result(timeout=5)]
        self.assertEqual(sum(result is not None for result in results), 1)

    def test_expired_claim_is_unknown_and_never_replayed(self):
        job, key = self.queue(), str(uuid4())
        self.service.claim(self.transport, key)
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_announcement_deliveries SET claimed_until='2000-01-01T00:00:00+00:00'")
        self.assertIsNone(self.service.claim(self.transport, key))
        self.assertIsNone(self.service.claim(self.transport, str(uuid4())))
        self.assertEqual(self.service.get(self.actor, job['id'])['counts']['unknown'], 1)

    def test_completed_claim_is_not_replayed_and_wrong_adapter_cannot_ack(self):
        self.queue()
        key = str(uuid4())
        delivery = self.service.claim(self.transport, key)
        wrong = Principal('other-adapter', PrincipalKind.ADAPTER, self.transport.scopes)
        with self.assertRaises(AccessDenied):
            self.service.acknowledge(wrong, delivery['id'], key, 'sent')
        self.service.acknowledge(self.transport, delivery['id'], key, 'sent')
        self.service.acknowledge(self.transport, delivery['id'], key, 'sent')
        self.assertIsNone(self.service.claim(self.transport, key))

    def test_retry_of_claim_rechecks_deleted_profile(self):
        job, key = self.queue(), str(uuid4())
        self.service.claim(self.transport, key)
        self.delete()
        self.assertIsNone(self.service.claim(self.transport, key))
        self.assertEqual(self.service.get(self.actor, job['id'])['counts']['unknown'], 1)

    def test_retry_of_claim_rechecks_sender_demotion(self):
        self.queue()
        key = str(uuid4())
        self.service.claim(self.transport, key)
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_accounts SET role='member' WHERE id=?", (self.admin.id,))
        self.assertIsNone(self.service.claim(self.transport, key))

    def test_restore_blocks_retry_of_preexisting_claim(self):
        self.queue()
        key = str(uuid4())
        self.service.claim(self.transport, key)
        backup_id = self.backups.create_snapshot()['backup_id']
        checksum = self.backups.detail(self.actor, backup_id)['checksum']
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_announcement_deliveries SET status='unknown'")
        self.backups.queue(self.actor, str(uuid4()), 'restore', backup_id, checksum)
        # Simulate an old delivery whose status was claimed before restore admission.
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_announcement_deliveries SET status='claimed'")
        self.assertIsNone(self.service.claim(self.transport, key))
