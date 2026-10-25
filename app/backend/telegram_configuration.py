"""Privileged workstation bridge for Telegram process configuration.

Secrets only cross the authenticated SSH input stream and the private .env file.
They are never returned, stored in operation records, or included in audit text.
"""
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile
from contextlib import contextmanager
from urllib.request import urlopen

from .authorization import AccessDenied, require_permission
from .accounts import AccountService


def _env(path):
    path = Path(path)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise AccessDenied('unsafe_telegram_configuration', 409)
    return path.read_text()


def _write(path, original, values):
    path = Path(path)
    lines = [line for line in original.splitlines()
             if not any(re.match(r'\s*(?:export\s+)?' + key + r'\s*=', line) for key in values)]
    lines.extend(f'{key}={value}' for key, value in values.items())
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, delete=False) as temporary:
        name = temporary.name
        try:
            os.chmod(name, 0o600)
            temporary.write('\n'.join(lines) + '\n')
            temporary.flush()
            os.fsync(temporary.fileno())
            os.replace(name, path)
        finally:
            if os.path.exists(name):
                os.unlink(name)


def overview(db, actor, path):
    require_permission(actor, 'settings.manage')
    content = _env(path)
    with db.connect() as conn:
        rows = conn.execute("""SELECT a.id, a.revision, i.subject AS telegram_user_id, d.username,
            d.first_name, d.last_name FROM backend_accounts a
            JOIN backend_external_identities i ON i.account_id=a.id AND i.provider='telegram'
            LEFT JOIN backend_telegram_identity_details d ON d.subject=i.subject
            WHERE a.role='admin' AND a.status='approved' ORDER BY i.subject""").fetchall()
    return {'token_configured': bool(re.search(r'^BOT_TOKEN=\S+', content, re.MULTILINE)),
            'admins': [dict(row) for row in rows]}


def verify_token(token):
    if not isinstance(token, str) or not re.fullmatch(r'[1-9][0-9]{0,19}:[A-Za-z0-9_-]{20,100}', token):
        raise AccessDenied('invalid_bot_token', 422)
    try:
        with urlopen('https://api.telegram.org/bot' + token + '/getMe', timeout=10) as response:
            value = json.loads(response.read(65536))
        if value.get('ok') is not True or value.get('result', {}).get('is_bot') is not True:
            raise ValueError()
    except Exception:
        raise AccessDenied('bot_token_verification_failed', 422) from None


def change(db, actor, identities, path, request, *, verify=verify_token, restart=None):
    require_permission(actor, 'settings.manage')
    original = _env(path)
    action = request['action']
    patch = {}
    if action == 'telegram-token':
        verify(request['token'])
        patch['BOT_TOKEN'] = request['token']
    else:
        require_permission(actor, 'accounts.manage')
        changed_file = False
        try:
            with db.transaction() as conn:
                class BoundDatabase:
                    @contextmanager
                    def connect(self):
                        yield conn
                    transaction = connect
                local = BoundDatabase()
                from .identity_repository import SQLIdentityRepository
                identities = SQLIdentityRepository(local)
                conn.execute('UPDATE backend_account_guard SET revision=revision+1 WHERE id=1')
                target = identities.find_telegram_account(request['telegram_id'])
                if target is None:
                    if action != 'telegram-admin-add':
                        raise AccessDenied('resource_not_found', 404)
                    target = identities.create_account(status='approved')
                    identities.link_telegram(target.id, request['telegram_id'])
                pending = conn.execute("""SELECT id FROM backend_access_requests WHERE status='pending'
                    AND (account_id=? OR (account_id IS NULL AND telegram_subject=?))""", (target.id, str(request['telegram_id']))).fetchone()
                if pending and action == 'telegram-admin-add':
                    from .access_requests import AccessRequestService
                    AccessRequestService(local).decide(actor, pending['id'], 'approve', request['command_id'])
                if action == 'telegram-admin-add':
                    conn.execute('DELETE FROM backend_telegram_menu_refresh WHERE subject=?', (str(request['telegram_id']),))
                service = AccountService(local)
                previous = conn.execute('SELECT result_json FROM backend_account_commands WHERE actor_account_id=? AND command_key=?',
                    (actor.account.id, request['command_id'])).fetchone()
                if previous is None:
                    current = service.get(actor, target.id)
                    service.update(actor, target.id, role='admin' if action == 'telegram-admin-add' else 'member',
                        status='approved' if action == 'telegram-admin-add' else None,
                        revision=current['revision'], command_key=request['command_id'])
                elif (json.loads(previous['result_json'])['id'] != target.id
                        or json.loads(previous['result_json'])['role'] != ('admin' if action == 'telegram-admin-add' else 'member')):
                    raise AccessDenied('idempotency_conflict', 409)
                patch['ADMIN_IDS'] = ','.join(str(item['telegram_user_id'])
                    for item in overview(local, actor, path)['admins'])
                if _env(path) != original:
                    raise AccessDenied('telegram_configuration_changed', 409)
                _write(path, original, patch)
                changed_file = True
        except Exception:
            if changed_file:
                _write(path, original, {})
            raise
    if action == 'telegram-token':
        changed_file = False
        try:
            with db.transaction() as conn:
                from .maintenance_gate import admit
                admit(conn)
                conn.execute('UPDATE backend_account_guard SET revision=revision+1 WHERE id=1')
                current = conn.execute('SELECT role,status FROM backend_accounts WHERE id=?', (actor.account.id,)).fetchone()
                if not current or (current['role'], current['status']) != ('admin', 'approved'):
                    raise AccessDenied('permission_denied', 403)
                if _env(path) != original:
                    raise AccessDenied('telegram_configuration_changed', 409)
                _write(path, original, patch)
                changed_file = True
        except Exception:
            if changed_file:
                _write(path, original, {})
            raise
    if restart is None:
        restart = lambda: subprocess.run(['systemctl', 'try-restart', 'node-plane-telegram.service'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30, check=False).returncode == 0
    try:
        # Administrator routing reads current backend roles. Keep the bot FSM
        # alive so queued role changes can update already open user menus.
        restarted = restart() if action == 'telegram-token' else True
    except (OSError, subprocess.TimeoutExpired):
        restarted = False
    return {**overview(db, actor, path), 'restart_pending': not restarted}
