"""Cache Cargo dependencies across release tags, excluding project binaries."""
import os
from pathlib import Path, PurePosixPath
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]
CARGO = Path(os.environ.get('CARGO_HOME', Path.home() / '.cargo'))
LOCATIONS = {
    'cargo/registry/cache': CARGO / 'registry/cache',
    'cargo/registry/src': CARGO / 'registry/src',
    'target': ROOT / 'target',
    **{f'rust/{crate}/target': ROOT / f'rust/{crate}/target'
       for crate in ('node-agent', 'node-driver', 'node-plane-cli')},
}
ARCHIVE = ROOT / '.release-rust-cache/dependencies.tar.gz'


def dependency(name):
    parts = PurePosixPath(name).parts
    if 'target' in parts:
        parts = parts[parts.index('target') + 1:]
    return not any(part in {'incremental', 'distrib', '.git'} or
                   part.startswith(('node-plane', 'node_plane', 'libnode_plane', 'node-agent', 'node_agent',
                                    'node-driver', 'node_driver')) for part in parts)


def save():
    ARCHIVE.parent.mkdir(exist_ok=True)
    with tarfile.open(ARCHIVE, 'w:gz', compresslevel=1) as archive:
        for prefix, directory in LOCATIONS.items():
            if not directory.exists():
                continue
            for path in directory.rglob('*'):
                name = prefix + '/' + path.relative_to(directory).as_posix()
                if path.is_file() and not path.is_symlink() and dependency(name):
                    archive.add(path, arcname=name, recursive=False)


def restore():
    with tarfile.open(ARCHIVE, 'r:gz') as archive:
        for member in archive:
            parts = PurePosixPath(member.name).parts
            if not member.isfile() or '..' in parts or member.name.startswith('/') or '\\' in member.name:
                raise ValueError('Unsafe dependency cache member')
            location = next(((prefix, path) for prefix, path in LOCATIONS.items()
                             if member.name.startswith(prefix + '/')), None)
            if location is None or not dependency(member.name):
                raise ValueError('Unexpected dependency cache member')
            prefix, directory = location
            destination = directory / member.name[len(prefix) + 1:]
            if not destination.resolve().is_relative_to(directory.resolve()):
                raise ValueError('Dependency cache path escapes its directory')
            destination.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(member) as source, destination.open('wb') as output:
                import shutil
                shutil.copyfileobj(source, output)
            destination.chmod(member.mode & 0o777)
            os.utime(destination, (member.mtime, member.mtime))


if __name__ == '__main__':
    {'save': save, 'restore': restore}[sys.argv[1]]()
