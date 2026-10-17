import importlib.util
from pathlib import Path
import sys
import tempfile
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

    def test_controller_cpu_requires_current_boot_fresh_sample_and_a_full_window(self):
        from datetime import datetime, timedelta, timezone
        sample = {'boot_id':'boot','at':datetime.now(timezone.utc).isoformat(),
                  'host_metrics':{'cpu_used_percent':96,'cpu_sample_window_seconds':60,'cpu_sample_age_seconds':0}}
        with patch.object(diagnostics.Path,'read_text',return_value='boot'):
            self.assertEqual(diagnostics.controller_cpu(sample),96)
            for key,value in [('cpu_used_percent',True),('cpu_sample_window_seconds',5),('cpu_sample_age_seconds',20),('cpu_sample_age_seconds',True)]:
                changed = {**sample,'host_metrics':{**sample['host_metrics'],key:value}}
                self.assertIsNone(diagnostics.controller_cpu(changed))
            self.assertIsNone(diagnostics.controller_cpu({**sample,'at':(datetime.now(timezone.utc)-timedelta(minutes=2)).isoformat()}))
            self.assertIsNone(diagnostics.controller_cpu({**sample,'boot_id':'other-boot'}))

    def test_command_timeout_does_not_leak_output_or_credentials(self):
        import subprocess
        with patch.object(diagnostics.subprocess, 'run', side_effect=subprocess.TimeoutExpired('secret', 5)):
            self.assertEqual(diagnostics.command(['systemctl', 'show']), (-1, ''))

    def test_backend_readiness_wait_tolerates_startup_but_is_bounded(self):
        for probes, expected in (([False, False, True], True), ([False] * 3, False)):
            clock = [0.0]
            def sleep(seconds):
                clock[0] += seconds
            with self.subTest(expected=expected), \
                 patch.object(diagnostics.time, 'monotonic', side_effect=lambda: clock[0]), \
                 patch.object(diagnostics.time, 'sleep', side_effect=sleep), \
                 patch.object(diagnostics, 'backend_ready', side_effect=probes) as ready, \
                 patch.object(diagnostics, 'unit_state', return_value={'ActiveState':'active'}), \
                 patch.object(diagnostics, 'command') as mutation:
                self.assertEqual(diagnostics.wait_backend_ready(timeout=3), expected)
            self.assertEqual(ready.call_count, 3)
            self.assertLessEqual(clock[0], 3)
            mutation.assert_not_called()

    def test_failed_backend_process_does_not_wait_or_restart(self):
        with patch.object(diagnostics, 'backend_ready', return_value=False), \
             patch.object(diagnostics, 'unit_state', return_value={'ActiveState':'failed'}), \
             patch.object(diagnostics.time, 'sleep') as sleep, \
             patch.object(diagnostics, 'command') as mutation:
            self.assertFalse(diagnostics.wait_backend_ready())
        sleep.assert_not_called()
        mutation.assert_not_called()

    def test_readiness_wait_is_after_confirmed_backend_start_only(self):
        for action, status, expected in (
            ('node-plane-backend.service', 'dispatched', True),
            ('node-plane-backend.service', 'refused', False),
            ('node-plane-driver.service', 'dispatched', False),
        ):
            result = SimpleNamespace(returncode=0, stdout='{"status":"' + status + '"}')
            with self.subTest(action=action, status=status), \
                 patch.object(diagnostics.subprocess, 'run', return_value=result), \
                 patch.object(diagnostics, 'wait_backend_ready', return_value=True) as wait:
                value = diagnostics.installed_database(action)
            self.assertEqual(wait.called, expected)
            self.assertEqual(value.get('ready'), True if expected else None)

    def test_observations_do_not_propose_repair_when_database_is_unavailable(self):
        with patch.object(diagnostics, 'installed_database', return_value={'status': 'unavailable'}), \
             patch.object(diagnostics, 'unit_state', return_value={'LoadState': 'loaded', 'ActiveState': 'failed'}), \
             patch.object(diagnostics.urllib.request, 'urlopen', side_effect=OSError):
            report = diagnostics.observe()
        self.assertGreater(report['errors'], 0)
        self.assertFalse(any('repair' in check for check in report['checks']))
        self.assertTrue(any(check['id'] == 'worker' for check in report['checks']))

    def test_polling_conflict_is_an_error_even_with_active_services(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            marker = base / 'shared/data/telegram-polling-conflict.json'
            marker.parent.mkdir(parents=True)
            marker.write_text('{"code":"telegram_polling_conflict"}')
            with patch.object(diagnostics, 'BASE', base), \
                 patch.object(diagnostics, 'installed_database', return_value={'status': 'ok'}), \
                 patch.object(diagnostics, 'unit_state', return_value={'LoadState': 'loaded', 'ActiveState': 'active'}), \
                 patch.object(diagnostics.urllib.request, 'urlopen', side_effect=OSError):
                report = diagnostics.observe()
            check = next(c for c in report['checks'] if c['id'] == 'telegram_polling')
            self.assertEqual(check['status'], 'error')
            self.assertIn('other installation', check['detail'])

    def test_schema_mismatch_is_reported_and_does_not_offer_service_repair(self):
        with patch.object(diagnostics, 'installed_database', return_value={'status':'ok','schema_valid':False,'maintenance':False}), \
             patch.object(diagnostics, 'unit_state', return_value={'LoadState':'loaded','ActiveState':'failed'}), \
             patch.object(diagnostics.urllib.request, 'urlopen', side_effect=OSError):
            report = diagnostics.observe()
        self.assertEqual(report['scope'], 'controller')
        self.assertEqual(next(c for c in report['checks'] if c['id']=='schema')['status'], 'error')
        self.assertFalse(any('repair' in check for check in report['checks']))

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
