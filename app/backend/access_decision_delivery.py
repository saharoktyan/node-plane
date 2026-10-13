"""Observe committed decisions and edit known Telegram messages, never send twice."""
import json

from .authorization import AccessDenied
from .announcements import AnnouncementService


def recipients(db, principal):
    AnnouncementService._transport(principal)
    with db.connect() as conn:
        rows = conn.execute("""SELECT i.subject FROM backend_accounts a
            JOIN backend_external_identities i ON i.account_id=a.id AND i.provider='telegram'
            LEFT JOIN backend_system_settings s ON s.key='admin_request_notifications:' || a.id
            WHERE a.role='admin' AND a.status='approved' AND (s.value IS NULL OR s.value!='0')
            ORDER BY i.subject""").fetchall()
    return {'items': [int(row['subject']) for row in rows]}


def register(db, principal, request_id, values):
    AnnouncementService._transport(principal)
    if (set(values) != {'chat_id', 'message_id', 'locale'}
            or any(type(values[key]) is not int or values[key] <= 0 for key in ('chat_id', 'message_id'))
            or values['locale'] not in {'en', 'ru'}):
        raise AccessDenied('invalid_input', 422)
    with db.transaction() as conn:
        request = conn.execute('SELECT status FROM backend_access_requests WHERE id=?', (request_id,)).fetchone()
        if request is None:
            raise AccessDenied('resource_not_found', 404)
        conn.execute('''INSERT INTO backend_access_request_messages(request_id,chat_id,message_id,locale,status)
            VALUES (?,?,?,?,?) ON CONFLICT(request_id,chat_id,message_id) DO NOTHING''',
            (request_id, values['chat_id'], values['message_id'], values['locale'],
             'pending' if request['status'] == 'pending' else 'queued'))
    return {'ok': True}


def claim(db, principal):
    AnnouncementService._transport(principal)
    with db.connect() as conn:
        from .maintenance_gate import active
        if active(conn):
            return {'delivery': None}
        row = conn.execute('''SELECT m.*,r.status AS decision,d.decider_json,d.surface
            FROM backend_access_request_messages m
            JOIN backend_access_requests r ON r.id=m.request_id
            JOIN backend_access_decision_details d ON d.request_id=r.id
            WHERE m.status='queued' ORDER BY m.request_id,m.chat_id,m.message_id LIMIT 1''').fetchone()
    if row is None:
        return {'delivery': None}
    value = dict(row)
    value['decider'] = json.loads(value.pop('decider_json'))
    return {'delivery': value}


def acknowledge(db, principal, request_id, chat_id, message_id):
    AnnouncementService._transport(principal)
    with db.transaction() as conn:
        conn.execute("UPDATE backend_access_request_messages SET status='updated' WHERE request_id=? AND chat_id=? AND message_id=? AND status='queued'",
            (request_id, chat_id, message_id))
    return {'ok': True}
