"""The installation's services in one shape for every client.

A service is of one of three kinds: `system` (what the installation itself
runs: the relay, the web cabinet and the Telegram bot; staff only), `builtin`
(VPN: its own code inside, the common shape outside) and `user` (from a
manifest, stage I). Clients list services and an account's service profiles
by this shape; a type's own screens come from the client's module for it,
or from the generic one that renders what the type declares.
"""
from datetime import datetime, timezone

from . import localized
from .authorization import CABINET_PERMISSION, RELAY_PERMISSION, AccessDenied, require_permission, service_permission
from .service_instances import RELAY_STATES, TYPES as VPN_ENGINES

# Services people hold profiles of, with the instance types that serve them.
BUILTIN = {'vpn': VPN_ENGINES}
# System services and the permission that sees them.
SYSTEM = {'relay': RELAY_PERMISSION, 'web_cabinet': CABINET_PERMISSION, 'telegram_bot': 'settings.manage'}


def _may(actor, permission):
    try:
        require_permission(actor, permission)
    except AccessDenied:
        return False
    return True


def _access(conn, actor, service):
    """Where the account stands with a service people hold profiles of."""
    from .access_requests import AccessRequestService
    from .service_profiles import services
    if actor.account.role == 'admin' or service in services(conn, actor.account.id):
        return 'active'
    if conn.execute("SELECT 1 FROM backend_access_requests WHERE account_id=? AND service=? AND status='pending'",
                    (actor.account.id, service)).fetchone():
        return 'pending'
    return 'requestable' if AccessRequestService._policy(conn)['enabled'] else 'unavailable'


def _instances(conn, types):
    marks = ','.join('?' * len(types))
    return conn.execute(f'SELECT COUNT(*) AS count FROM backend_service_instances WHERE service IN ({marks})',
                        tuple(types)).fetchone()['count']


def _system_state(conn, service):
    if service == 'relay':
        row = conn.execute('SELECT status FROM backend_relays LIMIT 1').fetchone()
        return RELAY_STATES[row['status']] if row else 'off'
    if service == 'web_cabinet':
        from .web_cabinet import STATES
        row = conn.execute('SELECT status FROM backend_web_cabinet WHERE id=1').fetchone()
        return STATES[row['status']] if row else 'off'
    from .account_channels import enabled
    from .telegram_bot_placement import STATES as BOT_STATES
    row = conn.execute('SELECT status FROM backend_telegram_bot_placement WHERE id=1').fetchone()
    if row is not None:
        return BOT_STATES[row['status']]
    return 'active' if 'telegram' in enabled(conn) else 'off'


def catalog(db, actor):
    """The services the actor may see: those people hold profiles of, with
    the account's access to each, and for staff the system services and
    how many instances each type runs."""
    require_permission(actor, 'account.self.read')
    items = []
    with db.connect() as conn:
        for service, types in BUILTIN.items():
            staff = any(_may(actor, service_permission(kind)) for kind in types)
            items.append({'service': service, 'kind': 'builtin', 'profiles': True, 'instance_types': list(types),
                          'access': _access(conn, actor, service), 'state': None,
                          'instances': _instances(conn, types) if staff else None})
        for service, permission in SYSTEM.items():
            if not _may(actor, permission):
                continue
            types = ['relay'] if service == 'relay' else []
            items.append({'service': service, 'kind': 'system', 'profiles': False, 'instance_types': types,
                          'access': None, 'state': _system_state(conn, service),
                          'instances': _instances(conn, types) if types else None})
    return items


def _vpn(conn, row, now, language):
    from .quotas import blocked, overview
    renewal = conn.execute('''SELECT status FROM backend_renewal_requests WHERE profile_id=?
        ORDER BY created_at DESC LIMIT 1''', (row['id'],)).fetchone()
    expired = bool(row['expires_at'] and datetime.fromisoformat(row['expires_at']) <= now)
    status = ('frozen' if row['frozen'] else 'expired' if expired
              else 'suspended' if blocked(conn, row['id']) else 'active')
    actions = ['devices'] + (['configs'] if status == 'active' else [])
    if row['expires_at'] and not (renewal and renewal['status'] == 'pending'):
        actions.append('renewal')
    # The traffic of the open period, when a limit holds.
    quota = overview(conn, row['id'])
    period = quota['period'] if quota['limit_bytes'] is not None else None
    return {'status': status, 'actions': actions,
            'summary': {'title': row['display_name'], 'devices': row['devices'],
                        'tariff': localized.resolve(localized.loads(row['tariff']), language) if row['tariff'] else None,
                        'expires_at': row['expires_at'],
                        'renewal': renewal['status'] if renewal else None,
                        'quota_used_bytes': period['used_bytes'] if period else None,
                        'quota_limit_bytes': quota['limit_bytes'] if period else None,
                        'quota_reset_at': period['end'] if period else None}}


def own(db, actor, now=None):
    """The account's service profiles, each with its status, a short
    summary and what its holder can do with it."""
    require_permission(actor, 'account.self.read')
    now = now or datetime.now(timezone.utc)
    items = []
    with db.connect() as conn:
        language = localized.actor_language(conn, actor)
        rows = conn.execute('''SELECT s.id, s.service, p.display_name, p.frozen, p.expires_at,
            (SELECT COUNT(*) FROM backend_devices v WHERE v.profile_id = p.id AND v.status = 'active') AS devices,
            (SELECT t.titles_json FROM backend_profile_tariffs pt JOIN backend_tariffs t ON t.id = pt.tariff_id
                WHERE pt.profile_id = p.id) AS tariff
            FROM backend_service_profiles s LEFT JOIN backend_profiles p ON p.id = s.id
            WHERE s.account_id = ?
            AND NOT EXISTS (SELECT 1 FROM backend_profile_deletions d WHERE d.profile_id = s.id)
            ORDER BY s.service, s.created_at, s.id''', (actor.account.id,)).fetchall()
        for row in rows:
            kind = 'builtin' if row['service'] in BUILTIN else 'user'
            items.append({'id': row['id'], 'service': row['service'], 'kind': kind,
                          **(_vpn(conn, row, now, language) if row['service'] == 'vpn' else
                             {'status': 'active', 'actions': [], 'summary': {}})})
    return items
