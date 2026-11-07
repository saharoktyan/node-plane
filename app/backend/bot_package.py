"""The Telegram bot's package a node installs: built from this release.

The bot's code is a part of the controller's: the `telegram_client`
package and the few modules it imports. With it go the bot's Python
packages pinned by hash (the `telegram` dependency group of the release's
lock) and the controller's own `uv`, compressed, which the node installs
them with. A package is built once per release under the data directory;
the driver reads it from there.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import zlib

from config import APP_ROOT, DATA_DIR, SHARED_ROOT

# What `telegram_client` imports from the rest of `app/`.
MODULES = ('config.py', 'domain_names.py', 'node_public_settings.py', 'awg_parameters.py', 'awg_profile.py',
           'services/__init__.py', 'services/release_versions.py', 'services/access_expiry.py',
           'backend/__init__.py', 'backend/node_templates.py')
SUFFIXES = {'.py', '.json', '.txt', '.md'}
_UV_VERSION = re.compile(r'^NODE_PLANE_UV_VERSION=(\S+)$', re.MULTILINE)


class PackageError(Exception):
    pass


def package_root():
    return Path(os.environ.get('NODE_PLANE_BOT_PACKAGE_DIR') or f'{DATA_DIR}/bot-package')


def uv_binary():
    """The uv the controller was installed with."""
    configured = os.environ.get('NODE_PLANE_UV_BIN')
    if configured:
        return Path(configured)
    script = Path(APP_ROOT) / 'scripts/uv_runtime.sh'
    match = _UV_VERSION.search(script.read_text()) if script.is_file() else None
    if match is None:
        raise PackageError('bot_package_uv_missing')
    return Path(SHARED_ROOT) / 'tools' / f'uv-{match[1]}' / 'uv'


def requirements(uv):
    """The bot's packages with their hashes, from the release's lock."""
    result = subprocess.run(
        [str(uv), 'export', '--frozen', '--only-group', 'telegram', '--no-emit-project', '--no-header',
         '--format', 'requirements.txt', '--project', APP_ROOT],
        capture_output=True, text=True, timeout=60, check=False, env={**os.environ, 'UV_OFFLINE': '1'})
    if result.returncode != 0 or '--hash=sha256:' not in result.stdout:
        raise PackageError('bot_package_requirements_failed')
    return result.stdout


def _code(app):
    yield from (path for path in sorted((app / 'telegram_client').rglob('*'))
                if path.is_file() and path.suffix in SUFFIXES and '__pycache__' not in path.parts)
    for name in MODULES:
        yield app / name


def build(version):
    """The package's directory for this release, built when missing; older
    releases' packages go."""
    root = package_root()
    target = root / version
    # The directory appears whole, uv last: one with uv in it is complete.
    if not (target / 'uv.zlib').is_file():
        uv = uv_binary()
        if not uv.is_file():
            raise PackageError('bot_package_uv_missing')
        staging = root / f'.{version}.tmp'
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        app = Path(APP_ROOT) / 'app'
        for path in _code(app):
            destination = staging / 'app' / path.relative_to(app)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)
        for name in ('VERSION', 'RELEASE_CHANNEL', 'BUILD_COMMIT'):
            source = Path(APP_ROOT) / name
            if source.is_file():
                shutil.copyfile(source, staging / name)
        (staging / 'requirements.txt').write_text(requirements(uv))
        (staging / 'uv.zlib').write_bytes(zlib.compress(uv.read_bytes(), 9))
        shutil.rmtree(target, ignore_errors=True)
        staging.rename(target)
    for entry in root.iterdir():
        if entry.name != version:
            shutil.rmtree(entry, ignore_errors=True)
    return target
