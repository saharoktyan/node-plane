from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest


class TelegramSystemdInstallTests(unittest.TestCase):
    def test_activation_retires_old_unit_without_restarting_ptb_on_failure(self):
        for restart_status in (0, 1):
            with self.subTest(restart_status=restart_status), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                base, shared, binaries = root / 'base', root / 'shared', root / 'bin'
                (base / 'current' / '.venv' / 'bin').mkdir(parents=True)
                shared.mkdir()
                binaries.mkdir()
                (base / 'current' / '.venv' / 'bin' / 'python').symlink_to(sys.executable)
                token = shared / 'adapter.token'
                token.write_text('test-credential\n')
                (shared / '.env').write_text(f'BOT_TOKEN=example\nNODE_PLANE_BACKEND_ADAPTER_TOKEN_FILE={token}\n')
                log = root / 'systemctl.log'
                for command, source in {
                    'id': '#!/bin/sh\nprintf "0\\n"\n',
                    'install': '#!/bin/sh\nexit 0\n',
                    'sleep': '#!/bin/sh\nexit 0\n',
                    'systemctl': f'''#!/bin/sh
printf '%s\\n' "$*" >> "$COMMAND_LOG"
case "$*" in
  'restart node-plane-telegram.service') exit {restart_status} ;;
esac
exit 0
''',
                }.items():
                    path = binaries / command
                    path.write_text(source)
                    path.chmod(0o755)
                script = Path(__file__).resolve().parents[1] / 'scripts/install_telegram_client_systemd.sh'
                result = subprocess.run(['bash', str(script), str(base), str(shared), '--activate'],
                    env={**os.environ, 'PATH': f'{binaries}:{os.environ["PATH"]}', 'COMMAND_LOG': str(log)},
                    capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, restart_status, result.stderr)
                commands = log.read_text().splitlines()
                self.assertIn('disable node-plane.service', commands)
                self.assertIn('stop node-plane.service', commands)
                self.assertIn('restart node-plane-telegram.service', commands)
                self.assertNotIn('start node-plane.service', commands)
                self.assertNotIn('enable node-plane.service', commands)

    def test_rendered_unit_runs_separate_client_and_does_not_activate_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            base, shared, rendered = root / 'base', root / 'shared', root / 'units'
            (base / 'current' / '.venv' / 'bin').mkdir(parents=True)
            shared.mkdir()
            (base / 'current' / '.venv' / 'bin' / 'python').symlink_to(sys.executable)
            token = shared / 'adapter.token'
            token.write_text('test-credential\n')
            (shared / '.env').write_text(
                f'BOT_TOKEN=example\nNODE_PLANE_BACKEND_ADAPTER_TOKEN_FILE={token}\n')
            script = Path(__file__).resolve().parents[1] / 'scripts' / 'install_telegram_client_systemd.sh'
            result = subprocess.run(['bash', str(script), str(base), str(shared),
                                     '--render-dir', str(rendered)],
                                    capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            unit = (rendered / 'node-plane-telegram.service').read_text()
            self.assertIn('ExecStart=' + str(base / 'current' / '.venv' / 'bin' / 'python')
                          + ' -m telegram_client.main', unit)
            self.assertIn('After=network-online.target node-plane-backend.service', unit)
            self.assertNotIn('systemctl', unit)


if __name__ == '__main__':
    unittest.main()
