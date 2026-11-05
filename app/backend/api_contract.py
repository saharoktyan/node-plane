"""The HTTP API contract that clients and SDKs are built from.

`api/openapi.json` in the repository is the schema of this release, written
by `scripts/export_openapi.py`; a test fails while it differs from the
application. Operations whose success response has no model are listed in
`api/untyped-operations.txt`: the list may only shrink, so a new route always
declares what it returns.
"""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2] / 'api'
SCHEMA = ROOT / 'openapi.json'
UNTYPED = ROOT / 'untyped-operations.txt'
# The contract's own version: bump the major part for a breaking change.
VERSION = '1.0.0'


def schema():
    from .http_api import create_app
    # Building the routes reads no database.
    document = create_app(object()).openapi()
    document['info']['version'] = VERSION
    return document


def text(document):
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + '\n'


def untyped_operations(document):
    """`METHOD path` of operations whose 2xx JSON response has no schema."""
    found = []
    for path, operations in document['paths'].items():
        for method, operation in operations.items():
            for status, response in operation.get('responses', {}).items():
                if status.startswith('2') and response.get('content', {}).get(
                        'application/json', {}).get('schema') == {}:
                    found.append(f'{method.upper()} {path}')
                    break
    return sorted(found)
