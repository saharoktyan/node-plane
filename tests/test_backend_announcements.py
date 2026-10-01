"""Announcement policy, durable delivery and HTTP authorization regression tests."""

import unittest
from uuid import uuid4

from backend.announcements import AnnouncementService
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
