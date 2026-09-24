from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.postgres_test_harness import configure_postgres_test_env

APP_ROOT = str(Path(__file__).resolve().parents[1] / "app")
if APP_ROOT not in sys.path:
    sys.path.insert(0, APP_ROOT)
configure_postgres_test_env(tempfile.mkdtemp(prefix="node-plane-agent-rollout-"))

from services import agent_rollout


class AgentRolloutTests(unittest.TestCase):
    def test_rollout_targets_only_requested_ssh_node(self) -> None:
        server = SimpleNamespace(transport="ssh")
        proc = SimpleNamespace(returncode=0, stdout="ready", stderr="")
        with patch.object(agent_rollout, "NODE_DRIVER_BACKEND", "inprocess"), patch.object(
            agent_rollout, "get_server", return_value=server
        ), patch.object(
            agent_rollout.os.path, "isfile", return_value=True
        ), patch.object(
            agent_rollout.subprocess, "run", return_value=proc
        ) as run:
            code, output = agent_rollout.ensure_driver_agent_rollout_for_ssh("spb1")
        self.assertEqual(code, 0)
        self.assertIn("ready", output)
        self.assertEqual(run.call_args.args[0][-2:], ["--node-key", "spb1"])
        self.assertEqual(run.call_args.kwargs["env"]["NODE_PLANE_BIN_SOURCE"], "auto")

    def test_grpc_bootstrap_does_not_repeat_agent_rollout(self) -> None:
        server = SimpleNamespace(transport="ssh")
        with patch.object(agent_rollout, "NODE_DRIVER_BACKEND", "grpc"), patch.object(
            agent_rollout, "get_server", return_value=server
        ), patch.object(agent_rollout.subprocess, "run") as run:
            code, output = agent_rollout.ensure_driver_agent_rollout_for_ssh("spb1", skip_if_connected=True)
        self.assertEqual((code, output), (0, ""))
        run.assert_not_called()

    def test_manual_grpc_rollout_still_runs(self) -> None:
        server = SimpleNamespace(transport="ssh")
        proc = SimpleNamespace(returncode=0, stdout="ready", stderr="")
        with patch.object(agent_rollout, "NODE_DRIVER_BACKEND", "grpc"), patch.object(
            agent_rollout, "get_server", return_value=server
        ), patch.object(
            agent_rollout.os.path, "isfile", return_value=True
        ), patch.object(
            agent_rollout.subprocess, "run", return_value=proc
        ) as run:
            code, output = agent_rollout.ensure_driver_agent_rollout_for_ssh("spb1")
        self.assertEqual((code, output), (0, "ready"))
        run.assert_called_once()

    def test_rust_install_requires_explicit_bot_confirmation(self) -> None:
        server = SimpleNamespace(transport="ssh")
        proc = SimpleNamespace(returncode=1, stdout="", stderr="RUST_INSTALL_REQUIRED")
        with patch.object(agent_rollout, "get_server", return_value=server), patch.object(
            agent_rollout.os.path, "isfile", return_value=True
        ), patch.object(agent_rollout.subprocess, "run", return_value=proc) as run:
            code, output = agent_rollout.ensure_driver_agent_rollout_for_ssh("spb1")
            self.assertEqual((code, output), (1, "RUST_INSTALL_REQUIRED"))
            self.assertEqual(run.call_args.kwargs["env"]["NODE_PLANE_INSTALL_RUST"], "no")
            agent_rollout.ensure_driver_agent_rollout_for_ssh("spb1", install_rust=True)
            self.assertEqual(run.call_args.kwargs["env"]["NODE_PLANE_INSTALL_RUST"], "yes")


if __name__ == "__main__":
    unittest.main()
