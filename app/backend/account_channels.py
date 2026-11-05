"""Where an account is reached: Telegram today, the web cabinet later.

A channel is separate from a sign-in identity. Telegram logins attach the
Telegram channel; delivery reads channels, never identities.
"""

import json

CHANNELS = ('telegram', 'web')
# Installation setting: the channels this installation runs. Without it the
# installation is one from before channels, which always had the bot.
SETTING = 'channels.enabled'
DEFAULT = frozenset({'telegram'})

# Joins the enabled Telegram channel of account `a` as `i`; `i.address` is the chat.
TELEGRAM_JOIN = "JOIN backend_account_channels i ON i.account_id=a.id AND i.channel='telegram' AND i.enabled=1"


def attach(conn, account_id, channel, address):
    if channel not in CHANNELS:
        raise ValueError('unknown channel')
    conn.execute('''INSERT INTO backend_account_channels(account_id, channel, address) VALUES (?, ?, ?)
        ON CONFLICT(account_id, channel) DO UPDATE SET address = excluded.address''',
        (account_id, channel, str(address)))


def detach(conn, account_id, channel=None):
    if channel is None:
        conn.execute('DELETE FROM backend_account_channels WHERE account_id = ?', (account_id,))
    else:
        conn.execute('DELETE FROM backend_account_channels WHERE account_id = ? AND channel = ?',
                     (account_id, channel))


def enabled(conn) -> frozenset:
    row = conn.execute('SELECT value FROM backend_system_settings WHERE key = ?', (SETTING,)).fetchone()
    return DEFAULT if row is None else frozenset(json.loads(row['value']))


def set_enabled(conn, channels) -> frozenset:
    """Turn installation channels on or off; what Telegram still had to send stays in the Inbox."""
    channels = frozenset(channels)
    if not channels <= set(CHANNELS):
        raise ValueError('unknown channel')
    conn.execute('''INSERT INTO backend_system_settings(key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value''', (SETTING, json.dumps(sorted(channels))))
    if 'telegram' not in channels:
        conn.execute("UPDATE backend_announcement_deliveries SET status='inbox_only' WHERE status='queued'")
        conn.execute("UPDATE backend_alert_deliveries SET status='skipped' WHERE status='queued'")
    return channels


def telegram_chat(conn, row):
    """The Telegram chat of a recipient row, or None when it is reached only in the Inbox."""
    return row['subject'] if row['subject'] and 'telegram' in enabled(conn) else None
