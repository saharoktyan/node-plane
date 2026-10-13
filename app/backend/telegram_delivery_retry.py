"""Durable backoff after a definite Telegram 429 response."""
import math
from datetime import datetime, timedelta, timezone

from .authorization import AccessDenied


def validate_ack(status, retry_after):
    if status not in {'sent', 'failed', 'unknown', 'retry'} or (
            status == 'retry' and (type(retry_after) is not int or not 1 <= retry_after <= 86400)) or (
            status != 'retry' and retry_after is not None):
        raise AccessDenied('invalid_input', 422)


def remaining(conn, principal, timestamp=None):
    row = conn.execute('SELECT value FROM backend_system_settings WHERE key=?',
        ('telegram_retry_until:' + principal.id,)).fetchone()
    if row is None:
        return 0
    timestamp = timestamp or datetime.now(timezone.utc)
    return max(0, math.ceil((datetime.fromisoformat(row['value']) - timestamp).total_seconds()))


def defer(conn, table, delivery_id, principal, retry_after):
    assert table in {'backend_alert_deliveries', 'backend_announcement_deliveries'}
    until = datetime.now(timezone.utc) + timedelta(seconds=retry_after)
    key = 'telegram_retry_until:' + principal.id
    row = conn.execute('SELECT value FROM backend_system_settings WHERE key=?', (key,)).fetchone()
    if row:
        until = max(until, datetime.fromisoformat(row['value']))
    conn.execute('INSERT INTO backend_system_settings(key,value) VALUES (?,?) '
        'ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, until.isoformat()))
    conn.execute(f"UPDATE {table} SET status='queued',claimed_until=? WHERE id=?",
        (until.isoformat(), delivery_id))
