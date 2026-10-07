import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/install_workstation.sh'
ASSET = 'node-plane-cli-linux-amd64.tar.gz'
MEMBER = 'node-plane-cli-linux-amd64'


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
                    'BOOTSTRAP_FIXTURES': str(self.root), 'TEST_INSTALL_MARKER': str(self.marker)}
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
        self.make_archive()

    def make_archive(self, member=MEMBER, version='0.4.3-alpha.50'):
        binary = f'''#!/usr/bin/env bash
set -e
if [[ "$1" == --version ]]; then echo "node-plane {version}"; exit 0; fi
[[ "$1 $2" == 'self install' ]]
printf installed > "$TEST_INSTALL_MARKER"
'''.encode()
        with tarfile.open(self.root / ASSET, 'w:gz') as archive:
            info = tarfile.TarInfo(member)
            info.mode, info.size = 0o755, len(binary)
            archive.addfile(info, io.BytesIO(binary))
        digest = hashlib.sha256((self.root / ASSET).read_bytes()).hexdigest()
        (self.root / 'SHA256SUMS.txt').write_text(f'{digest}  {ASSET}\n')

    def run_installer(self, *args):
        return subprocess.run(['bash', str(SCRIPT), *args], env=self.env,
                              text=True, capture_output=True, timeout=10)

    def test_default_channel_selects_newest_alpha_and_cleans_downloads(self):
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.marker.exists())
        self.assertIn('[4/4]', result.stdout)
        requests = self.requests.read_text().splitlines()
        self.assertIn('/v0.4.3-alpha.50/', requests[2])
        self.assertTrue(all(not Path(path).exists() for path in requests[1::2]))

    def test_pinned_release_does_not_query_discovery(self):
        result = self.run_installer('--tag', 'v0.4.3-alpha.50')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('api.github.com', self.requests.read_text())

    def test_bad_checksum_never_executes_installer(self):
        (self.root / 'SHA256SUMS.txt').write_text(f'{"0" * 64}  {ASSET}\n')
        result = self.run_installer('--tag', 'v0.4.3-alpha.50')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('checksum mismatch', result.stderr)
        self.assertFalse(self.marker.exists())

    def test_wrong_archive_or_version_never_installs(self):
        for options in ({'member': 'unexpected'}, {'version': '0.4.3-alpha.49'}):
            with self.subTest(options=options):
                self.make_archive(**options)
                result = self.run_installer('--tag', 'v0.4.3-alpha.50')
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(self.marker.exists())

    def test_stable_selection_and_invalid_arguments(self):
        (self.root / 'releases.json').write_text(json.dumps({'tag_name': 'v0.4.3'}))
        self.make_archive(version='0.4.3')
        result = self.run_installer('--channel', 'stable')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('/releases/latest', self.requests.read_text())
        for args in (('--tag', '../../bad'), ('--channel', 'unknown'), ('--tag',)):
            with self.subTest(args=args):
                self.assertNotEqual(self.run_installer(*args).returncode, 0)

    def test_unsupported_platform_downloads_nothing(self):
        uname = self.bin / 'uname'
        uname.write_text('#!/bin/sh\necho Darwin\n')
        uname.chmod(0o755)
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.requests.exists())
