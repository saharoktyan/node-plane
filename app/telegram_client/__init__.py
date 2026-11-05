"""Telegram presentation adapter for the standalone backend."""
import sys
from pathlib import Path

# The Python SDK ships beside `app/` (`sdk/python`); until the repository
# layout gives it its own import path, the adapter adds it.
_SDK = Path(__file__).resolve().parents[2] / 'sdk' / 'python'
if _SDK.is_dir() and str(_SDK) not in sys.path:
    sys.path.insert(0, str(_SDK))
