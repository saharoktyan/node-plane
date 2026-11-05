"""Where an account is reached: Telegram today, the web cabinet later.

A channel is separate from a sign-in identity. Telegram logins attach the
Telegram channel; delivery reads channels, never identities.
"""

CHANNELS = ('telegram', 'web')

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
