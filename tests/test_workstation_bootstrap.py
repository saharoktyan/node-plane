import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/install_workstation.sh'
ASSET = 'node-plane-cli-linux-amd64.tar.gz'


class WorkstationBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.marker = self.root / 'installed'
        self.requests = self.root / 'requests'
        self.env = {**os.environ, 'PATH': str(self.bin) + os.pathsep + os.environ['PATH'],
                    'BOOTSTRAP_FIXTURES': str(self.root), 'TEST_INSTALL_MARKER': str(self.marker),
                    'HOME': str(self.root / 'home'), 'XDG_CONFIG_HOME': str(self.root / 'config')}
        for key in ('NODE_PLANE_CLI_INSTALL_DIR', 'CARGO_DIST_INSTALL_DIR', 'CARGO_DIST_FORCE_INSTALL_DIR'):
            self.env.pop(key, None)
        curl = self.bin / 'curl'
        curl.write_text(f'#!{sys.executable}\n' + '''import os, pathlib, shutil, sys
root = pathlib.Path(os.environ['BOOTSTRAP_FIXTURES'])
url = next(a for a in sys.argv[1:] if a.startswith('https://'))
destination = pathlib.Path(sys.argv[sys.argv.index('-o') + 1])
with (root / 'requests').open('a') as out:
    out.write(url + '\\n' + str(destination) + '\\n')
name = 'releases.json' if 'api.github.com' in url else url.rsplit('/', 1)[1]
shutil.copyfile(root / name, destination)
''')
        curl.chmod(0o755)
        (self.root / 'releases.json').write_text(json.dumps([
            {'tag_name': 'v0.4.3-alpha.50'}, {'tag_name': 'v0.4.3-alpha.49'}]))
        (self.root / 'node-plane-cli-installer.sh').write_text(
            '#!/bin/sh\nprintf installed > "$TEST_INSTALL_MARKER"\n'
            'mkdir -p "$CARGO_DIST_FORCE_INSTALL_DIR"\n'
            'printf "#!/bin/sh\\nexit 0\\n" > "$CARGO_DIST_FORCE_INSTALL_DIR/node-plane"\n'
            'chmod +x "$CARGO_DIST_FORCE_INSTALL_DIR/node-plane"\n'
            'printf "%s\\n" "$XDG_CONFIG_HOME" > "$CARGO_DIST_FORCE_INSTALL_DIR/receipt-path"\n')

    def run_installer(self, *args):
        return subprocess.run(['bash', str(SCRIPT), *args], env=self.env,
                              text=True, capture_output=True, timeout=10)

    def test_explicit_dev_channel_selects_newest_alpha_and_cleans_downloads(self):
        result = self.run_installer('--channel', 'dev')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.marker.exists())
        self.assertIn('[4/4]', result.stdout)
        requests = self.requests.read_text().splitlines()
        self.assertIn('/v0.4.3-alpha.50/', requests[2])
        self.assertTrue(all(not Path(path).exists() for path in requests[1::2]))

    def test_pinned_release_does_not_query_discovery(self):
        result = self.run_installer('--channel', 'dev', '--tag', 'v0.4.3-alpha.50')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('api.github.com', self.requests.read_text())
        self.assertIn('/v0.4.3-alpha.50/node-plane-cli-installer.sh', self.requests.read_text())
        launcher = Path(self.env['HOME']) / '.local/bin/node-plane-dev'
        self.assertTrue(launcher.is_file())

    def test_missing_generated_installer_never_falls_back_to_legacy(self):
        (self.root / 'node-plane-cli-installer.sh').unlink()
        (self.root / ASSET).write_bytes(b'legacy archive')
        result = self.run_installer('--channel', 'dev', '--tag', 'v0.4.3-alpha.50')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('No published Workstation installer', result.stderr)
        self.assertIn('#build-from-source', result.stderr)
        self.assertFalse(self.marker.exists())
        self.assertNotIn(ASSET, self.requests.read_text())

    def test_stable_selection_and_invalid_arguments(self):
        (self.root / 'releases.json').write_text(json.dumps({'tag_name': 'v0.4.3'}))
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('/releases/latest', self.requests.read_text())
        for args in (('--tag', '../../bad'), ('--channel', 'unknown'), ('--tag',)):
            with self.subTest(args=args):
                self.assertNotEqual(self.run_installer(*args).returncode, 0)

    def test_unsupported_platform_downloads_nothing(self):
        uname = self.bin / 'uname'
        uname.write_text('#!/bin/sh\necho Windows_NT\n')
        uname.chmod(0o755)
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.requests.exists())

    def test_macos_uses_generated_installer_without_legacy_archive(self):
        uname = self.bin / 'uname'
        uname.write_text('#!/bin/sh\necho Darwin\n')
        uname.chmod(0o755)
        (self.root / 'node-plane-cli-installer.sh').write_text(
            '#!/bin/sh\nprintf installed > "$TEST_INSTALL_MARKER"\n'
            'mkdir -p "$CARGO_DIST_FORCE_INSTALL_DIR"\n'
            'printf "#!/bin/sh\\nexit 0\\n" > "$CARGO_DIST_FORCE_INSTALL_DIR/node-plane"\n'
            'chmod +x "$CARGO_DIST_FORCE_INSTALL_DIR/node-plane"\n'
            'printf "%s\\n" "$XDG_CONFIG_HOME" > "$CARGO_DIST_FORCE_INSTALL_DIR/receipt-path"\n')
        result = self.run_installer('--channel', 'dev')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.marker.exists())
        self.assertIn('node-plane-cli-installer.sh', self.requests.read_text())
        self.assertNotIn(ASSET, self.requests.read_text())

    def test_channels_use_distinct_prefixes_launchers_and_receipt_paths(self):
        for channel, tag in (('dev', 'v1.0.0-alpha.1'), ('stable', 'v1.0.0')):
            result = self.run_installer('--channel', channel, '--tag', tag)
            self.assertEqual(result.returncode, 0, result.stderr)
            prefix = Path(self.env['HOME']) / '.local/share/node-plane-workstation' / channel
            self.assertTrue((prefix / 'node-plane').is_file())
            launcher = Path(self.env['HOME']) / '.local/bin' / ('node-plane-dev' if channel == 'dev' else 'node-plane')
            self.assertEqual(launcher.resolve(), prefix / 'node-plane')
            self.assertEqual((prefix / 'receipt-path').read_text().strip(),
                str(Path(self.env['XDG_CONFIG_HOME']) / 'node-plane-workstation' / channel))
        self.assertNotEqual((Path(self.env['HOME']) / '.local/bin/node-plane-dev').resolve(),
            (Path(self.env['HOME']) / '.local/bin/node-plane').resolve())
        for channel, tag in (('dev', 'v1.0.0'), ('stable', 'v1.0.0-alpha.1')):
            self.assertNotEqual(self.run_installer('--channel', channel, '--tag', tag).returncode, 0)

    def test_generated_installer_failure_is_not_replayed_as_legacy_install(self):
        (self.root / 'node-plane-cli-installer.sh').write_text('#!/bin/sh\nexit 7\n')
        result = self.run_installer('--channel', 'dev')
        self.assertEqual(result.returncode, 7)
        self.assertFalse(self.marker.exists())
        self.assertNotIn(ASSET, self.requests.read_text())
