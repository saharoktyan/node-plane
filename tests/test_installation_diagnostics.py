import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch


spec = importlib.util.spec_from_file_location('installation_diagnostics',
    Path(__file__).resolve().parents[1] / 'scripts/installation_diagnostics.py')
diagnostics = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diagnostics)


class InstallationDiagnosticsTests(unittest.TestCase):
    def database_case(self, maintenance=False, state='failed', guard=True):
        conn = MagicMock()
        def execute(sql):
            cursor = MagicMock()
            cursor.fetchone.return_value = (
                (1,) if 'FOR UPDATE' in sql and guard else
                (1,) if 'controller_update_gate' in sql and maintenance else None)
            return cursor
        conn.execute.side_effect = execute
        connection = MagicMock()
        connection.__enter__.return_value = conn
        driver = SimpleNamespace(connect=MagicMock(return_value=connection))
        return conn, driver, {'LoadState': 'loaded', 'ActiveState': state}

    def repair(self, maintenance=False, state='failed', guard=True):
        conn, driver, unit = self.database_case(maintenance, state, guard)
        with patch.dict(sys.modules, {'config': SimpleNamespace(POSTGRES_DSN='secret'), 'psycopg': driver}), \
             patch.object(diagnostics, 'unit_state', return_value=unit), \
             patch.object(diagnostics, 'command', return_value=(0, '')) as command:
            result = diagnostics.database('node-plane-backend.service')
        return result, command, conn

    def test_recovery_refuses_maintenance_running_services_and_missing_guard(self):
        for options in ({'maintenance': True}, {'state': 'active'}, {'guard': False}):
            with self.subTest(options=options):
                result, command, _ = self.repair(**options)
                self.assertEqual(result['status'], 'refused')
                command.assert_not_called()

    def test_recovery_starts_only_fixed_failed_unit_under_guard(self):
        result, command, conn = self.repair()
        self.assertEqual(result['status'], 'dispatched')
        command.assert_called_once_with(['systemctl', 'start', 'node-plane-backend.service'], timeout=30)
        sql = [call.args[0] for call in conn.execute.call_args_list]
        self.assertTrue(any('FOR UPDATE' in statement for statement in sql))
        self.assertFalse(any(statement.startswith(('DELETE', 'UPDATE', 'INSERT')) for statement in sql))

    def test_command_timeout_does_not_leak_output_or_credentials(self):
        import subprocess
        with patch.object(diagnostics.subprocess, 'run', side_effect=subprocess.TimeoutExpired('secret', 5)):
            self.assertEqual(diagnostics.command(['systemctl', 'show']), (-1, ''))

    def test_observations_do_not_propose_repair_when_database_is_unavailable(self):
        with patch.object(diagnostics, 'installed_database', return_value={'status': 'unavailable'}), \
             patch.object(diagnostics, 'unit_state', return_value={'LoadState': 'loaded', 'ActiveState': 'failed'}), \
             patch.object(diagnostics.urllib.request, 'urlopen', side_effect=OSError):
            report = diagnostics.observe()
        self.assertGreater(report['errors'], 0)
        self.assertFalse(any('repair' in check for check in report['checks']))
        self.assertTrue(any(check['id'] == 'worker' for check in report['checks']))

    def test_recovery_rechecks_after_diagnosis_and_rejects_unknown_units(self):
        conn, driver, state = self.database_case()
        with patch.dict(sys.modules, {'config': SimpleNamespace(POSTGRES_DSN='secret'), 'psycopg': driver}), \
             patch.object(diagnostics, 'unit_state', return_value=state), \
             patch.object(diagnostics, 'command') as command:
            result = diagnostics.database('postgresql.service')
        self.assertEqual(result['status'], 'refused')
        command.assert_not_called()


if __name__ == '__main__':
    unittest.main()
