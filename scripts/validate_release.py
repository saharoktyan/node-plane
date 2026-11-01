#!/usr/bin/env python3
"""Validate release identity before building or publishing any assets."""
import argparse
from datetime import datetime, timezone
from pathlib import Path
import re
import subprocess
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
from services.release_versions import CHANNEL_BRANCHES, parse_release_metadata, release_channel


def validate(root: Path, tag: str, commit: str, *, check_branch: bool = False) -> str:
    channel = release_channel(tag)
    version = tag.removeprefix('v')
    if not re.fullmatch(r'[0-9a-f]{40}', commit):
        raise ValueError('A full release commit is required')
    if (root / 'VERSION').read_text().strip() != version:
        raise ValueError('Release tag must match VERSION')
    if (root / 'RELEASE_CHANNEL').read_text().strip() != channel:
        raise ValueError('RELEASE_CHANNEL must match the tag stage')
    for name in ('pyproject.toml', 'rust/node-plane-cli/Cargo.toml'):
        data = tomllib.loads((root / name).read_text())
        section = 'project' if name == 'pyproject.toml' else 'package'
        if data[section]['version'] != version:
            raise ValueError(f'{name} version must match VERSION')
    if check_branch:
        actual = subprocess.check_output(['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()
        if actual != commit:
            raise ValueError('Release commit must match the checkout')
        subprocess.run(['git', '-C', str(root), 'merge-base', '--is-ancestor', commit,
                        'origin/' + CHANNEL_BRANCHES[channel]], check=True, capture_output=True)
    return channel


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tag', required=True)
    parser.add_argument('--commit', required=True)
    parser.add_argument('--check-branch', action='store_true')
    parser.add_argument('--check-local-branch', action='store_true')
    parser.add_argument('--metadata', type=Path)
    parser.add_argument('--verify-metadata', type=Path)
    args = parser.parse_args()
    try:
        channel = validate(ROOT, args.tag, args.commit, check_branch=args.check_branch)
        if args.check_local_branch:
            branch = subprocess.check_output(['git', '-C', str(ROOT), 'branch', '--show-current'], text=True).strip()
            if branch != CHANNEL_BRANCHES[channel]:
                raise ValueError('Release must be prepared on its matching source branch')
        if args.verify_metadata:
            parse_release_metadata(args.verify_metadata.read_bytes(), tag=args.tag, channel=channel, commit=args.commit)
        if args.metadata:
            args.metadata.write_text(f'format=2\nchannel={channel}\ntag={args.tag}\n'
                                     f'version={args.tag[1:]}\ncommit={args.commit}\n'
                                     f'built_at={datetime.now(timezone.utc).isoformat()}\n')
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        parser.exit(1, f'Release validation failed: {exc}\n')


if __name__ == '__main__':
    main()
