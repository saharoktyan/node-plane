"""Exercise the real Git catalog used to admit whole-stack updates."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class UpdateCatalogScriptTests(unittest.TestCase):
    def test_source_beta_requires_a_matching_tag_and_only_dev_accepts_head(self):
        from tests.test_controller_release import release
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def git(*args):
                return subprocess.check_output(['git', '-C', str(root), *args], text=True,
                                               stderr=subprocess.PIPE).strip()
            git('init', '-b', 'beta')
            git('config', 'user.name', 'Test')
            git('config', 'user.email', 'test@example.invalid')
            (root / 'VERSION').write_text('0.5.0-beta.1\n')
            (root / 'RELEASE_CHANNEL').write_text('beta\n')
            git('add', '.')
            git('commit', '-m', 'public beta')
            beta_commit = git('rev-parse', 'HEAD')
            git('update-ref', 'refs/remotes/origin/beta', beta_commit)
            git('tag', 'v0.5.0-beta.1')
            git('tag', 'v0.5.0-beta.2')  # Same source VERSION cannot identify another release.
            git('tag', 'v0.5.0-alpha.1')
            git('tag', 'legacy/v1.0.0-beta.11')
            self.assertEqual(release.source_refs(root, 'beta'), ['v0.5.0-beta.1'])
            release.validate_source(root, 'beta', 'v0.5.0-beta.1')
            for ref in ['HEAD', 'origin/beta', beta_commit, 'v0.5.0-beta.2',
                        'v0.5.0-alpha.1', 'legacy/v1.0.0-beta.11', '--output=unexpected']:
                with self.subTest(ref=ref), self.assertRaises(ValueError):
                    release.validate_source(root, 'beta', ref)
            (root / 'VERSION').write_text('0.5.0-alpha.2\n')
            (root / 'RELEASE_CHANNEL').write_text('dev\n')
            git('add', '.')
            git('commit', '-m', 'development head')
            git('update-ref', 'refs/remotes/origin/dev', git('rev-parse', 'HEAD'))
            for ref in ['HEAD', 'origin/dev', git('rev-parse', 'HEAD')]:
                release.validate_source(root, 'dev', ref)

    def test_tags_expose_full_commit_ids_including_annotated_tags(self):
        script = Path(__file__).resolve().parents[1] / 'scripts/check_updates.sh'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote, checkout = root / 'origin.git', root / 'checkout'

            def git(*args, cwd=root):
                return subprocess.run(['git', *args], cwd=cwd, check=True,
                    capture_output=True, text=True).stdout.strip()

            git('init', '--bare', str(remote))
            git('init', '-b', 'dev', str(checkout))
            git('config', 'user.name', 'Test', cwd=checkout)
            git('config', 'user.email', 'test@example.invalid', cwd=checkout)
            (checkout / 'VERSION').write_text('0.4.3-alpha.1\n')
            (checkout / 'RELEASE_CHANNEL').write_text('dev\n')
            git('add', 'VERSION', 'RELEASE_CHANNEL', cwd=checkout)
            git('commit', '-m', 'fixture', cwd=checkout)
            commit = git('rev-parse', 'HEAD', cwd=checkout)
            git('tag', 'v0.4.3-alpha.1', cwd=checkout)
            (checkout / 'VERSION').write_text('0.4.3-alpha.2\n')
            git('add', 'VERSION', cwd=checkout)
            git('commit', '-m', 'second release', cwd=checkout)
            commit2 = git('rev-parse', 'HEAD', cwd=checkout)
            git('tag', '-a', 'v0.4.3-alpha.2', '-m', 'annotated', cwd=checkout)
            git('remote', 'add', 'origin', str(remote), cwd=checkout)
            git('push', 'origin', 'dev', '--tags', cwd=checkout)
            result = subprocess.run(['bash', str(script), '--branch', 'dev', '--list'],
                env={**os.environ, 'NODE_PLANE_SOURCE_DIR': str(checkout),
                     'NODE_PLANE_APP_DIR': str(checkout)},
                check=True, capture_output=True, text=True)
            for version, sha in [('0.4.3-alpha.1', commit), ('0.4.3-alpha.2', commit2)]:
                self.assertIn(f'version_item: {version}|v{version}|tag|{sha}\n', result.stdout)

    def test_git_catalog_sorts_prereleases_numerically_and_excludes_stable_on_dev(self):
        script = Path(__file__).resolve().parents[1] / 'scripts/check_updates.sh'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            remote, checkout = root / 'origin.git', root / 'checkout'

            def git(*args, cwd=root):
                return subprocess.run(['git', *args], cwd=cwd, check=True,
                    capture_output=True, text=True).stdout.strip()

            git('init', '--bare', str(remote))
            git('init', '-b', 'dev', str(checkout))
            git('config', 'user.name', 'Test', cwd=checkout)
            git('config', 'user.email', 'test@example.invalid', cwd=checkout)
            # Local Git preference must not reorder release stages.
            for suffix in ('-rc', '-beta', '-alpha'):
                git('config', '--add', 'versionsort.suffix', suffix, cwd=checkout)
            (checkout / 'VERSION').write_text('0.5.0-alpha.1\n')
            (checkout / 'RELEASE_CHANNEL').write_text('dev\n')
            git('add', 'VERSION', 'RELEASE_CHANNEL', cwd=checkout)
            git('commit', '-m', 'fixture', cwd=checkout)
            git('tag', 'v0.5.0-alpha.1', cwd=checkout)
            (checkout / 'VERSION').write_text('0.5.0-alpha.12\n')
            git('add', 'VERSION', cwd=checkout)
            git('commit', '-m', 'next alpha', cwd=checkout)
            git('tag', 'v0.5.0-alpha.12', cwd=checkout)
            for tag in ('v1.0.0', 'v1.0.0-rc.10', 'v1.0.0-rc.2', 'v1.0.0-beta.12', 'v1.0.0-beta.2', 'v1.0.0-alpha.1'):
                git('tag', tag, cwd=checkout)
            git('remote', 'add', 'origin', str(remote), cwd=checkout)
            git('push', 'origin', 'dev', '--tags', cwd=checkout)

            result = subprocess.run(['bash', str(script), '--branch', 'dev', '--list'],
                env={**os.environ, 'NODE_PLANE_SOURCE_DIR': str(checkout),
                     'NODE_PLANE_APP_DIR': str(checkout)},
                check=True, capture_output=True, text=True)

            versions = [line.split('|')[0].replace('version_item: ', '')
                        for line in result.stdout.splitlines()
                        if line.startswith('version_item: ') and not line.startswith('version_item: HEAD')]
            self.assertEqual(versions, ['0.5.0-alpha.12', '0.5.0-alpha.1'])
            self.assertNotIn('1.0.0|v1.0.0', result.stdout)

            check_result = subprocess.run(['bash', str(script), '--branch', 'dev'],
                env={**os.environ, 'NODE_PLANE_SOURCE_DIR': str(checkout),
                     'NODE_PLANE_APP_DIR': str(checkout)},
                check=True, capture_output=True, text=True)
            self.assertIn('upstream_ref: v0.5.0-alpha.12', check_result.stdout)
