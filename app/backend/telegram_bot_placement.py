"""The Telegram bot on a node instead of the controller.

The controller runs the bot itself (the unit `node-plane-telegram`) until
an administrator moves it to a node with an agent. The node needs the
relay: the bot reaches the API through it and signs every request with a
key of its own, made on the node, whose public half the controller pins.
The worker hands the node this release's bot package with the token from
`shared/.env`, then stops the controller's bot. Moving the bot back takes
it off the node, revokes its key and starts the controller's bot again.
The bot's code is the controller's, so it updates with the controller: the
worker applies it again when it starts.
"""
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import re

from config import APP_SEMVER, INSTALL_ROOT, SHARED_ROOT
from .authorization import AccessDenied, require_permission

PERMISSION = 'settings.manage'
RETRY = timedelta(seconds=30)
STATES = {'applying': 'changes_pending', 'active': 'active', 'failed': 'failed', 'removing': 'removing'}


def _now():
    return datetime.now(timezone.utc)


def _row(conn):
    return conn.execute('SELECT * FROM backend_telegram_bot_placement WHERE id=1').fetchone()


def _relay(conn):
    return conn.execute("SELECT * FROM backend_relays WHERE status != 'removing' ORDER BY created_at LIMIT 1").fetchone()


def _telegram(conn):
    from .account_channels import enabled
    return 'telegram' in enabled(conn)


def placed(conn):
    """The node the bot runs on, or None while the controller runs it."""
    row = _row(conn)
    return row['node_key'] if row is not None else None


def public(row):
    if row is None:
        return None
    return {'node_key': row['node_key'], 'status': row['status'], 'state': STATES[row['status']],
            'revision': row['revision'], 'applied_revision': row['applied_revision'],
            'applied_version': row['applied_version'], 'version': APP_SEMVER,
            'error_code': row['error_code'], 'updated_at': row['updated_at']}


def _state(conn):
    """What the bot's page shows: where it runs and whether it can move."""
    relay = _relay(conn)
    return {'placement': public(_row(conn)), 'telegram_enabled': _telegram(conn),
            'relay_node_key': relay['node_key'] if relay else None}


def reapply(conn):
    """The token changed or the bot was turned on again: the node gets it.
    Returns whether the bot runs on a node."""
    row = _row(conn)
    if row is None:
        return False
    if row['status'] != 'removing':
        conn.execute('''UPDATE backend_telegram_bot_placement SET status='applying', revision=revision+1,
            error_code=NULL, attempted_at=NULL, updated_at=? WHERE id=1''', (_now().isoformat(),))
    return True


def _token(path):
    from .telegram_bot_setup import _values
    try:
        return _values(Path(path).read_text()).get('BOT_TOKEN', '').strip()
    except OSError:
        return ''


def _relay_ca():
    """The private CA the relay's certificate comes from, when it has one."""
    path = os.environ.get('NODE_PLANE_RELAY_CA_FILE', '').strip()
    return Path(path).read_text() if path else ''


