import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class BackendSystemdInstallTests(unittest.TestCase):
    def test_rendered_units_follow_active_release_and_shared_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            base = root / 'install'
            shared = base / 'shared'
            (base / 'current' / '.venv' / 'bin').mkdir(parents=True)
            shared.mkdir()
            (shared / '.env').write_text('DB_BACKEND=postgres\n')
            python = base / 'current' / '.venv' / 'bin' / 'python'
            python.touch(mode=0o700)
            python.chmod(0o700)
            rendered = root / 'units'
            subprocess.run(['bash', str(ROOT / 'scripts' / 'install_backend_systemd.sh'),
                str(base), str(shared), '--render-dir', str(rendered)], check=True)
            api = (rendered / 'node-plane-backend.service').read_text()
            worker = (rendered / 'node-plane-backend-worker.service').read_text()
            timer = (rendered / 'node-plane-backend-worker.timer').read_text()
            self.assertIn(f'EnvironmentFile={shared}/.env', api)
            self.assertIn('uvicorn backend.http_api:application --factory --host 127.0.0.1', api)
            self.assertIn('ExecStart=' + str(python) + ' -m backend.executor', worker)
            self.assertIn(str(shared / 'data' / 'backend-worker.lock'), worker)
            self.assertIn('Unit=node-plane-backend-worker.service', timer)
            self.assertIn('OnUnitInactiveSec=5s', timer)


if __name__ == '__main__':
    unittest.main()
