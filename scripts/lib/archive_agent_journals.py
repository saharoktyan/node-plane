#!/usr/bin/env python3
"""Quiesce and archive a previous controller's agent journals on onboarding."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from uuid import UUID, uuid4


def safe_path(path):
    path = Path(path)
    if not path.is_absolute() or '..' in path.parts or len(path.parts) < 3:
        raise ValueError('invalid agent journal path')
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError('symlinked agent journal path')
    return path


def archive(controller, ca_digest, node_key, *, config=Path('/etc/node-plane/agent.toml'),
            journal=Path('/etc/node-plane/profile-intents.sqlite3'),
            state=Path('/var/lib/node-plane-agent'), stop=None):
    if str(UUID(controller)) != controller or not re.fullmatch('[0-9a-f]{64}', ca_digest):
        raise ValueError('invalid controller identity')
    if not re.fullmatch('[A-Za-z0-9_-]{1,64}', node_key):
        raise ValueError('invalid node key')
    config, journal = safe_path(config), safe_path(journal)
    old_node = None
    if config.exists():
        raw = config.read_text()
        found = re.search(r'^node_key\s*=\s*("[^\n]+")\s*$', raw, re.M)
        if found:
            old_node = json.loads(found[1])
    state = safe_path(state)
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    marker = safe_path(state / 'controller-installation-id')
    lock_path = safe_path(state / 'journal-archive.lock')
    with open(lock_path, 'a') as lock:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        previous = marker.read_text().strip() if marker.exists() else None
        if previous is not None and str(UUID(previous)) != previous:
            raise ValueError('invalid previous controller identity')
        ca = safe_path(config.parent / 'tls/ca.crt')
        same_legacy_controller = previous is None and ca.exists() and hashlib.sha256(ca.read_bytes()).hexdigest() == ca_digest
        files = [safe_path(Path(str(journal) + suffix))
                 for suffix in ('', '-wal', '-shm', '-journal', '.lock', '.disabled')]
        files = [path for path in files if path.exists()]
        if any(not path.is_file() for path in files):
            raise ValueError('agent journal artifact is not a regular file')
        same = previous == controller or same_legacy_controller
        if same and old_node is not None and old_node != node_key:
            raise ValueError('a different node of this controller already owns the agent')
        if previous is None and files and not ca.exists():
            raise ValueError('cannot identify legacy journal owner; refusing to discard its fences')
        destination = None
        if not same and files:
            if stop is None:
                quiesce_agent()
            else:
                stop()
            archives = safe_path(state / 'journal-archives')
            archives.mkdir(mode=0o700, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            destination = archives / f'previous-controller-{previous or "legacy"}-{stamp}-{uuid4().hex[:8]}'
            destination.mkdir(mode=0o700)
            manifest = {'previous_controller': previous, 'new_controller': controller,
                        'previous_node': old_node, 'new_node': node_key,
                        'archived_at': stamp, 'files': [str(path) for path in files]}
            (destination / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
            os.chmod(destination / 'manifest.json', 0o600)
            moved = []
            try:
                for path in files:
                    target = destination / path.name
                    shutil.move(str(path), str(target))
                    moved.append((path, target))
                    os.chmod(target, 0o600)
            except Exception:
                for original, target in reversed(moved):
                    shutil.move(str(target), str(original))
                raise
        temporary = marker.with_name(marker.name + '.tmp')
        with open(temporary, 'w') as output:
            os.chmod(temporary, 0o600)
            output.write(controller + '\n')
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, marker)
        return destination


def quiesce_agent():
    loaded = subprocess.run(['systemctl', 'show', '-p', 'LoadState', '--value', 'node-plane-agent.service'],
                            capture_output=True, text=True, check=True).stdout.strip()
    if loaded == 'loaded':
        subprocess.run(['systemctl', 'stop', 'node-plane-agent.service'], check=True)
    if subprocess.run(['systemctl', 'is-active', '--quiet', 'node-plane-agent.service']).returncode == 0:
        raise ValueError('agent is still active; journal archival refused')


if __name__ == '__main__':
    os.umask(0o077)
    destination = archive(*sys.argv[1:4])
    if destination is not None:
        print(f'AGENT_JOURNAL_ARCHIVE|{sys.argv[3]}|{destination}')
