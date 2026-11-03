"""Traffic quotas per profile (VPN plan, section 8).

A quota counts uplink and downlink together across every node. Usage
accrues into the profile's open period from the regular traffic collection,
so the limit is soft: it can be exceeded by up to one collection interval.
The worker opens and closes periods on the profile's reset strategy and,
when a period is exhausted, either suspends access (`freeze`) or only
notifies (`notify`). A quota suspension is a reason of its own: a new
period lifts it, while an administrator's freeze stays.

Reset strategies:
- `calendar_month`: the 1st of each month, 00:00 in the installation time zone;
- `monthly_anchor`: monthly on the anchor's day and time, or on the last day
  of shorter months;
- `interval`: every `quota.interval_days` days from the anchor;
- `none`: one period without a reset.

The anchor is set when a profile first gets a quota and can be moved by an
administrator. Changing the strategy, interval, anchor or time zone takes
effect from the next reset; the current period and its usage stay. A new
limit applies to the current period at once.
"""
from calendar import monthrange
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from .authorization import AccessDenied, Account, Actor, Principal, PrincipalKind, require_permission
from .vpn_policies import effective

KEYS = ('quota.bytes', 'quota.reset', 'quota.interval_days', 'quota.on_exhausted', 'quota.throttle_kbit')
STRATEGIES = ('calendar_month', 'monthly_anchor', 'interval', 'none')
WARN_PERCENT = 80
STATE_COLUMNS = ('profile_id', 'anchor_at', 'revision')
PERIOD_COLUMNS = ('id', 'profile_id', 'period_start', 'period_end', 'uplink_bytes', 'downlink_bytes', 'warned_at',
                  'exhausted_at', 'blocked', 'opened_command', 'closed_at', 'close_reason', 'closed_by')
HISTORY = 6
# For SQL over `backend_profiles p`: the profile's access is suspended by its quota.
BLOCKED_SQL = '''EXISTS (SELECT 1 FROM backend_quota_periods q
    WHERE q.profile_id=p.id AND q.closed_at IS NULL AND q.blocked=1)'''


def size_label(value):
    """Bytes in binary units, as the bot shows traffic."""
    amount = float(value)
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if amount < 1024 or unit == 'TiB':
            return f'{amount:.1f} {unit}'
        amount /= 1024


def create_schema(conn):
    from db.migration_revisions.r0020_traffic_quotas import upgrade
    upgrade(conn)


def zone(name):
    # UTC needs no time zone database on the host.
    return timezone.utc if name == 'UTC' else ZoneInfo(name)


def _months(year, month, count):
    index = year * 12 + month - 1 + count
    return index // 12, index % 12 + 1


def _on_day(year, month, day, time, tz):
    """Wall-clock time in `tz`; a day past the month's end is its last day."""
    return datetime(year, month, min(day, monthrange(year, month)[1]), time.hour, time.minute, time.second,
                    tzinfo=tz)


def period(strategy, anchor, interval_days, tz, now):
    """UTC bounds `(start, end)` of the period that contains `now`; `end`
    is None without a reset."""
    local = now.astimezone(tz)
    if strategy == 'none':
        return anchor, None
    if strategy == 'calendar_month':
        start = datetime(local.year, local.month, 1, tzinfo=tz)
        end = datetime(*_months(local.year, local.month, 1), 1, tzinfo=tz)
    elif strategy == 'monthly_anchor':
        day, time = anchor.astimezone(tz).day, anchor.astimezone(tz).time()
        year, month = local.year, local.month
        start = _on_day(year, month, day, time, tz)
        if start > now:
            year, month = _months(year, month, -1)
            start = _on_day(year, month, day, time, tz)
        end = _on_day(*_months(year, month, 1), day, time, tz)
    elif strategy == 'interval':
        # Whole days of wall-clock time, so a reset keeps its local hour.
        base, step = anchor.astimezone(tz).replace(tzinfo=None), timedelta(days=interval_days)
        count = (local.replace(tzinfo=None) - base) // step
        start, end = ((base + step * n).replace(tzinfo=tz) for n in (count, count + 1))
    else:
        raise ValueError('unknown reset strategy')
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def blocked(conn, profile_id):
    return conn.execute('''SELECT 1 FROM backend_quota_periods
        WHERE profile_id=? AND closed_at IS NULL AND blocked=1''', (profile_id,)).fetchone() is not None


