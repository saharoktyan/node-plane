"""Pre-approval Telegram presentation, separate from authenticated accounts."""
from dataclasses import dataclass
from datetime import datetime, timezone

from .authorization import Account, AccessDenied, Principal, PrincipalKind, validate_telegram_id


@dataclass(frozen=True)
class TelegramVisitor:
    principal: Principal
    subject: str
    details: dict

    def require(self, permission):
        if (self.principal.kind != PrincipalKind.ADAPTER
                or 'delegate.telegram' not in self.principal.scopes
                or permission not in {'account.self.read', 'account.self.preferences.write',
                                      'access_requests.self.read', 'access_requests.self.create'}
                or permission not in self.principal.scopes):
            raise AccessDenied('permission_denied')

    def public(self):
        self.require('account.self.read')
        return {'id': 'telegram:' + self.subject, 'role': 'member', 'status': 'pending',
                'account_created': False, 'permissions': sorted(self.principal.scopes & {
                    'account.self.read', 'account.self.preferences.write',
                    'access_requests.self.read', 'access_requests.self.create'}),
                'onboarding_completed': False, 'announcement_silent': False,
                'traffic_available': False, **self.details}


def visitor(db, principal, user_id):
    validate_telegram_id(user_id)
    if principal.kind != PrincipalKind.ADAPTER or 'delegate.telegram' not in principal.scopes:
        raise AccessDenied('delegation_not_allowed')
    with db.connect() as conn:
        row = conn.execute('''SELECT language_code,locale,locale_selected
            FROM backend_telegram_identity_details WHERE subject=?''', (str(user_id),)).fetchone()
    if row is None:
        raise AccessDenied('identity_not_registered', 401)
    return TelegramVisitor(principal, str(user_id), {
        **dict(row), 'locale_selected': bool(row['locale_selected'])})


def observe(db, principal, user_id, key, **details):
    """Store display/locale metadata, never allocate an account or VPN profile."""
    validate_telegram_id(user_id)
    if (principal.kind != PrincipalKind.ADAPTER
            or not {'identity.telegram.resolve', 'delegate.telegram'} <= principal.scopes):
        raise AccessDenied('permission_denied')
    from .access_requests import _command_key
    key = _command_key(key)
    with db.transaction() as conn:
        from .maintenance_gate import admit
        admit(conn)
        conn.execute('UPDATE backend_account_guard SET revision=revision WHERE id=1')
        conn.execute('''INSERT INTO backend_identity_commands(principal_id,command_key,telegram_subject)
            VALUES (?,?,?) ON CONFLICT(principal_id,command_key) DO NOTHING''',
            (principal.id, key, str(user_id)))
        saved = conn.execute('''SELECT telegram_subject FROM backend_identity_commands
            WHERE principal_id=? AND command_key=?''', (principal.id, key)).fetchone()
        if saved['telegram_subject'] != str(user_id):
            raise AccessDenied('idempotency_conflict', 409)
        language = details.get('language_code')
        locale = language[:2].lower() if (language or '')[:2].lower() in {'ru', 'en'} else 'en'
        conn.execute('''INSERT INTO backend_telegram_identity_details
            (subject,username,first_name,last_name,language_code,locale,updated_at)
            VALUES (?,?,?,?,?,?,?) ON CONFLICT(subject) DO UPDATE SET
            username=excluded.username,first_name=excluded.first_name,last_name=excluded.last_name,
            language_code=excluded.language_code,
            locale=COALESCE(backend_telegram_identity_details.locale,excluded.locale),
            updated_at=excluded.updated_at''',
            (str(user_id), details.get('username'), details.get('first_name'), details.get('last_name'),
             language, locale, datetime.now(timezone.utc).isoformat()))
        row = conn.execute('''SELECT a.id,a.role,a.status FROM backend_accounts a
            JOIN backend_external_identities i ON i.account_id=a.id
            WHERE i.provider='telegram' AND i.subject=?''', (str(user_id),)).fetchone()
        account = Account(row['id'], row['role'], row['status']) if row else None
        if account and account.status == 'approved':
            from .profiles import ProfileRepository
            ProfileRepository.ensure_account_profile_in_transaction(conn, account.id)
    return account or visitor(db, principal, user_id)


def set_locale(db, current, locale):
    current.require('account.self.preferences.write')
    with db.transaction() as conn:
        from .maintenance_gate import admit
        admit(conn)
        conn.execute('''UPDATE backend_telegram_identity_details
            SET locale=?,locale_selected=1,updated_at=? WHERE subject=?''',
            (locale, datetime.now(timezone.utc).isoformat(), current.subject))
    return visitor(db, current.principal, int(current.subject)).public()
