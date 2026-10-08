#!/usr/bin/env python3
"""Build and consume the controller-only release artifact (stdlib only)."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

ASSET = 'node-plane-controller.tar.gz'
MANIFEST = 'CONTROLLER_PACKAGE.json'
SCRIPTS = {
    'install.sh', 'update.sh', 'rollback.sh', 'check_updates.sh',
    'healthcheck.sh', 'installation_diagnostics.py', 'controller_release.py',
    'setup_driver_agents.sh', 'install_backend_systemd.sh',
    'install_telegram_client_systemd.sh', 'postgres_runtime.sh', 'python_runtime.sh',
    'uv_runtime.sh', 'release_retention.py', 'run_node_driver.sh', 'run_node_agent.sh',
    'lib/agent_ssh.sh', 'lib/install_progress.sh', 'lib/stack_update.sh', 'lib/controller_archive.sh',
    'lib/archive_agent_journals.py',
}
ROOT_FILES = {'VERSION', 'LICENSE', '.env.example', 'requirements.txt',
              'requirements-backend.txt', 'requirements-telegram.txt'}
REQUIRED = ROOT_FILES | {'app/backend/http_api.py', 'app/backend/executor.py',
                        'app/telegram_client/main.py', 'runtime_assets/manifest.json'} | {'scripts/' + s for s in SCRIPTS}
TAG = re.compile(r'^v?(\d+)\.(\d+)\.(\d+)(?:-alpha\.(\d+))?$')
COMMIT = re.compile(r'^[0-9a-f]{40}$')
LIMIT = 64 * 1024 * 1024


def allowed(name, *, forward_compatible=False):
    path = PurePosixPath(name)
    if path.is_absolute() or '..' in path.parts or str(path) != name:
        return False
    runtime_script = (forward_compatible and path.suffix in {'.py', '.sh'} and
                      (path.parent == PurePosixPath('scripts') or
                       path.parent == PurePosixPath('scripts/lib')))
    return runtime_script or name in ROOT_FILES or name in {'scripts/' + s for s in SCRIPTS} or (
        name.startswith('app/') and path.suffix == '.py' and '__pycache__' not in path.parts) or (
        name.startswith('runtime_assets/') and '__pycache__' not in path.parts
        and (path.suffix in {'.py', '.sh', '.json'} or path.name in {'Dockerfile', 'node.env.example'})
        and not any(part.startswith('.') for part in path.parts))


def verify_runtime_assets(contents):
    """Require the entire driver deployment bundle, including non-Python assets."""
    name = 'runtime_assets/manifest.json'
    if name not in contents:
        return
    entries = json.loads(contents[name])
    if not isinstance(entries, list) or not entries:
        raise ValueError('Invalid runtime asset manifest')
    for entry in entries:
        asset = entry.get('asset_path') if isinstance(entry, dict) else None
        if not isinstance(asset, str) or not allowed('runtime_assets/' + asset):
            raise ValueError('Unsafe runtime asset path')
        if 'runtime_assets/' + asset not in contents:
            raise ValueError('Missing runtime asset: ' + asset)


def build(root, output, ref, commit):
    if not TAG.fullmatch(ref) or not COMMIT.fullmatch(commit):
        raise ValueError('A release tag and full commit SHA are required')
    # Only tracked files: no local credentials, test outputs or build caches.
    tracked = subprocess.check_output(['git', '-C', str(root), 'ls-files', '-z']).decode().split('\0')
    files = sorted(name for name in tracked if name and allowed(name))
    if not REQUIRED.issubset(files):
        raise ValueError('Missing controller runtime files: ' + ', '.join(sorted(REQUIRED - set(files))))
    version = (root / 'VERSION').read_text().strip()
    if version != ref.removeprefix('v'):
        raise ValueError('Controller VERSION does not match release tag')
    payload = {}
    for name in files:
        path = root / name
        if path.is_symlink() or not path.is_file():
            raise ValueError('Controller package files must be regular files')
        payload[name] = path.read_bytes()
    payload['BUILD_COMMIT'] = (commit + '\n').encode()
    verify_runtime_assets(payload)
    manifest = {'format': 1, 'ref': ref, 'version': version, 'commit': commit,
                'files': {name: hashlib.sha256(value).hexdigest() for name, value in payload.items()}}
    payload[MANIFEST] = (json.dumps(manifest, sort_keys=True) + '\n').encode()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(output, 'w:gz') as archive:
        for name, value in sorted(payload.items()):
            info = tarfile.TarInfo(name)
            info.size, info.mtime = len(value), 0
            info.mode = 0o755 if name.endswith('.sh') else 0o644
            archive.addfile(info, io.BytesIO(value))
    verify(output, ref, commit)


def verify(archive_path, ref=None, commit=None):
    contents = {}
    total = 0
    with tarfile.open(archive_path, 'r:gz') as archive:
        for member in archive:
            if (not member.isfile() or member.size < 0 or member.name in contents or
                not (allowed(member.name, forward_compatible=True) or member.name in {MANIFEST, 'BUILD_COMMIT'})):
                raise ValueError('Unexpected or unsafe controller archive member: ' + member.name)
            total += member.size
            if total > LIMIT or len(contents) >= 4096:
                raise ValueError('Controller archive exceeds size limits')
            contents[member.name] = archive.extractfile(member).read()
    manifest = json.loads(contents[MANIFEST])
    if (not isinstance(manifest, dict) or not isinstance(manifest.get('ref'), str) or
        not isinstance(manifest.get('commit'), str) or
        manifest.get('format') != 1 or not TAG.fullmatch(manifest.get('ref', '')) or
        not COMMIT.fullmatch(manifest.get('commit', '')) or
        manifest.get('version') != manifest['ref'].removeprefix('v')):
        raise ValueError('Invalid controller package identity')
    if ref and manifest['ref'].removeprefix('v') != ref.removeprefix('v'):
        raise ValueError('Controller archive tag does not match requested release')
    if commit and manifest['commit'] != commit:
        raise ValueError('Controller archive commit does not match requested release')
    files = manifest.get('files', {})
    # Readers must accept previously published runtime layouts too. The helper
    # became mandatory in alpha.52; its absence in older packages is intentional.
    required = set(REQUIRED)
    version = tuple(int(value or 0) for value in TAG.fullmatch(manifest['ref']).groups())
    if version < (0, 4, 3, 0) or (version[:3] == (0, 4, 3) and version[3] in range(1, 52)):
        required.discard('scripts/lib/archive_agent_journals.py')
    # Runtime assets were accidentally omitted through alpha.53. Old archives
    # remain readable; new builds always require a complete deployment bundle.
    if version < (0, 4, 3, 0) or (version[:3] == (0, 4, 3) and version[3] in range(1, 54)):
        required.discard('runtime_assets/manifest.json')
    if not isinstance(files, dict) or set(files) != set(contents) - {MANIFEST} or not required.issubset(files):
        raise ValueError('Controller package manifest is incomplete')
    for name, digest in files.items():
        if hashlib.sha256(contents[name]).hexdigest() != digest:
            raise ValueError('Controller package file checksum mismatch: ' + name)
    verify_runtime_assets(contents)
    if (contents['VERSION'].decode().strip() != manifest['version'] or
        contents['BUILD_COMMIT'].decode().strip() != manifest['commit']):
        raise ValueError('Controller package build identity is inconsistent')
    return manifest, contents


def extract(archive_path, destination, ref=None, commit=None):
    manifest, contents = verify(archive_path, ref, commit)
    if destination.exists() or destination.is_symlink():
        raise ValueError('Controller destination must not already exist')
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix='.controller-', dir=destination.parent))
    try:
        for name, value in contents.items():
            path = temporary / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(value)
            path.chmod(0o755 if name.endswith('.sh') else 0o644)
        temporary.rename(destination)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    return manifest


def copy_package(root, destination):
    manifest = json.loads((root / MANIFEST).read_text())
    names = list(manifest.get('files', {})) + [MANIFEST]
    if len(names) > 4096:
        raise ValueError('Controller package exceeds file limit')
    with tempfile.TemporaryDirectory() as temporary:
        archive_path = Path(temporary) / ASSET
        total = 0
        with tarfile.open(archive_path, 'w:gz') as archive:
            for name in names:
                if not (allowed(name, forward_compatible=True) or name in {MANIFEST, 'BUILD_COMMIT'}):
                    raise ValueError('Unsafe controller package path')
                path = root / name
                if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
                    raise ValueError('Controller package contains a symlink')
                total += path.stat().st_size
                if total > LIMIT:
                    raise ValueError('Controller package exceeds size limit')
                data = path.read_bytes()
                info = tarfile.TarInfo(name)
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
        return extract(archive_path, destination)


def repository():
    repo = os.environ.get('NODE_PLANE_GITHUB_REPO', 'saharoktyan/node-plane')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo):
        raise ValueError('Invalid GitHub repository')
    return repo


def fetch(url, limit=LIMIT):
    # Public artifacts; no bearer credentials are forwarded across redirects.
    request = urllib.request.Request(url, headers={'User-Agent': 'node-plane-controller'})
    with urllib.request.urlopen(request, timeout=60) as response:
        value = response.read(limit + 1)
    if len(value) > limit:
        raise ValueError('Release response exceeds size limit')
    return value


def sort_key(ref):
    major, minor, patch, alpha = TAG.fullmatch(ref).groups()
    return (int(major), int(minor), int(patch), alpha is None, int(alpha or 0))


def catalog(branch):
    repo = repository()
    refs = subprocess.check_output(['git', 'ls-remote', 'https://github.com/' + repo + '.git',
                                    'refs/tags/*'], timeout=60, stderr=subprocess.PIPE).decode()
    commits, peeled = {}, {}
    for line in refs.splitlines():
        sha, name = line.split()
        tag = name.removeprefix('refs/tags/')
        if tag.endswith('^{}'):
            peeled[tag[:-3]] = sha
        else:
            commits[tag] = sha
    commits.update(peeled)
    items = []
    for page in range(1, 21):
        releases = json.loads(fetch(f'https://api.github.com/repos/{repo}/releases?per_page=100&page={page}'))
        if not isinstance(releases, list):
            raise ValueError('Invalid GitHub release catalog')
        for release in releases:
            tag = release.get('tag_name', '')
            match = TAG.fullmatch(tag)
            if not match or release.get('draft') or (branch == 'main' and match[4] is not None):
                continue
            if branch == 'dev' and match[4] is None:
                continue
            assets = {a['name'] for a in release.get('assets', [])}
            if {ASSET, 'SHA256SUMS.txt'} <= assets and COMMIT.fullmatch(commits.get(tag, '')):
                items.append({'ref': tag, 'version': tag.removeprefix('v'), 'commit': commits[tag]})
        if len(releases) < 100:
            break
    return sorted(items, key=lambda i: sort_key(i['ref']), reverse=True)


def download(branch, ref, destination):
    items = catalog(branch)
    selected = next((item for item in items if not ref or
                     item['ref'].removeprefix('v') == ref.removeprefix('v') or
                     item['commit'] == ref), None)
    if selected is None:
        raise ValueError('No published controller archive for the selected release/channel. Publish controller assets first; source builds require --from-source.')
    base = f"https://github.com/{repository()}/releases/download/{selected['ref']}"
    checksums = fetch(base + '/SHA256SUMS.txt', 1024 * 1024).decode()
    matches = [line.split()[0] for line in checksums.splitlines()
               if len(line.split()) == 2 and line.split()[1].lstrip('*') == ASSET]
    if len(matches) != 1 or not re.fullmatch(r'[0-9a-f]{64}', matches[0]):
        raise ValueError('Controller checksum is missing or ambiguous')
    data = fetch(base + '/' + ASSET)
    if hashlib.sha256(data).hexdigest() != matches[0]:
        raise ValueError('Controller archive checksum mismatch')
    with tempfile.TemporaryDirectory() as temporary:
        archive = Path(temporary) / ASSET
        archive.write_bytes(data)
        return extract(archive, destination, selected['ref'], selected['commit'])


def check(branch, installed, listing):
    items = catalog(branch)
    if not items:
        raise ValueError('No published controller archives in this update channel')
    version = (installed / 'VERSION').read_text().strip()
    commit = (installed / 'BUILD_COMMIT').read_text().strip()
    if listing:
        print('LIST_VERSIONS|ok')
        print('branch: ' + branch)
        print('current_version: ' + version)
        for item in items:
            print(f"version_item: {item['version']}|{item['ref']}|tag|{item['commit']}")
    else:
        latest = items[0]
        same = commit != 'unknown' and len(commit) >= 7 and latest['commit'].startswith(commit)
        print('CHECK_UPDATES|' + ('up_to_date' if same else 'available'))
        for name, value in {'branch': branch, 'upstream_ref': latest['ref'],
                            'local_commit': commit, 'remote_commit': latest['commit'],
                            'local_version': version, 'remote_version': latest['version'],
                            'local_label': version + ' · ' + commit[:7],
                            'remote_label': latest['version'] + ' · ' + latest['commit'][:7]}.items():
            print(name + ': ' + value)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('build', 'verify', 'extract', 'download', 'check', 'copy'):
        cmd = sub.add_parser(name)
        if name in {'build', 'verify', 'extract'}:
            cmd.add_argument('--archive', type=Path, required=True)
            cmd.add_argument('--commit')
        if name in {'build', 'copy'}:
            cmd.add_argument('--root', type=Path, required=True)
        if name in {'extract', 'download', 'copy'}:
            cmd.add_argument('--destination', type=Path, required=True)
        if name in {'build', 'verify', 'extract', 'download'}:
            cmd.add_argument('--ref', default='')
        if name in {'download', 'check'}:
            cmd.add_argument('--branch', choices=('main', 'dev'), required=True)
        if name == 'check':
            cmd.add_argument('--installed', type=Path, required=True)
            cmd.add_argument('--list', action='store_true')
    args = parser.parse_args()
    try:
        if args.command == 'build':
            build(args.root, args.archive, args.ref, args.commit or '')
        elif args.command == 'copy':
            copy_package(args.root, args.destination)
        elif args.command == 'verify':
            verify(args.archive, args.ref or None, args.commit)
        elif args.command == 'extract':
            extract(args.archive, args.destination, args.ref or None, args.commit)
        elif args.command == 'download':
            download(args.branch, args.ref, args.destination)
        else:
            check(args.branch, args.installed, args.list)
    except (ValueError, KeyError, OSError, subprocess.SubprocessError, tarfile.TarError) as exc:
        if args.command == 'check':
            print('CHECK_UPDATES|error\nmessage: ' + str(exc))
        else:
            print('Controller release unavailable: ' + str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
