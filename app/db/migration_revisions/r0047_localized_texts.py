"""Texts in several languages: tariff titles and descriptions, announcements,
and the bot's messages to visitors.

A tariff keeps `title` as its title in the source language, for ordering
and uniqueness; `titles_json` holds every language and `description_json`
an optional description. An announcement keeps `text` in its source
language and `texts_json` with every language; system notifications are
written by the bot from their kind and leave it empty. The bot's messages
stay system settings, now stored as such values. Existing texts become texts
in the installation's language: an owner's, else an administrator's, else
English.
"""
import json


def _column(conn, table, name, kind):
    if getattr(conn, 'backend_name', '') == 'postgres':
        conn.execute(f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {name} {kind}')
        return
    if name not in {row['name'] for row in conn.execute(f'PRAGMA table_info({table})').fetchall()}:
        conn.execute(f'ALTER TABLE {table} ADD COLUMN {name} {kind}')


def tariff_columns(conn):
    _column(conn, 'backend_tariffs', 'titles_json', 'TEXT')
    _column(conn, 'backend_tariffs', 'description_json', 'TEXT')


def announcement_columns(conn):
    _column(conn, 'backend_announcements', 'texts_json', 'TEXT')


def _language(conn):
    row = conn.execute('''SELECT a.locale FROM backend_accounts a
        WHERE a.role = 'admin' AND a.locale IS NOT NULL
        ORDER BY EXISTS (SELECT 1 FROM backend_role_assignments r
            WHERE r.account_id = a.id AND r.role_id = 'owner') DESC, a.id LIMIT 1''').fetchone()
    return row['locale'] if row and row['locale'] in ('en', 'ru') else 'en'


def _plain(source, text):
    return json.dumps({'source': source, 'texts': {source: text}, 'fallback': {}}, ensure_ascii=False, sort_keys=True)


def backfill(conn):
    source = _language(conn)
    for row in conn.execute('SELECT id, title FROM backend_tariffs WHERE titles_json IS NULL').fetchall():
        conn.execute('UPDATE backend_tariffs SET titles_json = ? WHERE id = ?', (_plain(source, row['title']), row['id']))
    for key in ('access_gate_message', 'access_welcome_message'):
        row = conn.execute('SELECT value FROM backend_system_settings WHERE key = ?', (key,)).fetchone()
        if row and not row['value'].startswith('{'):
            conn.execute('UPDATE backend_system_settings SET value = ? WHERE key = ?', (_plain(source, row['value']), key))


def upgrade(conn):
    tariff_columns(conn)
    announcement_columns(conn)
    backfill(conn)
