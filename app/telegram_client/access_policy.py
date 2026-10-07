"""Local access drafts; authoritative reconciliation belongs to the backend."""
import json

from .i18n import tr
from .screens import Section, Table


def values(data):
    return {'explicit_grants': [dict(g) for g in data.get('draft_grants') or []],
            'rules': [dict(r, protocols=list(r['protocols'])) for r in data.get('draft_rules') or []],
            'exclusions': [dict(g) for g in data.get('draft_exclusions') or []]}


def pairs(grants):
    return {(g['node_key'], g['protocol']) for g in grants}


def grants(selection):
    return [{'node_key': n, 'protocol': p} for n, p in sorted(selection)]


def inherited(policy, nodes):
    result = set()
    for node in nodes:
        if not node.get('policy_eligible', node.get('enabled', True) or node.get('applied_revision', 0)):
            continue
        for rule in policy['rules']:
            if rule['scope'] == 'all' or (node.get('region_id') and node['region_id'] == rule['region_id']):
                result.update((node['key'], p) for p in rule['protocols'] if p in node['protocols'])
    return result - pairs(policy['exclusions'])


def effective(data, nodes):
    policy = values(data)
    return pairs(policy['explicit_grants']) | inherited(policy, nodes)


def changed(data):
    return (pairs(data.get('draft_grants') or []) != pairs(data.get('original_grants') or [])
        or json.dumps(sorted(data.get('draft_rules') or [], key=lambda r: (r['scope'], r['region_id'] or '')), sort_keys=True)
           != json.dumps(sorted(data.get('original_rules') or [], key=lambda r: (r['scope'], r['region_id'] or '')), sort_keys=True)
        or pairs(data.get('draft_exclusions') or []) != pairs(data.get('original_exclusions') or []))


def toggle(data, nodes, node_key, protocol, add):
    policy = values(data)
    manual, excluded = pairs(policy['explicit_grants']), pairs(policy['exclusions'])
    target = (node_key, protocol)
    if add:
        excluded.discard(target)
        policy['exclusions'] = grants(excluded)
        if target not in inherited(policy, nodes):
            manual.add(target)
    else:
        manual.discard(target)
        without_exclusions = {**policy, 'exclusions': []}
        if target in inherited(without_exclusions, nodes):
            excluded.add(target)
    return {'draft_grants': grants(manual), 'draft_exclusions': grants(excluded)}


def bulk(data, nodes, region, add):
    scoped = [node for node in nodes if region is None or (node.get('region') or '') == region]
    policy = values(data)
    manual, excluded = pairs(policy['explicit_grants']), pairs(policy['exclusions'])
    keys = {n['key'] for n in scoped}
    if add:
        for node in scoped:
            if node.get('enabled', True):
                manual.update((node['key'], p) for p in node['protocols'])
                excluded.difference_update((node['key'], p) for p in node['protocols'])
    else:
        manual = {g for g in manual if g[0] not in keys} if region is not None else set()
        excluded.update(g for g in inherited({**policy, 'exclusions': []}, scoped))
    return {'draft_grants': grants(manual), 'draft_exclusions': grants(excluded)}


def summary(data, locale):
    rules = data.get('draft_rules') or []
    if not rules:
        return ()
    labels = data.get('policy_region_labels') or {}
    return (Section(tr(locale, 'policy.title'), tables=(Table(
        (tr(locale, 'policy.scope'), tr(locale, 'profile.rich.protocols')),
        tuple((tr(locale, 'policy.all') if rule['scope'] == 'all' else labels.get(rule['region_id'], rule['region_id']),
               ' · '.join(tr(locale, 'protocol.' + p) for p in rule['protocols'])) for rule in rules)),),
        lines=(tr(locale, 'policy.exclusions', count=len(data.get('draft_exclusions') or [])),)
              if data.get('draft_exclusions') else ()),)
