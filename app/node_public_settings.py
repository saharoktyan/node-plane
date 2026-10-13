"""Merge public node settings without losing inherited endpoint relationships."""


def merge_settings(previous, changes):
    result = {**previous, **changes}
    old_host = previous.get('public_host')
    host = result.get('public_host')
    if host != old_host:
        for field in ('xray_host', 'awg_public_host'):
            # Editors send a full settings object, including unchanged copies
            # of the old shared address. Distinct protocol addresses are overrides.
            if previous.get(field, old_host) == old_host and changes.get(field, old_host) == old_host:
                result[field] = host
    if 'awg_port' in changes and 'awg_port_mode' not in changes:
        result['awg_port_mode'] = 'manual'
    if result.get('awg_port_mode') == 'auto' and (
            ('awg_port_mode' in changes and 'awg_port' not in changes)
            or (result.get('awg_i1_preset') != previous.get('awg_i1_preset')
                and changes.get('awg_port', previous.get('awg_port')) == previous.get('awg_port'))):
        result.pop('awg_port', None)
    return result
