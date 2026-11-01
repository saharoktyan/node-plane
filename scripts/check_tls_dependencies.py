#!/usr/bin/env python3
"""Reject known affected TLS dependencies in the committed release inputs."""
from pathlib import Path
import re
import tomllib

ROOT = Path(__file__).resolve().parents[1]
UV_VERSION = '0.13.6'


def version(value):
    if not re.fullmatch(r'\d+\.\d+\.\d+', value):
        raise ValueError('unsupported dependency version: ' + value)
    return tuple(map(int, value.split('.')))


def check(root=ROOT):
    errors = []
    python = tomllib.loads((root / 'uv.lock').read_text())
    packages = {}
    for package in python['package']:
        packages.setdefault(package['name'], set()).add(package['version'])
    for name, minimum in (('certifi', '2026.10.19'), ('uv', '0.13.4')):
        if name not in packages or any(version(value) < version(minimum) for value in packages[name]):
            errors.append(f'{name} must be locked at {minimum} or newer')
    project = tomllib.loads((root / 'pyproject.toml').read_text())
    if packages.get('uv') != {UV_VERSION} or f'uv=={UV_VERSION}' not in project['dependency-groups']['dev']:
        errors.append('uv project and lock pins differ from the approved installer version')
    if f'NODE_PLANE_UV_VERSION={UV_VERSION}\n' not in (root / 'scripts/uv_runtime.sh').read_text():
        errors.append('controller installer uv pin differs from the approved version')
    workflow = (root / '.github/workflows/tests.yml').read_text()
    ci_pins = re.findall(r'pip install uv==([^\s]+)', workflow)
    if not ci_pins or any(pin != UV_VERSION for pin in ci_pins):
        errors.append('CI uv pins differ from the approved version')
    for component in ('node-agent', 'node-driver', 'node-plane-cli'):
        path = root / 'rust' / component / 'Cargo.lock'
        locked = tomllib.loads(path.read_text())['package']
        webpki = [p for p in locked if p['name'] == 'rustls-webpki']
        if not webpki:
            errors.append(f'{component}: rustls-webpki lock entry missing')
        for package in webpki:
            if not ((0, 103, 16) <= version(package['version']) < (0, 104, 0)):
                errors.append(f'{component}: review rustls-webpki {package["version"]}')
        reviewed_bundles = {p['version'] for p in locked
                            if p['name'] == 'webpki-roots' and version(p['version']) >= (1, 0, 10)}
        for package in locked:
            # webpki-roots 0.26 is a compatibility wrapper over the 1.x bundle.
            if package['name'] in {'webpki-roots', 'webpki-root-certs'}:
                number = version(package['version'])
                if number < (1, 0, 0) and package['name'] == 'webpki-roots':
                    if not any(f'webpki-roots {bundle}' in package.get('dependencies', [])
                               for bundle in reviewed_bundles):
                        errors.append(f'{component}: unreviewed legacy root bundle')
                elif number < (1, 0, 10):
                    errors.append(f'{component}: outdated {package["name"]} root bundle')
    return errors


if __name__ == '__main__':
    failures = check()
    if failures:
        raise SystemExit('\n'.join(failures))
    print('TLS dependency locks and installer/CI pins satisfy the reviewed policy.')
