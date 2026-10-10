"""Validation of public TLS server names, without DNS/network lookups."""
import re

_LABEL = re.compile(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\Z')


def valid_sni(value):
    if not isinstance(value, str) or not value.isascii() or len(value) > 253:
        return False
    labels = value.split('.')
    return (len(labels) >= 2 and all(_LABEL.fullmatch(label) for label in labels)
            and any(char.isalpha() for char in labels[-1]))
