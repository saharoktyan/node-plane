"""Linking Workstation to an installation through the relay, like
`gh auth login`: Workstation makes a key and asks for a link; the
controller answers with a short code and the cabinet's address; a staff
member signs in to the cabinet and confirms the code; the key then belongs
to their account and acts with its roles. Workstation learns the outcome by
asking about its request, whose id only it knows.
"""
from datetime import datetime, timedelta, timezone
import secrets
from uuid import uuid4

from . import client_keys
from .authorization import AccessDenied, PrincipalKind

TTL = timedelta(minutes=10)
# Requests a source may make in a quarter of an hour, and pending ones in all.
PER_SOURCE = 5
WINDOW = timedelta(minutes=15)
PENDING = 100
# No 0/O, 1/I/L: the code is read off one screen and typed into another.
ALPHABET = 'ABCDEFGHJKMNPQRSTUVWXYZ23456789'


def _now():
    return datetime.now(timezone.utc)


def code_key(value):
    """A typed code as stored: case, spaces and the dash do not matter."""
    return ''.join(c for c in str(value).upper() if c.isalnum())[:16]


def shown(code):
    return code[:4] + '-' + code[4:]


def _staff(actor):
    if actor.principal.kind != PrincipalKind.ACCOUNT:
        # A key is linked only by a person signed in, never through Telegram.
        raise AccessDenied('pairing_not_through_adapter', 403)
    if actor.account.role != 'admin':
        raise AccessDenied('permission_denied')


def request(db, public_key, name, *, source, now=None):
    now = now or _now()
    key = client_keys.public_key(public_key)
    name = ' '.join(str(name or '').split())[:64] or 'Workstation'
    with db.transaction() as conn:
        conn.execute('DELETE FROM backend_workstation_pairings WHERE expires_at<?', ((now - timedelta(days=1)).isoformat(),))
        recent = conn.execute('SELECT COUNT(*) AS count FROM backend_workstation_pairings WHERE source=? AND created_at>?',
                              (source, (now - WINDOW).isoformat())).fetchone()['count']
        pending = conn.execute("SELECT COUNT(*) AS count FROM backend_workstation_pairings WHERE status='pending' AND expires_at>?",
                               (now.isoformat(),)).fetchone()['count']
        if recent >= PER_SOURCE or pending >= PENDING:
            raise AccessDenied('pairing_rate_limited', 429)
        if conn.execute('SELECT 1 FROM backend_client_keys WHERE public_key=?', (key,)).fetchone():
            raise AccessDenied('client_key_exists', 409)
        while True:
            code = ''.join(secrets.choice(ALPHABET) for _ in range(8))
            if not conn.execute('SELECT 1 FROM backend_workstation_pairings WHERE code=?', (code,)).fetchone():
                break
        pairing_id = uuid4().hex
        expires = now + TTL
        conn.execute('''INSERT INTO backend_workstation_pairings(id,code,public_key,name,source,status,created_at,expires_at)
            VALUES (?,?,?,?,?,'pending',?,?)''', (pairing_id, code, key, name, source, now.isoformat(), expires.isoformat()))
        from .web_cabinet import origin
        url = origin(conn)
    return {'id': pairing_id, 'code': shown(code), 'expires_at': expires.isoformat(),
            'url': url + '/pair?code=' + shown(code) if url else None}


def status(db, pairing_id, *, now=None):
    """What became of a request: pending, approved (with the key's id), rejected or expired."""
    now = now or _now()
    with db.connect() as conn:
        row = conn.execute('SELECT * FROM backend_workstation_pairings WHERE id=?', (str(pairing_id),)).fetchone()
    if row is None:
        raise AccessDenied('resource_not_found', 404)
    state = 'expired' if row['status'] == 'pending' and row['expires_at'] <= now.isoformat() else row['status']
    return {'status': state, 'key_id': row['key_id'] if state == 'approved' else None, 'expires_at': row['expires_at']}


def _pending(conn, code, now):
    row = conn.execute('SELECT * FROM backend_workstation_pairings WHERE code=?', (code_key(code),)).fetchone()
    if row is None or row['status'] != 'pending' or row['expires_at'] <= now.isoformat():
        raise AccessDenied('pairing_not_found', 404)
    return row


def preview(db, actor, code, *, now=None):
    """What the staff member confirms: the device's name, where it asked from, when."""
    _staff(actor)
    with db.connect() as conn:
        row = _pending(conn, code, now or _now())
    return {'code': shown(row['code']), 'name': row['name'], 'source': row['source'],
            'created_at': row['created_at'], 'expires_at': row['expires_at']}


def decide(db, actor, code, approve, *, now=None):
    _staff(actor)
    now = now or _now()
    with db.transaction() as conn:
        row = _pending(conn, code, now)
        if approve:
            key_id = client_keys.register(conn, 'workstation', row['public_key'], name=row['name'],
                                          account_id=actor.account.id, now=now)
            conn.execute("UPDATE backend_workstation_pairings SET status='approved', account_id=?, key_id=? WHERE id=?",
                         (actor.account.id, key_id, row['id']))
        else:
            conn.execute("UPDATE backend_workstation_pairings SET status='rejected', account_id=? WHERE id=?",
                         (actor.account.id, row['id']))
    return {'status': 'approved' if approve else 'rejected', 'name': row['name']}


def listed(conn, account_id):
    return [client_keys.public(row) for row in conn.execute(
        """SELECT * FROM backend_client_keys WHERE account_id=? AND kind='workstation' AND revoked_at IS NULL
        ORDER BY created_at""", (account_id,))]


def count(conn, account_id):
    return conn.execute("""SELECT COUNT(*) AS count FROM backend_client_keys
        WHERE account_id=? AND kind='workstation' AND revoked_at IS NULL""", (account_id,)).fetchone()['count']
