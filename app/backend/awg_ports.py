"""Controller policy and validation for agent-selected AWG ports."""
import secrets


def preferred_port(preset):
    return {'quic': 443, 'dns': 53}.get(preset) or 1024 + secrets.randbelow(8976)


def allowed_port(preset, preferred, selected):
    if type(selected) is not int:
        return False
    if preset == 'chaos':
        return 1024 <= selected <= 9999 and (selected - preferred) % 8976 < 128
    return selected in {'quic': (preferred, 443, 8443, 4433, 4443),
        'dns': (preferred, 53, 5353, 5300, 8053)}.get(preset, ())
