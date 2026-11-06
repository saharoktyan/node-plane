"""Passkeys (WebAuthn, see r0042).

The relying party is the web cabinet, served by the relay, so a passkey is
made for the relay's domain and works only there. The controller issues each
challenge, keeps it for one use, and checks every signature itself; the
relay only carries the ceremony. The user handle is the account ID, so a
changed login breaks no passkey, and signing in needs no login.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import secrets
from uuid import uuid4

import webauthn
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.exceptions import WebAuthnException
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria, AuthenticatorTransport, PublicKeyCredentialDescriptor,
    ResidentKeyRequirement, UserVerificationRequirement,
)

from . import sessions
from .authorization import AccessDenied

CHALLENGE_TTL = timedelta(minutes=5)
# Sign-in ceremonies in progress at once, so unauthenticated requests cannot fill the table.
PENDING_SIGN_INS = 1000
PASSKEYS_PER_ACCOUNT = 20
NAME_LENGTH = 64
RP_NAME = 'Node Plane'


def _now():
    return datetime.now(timezone.utc)


def relying_party(conn):
    """The cabinet's domain and origin, from the relay's settings."""
    row = conn.execute("SELECT domain, port, tls FROM backend_relays WHERE status != 'removing' "
                       'ORDER BY created_at LIMIT 1').fetchone()
    if row is None:
        raise AccessDenied('passkeys_unavailable', 409)
    # Behind a reverse proxy the relay's own port is not the public one.
    public_port = 443 if row['tls'] == 'proxy' else int(row['port'])
    origin = f"https://{row['domain']}" + ('' if public_port == 443 else f':{public_port}')
    return row['domain'], origin


def _challenge(conn, kind, account_id, now):
    rp_id, origin = relying_party(conn)
    conn.execute('DELETE FROM backend_webauthn_challenges WHERE expires_at<=?', (now.isoformat(),))
    if kind == 'sign_in' and conn.execute(
            "SELECT COUNT(*) AS count FROM backend_webauthn_challenges WHERE kind='sign_in'").fetchone()['count'] \
            >= PENDING_SIGN_INS:
        raise AccessDenied('sign_in_rate_limited', 429)
    challenge_id, challenge = uuid4().hex, secrets.token_bytes(32)
    conn.execute('''INSERT INTO backend_webauthn_challenges(id, kind, account_id, rp_id, origin, challenge, expires_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)''', (challenge_id, kind, account_id, rp_id, origin,
                                         bytes_to_base64url(challenge), (now + CHALLENGE_TTL).isoformat()))
    return challenge_id, challenge, rp_id


def _take(conn, challenge_id, kind, account_id, now):
    """The challenge, used up whatever the outcome."""
    row = conn.execute('SELECT * FROM backend_webauthn_challenges WHERE id=? AND kind=?',
                       (challenge_id, kind)).fetchone() if isinstance(challenge_id, str) else None
    if row is not None:
        conn.execute('DELETE FROM backend_webauthn_challenges WHERE id=?', (challenge_id,))
    if row is None or row['account_id'] != account_id or datetime.fromisoformat(row['expires_at']) <= now:
        raise AccessDenied('passkey_challenge_invalid', 400)
    return row


def _options(options):
    return json.loads(webauthn.options_to_json(options))


def registration_options(db, account_id, now=None):
    """Options for `navigator.credentials.create`, with the challenge's ID to send back."""
    now = now or _now()
    from .logins import current as current_login
    with db.transaction() as conn:
        if count(conn, account_id) >= PASSKEYS_PER_ACCOUNT:
            raise AccessDenied('passkey_limit', 409)
        challenge_id, challenge, rp_id = _challenge(conn, 'register', account_id, now)
        login = current_login(conn, account_id)
        existing = conn.execute('SELECT id, transports_json FROM backend_passkeys WHERE account_id=? AND rp_id=?',
                                (account_id, rp_id)).fetchall()
    options = webauthn.generate_registration_options(
        rp_id=rp_id, rp_name=RP_NAME, user_id=account_id.encode(), user_name=login or 'Node Plane account',
        user_display_name=login or 'Node Plane account', challenge=challenge,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.REQUIRED, user_verification=UserVerificationRequirement.REQUIRED),
        exclude_credentials=[_descriptor(row) for row in existing])
    return {'challenge_id': challenge_id, 'options': _options(options)}


def _descriptor(row):
    transports = []
    for value in json.loads(row['transports_json']):
        try:
            transports.append(AuthenticatorTransport(value))
        except ValueError:
            pass
    return PublicKeyCredentialDescriptor(id=base64url_to_bytes(row['id']), transports=transports or None)


