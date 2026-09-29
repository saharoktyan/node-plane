from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class TelegramSystemdInstallTests(unittest.TestCase):
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
