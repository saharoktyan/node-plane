"""What the cabinet's charts draw, read from the stored history.

Node resources come from `backend_node_metrics` (readings for a day,
hourly averages for a week or a month); traffic from `backend_traffic_daily`;
who is online from the devices that moved traffic in the last few minutes.
"""
from datetime import date, datetime, timedelta, timezone

from .authorization import AccessDenied, account_permissions, require_permission

RANGES = {'24h': ('5m', timedelta(hours=24)), '7d': ('1h', timedelta(days=7)), '30d': ('1h', timedelta(days=30))}
# A device that moved traffic this recently is online; traffic is read every two minutes.
ONLINE = timedelta(minutes=5)
POINT = ('cpu_percent', 'load1', 'cpus', 'ram_percent', 'disk_free_percent', 'net_rx_bps', 'net_tx_bps')


def _staff(actor):
    require_permission(actor, 'profiles.manage' if 'profiles.manage' in account_permissions(actor.account)
                       else 'nodes.inspect')


def node_history(db, actor, span='24h', now=None):
    """Each node's readings over a span, oldest first, and its latest one."""
    require_permission(actor, 'nodes.inspect')
    if span not in RANGES:
        raise AccessDenied('invalid_input', 422)
    resolution, length = RANGES[span]
    now = now or datetime.now(timezone.utc)
    with db.connect() as conn:
        nodes = conn.execute('SELECT key,title,flag,region FROM backend_nodes ORDER BY region,title,key').fetchall()
        rows = conn.execute(f'''SELECT node_key,at,{",".join(POINT)} FROM backend_node_metrics
            WHERE resolution=? AND at>=? ORDER BY node_key,at''',
            (resolution, (now - length).isoformat())).fetchall()
        latest = {row['node_key']: row for row in conn.execute(f'''SELECT m.node_key,m.at,{",".join(POINT)}
            FROM backend_node_metrics m WHERE m.resolution='5m' AND m.at=(SELECT MAX(x.at)
                FROM backend_node_metrics x WHERE x.node_key=m.node_key AND x.resolution='5m')''').fetchall()}
    points = {}
    for row in rows:
        points.setdefault(row['node_key'], []).append({'at': row['at'], **{key: row[key] for key in POINT}})
    point = lambda row: {'at': row['at'], **{key: row[key] for key in POINT}} if row else None
    return {'range': span, 'resolution': resolution,
            'nodes': [{'key': node['key'], 'title': node['title'], 'flag': node['flag'], 'region': node['region'],
                       'latest': point(latest.get(node['key'])), 'points': points.get(node['key'], [])}
                      for node in nodes]}


