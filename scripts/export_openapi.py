#!/usr/bin/env python3
"""Write the API contract: `api/openapi.json` and, with `--untyped`, the
list of operations without a response model (only to record a shrink)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'app'), str(ROOT)]

from backend import api_contract  # noqa: E402


def main():
    document = api_contract.schema()
    api_contract.SCHEMA.parent.mkdir(exist_ok=True)
    api_contract.SCHEMA.write_text(api_contract.text(document))
    if '--untyped' in sys.argv[1:]:
        api_contract.UNTYPED.write_text(''.join(line + '\n' for line in api_contract.untyped_operations(document)))
    print(api_contract.SCHEMA.relative_to(ROOT))


if __name__ == '__main__':
    main()
