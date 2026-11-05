"""Turn the Telegram bot on or off on an installed controller.

Enabling writes the token, gives the bot its adapter credential, turns the
Telegram channel on and starts its service. Disabling stops and removes the
service, revokes the bot's adapter credentials and turns the channel off.
Accounts, their Telegram logins and channels stay as they are.
"""
from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess

from .account_channels import enabled, set_enabled
from .credentials import ADAPTER_SCOPES, CredentialService
from .authorization import PrincipalKind

UNIT = 'node-plane-telegram.service'
UNIT_PATH = Path('/etc/systemd/system') / UNIT
TOKEN_KEY = 'NODE_PLANE_BACKEND_ADAPTER_TOKEN_FILE'


class SystemdBot:
    """The host side: the bot's systemd unit, installed from the current release."""

    def __init__(self, base_dir, shared_dir):
        self.base_dir = Path(base_dir)
        self.shared_dir = Path(shared_dir)

    def install(self):
        script = self.base_dir / 'current/scripts/install_telegram_client_systemd.sh'
        result = subprocess.run(['bash', str(script), str(self.base_dir), str(self.shared_dir), '--activate'],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180, check=False)
        return result.returncode == 0

    def remove(self):
        subprocess.run(['systemctl', 'disable', '--now', UNIT], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=60, check=False)
        UNIT_PATH.unlink(missing_ok=True)
        subprocess.run(['systemctl', 'daemon-reload'], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=60, check=False)
        return not UNIT_PATH.exists()


def _values(content):
    return dict(line.split('=', 1) for line in content.splitlines()
                if '=' in line and not line.lstrip().startswith('#'))


def _token_file(path, values):
    configured = values.get(TOKEN_KEY, '').strip()
    return Path(configured) if configured else Path(path).parent / 'data/telegram-adapter.token'


def _write_secret(path, secret):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as stream:
        stream.write(secret + '\n')
        stream.flush()
        os.fsync(stream.fileno())


def enable(db, path, token, *, read, write, system):
    """Start the bot with this (already verified) token. Returns whether its service started."""
    original = read(path)
    values = _values(original)
    token_file = _token_file(path, values)
    if not token_file.is_file():
        token_file.unlink(missing_ok=True)
        _, secret = CredentialService(db).issue(PrincipalKind.ADAPTER, ADAPTER_SCOPES)
        _write_secret(token_file, secret)
    write(path, original, {'BOT_TOKEN': token, TOKEN_KEY: str(token_file)})
    with db.transaction() as conn:
        set_enabled(conn, enabled(conn) | {'telegram'})
    return system.install()


def disable(db, path, *, read, write, system):
    """Stop the bot and take its access away. Returns whether its service is gone."""
    with db.transaction() as conn:
        set_enabled(conn, enabled(conn) - {'telegram'})
        conn.execute("UPDATE backend_credentials SET revoked_at=? WHERE kind='adapter' AND revoked_at IS NULL",
                     (datetime.now(timezone.utc).isoformat(),))
    removed = system.remove()
    original = read(path)
    token_file = _token_file(path, _values(original))
    token_file.unlink(missing_ok=True)
    write(path, original, {'BOT_TOKEN': '', TOKEN_KEY: ''})
    return removed
