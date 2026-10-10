import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('dist_controller_release', ROOT / 'scripts/controller_release.py')
controller = importlib.util.module_from_spec(spec)
spec.loader.exec_module(controller)


class CargoDistReleaseTests(unittest.TestCase):
    def test_global_build_packages_server_stack_without_legacy_workstation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in controller.REQUIRED:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('fixture\n')
            (root / 'VERSION').write_text('0.4.3-alpha.59\n')
            (root / 'runtime_assets/manifest.json').write_text(json.dumps([{'asset_path': 'entry.sh'}]))
            (root / 'runtime_assets/entry.sh').write_text('#!/bin/sh\nexit 0\n')
            shutil.copyfile(ROOT / 'scripts/controller_release.py', root / 'scripts/controller_release.py')
            shutil.copyfile(ROOT / 'scripts/build_release_stack.sh', root / 'scripts/build_release_stack.sh')
            (root / '.env').write_text('must not be packaged\n')
            subprocess.run(['git', 'init', '-q', str(root)], check=True, capture_output=True)
            subprocess.run(['git', '-C', str(root), 'add', '.'], check=True, capture_output=True)

            binary = b'\x7fELF-fixture'
            for crate, name in [('node-driver', 'driver'), ('node-agent', 'agent')]:
                path = root / f'rust/{crate}/target/release/node-plane-{name}'
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(binary)
            commands = root / 'commands'
            commands.mkdir()
            for name, content in {
                'cargo': '#!/bin/sh\nexit 0\n',
                'protoc': '#!/bin/sh\nexit 0\n',
                'readelf': '#!/bin/sh\n[ "$1" != --version-info ] || echo GLIBC_2.35\nexit 0\n',
            }.items():
                path = commands / name
                path.write_text(content)
                path.chmod(0o755)
            env = {**os.environ, 'PATH': str(commands) + os.pathsep + os.environ['PATH'],
                   'GITHUB_REF_NAME': 'v0.4.3-alpha.59', 'GITHUB_SHA': 'a' * 40}
            result = subprocess.run(['bash', str(root / 'scripts/build_release_stack.sh')],
                                    env=env, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)

            config = tomllib.loads((ROOT / 'dist-workspace.toml').read_text())
            for name in config['dist']['extra-artifacts'][0]['artifacts']:
                self.assertTrue((root / name).is_file(), name)
            out = root / 'dist/release-stack'
            for line in (out / 'SHA256SUMS.txt').read_text().splitlines():
                digest, name = line.split()
                self.assertEqual(hashlib.sha256((out / name).read_bytes()).hexdigest(), digest)
            self.assertFalse((out / 'node-plane-cli-linux-amd64.tar.gz').exists())
            _, payload = controller.verify(out / 'node-plane-controller.tar.gz', 'v0.4.3-alpha.59', 'a' * 40)
            self.assertNotIn('.env', payload)
            self.assertNotIn('scripts/build_release_stack.sh', payload)
            # The global packaging job consumes the parallel job's artifacts,
            # and must not fall back to compiling when an artifact is missing.
            incoming = root / 'target/distrib'
            shutil.copytree(out, incoming)
            (commands / 'cargo').write_text('#!/bin/sh\nexit 99\n')
            result = subprocess.run(['bash', str(root / 'scripts/build_release_stack.sh'), '--from-ci'],
                env={**env, 'CI': 'true'}, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            (incoming / 'node-plane-agent-linux-amd64.tar.gz').unlink()
            result = subprocess.run(['bash', str(root / 'scripts/build_release_stack.sh'), '--from-ci'],
                env={**env, 'CI': 'true'}, capture_output=True, text=True, timeout=20)
            self.assertNotEqual(result.returncode, 0)

    def test_dependency_cache_preserves_dependencies_but_excludes_project_outputs(self):
        spec = importlib.util.spec_from_file_location('release_cache', ROOT / 'scripts/release_rust_cache.py')
        cache = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cache)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / 'target'
            files = ('release/deps/libserde-fixture.rlib', 'release/deps/node_plane-fixture',
                'release/build/node-agent-fixture/output', 'release/node-plane.exe',
                'distrib/archive.tar.gz', 'release/incremental/fixture')
            for name in files:
                path = target / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(name)
            cache.LOCATIONS = {'rust/node-plane-cli/target': target}
            cache.ARCHIVE = root / 'cache/dependencies.tar.gz'
            cache.save()
            shutil.rmtree(target)
            cache.restore()
            self.assertEqual([p.relative_to(target).as_posix() for p in target.rglob('*') if p.is_file()],
                ['release/deps/libserde-fixture.rlib'])

    def test_dependency_cache_rejects_path_traversal(self):
        import io
        spec = importlib.util.spec_from_file_location('release_cache', ROOT / 'scripts/release_rust_cache.py')
        cache = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cache)
        with tempfile.TemporaryDirectory() as temporary:
            cache.ARCHIVE = Path(temporary) / 'bad.tar.gz'
            with tarfile.open(cache.ARCHIVE, 'w:gz') as archive:
                member = tarfile.TarInfo('target/../../outside')
                member.size = 1
                archive.addfile(member, io.BytesIO(b'x'))
            with self.assertRaises(ValueError):
                cache.restore()