def throttle(conn, profile_id):
    """The speed in kbit/s a used-up quota limits the profile's devices to,
    or None. Raising the limit or a new period lifts it."""
    values = effective(conn, profile_id=profile_id, keys=KEYS)
    row = _open(conn, profile_id)
    if (values['quota.on_exhausted'] != 'throttle' or not values['quota.bytes'] or row is None
            or row['uplink_bytes'] + row['downlink_bytes'] < values['quota.bytes']):
        return None
    return values['quota.throttle_kbit']


def accrue(conn, profile_id, uplink, downlink):
    """Adds observed traffic to the profile's open period, if any."""
    if uplink or downlink:
        conn.execute('''UPDATE backend_quota_periods SET uplink_bytes=uplink_bytes+?,downlink_bytes=downlink_bytes+?
            WHERE profile_id=? AND closed_at IS NULL''', (uplink, downlink, profile_id))


def _open(conn, profile_id):
    return conn.execute('SELECT * FROM backend_quota_periods WHERE profile_id=? AND closed_at IS NULL',
                        (profile_id,)).fetchone()


def _period(row):
    if row is None:
        return None
    return {'id': row['id'], 'start': row['period_start'], 'end': row['period_end'],
            'uplink_bytes': row['uplink_bytes'], 'downlink_bytes': row['downlink_bytes'],
            'used_bytes': row['uplink_bytes'] + row['downlink_bytes'], 'blocked': bool(row['blocked']),
            'warned_at': row['warned_at'], 'exhausted_at': row['exhausted_at'],
            'closed_at': row['closed_at'], 'close_reason': row['close_reason']}


def overview(conn, profile_id):
    """The profile's quota settings, open period and recent closed periods."""
    values = effective(conn, profile_id=profile_id, keys=[*KEYS, 'quota.timezone'])
    state = conn.execute('SELECT anchor_at,revision FROM backend_quota_state WHERE profile_id=?',
                         (profile_id,)).fetchone()
    history = conn.execute('''SELECT * FROM backend_quota_periods WHERE profile_id=? AND closed_at IS NOT NULL
        ORDER BY period_start DESC,id DESC LIMIT ?''', (profile_id, HISTORY)).fetchall()
    return {'limit_bytes': values['quota.bytes'] or None, 'reset': values['quota.reset'],
            'interval_days': values['quota.interval_days'], 'on_exhausted': values['quota.on_exhausted'],
            'throttle_kbit': values['quota.throttle_kbit'], 'throttled': throttle(conn, profile_id),
            'timezone': values['quota.timezone'],
            'anchor_at': state['anchor_at'] if state else None, 'anchor_revision': state['revision'] if state else 0,
            # The start day as administrators enter it: a date in the quota time zone.
            'anchor_day': datetime.fromisoformat(state['anchor_at']).astimezone(
                zone(values['quota.timezone'])).date().isoformat() if state else None,
            'period': _period(_open(conn, profile_id)), 'history': [_period(row) for row in history]}


def _change_access(conn, profile_id):
    """Applies a changed suspension like an expiry: a new profile revision and
    operation, attributed to the account behind the latest operation."""
    latest = conn.execute('''SELECT actor_id FROM backend_operations WHERE profile_id=?
        ORDER BY desired_revision DESC,created_at DESC LIMIT 1''', (profile_id,)).fetchone()
    if latest is None:
        return
    from .operations import OperationRepository
    conn.execute('UPDATE backend_profiles SET desired_revision=desired_revision+1 WHERE id=?', (profile_id,))
    actor = Actor(Principal('scheduled-traffic-quota', PrincipalKind.ACCOUNT, frozenset(), latest['actor_id']),
                  Account(latest['actor_id']))
    OperationRepository.record(conn, actor, profile_id, OperationRepository.targets(conn, profile_id))


def _notify(conn, kind, profile_id, period_id, now, payload):
    from .service_notifications import queue
    queue(conn, kind=kind, profile_id=profile_id, dedup_key=f'{kind}:{period_id}', timestamp=now, payload=payload)


