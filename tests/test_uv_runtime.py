"""Exercise actual uv environments using a tiny offline wheel."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]


class UvProvisioningTests(unittest.TestCase):
    def test_missing_supported_python_provisions_private312_and_preserves_override(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            uv = base / 'uv'
            uv.write_text('#!/bin/bash\nprintf "%s\\n" "$*" >> "$CALL_LOG"\n'
                          'if [[ "$1 $2" == "python install" ]]; then mkdir -p "$(dirname "$MANAGED_PYTHON")"; touch "$MANAGED_PYTHON"; fi\n'
                          'if [[ "$1 $2" == "python find" ]]; then [[ -f "$MANAGED_PYTHON" ]] || exit 2; printf "%s\\n" "$MANAGED_PYTHON"; fi\n')
            uv.chmod(0o755)
            env = {**os.environ, 'NODE_PLANE_UV_BIN': str(uv), 'CALL_LOG': str(base / 'calls'),
                   'MANAGED_PYTHON': str(base / 'shared/tools/python/python3.12')}
            body = 'source "$1"; select_python_runtime() { return 1; }; select_controller_python "$2"'
            env.pop('NODE_PLANE_PYTHON_BIN', None)
            result = subprocess.run(['bash', '-c', body, 'bash', str(ROOT / 'scripts/uv_runtime.sh'),
                str(base / 'shared')], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), env['MANAGED_PYTHON'])
            calls = (base / 'calls').read_text()
            self.assertIn('python install 3.12', calls)
            self.assertIn('python find --managed-python 3.12', calls)
            (base / 'calls').unlink()
            result = subprocess.run(['bash', '-c', body, 'bash', str(ROOT / 'scripts/uv_runtime.sh'),
                str(base / 'shared')], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn('python install', (base / 'calls').read_text())
            (base / 'calls').unlink()
            env['NODE_PLANE_PYTHON_BIN'] = '/unsupported/python'
            result = subprocess.run(['bash', '-c', body, 'bash', str(ROOT / 'scripts/uv_runtime.sh'),
                str(base / 'shared')], env=env, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse((base / 'calls').exists())

    def test_missing_host_tools_are_installed_without_adding_python_apt_repository(self):
        body = '''source "$1"
command() {
  if [[ "$1" == -v && ( "$2" == python3 || "$2" == curl || "$2" == ssh-keygen ) ]]; then return 1; fi
  builtin command "$@"
}
install_packages_if_needed() { printf 'packages: %s\\n' "$*"; }
ensure_controller_host_tools
'''
        result = subprocess.run(['bash', '-c', body, 'bash', str(ROOT / 'scripts/uv_runtime.sh')],
            capture_output=True, text=True, check=True)
        self.assertIn('python3', result.stdout)
        self.assertIn('curl', result.stdout)
        self.assertIn('openssh-client', result.stdout)
        self.assertNotIn('python3.12', result.stdout)

@unittest.skipUnless(shutil.which('uv'), 'uv binary unavailable for offline integration test')
class UvRuntimeTests(unittest.TestCase):
    def test_release_environments_share_package_inodes_without_pip(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            wheel = base / 'fixture_pkg-1.0-py3-none-any.whl'
            with zipfile.ZipFile(wheel, 'w') as archive:
                archive.writestr('fixture_pkg/__init__.py', 'VALUE = 42\n')
                archive.writestr('fixture_pkg-1.0.dist-info/METADATA',
                    'Metadata-Version: 2.1\nName: fixture-pkg\nVersion: 1.0\n')
                archive.writestr('fixture_pkg-1.0.dist-info/WHEEL',
                    'Wheel-Version: 1.0\nGenerator: fixture\nRoot-Is-Purelib: true\nTag: py3-none-any\n')
                archive.writestr('fixture_pkg-1.0.dist-info/RECORD', '')
            packages = []
            for name in ('one', 'two'):
                release = base / name
                release.mkdir()
                (release / 'requirements.txt').write_text(str(wheel) + '\n')
                result = subprocess.run(['bash', '-c',
                    'source "$1"; install_release_dependencies "$2" "$3"', 'bash',
                    str(ROOT / 'scripts/uv_runtime.sh'), str(release), str(base / 'shared')],
                    env={**os.environ, 'NODE_PLANE_UV_BIN': shutil.which('uv'),
                         'PYTHON_BIN': sys.executable, 'UV_OFFLINE': 'true'},
                    capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                python = release / '.venv/bin/python'
                output = subprocess.check_output([str(python), '-c',
                    'import fixture_pkg, importlib.util; print(fixture_pkg.__file__); '
                    'assert fixture_pkg.VALUE == 42; assert importlib.util.find_spec("pip") is None'], text=True)
                packages.append(Path(output.strip()))
            self.assertEqual(packages[0].stat().st_ino, packages[1].stat().st_ino)
            self.assertGreaterEqual(packages[0].stat().st_nlink, 3)
            # Removing one complete release does not damage the other environment.
            shutil.rmtree(base / 'one')
            subprocess.run([str(base / 'two/.venv/bin/python'), '-c', 'import fixture_pkg'], check=True)

    def test_locked_release_sync_excludes_dev_cleans_extras_and_rejects_stale_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            def wheel(name):
                path = base / f'{name}-1.0-py3-none-any.whl'
                files = {f'{name}/__init__.py':'VALUE = 42\n',
                    f'{name}-1.0.dist-info/METADATA':f'Metadata-Version: 2.1\nName: {name}\nVersion: 1.0\n',
                    f'{name}-1.0.dist-info/WHEEL':'Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n'}
                record = f'{name}-1.0.dist-info/RECORD'
                files[record] = ''.join(f'{entry},,\n' for entry in [*files,record])
                with zipfile.ZipFile(path,'w') as archive:
                    for entry,content in files.items():
                        archive.writestr(entry,content)
                return path
            runtime, dev, retired = wheel('fixture_pkg'),wheel('fixture_dev'),wheel('retired_pkg')
            release = base / 'release'
            release.mkdir()
            manifest = ('[project]\nname="fixture-controller"\nversion="1.0"\n'
                'requires-python=">=3.11,<3.13"\n'
                f'dependencies=["fixture-pkg @ {runtime.as_uri()}"]\n'
                '[dependency-groups]\nbackend=[]\ntelegram=[]\n'
                f'dev=["fixture-dev @ {dev.as_uri()}"]\n'
                '[tool.uv]\npackage=false\n')
            (release/'pyproject.toml').write_text(manifest)
            (release/'requirements.txt').write_text(str(runtime)+'\n')
            env = {**os.environ,'NODE_PLANE_UV_BIN':shutil.which('uv'),
                'PYTHON_BIN':sys.executable,'UV_OFFLINE':'true','UV_CACHE_DIR':str(base/'shared/cache/uv')}
            subprocess.run([shutil.which('uv'),'lock','--project',str(release),'--python',sys.executable],
                env=env,check=True,capture_output=True,text=True)
            locked = (release/'uv.lock').read_bytes()
            def sync():
                return subprocess.run(['bash','-c','source "$1"; install_release_dependencies "$2" "$3"',
                    'bash',str(ROOT/'scripts/uv_runtime.sh'),str(release),str(base/'shared')],
                    env=env,capture_output=True,text=True,timeout=30)
            result = sync()
            self.assertEqual(result.returncode,0,result.stderr)
            python = release/'.venv/bin/python'
            subprocess.run([str(python),'-c','import fixture_pkg,importlib.util; '
                'assert importlib.util.find_spec("fixture_dev") is None; '
                'assert importlib.util.find_spec("pip") is None'],check=True)
            subprocess.run([shutil.which('uv'),'pip','install','--python',str(python),str(retired)],
                env=env,check=True,capture_output=True)
            result = sync()
            self.assertEqual(result.returncode,0,result.stderr)
            subprocess.run([str(python),'-c','import importlib.util; '
                'assert importlib.util.find_spec("retired_pkg") is None'],check=True)
            self.assertEqual((release/'uv.lock').read_bytes(),locked)
            (release/'pyproject.toml').write_text(manifest.replace('version="1.0"','version="2.0"'))
            result = sync()
            self.assertNotEqual(result.returncode,0)
            self.assertIn('lockfile',result.stderr.lower())
            self.assertEqual((release/'uv.lock').read_bytes(),locked)
            (release/'uv.lock').unlink()
            result = sync()
            self.assertNotEqual(result.returncode,0)
            self.assertIn('both required',result.stderr)
            subprocess.run([str(python),'-c','import fixture_pkg'],check=True)
