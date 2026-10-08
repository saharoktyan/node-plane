"""Controller packages and downloads without host services or public network."""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('controller_release', ROOT / 'scripts/controller_release.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class ControllerReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.tag = 'v' + (ROOT / 'VERSION').read_text().strip()
        self.commit = 'a' * 40
        for name in release.REQUIRED:
            target = self.source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, target)
        shutil.copytree(ROOT / 'app', self.source / 'app', dirs_exist_ok=True)
        for name in ('rust/node-agent/main.rs', 'docs/plan.md', 'tests/example.py',
                     'scripts/tag_release.sh', '.env', 'app/__pycache__/cached.pyc',
                     'app/backend/README.md'):
            path = self.source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('EXCLUDED')
        subprocess.run(['git', 'init', '-q', str(self.source)], check=True)
        subprocess.run(['git', '-C', str(self.source), 'add', '-f', '.'], check=True)
        self.archive = self.root / release.ASSET
        release.build(self.source, self.archive, self.tag, self.commit)

    def test_package_has_only_runtime_files_and_full_build_identity(self):
        manifest, contents = release.verify(self.archive, self.tag, self.commit)
        self.assertTrue(release.REQUIRED.issubset(contents))
        self.assertEqual(manifest['commit'], self.commit)
        self.assertEqual(contents['BUILD_COMMIT'], (self.commit + '\n').encode())
        self.assertTrue(all(release.allowed(name) or name in {release.MANIFEST, 'BUILD_COMMIT'}
                            for name in contents))
        for name in contents:
            self.assertNotIn('__pycache__', name)
            self.assertFalse(name.startswith(('rust/', 'tests/', 'docs/')))
        destination = self.root / 'release'
        release.extract(self.archive, destination, self.tag, self.commit)
        self.assertTrue((destination / 'app/telegram_client/main.py').is_file())
        self.assertEqual((destination / 'scripts/update.sh').stat().st_mode & 0o777, 0o755)
        result = subprocess.run(['python3', '-m', 'compileall', '-q', str(destination / 'app')],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_reusing_package_does_not_copy_venv_secrets_or_untracked_files(self):
        installed = self.root / 'installed'
        release.extract(self.archive, installed)
        (installed / '.venv').mkdir()
        (installed / '.venv/secret').write_text('SECRET')
        (installed / '.env').write_text('BOT_TOKEN=SECRET')
        target = self.root / 'copy'
        release.copy_package(installed, target)
        self.assertFalse((target / '.venv').exists())
        self.assertFalse((target / '.env').exists())
        (installed / 'scripts/update.sh').write_text('altered')
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            release.copy_package(installed, self.root / 'invalid')
        self.assertFalse((self.root / 'invalid').exists())

    def test_packaged_agent_setup_contains_and_runs_its_journal_helper(self):
        installed = self.root / 'installed'
        release.extract(self.archive, installed)
        library = ROOT / 'scripts/lib'
        for dependency in library.iterdir():
            if dependency.is_file() and dependency.suffix in {'.sh', '.py'}:
                self.assertTrue((installed / 'scripts/lib' / dependency.name).is_file(),
                                f'Missing operational library: {dependency.name}')
        helper = installed / 'scripts/lib/archive_agent_journals.py'
        spec = importlib.util.spec_from_file_location('packaged_journal_helper', helper)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        host = self.root / 'disposable-host'
        host.mkdir()
        controller = '4cfbcbc4-52ec-40e8-874d-a9ee692f1123'
        module.archive(controller, 'a' * 64, 'spb1', config=host / 'agent.toml',
                       journal=host / 'profile-intents.sqlite3', state=host / 'state',
                       stop=lambda: self.fail('A clean host needs no service stop'))
        self.assertEqual((host / 'state/controller-installation-id').read_text().strip(), controller)

    def test_reader_accepts_checksummed_future_libraries_but_builder_stays_explicit(self):
        extra = 'scripts/lib/future_runtime.py'
        self.assertFalse(release.allowed(extra))
        self.assertTrue(release.allowed(extra, forward_compatible=True))
        for name in ('scripts/lib/../secret.py', '/scripts/lib/helper.py',
                     'scripts/lib/keys.pem', 'docs/example.py', 'rust/main.rs'):
            self.assertFalse(release.allowed(name, forward_compatible=True))
        manifest, contents = release.verify(self.archive)
        contents[extra] = b'# new operational dependency\n'
        manifest['files'][extra] = hashlib.sha256(contents[extra]).hexdigest()
        contents[release.MANIFEST] = json.dumps(manifest).encode()
        future = self.root / 'future.tar.gz'
        with tarfile.open(future, 'w:gz') as output:
            for name, value in contents.items():
                info = tarfile.TarInfo(name)
                info.size = len(value)
                output.addfile(info, io.BytesIO(value))
        destination = self.root / 'future'
        release.extract(future, destination, self.tag, self.commit)
        release.copy_package(destination, self.root / 'future-copy')
        self.assertEqual((self.root / 'future-copy' / extra).read_bytes(), contents[extra])

    def test_download_failure_in_nested_shell_function_writes_terminal_receipt(self):
        source = (ROOT / 'scripts/update.sh').read_text()
        begin = source.index('fetch_code() {')
        function = source[begin:source.index('\n}\n', begin) + 3]
        receipt = self.root / 'failure-receipt'
        script = source.splitlines()[1] + '''
FROM_SOURCE=0
TARGET_BRANCH=dev
TARGET_REF=v0.4.3-alpha.52
set_step() { :; }
prepare_controller_archive() { return 1; }
on_error() { printf failed > "$RECEIPT"; }
trap 'on_error $?' ERR
''' + function + '\nmain() { fetch_code; }\nmain\n'
        result = subprocess.run(['bash', '-c', script], env={**os.environ, 'RECEIPT': str(receipt)},
                                capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(receipt.read_text(), 'failed')

    def test_reader_still_verifies_actual_previous_archive_layout(self):
        manifest, contents = release.verify(self.archive)
        contents.pop('scripts/lib/archive_agent_journals.py')
        manifest['files'].pop('scripts/lib/archive_agent_journals.py')
        manifest.update(ref='v0.4.3-alpha.51', version='0.4.3-alpha.51')
        contents['VERSION'] = b'0.4.3-alpha.51\n'
        manifest['files']['VERSION'] = hashlib.sha256(contents['VERSION']).hexdigest()
        contents[release.MANIFEST] = json.dumps(manifest).encode()
        old = self.root / 'old.tar.gz'
        with tarfile.open(old, 'w:gz') as output:
            for name, value in contents.items():
                info = tarfile.TarInfo(name)
                info.size = len(value)
                output.addfile(info, io.BytesIO(value))
        release.extract(old, self.root / 'previous', 'v0.4.3-alpha.51', self.commit)

    def test_wrong_identity_and_existing_destination_do_not_replace_files(self):
        target = self.root / 'target'
        for ref, commit in [('v9.9.9', self.commit), (self.tag, 'b' * 40)]:
            with self.assertRaises(ValueError):
                release.extract(self.archive, target, ref, commit)
            self.assertFalse(target.exists())
        target.mkdir()
        (target / 'keep').write_text('previous')
        with self.assertRaises(ValueError):
            release.extract(self.archive, target)
        self.assertEqual((target / 'keep').read_text(), 'previous')

    def test_unsafe_paths_links_extra_sources_and_duplicate_members_are_refused(self):
        for name, kind in [('../escape', tarfile.REGTYPE), ('rust/main.rs', tarfile.REGTYPE),
                           ('app/evil.py', tarfile.SYMTYPE), ('VERSION', tarfile.REGTYPE)]:
            with self.subTest(name=name), tarfile.open(self.archive, 'r:gz') as original:
                bad = self.root / 'bad.tar.gz'
                with tarfile.open(bad, 'w:gz') as output:
                    for member in original:
                        output.addfile(member, original.extractfile(member))
                    extra = tarfile.TarInfo(name)
                    extra.type = kind
                    extra.linkname = '/etc/passwd'
                    extra.size = 0
                    output.addfile(extra, io.BytesIO())
                with self.assertRaises(ValueError):
                    release.extract(bad, self.root / 'unsafe')
                self.assertFalse((self.root / 'unsafe').exists())

    def test_download_checks_outer_checksum_and_resolved_tag_commit(self):
        item = {'ref': self.tag, 'version': self.tag[1:], 'commit': self.commit}
        data = self.archive.read_bytes()
        checksums = (hashlib.sha256(data).hexdigest() + '  ' + release.ASSET + '\n').encode()
        def fetch(url, *args):
            return checksums if url.endswith('SHA256SUMS.txt') else data
        with patch.object(release, 'catalog', return_value=[item]), patch.object(release, 'fetch', side_effect=fetch):
            release.download('dev', self.tag, self.root / 'downloaded')
            self.assertTrue((self.root / 'downloaded/scripts/update.sh').exists())
            with patch.object(release, 'fetch', return_value=b'0' * 64 + b'  ' + release.ASSET.encode() + b'\n'):
                with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
                    release.download('dev', self.tag, self.root / 'bad-download')
            item['commit'] = 'b' * 40
            with self.assertRaisesRegex(ValueError, 'commit does not match'):
                release.download('dev', self.tag, self.root / 'wrong-commit')
        self.assertFalse((self.root / 'bad-download').exists())
        self.assertFalse((self.root / 'wrong-commit').exists())

    def test_release_catalog_ignores_missing_assets_and_peels_annotated_tags(self):
        entries = [{'tag_name': self.tag, 'assets': [{'name': release.ASSET}, {'name': 'SHA256SUMS.txt'}]},
                   {'tag_name': 'v0.0.1-alpha.1', 'assets': []},
                   {'tag_name': 'v9.9.9', 'assets': [{'name': release.ASSET}, {'name': 'SHA256SUMS.txt'}]}]
        refs = f"{'b' * 40}\trefs/tags/{self.tag}\n{self.commit}\trefs/tags/{self.tag}^{{}}\n"
        with patch.object(release.subprocess, 'check_output', return_value=refs.encode()), \
             patch.object(release, 'fetch', return_value=json.dumps(entries).encode()):
            items = release.catalog('dev')
        self.assertEqual(items, [{'ref': self.tag, 'version': self.tag[1:], 'commit': self.commit}])
        installed = self.root / 'installed'
        release.extract(self.archive, installed)
        for listing in (False, True):
            stdout = io.StringIO()
            with patch.object(release, 'catalog', return_value=items), contextlib.redirect_stdout(stdout):
                release.check('dev', installed, listing)
            self.assertIn('LIST_VERSIONS|ok' if listing else 'CHECK_UPDATES|up_to_date', stdout.getvalue())
            self.assertIn(self.commit, stdout.getvalue())

    def test_shell_staging_exports_archive_without_git_or_active_release_changes(self):
        installed = self.root / 'installed'
        release.extract(self.archive, installed)
        active = self.root / 'current'
        active.symlink_to(installed)
        for script_name in ('install.sh', 'update.sh'):
            script = (ROOT / 'scripts' / script_name).read_text()
            function = script[script.index('export_release_tree() {'):script.index('\n}\n', script.index('export_release_tree() {')) + 3]
            destination = self.root / script_name
            env = {**os.environ, 'CONTROLLER_STAGE_DIR': str(installed), 'DESTINATION': str(destination)}
            result = subprocess.run(['bash', '-c', 'set -euo pipefail\n' + function + '\nexport_release_tree "$DESTINATION" fake-ref'],
                                    env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((destination / 'BUILD_COMMIT').read_text().strip(), self.commit)
            self.assertEqual(active.resolve(), installed)