def _close(conn, row, reason, now, closed_by=None):
    conn.execute('''UPDATE backend_quota_periods SET closed_at=?,close_reason=?,closed_by=?,blocked=0
        WHERE id=? AND closed_at IS NULL''', (now.isoformat(), reason, closed_by, row['id']))
    if row['blocked']:
        _change_access(conn, row['profile_id'])


def _start(conn, profile_id, start, end, now, *, command=None, lifted=False):
    period_id = str(uuid4())
    conn.execute('''INSERT INTO backend_quota_periods(id,profile_id,period_start,period_end,opened_command)
        VALUES (?,?,?,?,?)''', (period_id, profile_id, start.isoformat(), end and end.isoformat(), command))
    if lifted:
        _notify(conn, 'quota_reset', profile_id, period_id, now, {})
    return _open(conn, profile_id)


def _anchor(conn, profile_id, now):
    conn.execute('''INSERT INTO backend_quota_state(profile_id,anchor_at) VALUES (?,?)
        ON CONFLICT(profile_id) DO NOTHING''', (profile_id, now.isoformat()))
    return datetime.fromisoformat(conn.execute('SELECT anchor_at FROM backend_quota_state WHERE profile_id=?',
                                               (profile_id,)).fetchone()['anchor_at'])


def _evaluate(conn, profile_id, values, row, now):
    """Warnings, exhaustion and the suspension for the open period."""
    limit, used = values['quota.bytes'], row['uplink_bytes'] + row['downlink_bytes']
    payload = {'used_bytes': used, 'limit_bytes': limit, 'period_end': row['period_end'],
               'used': size_label(used), 'limit': size_label(limit)}
    if used >= limit * WARN_PERCENT // 100 and row['warned_at'] is None:
        conn.execute('UPDATE backend_quota_periods SET warned_at=? WHERE id=?', (now.isoformat(), row['id']))
        if used < limit:
            _notify(conn, 'quota_warning', profile_id, row['id'], now, {**payload, 'percent': WARN_PERCENT})
    if used >= limit and row['exhausted_at'] is None:
        conn.execute('UPDATE backend_quota_periods SET exhausted_at=? WHERE id=?', (now.isoformat(), row['id']))
        _notify(conn, 'quota_exhausted', profile_id, row['id'], now,
                {**payload, 'suspended': values['quota.on_exhausted'] == 'freeze',
                 **({'throttled': values['quota.throttle_kbit']} if values['quota.on_exhausted'] == 'throttle' else {})})
    suspend = used >= limit and values['quota.on_exhausted'] == 'freeze'
    if suspend != bool(row['blocked']):
        conn.execute('UPDATE backend_quota_periods SET blocked=? WHERE id=?', (int(suspend), row['id']))
        _change_access(conn, profile_id)
        return True
    return False


