#!/usr/bin/env python3
"""Retain the verified active release and its actual rollback target."""
import argparse
from pathlib import Path
import shutil


def retain(base, current, previous=''):
    base = Path(base).resolve()
    releases = base / 'releases'
    current = Path(current).resolve(strict=True)
    if releases.is_symlink() or current.parent != releases or not current.is_dir():
        raise ValueError('Active release must be a direct directory under releases')
    if (base / 'current').resolve(strict=True) != current:
        raise ValueError('Active release changed; cleanup refused')
    marker = base / 'previous'
    rollback = Path(previous).resolve() if previous else None
    if rollback == current:
        rollback = marker.resolve() if marker.is_symlink() else None
    if rollback is not None and (rollback == current or rollback.parent != releases or not rollback.is_dir()):
        raise ValueError('Previous release is not a valid rollback target')
    # Snapshot the directory list before mutation; never traverse symlink entries.
    candidates = [p for p in releases.iterdir() if p.is_dir() and not p.is_symlink()]
    protected = {current, rollback}
    if marker.exists() and not marker.is_symlink():
        raise ValueError('Previous release marker is not a symlink')
    temporary = base / '.previous-next'
    if temporary.exists() or temporary.is_symlink():
        raise ValueError('Previous release marker update is already pending')
    if rollback:
        temporary.symlink_to(rollback)
        temporary.replace(marker)
    elif marker.is_symlink():
        marker.unlink()
    removed = []
    for candidate in candidates:
        if candidate in protected:
            continue
        if (base / 'current').resolve(strict=True) != current:
            raise ValueError('Active release changed; cleanup stopped')
        shutil.rmtree(candidate)
        removed.append(candidate.name)
    return removed


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base', required=True)
    parser.add_argument('--current', required=True)
    parser.add_argument('--previous', default='')
    args = parser.parse_args()
    try:
        removed = retain(args.base, args.current, args.previous)
    except (OSError, ValueError) as exc:
        parser.exit(1, f'Automatic release cleanup failed: {exc}\n')
    print(f'Automatic release cleanup: removed {len(removed)} old releases; retained current and rollback target.')