def register(db, account_id, challenge_id, credential, name=None, now=None):
    """Keeps the passkey the browser made for the challenge."""
    now = now or _now()
    with db.transaction() as conn:
        row = _take(conn, challenge_id, 'register', account_id, now)
    try:
        verified = webauthn.verify_registration_response(
            credential=credential, expected_challenge=base64url_to_bytes(row['challenge']),
            expected_rp_id=row['rp_id'], expected_origin=row['origin'], require_user_verification=True)
    except (WebAuthnException, ValueError, TypeError, KeyError):
        raise AccessDenied('passkey_invalid', 400) from None
    credential_id = bytes_to_base64url(verified.credential_id)
    transports = (credential.get('response') or {}).get('transports') if isinstance(credential, dict) else None
    transports = [value for value in transports if isinstance(value, str)][:8] if isinstance(transports, list) else []
    name = (name or '').strip()[:NAME_LENGTH] or 'Passkey'
    with db.transaction() as conn:
        if conn.execute('SELECT 1 FROM backend_passkeys WHERE id=?', (credential_id,)).fetchone():
            raise AccessDenied('passkey_exists', 409)
        if count(conn, account_id) >= PASSKEYS_PER_ACCOUNT:
            raise AccessDenied('passkey_limit', 409)
        conn.execute('''INSERT INTO backend_passkeys(id, account_id, rp_id, public_key, sign_count, transports_json,
            name, aaguid, backed_up, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (credential_id, account_id, row['rp_id'], bytes_to_base64url(verified.credential_public_key),
             verified.sign_count, json.dumps(transports), name, str(verified.aaguid),
             int(bool(verified.credential_backed_up)), now.isoformat()))
        return _public(conn.execute('SELECT * FROM backend_passkeys WHERE id=?', (credential_id,)).fetchone())


def sign_in_options(db, *, source, now=None):
    """Options for `navigator.credentials.get`: any passkey of the cabinet, picked on the device."""
    now = now or _now()
    with db.transaction() as conn:
        sessions.limited(conn, (sessions.source_scope(source),), now)
        challenge_id, challenge, rp_id = _challenge(conn, 'sign_in', None, now)
    options = webauthn.generate_authentication_options(
        rp_id=rp_id, challenge=challenge, user_verification=UserVerificationRequirement.REQUIRED)
    return {'challenge_id': challenge_id, 'options': _options(options)}


def sign_in(db, challenge_id, credential, *, device, source, now=None):
    """Tokens of a new session, or `sign_in_failed` for any passkey that does not verify."""
    now = now or _now()
    scopes = (sessions.source_scope(source),)
    with db.transaction() as conn:
        sessions.limited(conn, scopes, now)
        challenge = _take(conn, challenge_id, 'sign_in', None, now)
        credential_id = credential.get('id') if isinstance(credential, dict) else None
        row = conn.execute(f'''SELECT k.* FROM backend_passkeys k JOIN backend_accounts a ON a.id = k.account_id
            WHERE k.id = ? AND k.rp_id = ? AND {sessions.ACTIVE_ACCOUNT_SQL}''',
            (credential_id, challenge['rp_id'])).fetchone() if isinstance(credential_id, str) else None
    verified = None
    if row is not None:
        handle = ((credential.get('response') or {}).get('userHandle'))
        try:
            if handle and base64url_to_bytes(handle) != row['account_id'].encode():
                raise ValueError('another account')
            verified = webauthn.verify_authentication_response(
                credential=credential, expected_challenge=base64url_to_bytes(challenge['challenge']),
                expected_rp_id=challenge['rp_id'], expected_origin=challenge['origin'],
                credential_public_key=base64url_to_bytes(row['public_key']),
                credential_current_sign_count=row['sign_count'], require_user_verification=True)
        except (WebAuthnException, ValueError, TypeError, KeyError):
            verified = None
    with db.transaction() as conn:
        if verified is None:
            sessions.failed(conn, scopes, now)
            refused = True
        else:
            refused = False
            conn.execute('UPDATE backend_passkeys SET sign_count=?, backed_up=?, used_at=? WHERE id=?',
                         (verified.new_sign_count, int(bool(verified.credential_backed_up)), now.isoformat(),
                          row['id']))
            result = sessions.open_session(conn, row['account_id'], 'passkey', device, now)
    if refused:
        # Outside the transaction, so the counted attempt stays.
        raise AccessDenied('sign_in_failed', 401)
    return result


def _public(row):
    return {'id': row['id'], 'name': row['name'], 'created_at': row['created_at'], 'used_at': row['used_at'],
            'backed_up': bool(row['backed_up'])}


def count(conn, account_id):
    return conn.execute('SELECT COUNT(*) AS count FROM backend_passkeys WHERE account_id=?',
                        (account_id,)).fetchone()['count']


def listed(conn, account_id):
    return [_public(row) for row in conn.execute(
        'SELECT * FROM backend_passkeys WHERE account_id=? ORDER BY created_at', (account_id,)).fetchall()]


def remove(conn, account_id, passkey_id):
    if conn.execute('DELETE FROM backend_passkeys WHERE id=? AND account_id=?',
                    (passkey_id, account_id)).rowcount == 0:
        raise AccessDenied('resource_not_found', 404)


def remove_all(conn, account_id):
    conn.execute('DELETE FROM backend_passkeys WHERE account_id=?', (account_id,))

