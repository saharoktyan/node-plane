from __future__ import annotations

import importlib
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch


TESTS_DIR = os.path.dirname(__file__)
REPO_ROOT = os.path.abspath(os.path.join(TESTS_DIR, ".."))
APP_ROOT = os.path.join(REPO_ROOT, "app")
if APP_ROOT not in sys.path:
    sys.path.insert(0, APP_ROOT)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)


class AlertsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        base = self.tmpdir.name
        os.environ["NODE_PLANE_BASE_DIR"] = base
        os.environ["NODE_PLANE_APP_DIR"] = base
        os.environ["NODE_PLANE_SHARED_DIR"] = base
        os.environ["SQLITE_DB_PATH"] = os.path.join(base, "bot.sqlite3")
        os.environ["ADMIN_IDS"] = "123"

        import config
        import services.app_settings as app_settings
        import services.alerts as alerts

        self.config = importlib.reload(config)
        self.app_settings = importlib.reload(app_settings)
        self.alerts = importlib.reload(alerts)

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def test_alert_activates_and_resolves_after_single_confirm_cycle(self) -> None:
        bot = SimpleNamespace(messages=[])

        def send_message(**kwargs):
            bot.messages.append(kwargs)

        bot.send_message = send_message
        record = self.alerts.AlertRecord(
            alert_key="server:spb1:disk_low",
            server_key="spb1",
            alert_type="disk_low",
            severity="warning",
            payload={"server_name": "spb1", "free_percent": 12},
        )

        self.alerts._apply_scan([record], bot=bot)
        self.assertEqual(self.alerts.count_active_alerts(), 1)
        self.assertEqual(len(bot.messages), 1)

        self.alerts._apply_scan([], bot=bot)
        self.assertEqual(self.alerts.count_active_alerts(), 0)
        self.assertEqual(len(bot.messages), 2)

    def test_alert_monitor_job_skips_when_disabled(self) -> None:
        self.app_settings.set_alerts_enabled(False)
        with patch.object(self.alerts, "_collect_alerts") as collect:
            self.alerts.alert_monitor_job()
        collect.assert_not_called()
