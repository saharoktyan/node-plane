"""Automatic pruning protects the actual rollback release, not timestamps."""
import importlib.util
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('release_retention', ROOT / 'scripts/release_retention.py')
retention = importlib.util.module_from_spec(spec)
spec.loader.exec_module(retention)


class ReleaseRetentionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.releases = self.base / 'releases'
        self.releases.mkdir()
        for name in ('current', 'previous', 'failed', 'older', 'prepared'):
            release = self.releases / name
            release.mkdir()
            (release / 'VERSION').write_text(name)
        (self.base / 'current').symlink_to(self.releases / 'current')

    def test_success_keeps_exactly_current_and_actual_previous(self):
        removed = retention.retain(self.base, self.releases / 'current', self.releases / 'previous')
        self.assertEqual(set(removed), {'failed', 'older', 'prepared'})
        self.assertEqual({p.name for p in self.releases.iterdir()}, {'current', 'previous'})
        self.assertEqual((self.base / 'previous').resolve(), self.releases / 'previous')
        # Re-running installation of the active version retains its rollback target.
        self.assertEqual(retention.retain(self.base, self.releases / 'current', self.releases / 'current'), [])

    def test_first_install_keeps_only_current(self):
        retention.retain(self.base, self.releases / 'current')
        self.assertEqual([p.name for p in self.releases.iterdir()], ['current'])

    def test_wrong_current_or_previous_refuses_before_deleting_anything(self):
        for current, previous in ((self.releases / 'prepared', self.releases / 'previous'),
                                  (self.releases / 'current', self.base),
                                  (self.releases / 'current', self.releases / 'missing')):
            with self.subTest(current=current, previous=previous), self.assertRaises(ValueError):
                retention.retain(self.base, current, previous)
            self.assertEqual(len(list(self.releases.iterdir())), 5)

    def test_symlinked_release_entries_do_not_delete_external_files(self):
        external = self.base / 'unrelated'
        external.mkdir()
        (external / 'data').write_text('preserve')
        (self.releases / 'external').symlink_to(external)
        retention.retain(self.base, self.releases / 'current', self.releases / 'previous')
        self.assertEqual((external / 'data').read_text(), 'preserve')

    def test_cleanup_hook_runs_only_after_update_health_succeeds(self):
        import subprocess
        text = (ROOT / 'scripts/update.sh').read_text()
        start = text.index('  if wait_for_service "$HEALTH_TIMEOUT"; then', text.index('update_simple()'))
        end = text.index('\n}\n', start)
        tail = text[start:end]
        for healthy in (True, False):
            with self.subTest(healthy=healthy):
                body = '''
STACK_JOB=''; HEALTH_TIMEOUT=0; SIMPLE_BOT_SERVICE=fixture
base_dir=fixture; new_release_dir=new; previous_release=old; shared_dir=shared
wait_for_service() { return %d; }
sudo() { :; }
set_step() { :; }
retain_successful_releases() { printf 'retained %%s %%s\\n' "$2" "$3"; }
rollback_simple() { printf 'rolled-back\\n'; }
fixture() {
''' % (0 if healthy else 1)
                result = subprocess.run(['bash', '-c', body + tail + '\n}\nfixture'],
                    capture_output=True, text=True, check=True)
                self.assertEqual('retained new old' in result.stdout, healthy)
                self.assertEqual('rolled-back' in result.stdout, not healthy)
        self.assertLess(text.index('if [[ $SKIP_RESTART -eq 1 ]]', text.index('update_simple()')), start)

