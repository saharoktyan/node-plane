"""Generate legacy installer exports from uv.lock; never edit them by hand."""
import argparse
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
EXPORTS = {
    'requirements.txt': ['--no-default-groups', '--group', 'backend', '--group', 'telegram'],
    'requirements-backend.txt': ['--only-group', 'backend'],
    'requirements-telegram.txt': ['--only-group', 'telegram'],
    'requirements-dev.txt': ['--only-group', 'dev'],
}
HEADER = '# Generated from pyproject.toml and uv.lock. Do not edit.\n# Regenerate: uv run --locked python scripts/export_python_requirements.py\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='fail if committed exports differ')
    args = parser.parse_args()
    stale = []
    for name, selection in EXPORTS.items():
        result = subprocess.run([os.environ.get('NODE_PLANE_UV_BIN', 'uv'), 'export',
            '--project', str(ROOT), '--python', sys.executable, '--locked',
            '--format', 'requirements.txt', '--no-header', '--no-emit-project', *selection],
            check=True, capture_output=True, text=True)
        value = HEADER + result.stdout
        path = ROOT / name
        if args.check:
            if not path.exists() or path.read_text() != value:
                stale.append(name)
        else:
            path.write_text(value)
    if stale:
        raise SystemExit('Stale lock exports: ' + ', '.join(stale) +
            '; run uv run --locked python scripts/export_python_requirements.py')


if __name__ == '__main__':
    main()