def vpn_activity(db, actor, days=30, now=None):
    """Online now, traffic per day by node, protocols, expiring profiles and
    the heaviest users of the month."""
    _staff(actor)
    now = now or datetime.now(timezone.utc)
    first = (now - timedelta(days=days - 1)).date().isoformat()
    month = now.date().replace(day=1).isoformat()
    with db.connect() as conn:
        online = conn.execute('''SELECT p.protocol, p.node_key, p.device_id, d.profile_id, pr.owner_account_id
            FROM backend_traffic_peers p JOIN backend_devices d ON d.id=p.device_id
            JOIN backend_profiles pr ON pr.id=p.profile_id
            WHERE p.last_active_at>=?''', ((now - ONLINE).isoformat(),)).fetchall()
        nodes = {row['key']: row for row in conn.execute('SELECT key,title,flag FROM backend_nodes').fetchall()}
        daily = conn.execute('''SELECT day,node_key,protocol,SUM(uplink_bytes+downlink_bytes) AS bytes
            FROM backend_traffic_daily WHERE day>=? GROUP BY day,node_key,protocol ORDER BY day''',
            (first,)).fetchall()
        top = conn.execute('''SELECT t.profile_id,SUM(t.uplink_bytes+t.downlink_bytes) AS bytes,
                p.display_name,
                (SELECT COUNT(*) FROM backend_devices v WHERE v.profile_id=t.profile_id AND v.status='active') AS devices
            FROM backend_traffic_daily t JOIN backend_profiles p ON p.id=t.profile_id
            WHERE t.day>=? GROUP BY t.profile_id,p.display_name ORDER BY bytes DESC LIMIT 10''',
            (month,)).fetchall()
        expiring = conn.execute('''SELECT p.expires_at FROM backend_profiles p
            WHERE p.expires_at IS NOT NULL AND p.expires_at>=? AND p.expires_at<?
            AND NOT EXISTS (SELECT 1 FROM backend_profile_deletions d WHERE d.profile_id=p.id)''',
            (now.isoformat(), (now + timedelta(days=28)).isoformat())).fetchall()
        renewals = conn.execute("SELECT COUNT(*) AS n FROM backend_renewal_requests WHERE status='pending'"
                                ).fetchone()['n']
    by_day = {}
    protocols = {}
    for row in daily:
        entry = by_day.setdefault(row['day'], {'day': row['day'], 'bytes': 0, 'nodes': {}})
        entry['bytes'] += row['bytes']
        entry['nodes'][row['node_key']] = entry['nodes'].get(row['node_key'], 0) + row['bytes']
        if row['day'] >= month:
            protocols[row['protocol']] = protocols.get(row['protocol'], 0) + row['bytes']
    start = date.fromisoformat(first)
    days_out = [by_day.get((start + timedelta(days=index)).isoformat(),
                           {'day': (start + timedelta(days=index)).isoformat(), 'bytes': 0, 'nodes': {}})
                for index in range(days)]
    online_nodes = {}
    for row in online:
        online_nodes.setdefault(row['node_key'], set()).add(row['device_id'])
    weeks = [0, 0, 0, 0]
    for row in expiring:
        weeks[min(3, (datetime.fromisoformat(row['expires_at']) - now).days // 7)] += 1
    return {
        'online': {'accounts': len({row['owner_account_id'] or row['profile_id'] for row in online}),
                   'devices': len({row['device_id'] for row in online}),
                   'protocols': {name: len({row['device_id'] for row in online if row['protocol'] == name})
                                 for name in ('xray', 'awg')},
                   'nodes': sorted(({'key': key, 'title': nodes[key]['title'] if key in nodes else key,
                                     'flag': nodes[key]['flag'] if key in nodes else '', 'devices': len(devices)}
                                    for key, devices in online_nodes.items()), key=lambda item: -item['devices'])},
        'days': days_out,
        'protocols': {name: protocols.get(name, 0) for name in ('xray', 'awg')},
        'expiring': {'weeks': weeks, 'renewals_pending': renewals},
        'top': [{'profile_id': row['profile_id'], 'display_name': row['display_name'],
                 'bytes': row['bytes'], 'devices': row['devices']} for row in top],
    }


def member_history(db, actor, profile_id, days=30, now=None):
    """A member's traffic per day (sent and received) and by server and
    protocol over the same days."""
    require_permission(actor, 'profiles.self.read')
    now = now or datetime.now(timezone.utc)
    first = (now - timedelta(days=days - 1)).date()
    with db.connect() as conn:
        owner = conn.execute('SELECT owner_account_id FROM backend_profiles WHERE id=?', (profile_id,)).fetchone()
        if owner is None or owner['owner_account_id'] != actor.account.id:
            raise AccessDenied('resource_not_found', 404)
        rows = conn.execute('''SELECT t.day,t.node_key,t.protocol,t.uplink_bytes,t.downlink_bytes,n.title,n.flag
            FROM backend_traffic_daily t LEFT JOIN backend_nodes n ON n.key=t.node_key
            WHERE t.profile_id=? AND t.day>=? ORDER BY t.day''', (profile_id, first.isoformat())).fetchall()
    by_day, servers = {}, {}
    for row in rows:
        entry = by_day.setdefault(row['day'], [0, 0])
        entry[0] += row['uplink_bytes']
        entry[1] += row['downlink_bytes']
        server = servers.setdefault((row['node_key'], row['protocol']), {
            'node_key': row['node_key'], 'title': row['title'] or row['node_key'], 'flag': row['flag'] or '',
            'protocol': row['protocol'], 'bytes': 0})
        server['bytes'] += row['uplink_bytes'] + row['downlink_bytes']
    out = []
    for index in range(days):
        day = (first + timedelta(days=index)).isoformat()
        up, down = by_day.get(day, (0, 0))
        out.append({'day': day, 'uplink_bytes': up, 'downlink_bytes': down})
    return {'days': out, 'servers': sorted(servers.values(), key=lambda item: -item['bytes'])}