class TelegramBotPlacementService:
    def __init__(self, db, driver=None, *, local=None, env_path=None, package=None):
        self.db, self.driver = db, driver
        self.env_path = Path(env_path or Path(SHARED_ROOT) / '.env')
        # The worker's host side: the controller's bot and the bot's package.
        self._local, self._package = local, package

    @property
    def local(self):
        if self._local is None:
            from .telegram_bot_setup import SystemdBot
            self._local = SystemdBot(INSTALL_ROOT, SHARED_ROOT)
        return self._local

    def package(self, version):
        if self._package is None:
            from .bot_package import build
            self._package = build
        return self._package(version)

    def initialize_schema(self):
        from db.migration_revisions.r0052_telegram_bot_placement import create_schema
        with self.db.transaction() as conn:
            create_schema(conn)

    def get(self, actor):
        require_permission(actor, PERMISSION)
        with self.db.connect() as conn:
            return _state(conn)

    def place(self, actor, node_key):
        """Run the bot on this node."""
        require_permission(actor, PERMISSION)
        now = _now().isoformat()
        with self.db.transaction() as conn:
            if not _telegram(conn):
                raise AccessDenied('telegram_disabled', 409)
            if _relay(conn) is None:
                raise AccessDenied('telegram_bot_needs_relay', 409)
            row = _row(conn)
            if row is not None and row['status'] == 'removing':
                raise AccessDenied('telegram_bot_removing', 409)
            if row is not None and row['node_key'] != node_key:
                # The bot comes back to the controller first.
                raise AccessDenied('telegram_bot_node_change', 409)
            from .nodes import NodeService, agent_installed
            node = conn.execute(NodeService._select() + ' WHERE n.key=?', (node_key,)).fetchone()
            if node is None:
                raise AccessDenied('resource_not_found', 404)
            if not agent_installed(node):
                raise AccessDenied('telegram_bot_node_without_agent', 409)
            if row is None:
                conn.execute('''INSERT INTO backend_telegram_bot_placement(id,node_key,status,revision,actor_id,
                    created_at,updated_at) VALUES (1,?,'applying',1,?,?,?)''', (node_key, actor.account.id, now, now))
            else:
                conn.execute('''UPDATE backend_telegram_bot_placement SET status='applying', revision=revision+1,
                    error_code=NULL, attempted_at=NULL, actor_id=?, updated_at=? WHERE id=1''',
                    (actor.account.id, now))
            return _state(conn)

    def remove(self, actor):
        """Bring the bot back to the controller."""
        require_permission(actor, PERMISSION)
        with self.db.transaction() as conn:
            if _row(conn) is None:
                raise AccessDenied('resource_not_found', 404)
            conn.execute('''UPDATE backend_telegram_bot_placement SET status='removing', error_code=NULL,
                attempted_at=NULL, actor_id=?, updated_at=? WHERE id=1''', (actor.account.id, _now().isoformat()))
            return _state(conn)

    def recover(self):
        """At worker start: hand the node this release's bot; a node that has
        it already only restarts nothing."""
        with self.db.transaction() as conn:
            conn.execute("""UPDATE backend_telegram_bot_placement SET status='applying', attempted_at=NULL
                WHERE status IN ('active','failed')""")

    def _pin(self, public_key, node_key):
        """The node's key becomes the bot's only one."""
        from . import client_keys
        key = client_keys.encode(public_key)
        now = _now().isoformat()
        with self.db.transaction() as conn:
            existing = conn.execute("SELECT id FROM backend_client_keys WHERE kind='bot' AND public_key=?",
                                    (key,)).fetchone()
            if existing is not None:
                key_id = existing['id']
                conn.execute('UPDATE backend_client_keys SET revoked_at=NULL WHERE id=?', (key_id,))
            else:
                key_id = client_keys.register(conn, 'bot', key, name=f'Telegram bot on {node_key}')
            conn.execute("""UPDATE backend_client_keys SET revoked_at=? WHERE kind='bot' AND id<>?
                AND revoked_at IS NULL""", (now, key_id))
            conn.execute('UPDATE backend_telegram_bot_placement SET key_id=? WHERE id=1', (key_id,))
        return key_id

    def _apply(self, row):
        if row['status'] == 'removing':
            self.driver.remove_bot(row['node_key'])
            with self.db.transaction() as conn:
                conn.execute("UPDATE backend_client_keys SET revoked_at=? WHERE kind='bot' AND revoked_at IS NULL",
                             (_now().isoformat(),))
                telegram = _telegram(conn)
            if telegram and not self.local.install():
                raise AccessDenied('telegram_bot_local_start_failed', 409)
            return
        token = _token(self.env_path)
        with self.db.connect() as conn:
            relay = _relay(conn)
        if not token:
            raise AccessDenied('telegram_disabled', 409)
        if relay is None:
            raise AccessDenied('telegram_bot_needs_relay', 409)
        from .relays import public as relay_public
        key_id = self._pin(self.driver.bot_key(row['node_key'])['public_key'], row['node_key'])
        package = self.package(APP_SEMVER)
        self.driver.apply_bot(row['node_key'], str(package), APP_SEMVER, token=token,
                              api_url=relay_public(relay)['url'], relay_ca=_relay_ca(), key_id=key_id)
        # Both bots poll for a moment; Telegram hands each update to one.
        self.local.remove()

    def run_one(self):
        now = _now()
        with self.db.connect() as conn:
            row = conn.execute('''SELECT * FROM backend_telegram_bot_placement WHERE status IN ('applying','removing')
                AND (attempted_at IS NULL OR attempted_at<=?)''', ((now - RETRY).isoformat(),)).fetchone()
        if row is None or self.driver is None:
            return False
        import grpc
        from .bot_package import PackageError
        error = None
        try:
            self._apply(row)
        except AccessDenied as refused:
            error = (refused.code, True)
        except PackageError as failure:
            error = (str(failure), True)
        except grpc.RpcError as failure:
            details = failure.details() or ''
            if failure.code() == grpc.StatusCode.INVALID_ARGUMENT and re.fullmatch(r'bot_[a-z0-9_]+', details):
                error = (details, True)
            elif failure.code() == grpc.StatusCode.FAILED_PRECONDITION and details.startswith('bot_install_failed'):
                error = ('bot_install_failed', True)
            elif failure.code() in (grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED):
                error = ('agent_unavailable', False)
            else:
                error = ('telegram_bot_apply_failed', True)
        except Exception:
            error = ('telegram_bot_apply_failed', True)
        with self.db.transaction() as conn:
            current = _row(conn)
            if current is None or (current['status'], current['revision']) != (row['status'], row['revision']):
                return True
            stamp = now.isoformat()
            if error is not None:
                code, final = error
                status = 'failed' if final and row['status'] == 'applying' else row['status']
                conn.execute('UPDATE backend_telegram_bot_placement SET status=?, error_code=?, attempted_at=? WHERE id=1',
                             (status, code, stamp))
            elif row['status'] == 'removing':
                conn.execute('DELETE FROM backend_telegram_bot_placement WHERE id=1')
            else:
                conn.execute('''UPDATE backend_telegram_bot_placement SET status='active', applied_revision=revision,
                    applied_version=?, error_code=NULL, attempted_at=?, updated_at=? WHERE id=1''',
                    (APP_SEMVER, stamp, stamp))
        return True