class QuotaService:
    def __init__(self, db):
        self.db = db

    def initialize_schema(self):
        with self.db.transaction() as conn:
            create_schema(conn)

    @staticmethod
    def _candidates(conn):
        # Profiles that may have a quota, or still have a period to close.
        rows = conn.execute('''SELECT p.id FROM backend_profiles p
            WHERE NOT EXISTS (SELECT 1 FROM backend_profile_deletions d WHERE d.profile_id=p.id)
            AND (EXISTS (SELECT 1 FROM backend_vpn_policies v WHERE v.key='quota.bytes'
                    AND (v.subject_kind='installation' OR v.profile_id=p.id))
                OR EXISTS (SELECT 1 FROM backend_quota_periods q WHERE q.profile_id=p.id AND q.closed_at IS NULL))
            ORDER BY p.id''').fetchall()
        return [row['id'] for row in rows]

    @staticmethod
    def advance(conn, profile_id, tz, now):
        """One worker pass for a profile; True when its access changed."""
        values = effective(conn, profile_id=profile_id, keys=KEYS)
        row = _open(conn, profile_id)
        if not values['quota.bytes']:
            if row is not None:
                _close(conn, row, 'quota_removed', now)
                return bool(row['blocked'])
            return False
        anchor = _anchor(conn, profile_id, now)
        lifted = False
        if row is not None and row['period_end'] and datetime.fromisoformat(row['period_end']) <= now:
            _close(conn, row, 'period_end', now)
            lifted, row = bool(row['blocked']), None
        if row is None:
            start, end = period(values['quota.reset'], anchor, values['quota.interval_days'], tz, now)
            row = _start(conn, profile_id, start, end, now, lifted=lifted)
        return _evaluate(conn, profile_id, values, row, now) or lifted

    def scheduled(self, now=None):
        now = now or datetime.now(timezone.utc)
        with self.db.transaction() as conn:
            from .maintenance_gate import active
            if active(conn) or conn.execute(
                    "SELECT 1 FROM backend_backup_jobs WHERE action='restore' AND status IN ('awaiting_executor','running')"
            ).fetchone():
                return 0
            tz = zone(effective(conn, keys=['quota.timezone'])['quota.timezone'])
            return sum(self.advance(conn, profile_id, tz, now) for profile_id in self._candidates(conn))

    @staticmethod
    def _admin(actor, profile_id, conn):
        require_permission(actor, 'profiles.manage')
        if not conn.execute('''SELECT 1 FROM backend_profiles p WHERE id=? AND NOT EXISTS
                (SELECT 1 FROM backend_profile_deletions d WHERE d.profile_id=p.id)''', (profile_id,)).fetchone():
            raise AccessDenied('resource_not_found', 404)

    def reset(self, actor, key, profile_id, now=None):
        """Starts a new period now; it ends at the next regular reset. A
        replayed command key returns the period it opened."""
        try:
            key = str(UUID(key))
        except (ValueError, TypeError, AttributeError):
            raise AccessDenied('invalid_idempotency_key', 422) from None
        now = now or datetime.now(timezone.utc)
        with self.db.transaction() as conn:
            from .maintenance_gate import admit
            admit(conn)
            self._admin(actor, profile_id, conn)
            replay = conn.execute('SELECT profile_id FROM backend_quota_periods WHERE opened_command=?',
                                  (key,)).fetchone()
            if replay is not None:
                if replay['profile_id'] != profile_id:
                    raise AccessDenied('idempotency_conflict', 409)
                return overview(conn, profile_id)
            values = effective(conn, profile_id=profile_id, keys=[*KEYS, 'quota.timezone'])
            if not values['quota.bytes']:
                raise AccessDenied('quota_not_set', 409)
            row = _open(conn, profile_id)
            if row is not None:
                _close(conn, row, 'manual', now, actor.account.id)
            anchor = _anchor(conn, profile_id, now)
            end = period(values['quota.reset'], anchor, values['quota.interval_days'],
                         zone(values['quota.timezone']), now)[1]
            _start(conn, profile_id, now, end, now, command=key, lifted=bool(row and row['blocked']))
            return overview(conn, profile_id)

    def move_anchor(self, actor, profile_id, anchor_at, revision):
        """Moves the reset anchor; it applies from the next reset. A plain
        date means its midnight in the quota time zone."""
        try:
            anchor = datetime.fromisoformat(anchor_at)
        except (TypeError, ValueError):
            raise AccessDenied('invalid_input', 422) from None
        if anchor.tzinfo is None and len(anchor_at) != 10:
            raise AccessDenied('invalid_input', 422)
        with self.db.transaction() as conn:
            if anchor.tzinfo is None:
                anchor = anchor.replace(tzinfo=zone(effective(conn, keys=['quota.timezone'])['quota.timezone']))
            from .maintenance_gate import admit
            admit(conn)
            self._admin(actor, profile_id, conn)
            state = conn.execute('SELECT revision FROM backend_quota_state WHERE profile_id=?', (profile_id,)).fetchone()
            if state is None:
                # The anchor is set when the worker first applies the quota.
                raise AccessDenied('quota_not_set', 409)
            if state['revision'] != revision:
                raise AccessDenied('revision_conflict', 412)
            conn.execute('UPDATE backend_quota_state SET anchor_at=?,revision=revision+1 WHERE profile_id=?',
                         (anchor.astimezone(timezone.utc).isoformat(), profile_id))
            return overview(conn, profile_id)
