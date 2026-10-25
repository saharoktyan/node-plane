"""PostgreSQL pre-approval visitor tests, run separately against disposable PostgreSQL.

Skip explicitly when NODE_PLANE_TEST_POSTGRES_DSN is unset.
"""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
import tempfile
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from db.postgres_db import PostgresDB
from db.migrations import (
    REVISIONS,
    MigrationError,
    check_schema,
    migrate,
)
from backend.access_requests import AccessRequestService
from backend.accounts import AccountService
from backend.admin_cli import bootstrap_admin
from backend.authorization import (
    ADMIN_PERMISSIONS,
    AccessDenied,
    Actor,
    Principal,
    PrincipalKind,
)
from backend.backups import BackupService, encoded
from backend.identity_repository import SQLIdentityRepository
from backend.telegram_visitors import observe


@unittest.skipUnless(os.environ.get('NODE_PLANE_TEST_POSTGRES_DSN'), 'requires disposable PostgreSQL')
class PreapprovalVisitorsPostgresTests(unittest.TestCase):
    def setUp(self):
        from psycopg.conninfo import make_conninfo
        self.schema = 'preapproval_test_' + uuid4().hex
        self.base = PostgresDB(os.environ['NODE_PLANE_TEST_POSTGRES_DSN'])
        with self.base.transaction() as conn:
            conn.execute(f'CREATE SCHEMA {self.schema}')
        self.addCleanup(self.drop_schema)
        self.db = PostgresDB(make_conninfo(self.base.dsn, options=f'-c search_path={self.schema}'))
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def drop_schema(self):
        with self.base.transaction() as conn:
            conn.execute(f'DROP SCHEMA {self.schema} CASCADE')

    def test_migration_13_to_14_preserves_existing_data_and_enforces_visitor_constraints(self):
        from psycopg.errors import IntegrityError

        migrate(self.db, revisions=REVISIONS[:13])
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO backend_accounts(id, status, role) VALUES ('acc-existing', 'approved', 'member')")
            conn.execute("INSERT INTO backend_external_identities(provider, subject, account_id) VALUES ('telegram', '1001', 'acc-existing')")
            conn.execute("INSERT INTO backend_telegram_identity_details(subject, username, updated_at) VALUES ('1001', 'user1001', '2026-10-25')")
            conn.execute("INSERT INTO backend_access_requests(id, account_id, status, create_key, created_at) VALUES ('req-1', 'acc-existing', 'approved', 'key-1', '2026-10-25')")
            conn.execute("INSERT INTO backend_announcements VALUES ('ann-1', 'acc-existing', 'key-ann', 'Hello', '2026-10-25')")
            conn.execute("INSERT INTO backend_system_notifications VALUES ('ann-1', 'access_expired', 'prof-1', '2026-10-25', 'Title')")

        result = migrate(self.db, revisions=REVISIONS[:14])
        self.assertEqual(result['applied'], [14])
        self.assertTrue(check_schema(self.db, revisions=REVISIONS[:14])['ready'])

        with self.assertRaisesRegex(MigrationError, 'database_reader_incompatible'):
            check_schema(self.db, revisions=REVISIONS[:13])

        with self.db.connect() as conn:
            req_row = conn.execute("SELECT * FROM backend_access_requests WHERE id='req-1'").fetchone()
            self.assertEqual(req_row['account_id'], 'acc-existing')
            self.assertIsNone(req_row['telegram_subject'])
            self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM backend_system_notifications WHERE announcement_id='ann-1'").fetchone()['n'], 1)
            self.assertEqual(conn.execute("SELECT account_id FROM backend_external_identities WHERE subject='1001'").fetchone()['account_id'], 'acc-existing')

        with self.db.transaction() as conn:
            conn.execute("INSERT INTO backend_telegram_identity_details(subject, username, updated_at) VALUES ('2001', 'guest2001', '2026-10-25')")
            conn.execute("INSERT INTO backend_access_requests(id, account_id, telegram_subject, status, create_key, created_at) VALUES ('req-2', NULL, '2001', 'pending', 'create-k1', '2026-10-25')")

        with self.assertRaises(IntegrityError):
            with self.db.transaction() as conn:
                conn.execute("INSERT INTO backend_access_requests(id, account_id, telegram_subject, status, create_key, created_at) VALUES ('req-bad-fk', NULL, '99999', 'pending', 'create-k2', '2026-10-25')")

        with self.assertRaises(IntegrityError):
            with self.db.transaction() as conn:
                conn.execute("INSERT INTO backend_access_requests(id, account_id, telegram_subject, status, create_key, created_at) VALUES ('req-dup-key', NULL, '2001', 'rejected', 'create-k1', '2026-10-25')")

        with self.assertRaises(IntegrityError):
            with self.db.transaction() as conn:
                conn.execute("INSERT INTO backend_access_requests(id, account_id, telegram_subject, status, create_key, created_at) VALUES ('req-dup-pending', NULL, '2001', 'pending', 'create-k3', '2026-10-25')")

        with self.assertRaises(IntegrityError):
            with self.db.transaction() as conn:
                conn.execute("INSERT INTO backend_access_requests(id, account_id, telegram_subject, status, create_key, created_at) VALUES ('req-both-null', NULL, NULL, 'pending', 'create-k4', '2026-10-25')")

        with self.db.transaction() as conn:
            conn.execute("INSERT INTO backend_telegram_menu_refresh(subject, revision) VALUES ('2001', 1)")

        with self.assertRaises(IntegrityError):
            with self.db.transaction() as conn:
                conn.execute("INSERT INTO backend_telegram_menu_refresh(subject, revision) VALUES ('99999', 1)")

    def test_concurrent_same_approval_key_creates_single_account_and_profile(self):
        migrate(self.db)
        admin = bootstrap_admin(SQLIdentityRepository(self.db), 101, self.db)
        admin_actor = Actor(Principal('admin', PrincipalKind.ACCOUNT, ADMIN_PERMISSIONS, admin.id), admin)
        adapter = Principal('telegram', PrincipalKind.ADAPTER, frozenset({
            'identity.telegram.resolve', 'delegate.telegram', 'account.self.read',
            'access_requests.self.create', 'access_requests.self.read'
        }))

        vis = observe(self.db, adapter, 2002, str(uuid4()), username='guest2002')
        service = AccessRequestService(self.db)
        req = service.create(vis, str(uuid4()))
        req_id = req['id']
        self.assertIsNone(req['account_id'])

        decision_key = str(uuid4())
        barrier = threading.Barrier(2)

        def approve():
            barrier.wait(timeout=5)
            return AccessRequestService(self.db).decide(admin_actor, req_id, 'approve', decision_key)

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: approve(), range(2)))

        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0]['status'], 'approved')
        account_id = results[0]['account_id']
        self.assertIsNotNone(account_id)

        with self.db.connect() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM backend_accounts WHERE id=?", (account_id,)).fetchone()['n'], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM backend_profiles WHERE owner_account_id=?", (account_id,)).fetchone()['n'], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM backend_external_identities WHERE subject='2002' AND account_id=?", (account_id,)).fetchone()['n'], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM backend_telegram_menu_refresh WHERE subject='2002'").fetchone()['n'], 0)

    def test_concurrent_admin_provision_and_approval_guarantee_single_binding(self):
        migrate(self.db)
        admin = bootstrap_admin(SQLIdentityRepository(self.db), 101, self.db)
        admin_actor = Actor(Principal('admin', PrincipalKind.ACCOUNT, ADMIN_PERMISSIONS, admin.id), admin)
        adapter = Principal('telegram', PrincipalKind.ADAPTER, frozenset({
            'identity.telegram.resolve', 'delegate.telegram', 'account.self.read',
            'access_requests.self.create', 'access_requests.self.read'
        }))

        vis = observe(self.db, adapter, 2003, str(uuid4()), username='guest2003')
        req = AccessRequestService(self.db).create(vis, str(uuid4()))
        req_id = req['id']

        barrier = threading.Barrier(2)
        results = {}

        def run_provision():
            barrier.wait(timeout=5)
            try:
                results['provision'] = AccountService(self.db).provision_profile(admin_actor, '2003', str(uuid4()))
            except AccessDenied as exc:
                results['provision_err'] = exc.code

        def run_approval():
            barrier.wait(timeout=5)
            try:
                results['approval'] = AccessRequestService(self.db).decide(admin_actor, req_id, 'approve', str(uuid4()))
            except AccessDenied as exc:
                results['approval_err'] = exc.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(run_provision)
            f2 = pool.submit(run_approval)
            f1.result()
            f2.result()

        self.assertTrue('provision' in results or 'approval' in results)
        if 'approval_err' in results:
            self.assertEqual(results['approval_err'], 'request_already_decided')

        with self.db.connect() as conn:
            accounts = conn.execute("""SELECT a.id, a.role, a.status FROM backend_accounts a
                JOIN backend_external_identities i ON i.account_id=a.id
                WHERE i.provider='telegram' AND i.subject='2003'""").fetchall()
            self.assertEqual(len(accounts), 1)
            account_id = accounts[0]['id']
            self.assertEqual(accounts[0]['status'], 'approved')
            self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM backend_profiles WHERE owner_account_id=?", (account_id,)).fetchone()['n'], 1)
            req_row = conn.execute("SELECT status, account_id FROM backend_access_requests WHERE id=?", (req_id,)).fetchone()
            self.assertEqual(req_row['status'], 'approved')
            self.assertEqual(req_row['account_id'], account_id)

    def test_approval_failure_after_insert_rolls_back_entire_mutation(self):
        migrate(self.db)
        admin = bootstrap_admin(SQLIdentityRepository(self.db), 101, self.db)
        admin_actor = Actor(Principal('admin', PrincipalKind.ACCOUNT, ADMIN_PERMISSIONS, admin.id), admin)
        adapter = Principal('telegram', PrincipalKind.ADAPTER, frozenset({
            'identity.telegram.resolve', 'delegate.telegram', 'account.self.read',
            'access_requests.self.create', 'access_requests.self.read'
        }))

        vis = observe(self.db, adapter, 2004, str(uuid4()), username='guest2004')
        req = AccessRequestService(self.db).create(vis, str(uuid4()))
        req_id = req['id']

        with patch('backend.profiles.ProfileRepository.ensure_account_profile_in_transaction', side_effect=RuntimeError('simulated profile failure')):
            with self.assertRaises(RuntimeError):
                AccessRequestService(self.db).decide(admin_actor, req_id, 'approve', str(uuid4()))

        with self.db.connect() as conn:
            self.assertIsNone(conn.execute("SELECT account_id FROM backend_external_identities WHERE subject='2004'").fetchone())
            req_row = conn.execute("SELECT status, account_id FROM backend_access_requests WHERE id=?", (req_id,)).fetchone()
            self.assertEqual(req_row['status'], 'pending')
            self.assertIsNone(req_row['account_id'])

    def test_guest_request_rejection_creates_no_account_and_protects_menu_refresh_ack(self):
        migrate(self.db)
        admin = bootstrap_admin(SQLIdentityRepository(self.db), 101, self.db)
        admin_actor = Actor(Principal('admin', PrincipalKind.ACCOUNT, ADMIN_PERMISSIONS, admin.id), admin)
        adapter = Principal('telegram', PrincipalKind.ADAPTER, frozenset({
            'identity.telegram.resolve', 'delegate.telegram', 'account.self.read',
            'access_requests.self.create', 'access_requests.self.read'
        }))

        vis = observe(self.db, adapter, 2005, str(uuid4()), username='guest2005')
        req = AccessRequestService(self.db).create(vis, str(uuid4()))
        req_id = req['id']

        res = AccessRequestService(self.db).decide(admin_actor, req_id, 'reject', str(uuid4()))
        self.assertEqual(res['status'], 'rejected')
        self.assertIsNone(res['account_id'])

        with self.db.connect() as conn:
            self.assertIsNone(conn.execute("SELECT account_id FROM backend_external_identities WHERE subject='2005'").fetchone())
            row = conn.execute("SELECT revision FROM backend_telegram_menu_refresh WHERE subject='2005'").fetchone()
            self.assertEqual(row['revision'], 1)

        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_telegram_menu_refresh SET revision=2 WHERE subject='2005'")

        with self.db.transaction() as conn:
            conn.execute("DELETE FROM backend_telegram_menu_refresh WHERE subject='2005' AND revision=1")

        with self.db.connect() as conn:
            self.assertIsNotNone(conn.execute("SELECT * FROM backend_telegram_menu_refresh WHERE subject='2005'").fetchone())

        with self.db.transaction() as conn:
            conn.execute("DELETE FROM backend_telegram_menu_refresh WHERE subject='2005' AND revision=2")

        with self.db.connect() as conn:
            self.assertIsNone(conn.execute("SELECT * FROM backend_telegram_menu_refresh WHERE subject='2005'").fetchone())

    def test_config_backup_and_reset_compatibility_with_accountless_requests(self):
        migrate(self.db)
        admin = bootstrap_admin(SQLIdentityRepository(self.db), 101, self.db)
        admin_actor = Actor(Principal('admin', PrincipalKind.ACCOUNT, ADMIN_PERMISSIONS, admin.id), admin)
        adapter = Principal('telegram', PrincipalKind.ADAPTER, frozenset({
            'identity.telegram.resolve', 'delegate.telegram', 'account.self.read',
            'access_requests.self.create', 'access_requests.self.read'
        }))

        member = SQLIdentityRepository(self.db).resolve_telegram(3001)
        member_actor = Actor(Principal('member', PrincipalKind.ACCOUNT, frozenset({
            'account.self.read', 'access_requests.self.create', 'access_requests.self.read'
        })), member)
        legacy_req = AccessRequestService(self.db).create(member_actor, str(uuid4()))
        legacy_req_id = legacy_req['id']

        vis = observe(self.db, adapter, 2006, str(uuid4()), username='guest2006')
        req = AccessRequestService(self.db).create(vis, str(uuid4()))
        req_id = req['id']

        with self.db.transaction() as conn:
            conn.execute("INSERT INTO backend_telegram_menu_refresh(subject, revision) VALUES ('2006', 1)")

        backup_service = BackupService(self.db, self.tmp.name)
        snapshot = backup_service.create_snapshot()
        detail = backup_service.detail(admin_actor, snapshot['backup_id'])
        self.assertTrue(detail['compatible'])

        job = backup_service.queue(admin_actor, str(uuid4()), 'restore', snapshot['backup_id'], detail['checksum'])
        self.assertTrue(backup_service.run_one())
        self.assertTrue(backup_service.run_one())
        self.assertEqual(backup_service.get(admin_actor, job['id'])['status'], 'succeeded')

        with self.db.connect() as conn:
            req_row = conn.execute("SELECT * FROM backend_access_requests WHERE id=?", (req_id,)).fetchone()
            self.assertEqual(req_row['status'], 'pending')
            self.assertIsNone(req_row['account_id'])
            self.assertEqual(req_row['telegram_subject'], '2006')
            self.assertEqual(conn.execute("SELECT COUNT(*) AS n FROM backend_telegram_menu_refresh").fetchone()['n'], 0)

        stored = backup_service._load(snapshot['backup_id'])
        old_tables = dict(stored['tables'])
        old_req_data = dict(old_tables['backend_access_requests'])
        cols_without_subj = [c for c in old_req_data['columns'] if c != 'telegram_subject']
        rows_without_subj = [
            {k: v for k, v in r.items() if k != 'telegram_subject'}
            for r in old_req_data['rows']
            if r.get('account_id') is not None
        ]
        rows_without_subj.sort(key=lambda row: encoded(row))
        old_tables['backend_access_requests'] = {'columns': cols_without_subj, 'rows': rows_without_subj}
        old_checksum = hashlib.sha256(encoded(old_tables)).hexdigest()
        old_payload = {
            **stored,
            'checksum': old_checksum,
            'tables': old_tables,
        }

        old_id = str(uuid4())
        path = backup_service._path(old_id)
        path.write_bytes(encoded(old_payload))

        old_detail = backup_service.detail(admin_actor, old_id)
        self.assertTrue(old_detail['compatible'])
        self.assertEqual(old_detail['checksum'], old_checksum)

        job_old = backup_service.queue(admin_actor, str(uuid4()), 'restore', old_id, old_detail['checksum'])
        self.assertTrue(backup_service.run_one())
        self.assertTrue(backup_service.run_one())
        self.assertEqual(backup_service.get(admin_actor, job_old['id'])['status'], 'succeeded')

        with self.db.connect() as conn:
            req_row_old = conn.execute("SELECT * FROM backend_access_requests WHERE id=?", (legacy_req_id,)).fetchone()
            self.assertEqual(req_row_old['status'], 'pending')
            self.assertEqual(req_row_old['account_id'], member.id)
            self.assertIsNone(req_row_old['telegram_subject'])
            self.assertIsNone(conn.execute("SELECT * FROM backend_access_requests WHERE id=?", (req_id,)).fetchone())
