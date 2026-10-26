"""Exercise the real Git catalog used to admit whole-stack updates."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


class UpdateCatalogScriptTests(unittest.TestCase):
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
            (checkout / 'VERSION').write_text('0.4.3-alpha.2\n')
            git('add', 'VERSION', cwd=checkout)
            git('commit', '-m', 'fixture', cwd=checkout)
            commit = git('rev-parse', 'HEAD', cwd=checkout)
            git('tag', 'v0.4.3-alpha.1', cwd=checkout)
            git('tag', '-a', 'v0.4.3-alpha.2', '-m', 'annotated', cwd=checkout)
            git('remote', 'add', 'origin', str(remote), cwd=checkout)
            git('push', 'origin', 'dev', '--tags', cwd=checkout)
            result = subprocess.run(['bash', str(script), '--branch', 'dev', '--list'],
                env={**os.environ, 'NODE_PLANE_SOURCE_DIR': str(checkout),
                     'NODE_PLANE_APP_DIR': str(checkout)},
                check=True, capture_output=True, text=True)
            for version in ('0.4.3-alpha.1', '0.4.3-alpha.2'):
                self.assertIn(f'version_item: {version}|v{version}|tag|{commit}\n', result.stdout)

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
            (checkout / 'VERSION').write_text('1.0.0-rc.10\n')
            git('add', 'VERSION', cwd=checkout)
            git('commit', '-m', 'fixture', cwd=checkout)
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
            self.assertEqual(versions, ['1.0.0-rc.10', '1.0.0-rc.2', '1.0.0-beta.12', '1.0.0-beta.2', '1.0.0-alpha.1'])
            self.assertNotIn('1.0.0|v1.0.0', result.stdout)

            check_result = subprocess.run(['bash', str(script), '--branch', 'dev'],
                env={**os.environ, 'NODE_PLANE_SOURCE_DIR': str(checkout),
                     'NODE_PLANE_APP_DIR': str(checkout)},
                check=True, capture_output=True, text=True)
            self.assertIn('upstream_ref: v1.0.0-rc.10', check_result.stdout)
