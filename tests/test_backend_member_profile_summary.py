from __future__ import annotations

import json
import unittest
from uuid import uuid4

from backend.authorization import AccessDenied, Principal, PrincipalKind, resolve_actor
from backend.config_issuance import ConfigIssuanceService
from backend.identity_repository import SQLIdentityRepository
from backend.profiles import ProfileRepository, ProfileService
from tests.test_backend_identity import Database


class MemberProfileSummaryTests(unittest.TestCase):
    def test_existing_profile_table_gains_optional_creation_date(self):
        db = Database()
        self.addCleanup(db.connection.close)
        SQLIdentityRepository(db).initialize_schema()
        db.connection.execute('''CREATE TABLE backend_profiles (
            id TEXT PRIMARY KEY, runtime_name TEXT NOT NULL UNIQUE,
            display_name TEXT NOT NULL, owner_account_id TEXT,
            frozen INTEGER NOT NULL DEFAULT 0, expires_at TEXT,
            desired_revision INTEGER NOT NULL DEFAULT 1)''')
        db.connection.execute('''INSERT INTO backend_profiles
            (id, runtime_name, display_name) VALUES ('old', 'old', 'Old')''')
        ProfileRepository(db).initialize_schema()
        row = db.connection.execute('SELECT created_at FROM backend_profiles WHERE id = ?',
                                    ('old',)).fetchone()
        self.assertIsNone(row['created_at'])

    def setUp(self):
        self.db = Database()
        self.addCleanup(self.db.connection.close)
        self.identities = SQLIdentityRepository(self.db)
        self.identities.initialize_schema()
        self.repo = ProfileRepository(self.db)
        self.repo.initialize_schema()
        ConfigIssuanceService(self.db).initialize_schema()
        account = self.identities.create_account()
        other = self.identities.create_account()
        self.principal = Principal('member', PrincipalKind.ACCOUNT,
            frozenset({'profiles.self.read'}), account.id)
        self.actor = resolve_actor(self.principal, self.identities)
        self.profile_id = self.repo.create_profile(runtime_name='member',
            display_name='Member', owner_account_id=account.id)
        self.other_id = self.repo.create_profile(runtime_name='other',
            display_name='Other', owner_account_id=other.id)

    def test_summary_counts_grants_and_successful_issuances(self):
        with self.db.transaction() as conn:
            conn.execute('''INSERT INTO backend_nodes(key, title, region,
                protocols_json, xray_transports_json)
                VALUES (?, ?, ?, ?, ?)''',
                ('lv1', 'Latvia', 'Europe', json.dumps(['awg', 'xray']),
                 json.dumps(['tcp', 'xhttp'])))
            for protocol in ('awg', 'xray'):
                conn.execute('''INSERT INTO backend_grants(profile_id, node_key, protocol)
                    VALUES (?, ?, ?)''', (self.profile_id, 'lv1', protocol))
            for status in ('succeeded', 'blocked'):
                conn.execute('''INSERT INTO backend_config_issuances
                    (id, actor_account_id, command_key, profile_id, node_key,
                     protocol, transport, profile_revision, node_revision,
                     status, created_at, expires_at)
                    VALUES (?, ?, ?, ?, ?, 'xray', 'tcp', 1, 1, ?, ?, ?)''',
                    (str(uuid4()), self.actor.account.id, str(uuid4()),
                     self.profile_id, 'lv1', status,
                     '2026-09-29T10:00:00+00:00', '2026-09-29T10:15:00+00:00'))
        summary = ProfileService(self.repo).own_summary(self.actor, self.profile_id)
        self.assertEqual((summary['node_count'], summary['protocol_count'],
                          summary['awg_count'], summary['xray_count']), (1, 2, 1, 1))
        self.assertEqual(summary['issued_count'], 1)
        self.assertEqual(summary['last_issued_at'], '2026-09-29T10:00:00+00:00')
        self.assertIsNotNone(summary['created_at'])
        self.assertFalse(summary['expired'])
        self.assertEqual(summary['nodes'][0]['protocols'], ['awg', 'xray'])
        self.db.connection.execute('''UPDATE backend_profiles SET expires_at = ?
            WHERE id = ?''', ('2020-01-01T00:00:00+00:00', self.profile_id))
        self.assertTrue(ProfileService(self.repo).own_summary(
            self.actor, self.profile_id)['expired'])
        with self.assertRaises(AccessDenied):
            ProfileService(self.repo).own_summary(self.actor, self.other_id)
