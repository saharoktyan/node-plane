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
            for name in controller.BUILD_REQUIRED:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('fixture\n')
            (root / 'VERSION').write_text('0.4.3-alpha.59\n')
            (root / 'runtime_assets/manifest.json').write_text(json.dumps([{'asset_path': 'entry.sh'}]))
            (root / 'runtime_assets/entry.sh').write_text('#!/bin/sh\nexit 0\n')
            shutil.copyfile(ROOT / 'scripts/controller_release.py', root / 'scripts/controller_release.py')
            shutil.copyfile(ROOT / 'scripts/build_release_stack.sh', root / 'scripts/build_release_stack.sh')
            for name in ('install_workstation.sh', 'install_workstation.ps1'):
                shutil.copyfile(ROOT / 'scripts' / name, root / 'scripts' / name)
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
            self.assertEqual(config['dist']['include'], ['LICENSE', 'THIRD_PARTY_LICENSES'])
            self.assertFalse(config['dist']['auto-includes'])
            for name in config['dist']['extra-artifacts'][0]['artifacts']:
                self.assertTrue((root / name).is_file(), name)
            out = root / 'dist/release-stack'
            for line in (out / 'SHA256SUMS.txt').read_text().splitlines():
                digest, name = line.split()
                self.assertEqual(hashlib.sha256((out / name).read_bytes()).hexdigest(), digest)
            self.assertFalse((out / 'node-plane-cli-linux-amd64.tar.gz').exists())
            self.assertEqual((out / 'node-plane-workstation-installer.sh').read_bytes(),
                             (ROOT / 'scripts/install_workstation.sh').read_bytes())
            self.assertEqual((out / 'node-plane-workstation-installer.ps1').read_bytes(),
                             (ROOT / 'scripts/install_workstation.ps1').read_bytes())

            for crate_name in ('driver', 'agent'):
                tar_path = out / f'node-plane-{crate_name}-linux-amd64.tar.gz'
                with tarfile.open(tar_path, 'r:gz') as archive:
                    names = sorted(archive.getnames())
                    self.assertEqual(names, ['LICENSE', 'THIRD_PARTY_LICENSES', f'node-plane-{crate_name}-linux-amd64'])
                    self.assertEqual(archive.extractfile('LICENSE').read(), b'fixture\n')
                    self.assertEqual(archive.extractfile('THIRD_PARTY_LICENSES').read(), b'fixture\n')
                    self.assertEqual(archive.extractfile(f'node-plane-{crate_name}-linux-amd64').read(), binary)

            _, payload = controller.verify(out / 'node-plane-controller.tar.gz', 'v0.4.3-alpha.59', 'a' * 40)
            self.assertEqual(payload['LICENSE'], b'fixture\n')
            self.assertEqual(payload['THIRD_PARTY_LICENSES'], b'fixture\n')
            self.assertNotIn('.env', payload)
            self.assertNotIn('scripts/build_release_stack.sh', payload)
            # The global packaging job consumes the parallel job's artifacts,
            # and must not fall back to compiling when an artifact is missing.
            incoming = root / 'dist/server-input'
            shutil.copytree(out, incoming)
            # cargo-dist can clear its output directory before invoking the
            # extra-artifact command; server inputs must survive that cleanup.
            distrib = root / 'target/distrib'
            shutil.copytree(out, distrib)
            shutil.rmtree(distrib)
            (commands / 'cargo').write_text('#!/bin/sh\nexit 99\n')
            result = subprocess.run(['bash', str(root / 'scripts/build_release_stack.sh'), '--from-ci'],
                env={**env, 'CI': 'true'}, capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            (incoming / 'node-plane-agent-linux-amd64.tar.gz').unlink()
            result = subprocess.run(['bash', str(root / 'scripts/build_release_stack.sh'), '--from-ci'],
                env={**env, 'CI': 'true'}, capture_output=True, text=True, timeout=20)
            self.assertNotEqual(result.returncode, 0)

    def test_build_release_stack_refuses_missing_or_empty_legal_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in controller.BUILD_REQUIRED:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('fixture\n')
            (root / 'VERSION').write_text('0.4.3-alpha.59\n')
            (root / 'runtime_assets/manifest.json').write_text(json.dumps([{'asset_path': 'entry.sh'}]))
            (root / 'runtime_assets/entry.sh').write_text('#!/bin/sh\nexit 0\n')
            shutil.copyfile(ROOT / 'scripts/controller_release.py', root / 'scripts/controller_release.py')
            shutil.copyfile(ROOT / 'scripts/build_release_stack.sh', root / 'scripts/build_release_stack.sh')
            for name in ('install_workstation.sh', 'install_workstation.ps1'):
                shutil.copyfile(ROOT / 'scripts' / name, root / 'scripts' / name)
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

            (root / 'THIRD_PARTY_LICENSES').unlink()
            result = subprocess.run(['bash', str(root / 'scripts/build_release_stack.sh')],
                                    env=env, capture_output=True, text=True, timeout=20)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Missing or empty required legal file: THIRD_PARTY_LICENSES', result.stderr)

            (root / 'THIRD_PARTY_LICENSES').write_text('')
            result = subprocess.run(['bash', str(root / 'scripts/build_release_stack.sh')],
                                    env=env, capture_output=True, text=True, timeout=20)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Missing or empty required legal file: THIRD_PARTY_LICENSES', result.stderr)

    def test_tag_release_script_includes_legal_files(self):
        script = (ROOT / 'scripts/tag_release.sh').read_text()
        self.assertIn('for legal in LICENSE THIRD_PARTY_LICENSES;', script)
        self.assertIn('tar -C "$tmp_dir" -czf "${release_dir}/${driver_name}.tar.gz" "$driver_name" LICENSE THIRD_PARTY_LICENSES', script)
        self.assertIn('tar -C "$tmp_dir" -czf "${release_dir}/${agent_name}.tar.gz" "$agent_name" LICENSE THIRD_PARTY_LICENSES', script)

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

    def test_dependency_cache_round_trips_hardlinked_cargo_outputs(self):
        spec = importlib.util.spec_from_file_location('release_cache', ROOT / 'scripts/release_rust_cache.py')
        cache = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cache)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = root / 'target'
            first = target / 'release/deps/libserde-fixture.rlib'
            second = target / 'release/libserde.rlib'
            first.parent.mkdir(parents=True)
            first.write_bytes(b'compiled dependency')
            os.link(first, second)
            cache.LOCATIONS = {'target': target}
            cache.ARCHIVE = root / 'cache/dependencies.tar.gz'
            cache.save()
            shutil.rmtree(target)
            cache.restore()
            self.assertEqual(first.read_bytes(), b'compiled dependency')
            self.assertEqual(second.read_bytes(), first.read_bytes())

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
