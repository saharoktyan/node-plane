"""Announcement policy, durable delivery and HTTP authorization regression tests."""

import unittest
from unittest.mock import patch
from uuid import uuid4

from backend.announcements import AnnouncementService
from backend.profiles import ProfileRepository
from backend.profile_commands import ProfileCommands
from backend.authorization import (
    ADMIN_PERMISSIONS,
    AccessDenied,
    Actor,
    Principal,
    PrincipalKind,
)

from tests import test_backend_http as fixture


class AnnouncementTests(unittest.TestCase):
    def setUp(self):
        fixture.BackendHTTPTests.setUp(self)
        self.actor = Actor(
            Principal("test", PrincipalKind.SERVICE, ADMIN_PERMISSIONS), self.admin
        )
        self.transport = self.credentials.authenticate("Bearer " + self.token)
        self.service = AnnouncementService(self.db)
        self.member = fixture.BackendHTTPTests.register(self, 102).json()
        self.db.connection.execute(
            "UPDATE backend_accounts SET status='approved' WHERE id=?",
            (self.member["id"],),
        )
        self.db.connection.commit()
        self.profile_id = ProfileRepository(self.db).ensure_account_profile(self.member['id'])

    def test_approved_orphan_account_is_not_a_recipient(self):
        orphan = fixture.BackendHTTPTests.register(self, 103).json()
        self.db.connection.execute('DELETE FROM backend_profiles WHERE owner_account_id=?', (orphan['id'],))
        self.db.connection.execute("UPDATE backend_accounts SET status='approved' WHERE id=?", (orphan['id'],))
        self.db.connection.commit()
        self.assertEqual(self.service.preview(self.actor, 'Hello')['recipients'], 1)
        self.assertEqual(self.service.queue(self.actor, 'Hello', str(uuid4()))['total'], 1)

    def test_deleted_profile_is_excluded_from_preview_and_queue_even_if_account_stays_approved(self):
        ProfileCommands(self.db).execute(self.actor, str(uuid4()), action='delete',
            profile_id=self.profile_id, revision=1)
        # Covers old installations retaining approval, and admin accounts whose
        # approval is intentionally preserved by profile deletion.
        self.db.connection.execute("UPDATE backend_accounts SET status='approved' WHERE id=?", (self.member['id'],))
        self.db.connection.commit()
        self.assertEqual(self.service.preview(self.actor, 'Hello')['recipients'], 0)
        self.assertEqual(self.service.queue(self.actor, 'Hello', str(uuid4()))['total'], 0)

    def test_profile_deleted_after_queue_is_skipped_before_delivery(self):
        job = self.service.queue(self.actor, 'Hello', str(uuid4()))
        ProfileCommands(self.db).execute(self.actor, str(uuid4()), action='delete',
            profile_id=self.profile_id, revision=1)
        self.db.connection.execute("UPDATE backend_accounts SET status='approved' WHERE id=?", (self.member['id'],))
        self.db.connection.commit()
        self.assertIsNone(self.service.claim(self.transport, str(uuid4())))
        self.assertEqual(self.service.get(self.actor, job['id'])['counts']['skipped'], 1)

    def test_deleting_account_revokes_recipient_even_with_legacy_extra_profile(self):
        ProfileRepository(self.db).create_profile(runtime_name='second_profile',
            display_name='Second profile', owner_account_id=self.member['id'])
        ProfileCommands(self.db).execute(self.actor, str(uuid4()), action='delete',
            profile_id=self.profile_id, revision=1)
        self.assertEqual(self.service.preview(self.actor, 'Hello')['recipients'], 0)
        job = self.service.queue(self.actor, 'Hello', str(uuid4()))
        self.assertEqual(job['total'], 0)
        self.assertIsNone(self.service.claim(self.transport, str(uuid4())))

    def headers_for(self, user_id):
        return {**self.headers, "X-Node-Plane-Telegram-User-ID": str(user_id)}

    def test_approved_recipients_exclude_sender_pending_and_nontelegram_accounts(self):
        fixture.BackendHTTPTests.register(self, 103)
        self.identities.create_account()
        preview = self.service.preview(self.actor, " Hello ")
        self.assertEqual(preview, {"text": "Hello", "recipients": 1})
        job = self.service.queue(self.actor, "Hello", str(uuid4()))
        self.assertEqual(job["total"], 1)
        self.assertEqual(job["counts"]["queued"], 1)

    def test_delivery_exposes_sender_only_when_telegram_username_is_known(self):
        self.service.queue(self.actor, "First", str(uuid4()))
        delivery = self.service.claim(self.transport, str(uuid4()))
        self.assertIsNone(delivery['sender_username'])
        fixture.BackendHTTPTests.register(self, 101, username='sender')
        self.service.queue(self.actor, "Second", str(uuid4()))
        delivery = self.service.claim(self.transport, str(uuid4()))
        self.assertEqual(delivery['sender_username'], 'sender')

    def test_double_tap_and_changed_payload_reuse(self):
        key = str(uuid4())
        first = self.service.queue(self.actor, "Hello", key)
        self.assertEqual(
            self.service.queue(self.actor, "Hello", key)["id"], first["id"]
        )
        with self.assertRaises(AccessDenied) as error:
            self.service.queue(self.actor, "Changed", key)
        self.assertEqual(error.exception.code, "idempotency_conflict")
        self.assertEqual(
            self.db.connection.execute(
                "SELECT COUNT(*) FROM backend_announcement_deliveries"
            ).fetchone()[0],
            1,
        )

    def test_claim_ack_idempotency_and_completed_claim_is_not_replayed(self):
        job = self.service.queue(self.actor, "Hello", str(uuid4()))
        key = str(uuid4())
        delivery = self.service.claim(self.transport, key)
        self.assertEqual(delivery, self.service.claim(self.transport, key))
        self.assertIsNone(self.service.claim(self.transport, str(uuid4())))
        self.service.acknowledge(self.transport, delivery["id"], key, "sent")
        self.service.acknowledge(self.transport, delivery["id"], key, "sent")
        self.assertIsNone(self.service.claim(self.transport, key))
        self.assertEqual(self.service.get(self.actor, job["id"])["counts"]["sent"], 1)

    def test_definite_rate_limit_defers_claim_and_survives_adapter_restart(self):
        self.service.queue(self.actor, 'Hello', str(uuid4()))
        key = str(uuid4())
        delivery = self.service.claim(self.transport, key)
        self.service.acknowledge(self.transport, delivery['id'], key, 'retry', 30)
        first_until = self.db.connection.execute("SELECT value FROM backend_system_settings WHERE key=?",
            ('telegram_retry_until:' + self.transport.id,)).fetchone()[0]
        self.service.acknowledge(self.transport, delivery['id'], key, 'retry', 30)
        self.assertEqual(self.db.connection.execute("SELECT value FROM backend_system_settings WHERE key=?",
            ('telegram_retry_until:' + self.transport.id,)).fetchone()[0], first_until)
        restarted = AnnouncementService(self.db)
        self.assertIsNone(restarted.claim(self.transport, str(uuid4())))
        hint = self.client.get('/api/v1/integrations/telegram/deliveries/wait?timeout=1', headers=self.headers)
        self.assertEqual(hint.status_code, 200, hint.text)
        self.assertFalse(hint.json()['ready'])
        self.assertGreater(hint.json()['retry_after'], 0)
        self.db.connection.execute("UPDATE backend_system_settings SET value='2020-01-01T00:00:00+00:00' WHERE key=?",
            ('telegram_retry_until:' + self.transport.id,))
        self.db.connection.commit()
        next_key = str(uuid4())
        claimed = restarted.claim(self.transport, next_key)
        self.assertEqual(claimed['id'], delivery['id'])
        restarted.acknowledge(self.transport, claimed['id'], next_key, 'sent')
        self.assertIsNone(restarted.claim(self.transport, str(uuid4())))

    def test_retry_delay_is_required_and_unknown_delivery_cannot_be_requeued(self):
        self.service.queue(self.actor, 'Hello', str(uuid4()))
        key = str(uuid4())
        delivery = self.service.claim(self.transport, key)
        for delay in (None, 0, True, -1):
            with self.assertRaises(AccessDenied):
                self.service.acknowledge(self.transport, delivery['id'], key, 'retry', delay)
        self.service.acknowledge(self.transport, delivery['id'], key, 'unknown')
        with self.assertRaises(AccessDenied):
            self.service.acknowledge(self.transport, delivery['id'], key, 'retry', 30)

    def test_interrupted_delivery_expires_unknown_without_replay(self):
        job = self.service.queue(self.actor, "Hello", str(uuid4()))
        delivery = self.service.claim(self.transport, str(uuid4()))
        self.db.connection.execute(
            "UPDATE backend_announcement_deliveries SET claimed_until='2000-01-01T00:00:00+00:00'"
        )
        self.db.connection.commit()
        self.assertIsNone(self.service.claim(self.transport, str(uuid4())))
        self.assertEqual(
            self.service.get(self.actor, job["id"])["counts"]["unknown"], 1
        )
        self.assertIsNotNone(delivery)

    def test_revocation_before_delivery_skips_recipient(self):
        job = self.service.queue(self.actor, "Hello", str(uuid4()))
        self.db.connection.execute(
            "UPDATE backend_accounts SET status='disabled' WHERE id=?",
            (self.member["id"],),
        )
        self.db.connection.commit()
        self.assertIsNone(self.service.claim(self.transport, str(uuid4())))
        self.assertEqual(
            self.service.get(self.actor, job["id"])["counts"]["skipped"], 1
        )

    def test_member_sound_preference_is_backend_owned_and_used_at_delivery(self):
        response = self.client.patch(
            "/api/v1/me/preferences",
            headers=self.headers_for(102),
            json={"announcement_silent": True},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["announcement_silent"])
        self.assertFalse(
            self.client.get("/api/v1/me", headers=self.headers_for(101)).json()[
                "announcement_silent"
            ]
        )
        self.service.queue(self.actor, "Hello", str(uuid4()))
        self.assertTrue(self.service.claim(self.transport, str(uuid4()))["silent"])
        for body in (
            {},
            {"announcement_silent": "yes"},
            {"locale": None},
            {"account_id": self.admin.id},
        ):
            self.assertEqual(
                self.client.patch(
                    "/api/v1/me/preferences", headers=self.headers_for(102), json=body
                ).status_code,
                422,
            )

    def test_member_cannot_compose_and_only_trusted_adapter_claims(self):
        result = self.client.post(
            "/api/v1/announcements/preview",
            headers=self.headers_for(102),
            json={"text": "Hello"},
        )
        self.assertEqual(result.status_code, 403)
        for principal in (
            self.actor.principal,
            Principal("adapter", PrincipalKind.ADAPTER, frozenset()),
        ):
            with self.assertRaises(AccessDenied):
                self.service.claim(principal, str(uuid4()))
        result = self.client.post(
            "/api/v1/integrations/telegram/announcements/claim",
            headers=self.headers_for(101),
        )
        self.assertEqual(result.status_code, 422)

    def test_http_create_status_and_claim(self):
        result = self.client.post(
            "/api/v1/announcements",
            headers={**self.headers_for(101), "Idempotency-Key": str(uuid4())},
            json={"text": "Hello"},
        )
        self.assertEqual(result.status_code, 202, result.text)
        job_id = result.json()["id"]
        self.assertEqual(
            self.client.get(
                "/api/v1/announcements", headers=self.headers_for(101)
            ).json()["last_job"]["id"],
            job_id,
        )
        self.assertEqual(
            self.client.get(
                f"/api/v1/announcements/{job_id}", headers=self.headers_for(101)
            ).status_code,
            200,
        )
        key = str(uuid4())
        delivery = self.client.post(
            "/api/v1/integrations/telegram/announcements/claim",
            headers={**self.headers, "Idempotency-Key": key},
        ).json()["delivery"]
        response = self.client.post(
            f"/api/v1/integrations/telegram/announcements/{delivery['id']}/ack",
            headers={**self.headers, "Idempotency-Key": key},
            json={"status": "sent"},
        )
        self.assertEqual(response.status_code, 200, response.text)

    def test_wait_requires_adapter_and_does_not_claim_pending_delivery(self):
        endpoint = '/api/v1/integrations/telegram/deliveries/wait?timeout=1'
        job = self.service.queue(self.actor, 'Hello', str(uuid4()))
        result = self.client.get(endpoint, headers=self.headers)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertTrue(result.json()['ready'])
        self.assertEqual(self.service.get(self.actor, job['id'])['counts']['queued'], 1)
        self.assertEqual(self.client.get(endpoint, headers=self.headers_for(101)).status_code, 422)
        self.assertEqual(self.client.get(endpoint).status_code, 401)
        _, account_token = self.credentials.issue(PrincipalKind.ACCOUNT, ADMIN_PERMISSIONS,
                                                  account_id=self.admin.id)
        self.assertEqual(self.client.get(endpoint, headers={
            'Authorization': 'Bearer ' + account_token}).status_code, 403)
        self.credentials.revoke(self.transport.id)
        self.assertEqual(self.client.get(endpoint, headers=self.headers).status_code, 401)

    def test_empty_wait_does_not_mutate_guard_or_claim_deliveries(self):
        revision = self.db.connection.execute('SELECT revision FROM backend_account_guard WHERE id=1').fetchone()[0]
        with patch('time.sleep') as sleep:
            response = self.client.get('/api/v1/integrations/telegram/deliveries/wait',
                                       headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse(response.json()['ready'])
        sleep.assert_called_once_with(30)
        self.assertEqual(self.db.connection.execute(
            'SELECT revision FROM backend_account_guard WHERE id=1').fetchone()[0], revision)
