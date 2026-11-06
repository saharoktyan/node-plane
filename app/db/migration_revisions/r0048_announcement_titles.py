"""Announcements get an optional title in several languages: `titles_json`
holds it like `texts_json` holds the text; without it the announcement has
no title, as every earlier one."""


def announcement_titles(conn):
    if getattr(conn, 'backend_name', '') == 'postgres':
        conn.execute('ALTER TABLE backend_announcements ADD COLUMN IF NOT EXISTS titles_json TEXT')
        return
    if 'titles_json' not in {row['name'] for row in conn.execute('PRAGMA table_info(backend_announcements)').fetchall()}:
        conn.execute('ALTER TABLE backend_announcements ADD COLUMN titles_json TEXT')


def upgrade(conn):
    announcement_titles(conn)
