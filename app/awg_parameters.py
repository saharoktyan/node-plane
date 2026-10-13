"""Controller validation of explicit AWG profiles using the shipped runtime rules."""
import importlib.util
import json
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def profile_module():
    path = Path(__file__).resolve().parents[1] / 'runtime_assets' / 'awg_profile.py'
    spec = importlib.util.spec_from_file_location('node_plane_awg_profile', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def validate_parameters(values):
    profile = profile_module()
    if not isinstance(values, dict) or set(values) - set(profile.ALL_FIELDS):
        raise ValueError('unknown AWG parameters')
    if not values:
        return
    if any(not isinstance(v, str) or not v or len(v) > 4096
           or any(ord(c) < 32 or ord(c) > 126 for c in v) for v in values.values()):
        raise ValueError('invalid AWG parameter value')
    profile.validate(values)


def parse_parameters(text):
    if text.strip().lower() == 'auto':
        return {}
    if text.lstrip().startswith('{'):
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError('duplicate AWG parameter')
                result[key] = value
            return result
        values = json.loads(text, object_pairs_hook=unique)
    else:
        values = {}
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if '=' not in line:
                raise ValueError('expected parameter = value')
            key, value = (part.strip() for part in line.split('=', 1))
            if key in values:
                raise ValueError('duplicate AWG parameter')
            values[key] = value
        if not values:
            raise ValueError('empty AWG profile; use auto to reset')
    validate_parameters(values)
    return values
