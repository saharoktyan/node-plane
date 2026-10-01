"""Read-only Xray/AWG snapshots with realistic CLI outputs."""

import base64
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests import test_agent_node_operations as operations

SPEC = importlib.util.spec_from_file_location(
    "traffic_snapshot",
    Path(__file__).resolve().parents[1] / "runtime_assets/traffic-snapshot.py",
)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class AgentTrafficTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = {
            "XRAY_CONFIG": str(self.root / "config.json"),
            "AWG_CONFIG": str(self.root / "wg0.conf"),
            "XRAY_CONTAINER_NAME": "xray",
            "AWG_CONTAINER_NAME": "amnezia-awg",
            "SERVER_KEY": "n1",
            "AWG_IFACE": "wg0",
            "XRAY_INBOUND_TCP_TAG": "reality-tcp",
            "XRAY_INBOUND_XHTTP_TAG": "reality-xhttp",
        }
        self.uuid = "00000000-0000-4000-8000-000000000001"
        self.intent = {
            "node_key": "n1",
            "runtime_name": "alice",
            "protocol": "xray",
            "identity": self.uuid,
        }
        self.config = {
            "stats": {},
            "api": {"services": ["StatsService"]},
            "policy": {
                "levels": {"0": {"statsUserUplink": True, "statsUserDownlink": True}}
            },
            "inbounds": [
                {
                    "tag": tag,
                    "settings": {"clients": [{"email": "alice", "id": self.uuid}]},
                }
                for tag in ("reality-tcp", "reality-xhttp")
            ],
        }
        Path(self.env["XRAY_CONFIG"]).write_text(json.dumps(self.config))

    def test_xray_filtered_nonresetting_query_and_decimal_int64_response(self):
        raw = json.dumps(
            {
                "stat": [
                    {"name": "user>>>alice>>>traffic>>>uplink", "value": "123"},
                    {"name": "user>>>alice>>>traffic>>>downlink", "value": "456"},
                ]
            }
        )
        with patch.object(MODULE, "command", return_value=raw) as command:
            self.assertEqual(
                MODULE.xray(self.env, "alice", self.uuid),
                {"uplink": 123, "downlink": 456},
            )
        argv = command.call_args.args[0]
        self.assertIn("-reset=false", argv)
        self.assertIn("-pattern=user>>>alice>>>", argv)

    def test_xray_empty_valid_response_is_zero_and_unexpected_identity_is_rejected(
        self,
    ):
        with patch.object(MODULE, "command", return_value="{}"):
            self.assertEqual(
                MODULE.xray(self.env, "alice", self.uuid), {"uplink": 0, "downlink": 0}
            )
        for raw in (
            {"stat": [{"name": "user>>>bob>>>traffic>>>uplink", "value": 1}]},
            {"stat": [{"name": "user>>>alice>>>traffic>>>uplink", "value": True}]},
            {"stat": [{"name": "user>>>alice>>>traffic>>>uplink", "value": 1}] * 2},
            {"error": "unavailable"},
        ):
            with (
                patch.object(MODULE, "command", return_value=json.dumps(raw)),
                self.assertRaises(ValueError),
            ):
                MODULE.xray(self.env, "alice", self.uuid)

    def test_disabled_statistics_and_wrong_disk_uuid_are_unknown_without_query(self):
        for mutate in (
            lambda c: c["policy"]["levels"]["0"].update(statsUserUplink=False),
            lambda c: c["inbounds"][0]["settings"]["clients"][0].update(id="wrong"),
        ):
            mutate(self.config)
            Path(self.env["XRAY_CONFIG"]).write_text(json.dumps(self.config))
            with (
                patch.object(MODULE, "command") as command,
                self.assertRaises(ValueError),
            ):
                MODULE.xray(self.env, "alice", self.uuid)
            command.assert_not_called()

    def test_awg_maps_server_rx_to_upload_and_filters_other_peers(self):
        key = base64.b64encode(b"k" * 32).decode()
        Path(self.env["AWG_CONFIG"]).write_text(
            "[Interface]\nPrivateKey = secret\n\n# n1-alice\n[Peer]\nPublicKey = "
            + key
            + "\nAllowedIPs = 10.0.0.2/32\n"
        )
        with patch.object(
            MODULE, "command", return_value="other-key\t999\t999\n" + key + "\t150\t300"
        ) as command:
            self.assertEqual(
                MODULE.awg(self.env, "alice", key), {"uplink": 150, "downlink": 300}
            )
            self.assertEqual(
                command.call_args.args[0][-4:], ["wg", "show", "wg0", "transfer"]
            )
        with (
            patch.object(MODULE, "command", return_value=""),
            self.assertRaises(ValueError),
        ):
            MODULE.awg(self.env, "alice", key)
        with self.assertRaises(ValueError):
            MODULE.awg(self.env, "alice", "wrong-key")

    def test_restart_during_observation_is_unknown(self):
        with (
            patch.object(MODULE, "environment", return_value=self.env),
            patch.object(MODULE, "container_epoch", side_effect=["a" * 64, "b" * 64]),
            patch.object(MODULE, "xray", return_value={"uplink": 1, "downlink": 2}),
            self.assertRaises(ValueError),
        ):
            MODULE.read(self.intent)

    def test_snapshot_returns_only_expected_identity_and_counters(self):
        with (
            patch.object(MODULE, "environment", return_value=self.env),
            patch.object(MODULE, "container_epoch", return_value="a" * 64),
            patch.object(MODULE, "xray", return_value={"uplink": 1, "downlink": 2}),
        ):
            self.assertEqual(
                MODULE.read(self.intent),
                {
                    **self.intent,
                    "epoch": "a" * 64,
                    "uplink_bytes": 1,
                    "downlink_bytes": 2,
                },
            )
        for changes in (
            {"runtime_name": "../alice"},
            {"protocol": "unknown"},
            {"identity": "not-uuid"},
            {"node_key": "../wrong"},
            {"extra": "arbitrary arguments"},
        ):
            with self.assertRaises((ValueError, TypeError)):
                MODULE.read({**self.intent, **changes})

    def test_readonly_dispatch_uses_existing_shared_lock_without_creating_journal(self):
        (self.root / "traffic-snapshot.py").write_text(
            'def read(intent):\n    return {"observed": True}\n'
        )
        journal = self.root / "journal.sqlite3"
        Path(str(journal) + ".lock").write_text("")
        with patch.object(operations.MODULE, "ROOT", self.root):
            self.assertEqual(
                operations.MODULE.execute("traffic", "", self.intent, path=journal),
                {"observed": True},
            )
            with self.assertRaises(ValueError):
                operations.MODULE.execute(
                    "traffic", "", self.intent, recover=True, path=journal
                )
            Path(str(journal) + ".disabled").touch()
            with self.assertRaises(ValueError):
                operations.MODULE.execute("traffic", "", self.intent, path=journal)
        self.assertFalse(journal.exists())

    def test_counter_bounds_exclude_booleans_negative_and_overflow(self):
        for value in (True, -1, 2**63, "1.5", "-1", None):
            with self.assertRaises(ValueError):
                MODULE.counter(value)
