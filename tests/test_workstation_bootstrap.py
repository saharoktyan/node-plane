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
        self.env['SHELL'] = '/bin/bash'
        for key in ('NODE_PLANE_CLI_INSTALL_DIR', 'CARGO_DIST_INSTALL_DIR', 'CARGO_DIST_FORCE_INSTALL_DIR',
                    'ZDOTDIR', 'NODE_PLANE_CLI_NO_MODIFY_PATH', 'INSTALLER_NO_MODIFY_PATH'):
            self.env.pop(key, None)
        curl = self.bin / 'curl'
        curl.write_text(f'#!{sys.executable}\n' + '''import os, pathlib, shutil, sys
root = pathlib.Path(os.environ['BOOTSTRAP_FIXTURES'])
url = next(a for a in sys.argv[1:] if a.startswith('https://'))
destination = pathlib.Path(sys.argv[sys.argv.index('-o') + 1])
with (root / 'requests').open('a') as out:
    out.write(url + '\\n' + str(destination) + '\\n')
name = 'releases.json' if 'api.github.com' in url else url.rsplit('/', 1)[1]
if '&page=2' in url and (root / 'releases-page2.json').exists():
    name = 'releases-page2.json'
if name == 'RELEASE_METADATA.txt' and not (root / name).exists():
    tag = url.split('/')[-2]
    if tag in os.environ.get('LEGACY_METADATA_TAGS', '').split(','):
        destination.write_text(f'tag={tag}\\nversion={tag[1:]}\\ncommit=ce88545\\n')
        sys.exit(0)
    stage = tag.split('-', 1)[1].split('.')[0] if '-' in tag else ''
    channel = 'dev' if stage == 'alpha' else 'beta' if stage in ('beta', 'rc') else 'stable'
    destination.write_text(f'format=2\\nchannel={channel}\\ntag={tag}\\nversion={tag[1:]}\\ncommit={"a" * 40}\\n')
else:
    shutil.copyfile(root / name, destination)
''')
        curl.chmod(0o755)
        (self.root / 'releases.json').write_text(json.dumps([
            {'tag_name': 'v0.4.3-alpha.50'}, {'tag_name': 'v0.4.3-alpha.49'}]))
        (self.root / 'node-plane-cli-installer.sh').write_text(
            '#!/bin/sh\n[ "$NODE_PLANE_CLI_NO_MODIFY_PATH" = 1 ] || exit 94\nprintf installed > "$TEST_INSTALL_MARKER"\n'
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
        self.assertIn('/releases?per_page=100', self.requests.read_text())
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
        for channel, tag in (('dev', 'v1.0.0-alpha.1'), ('beta', 'v1.0.0-beta.2'), ('beta', 'v1.0.0-rc.3'), ('stable', 'v1.0.0')):
            result = self.run_installer('--channel', channel, '--tag', tag)
            self.assertEqual(result.returncode, 0, result.stderr)
            prefix = Path(self.env['HOME']) / '.local/share/node-plane-workstation' / channel
            self.assertTrue((prefix / 'node-plane').is_file())
            launcher = Path(self.env['HOME']) / '.local/bin' / ('node-plane' if channel == 'stable' else f'node-plane-{channel}')
            self.assertEqual(launcher.resolve(), prefix / 'node-plane')
            self.assertEqual((prefix / 'receipt-path').read_text().strip(),
                str(Path(self.env['XDG_CONFIG_HOME']) / 'node-plane-workstation' / channel))
        self.assertNotEqual((Path(self.env['HOME']) / '.local/bin/node-plane-dev').resolve(),
            (Path(self.env['HOME']) / '.local/bin/node-plane').resolve())
        for channel, tag in (('dev', 'v1.0.0'), ('stable', 'v1.0.0-alpha.1'),
                             ('stable', 'v1.0.0-beta.2'), ('stable', 'v1.0.0-rc.3')):
            self.assertNotEqual(self.run_installer('--channel', channel, '--tag', tag).returncode, 0)

    def test_discovered_beta_and_rc_accepted_only_beta(self):
        for tag in ('v0.4.3-beta.2', 'v0.4.3-rc.1'):
            with self.subTest(tag=tag):
                (self.root / 'releases.json').write_text(json.dumps([{'tag_name': tag}]))
                dev_result = self.run_installer('--channel', 'beta')
                self.assertEqual(dev_result.returncode, 0, dev_result.stderr)
                self.assertIn(f'/{tag}/', self.requests.read_text())

                stable_result = self.run_installer('--channel', 'stable')
                self.assertNotEqual(stable_result.returncode, 0)

    def test_explicit_beta_and_rc_accepted_only_beta(self):
        for tag in ('v0.4.3-beta.1', 'v0.4.3-rc.2'):
            with self.subTest(tag=tag):
                result = self.run_installer('--channel', 'beta', '--tag', tag)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(f'/{tag}/node-plane-cli-installer.sh', self.requests.read_text())

                mismatch_result = self.run_installer('--channel', 'stable', '--tag', tag)
                self.assertNotEqual(mismatch_result.returncode, 0)
                self.assertIn('The release tag lacks matching channel metadata', mismatch_result.stderr)

    def test_generated_installer_failure_is_not_replayed_as_legacy_install(self):
        (self.root / 'node-plane-cli-installer.sh').write_text('#!/bin/sh\nexit 7\n')
        result = self.run_installer('--channel', 'dev')
        self.assertEqual(result.returncode, 7)
        self.assertFalse(self.marker.exists())
        self.assertNotIn(ASSET, self.requests.read_text())

    def test_legacy_higher_beta_is_skipped_and_cannot_be_pinned(self):
        self.env['LEGACY_METADATA_TAGS'] = 'v1.0.0-beta.11'
        (self.root / 'releases.json').write_text(json.dumps([
            {'tag_name': 'v1.0.0-beta.11'}, {'tag_name': 'v0.5.0-alpha.20'},
            {'tag_name': 'v0.5.0-beta.2'}, {'tag_name': 'v0.5.0-beta.12'}]))
        result = self.run_installer('--channel', 'beta')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('/v0.5.0-beta.12/node-plane-cli-installer.sh', self.requests.read_text())
        self.assertNotIn('/v1.0.0-beta.11/node-plane-cli-installer.sh', self.requests.read_text())
        self.marker.unlink()
        result = self.run_installer('--channel', 'beta', '--tag', 'v1.0.0-beta.11')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.marker.exists())

    def test_shell_path_keeps_stable_command_when_beta_is_installed(self):
        for channel, tag in [('stable', 'v0.5.0'), ('beta', 'v0.5.0-beta.2'), ('dev', 'v0.5.0-alpha.2')]:
            result = self.run_installer('--channel', channel, '--tag', tag)
            self.assertEqual(result.returncode, 0, result.stderr)
        home = Path(self.env['HOME'])
        profile = (home / '.bashrc').read_text()
        self.assertEqual(profile.count('. "$HOME/.local/share/node-plane-workstation/env"'), 1)
        result = subprocess.run(['bash', '-c', '. "$HOME/.bashrc"; . "$HOME/.bashrc"; '
                                 'command -v node-plane; command -v node-plane-beta; command -v node-plane-dev; printf "%s\\n" "$PATH"'],
                                env=self.env, capture_output=True, text=True, check=True)
        lines = result.stdout.splitlines()
        for name, channel in [('node-plane', 'stable'), ('node-plane-beta', 'beta'), ('node-plane-dev', 'dev')]:
            launcher = home / '.local/bin' / name
            self.assertIn(str(launcher), lines)
            self.assertEqual(launcher.resolve(), home / '.local/share/node-plane-workstation' / channel / 'node-plane')
        self.assertEqual(lines[-1].split(os.pathsep).count(str(home / '.local/bin')), 1)
        self.assertNotIn(str(home / '.local/share/node-plane-workstation/beta'), lines[-1].split(os.pathsep))

    def test_path_configuration_respects_opt_out_and_custom_zdotdir(self):
        self.env['NODE_PLANE_CLI_NO_MODIFY_PATH'] = '1'
        result = self.run_installer('--channel', 'beta', '--tag', 'v0.5.0-beta.1')
        self.assertEqual(result.returncode, 0, result.stderr)
        home = Path(self.env['HOME'])
        self.assertFalse((home / '.profile').exists())
        self.env.pop('NODE_PLANE_CLI_NO_MODIFY_PATH')
        self.env['SHELL'] = '/bin/zsh'
        self.env['ZDOTDIR'] = str(self.root / 'custom zsh')
        result = self.run_installer('--channel', 'beta', '--tag', 'v0.5.0-beta.1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('. "$HOME/.local/share/node-plane-workstation/env"',
                      (Path(self.env['ZDOTDIR']) / '.zshenv').read_text())

    def test_other_channel_activity_does_not_hide_a_release_on_the_next_page(self):
        (self.root / 'releases.json').write_text(json.dumps([
            {'tag_name': f'legacy/archive-{number}'} for number in range(100)]))
        (self.root / 'releases-page2.json').write_text(json.dumps([{'tag_name': 'v0.5.0-beta.2'}]))
        result = self.run_installer('--channel', 'beta')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('&page=2', self.requests.read_text())
        self.assertIn('/v0.5.0-beta.2/node-plane-cli-installer.sh', self.requests.read_text())
