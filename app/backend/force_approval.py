"""Private, short-lived capability for the new privileged Driver RPC.

The existing loopback Driver boundary is not an authentication boundary. Only
the trusted backend OS identity may publish these exact-operation approvals.
"""
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import time


def publish(spec, intent_json):
    shared = os.environ.get('NODE_PLANE_SHARED_DIR', '')
    if not shared:
        raise ValueError('force_approval_store_unavailable')
    data = Path(shared) / 'data'
    if not data.is_absolute() or not data.is_dir():
        raise ValueError('force_approval_store_unavailable')
    owner = data.stat().st_uid
    for path in (data, *data.parents):
        info = path.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in {0, owner}
                or info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX):
            raise ValueError('force_approval_store_untrusted')
    directory = data / 'force-recovery-approvals'
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid not in {0, owner} or info.st_mode & 0o077:
        raise ValueError('force_approval_store_untrusted')
    value = {key: spec[key] for key in ('node_key', 'command_id', 'ticket_id', 'kind')}
    value.update(intent_sha256=hashlib.sha256(intent_json.encode()).hexdigest(), expires_at=int(time.time()) + 300)
    # No payload, tokens or VPN credentials are stored in this capability.
    with tempfile.NamedTemporaryFile(mode='w', dir=directory, delete=False) as staged:
        json.dump(value, staged, sort_keys=True)
        staged.flush()
        os.fsync(staged.fileno())
        path = Path(staged.name)
    try:
        os.replace(path, directory / (spec['ticket_id'] + '.json'))
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        path.unlink(missing_ok=True)
