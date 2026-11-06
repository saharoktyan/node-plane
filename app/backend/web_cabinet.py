"""The web cabinet: a system service apart from the relay.

The relay only carries requests to the API; the cabinet is the app people
open. It is served either by the relay's node, on the relay's domain beside
the API (`relay`), or by the controller's own server (`local`), which then
listens on the network. Its files are the controller's build, so they
update with the controller: the worker applies them again when it starts.
The cabinet's public address is the passkeys' domain and where invite links
lead.
"""
from datetime import datetime, timedelta, timezone
import ipaddress
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit

from config import APP_ROOT, APP_SEMVER, SHARED_ROOT
from .authorization import CABINET_PERMISSION, AccessDenied, require_permission

MODES = ('relay', 'local')
RETRY = timedelta(seconds=30)
STATES = {'applying': 'changes_pending', 'active': 'active', 'failed': 'failed', 'removing': 'removing'}
_HOST = re.compile(r'(?=.{1,253}\Z)(?!-)[a-z0-9-]{1,63}(?<!-)(?:\.(?!-)[a-z0-9-]{1,63}(?<!-))*\Z')


def build_dir():
    """The cabinet's files in the running controller release."""
    return Path(os.environ.get('NODE_PLANE_CABINET_DIR') or f'{APP_ROOT}/cabinet')


def local_settings_path():
    """Where the worker tells the controller's own cabinet server what to do."""
    return Path(os.environ.get('NODE_PLANE_CABINET_LOCAL_SETTINGS') or f'{SHARED_ROOT}/cabinet-local.json')


def available():
    return (build_dir() / 'index.html').is_file()


def _now():
    return datetime.now(timezone.utc)


def _listen(value):
    """`host:port` for the local server: an address of this host."""
    if not isinstance(value, str):
        raise AccessDenied('cabinet_listen_invalid', 422)
    host, _, port = value.strip().rpartition(':')
    try:
        ipaddress.ip_address(host.strip('[]'))
        number = int(port)
    except ValueError:
        raise AccessDenied('cabinet_listen_invalid', 422) from None
    if not 1 <= number <= 65535 or number == 8080:
        raise AccessDenied('cabinet_listen_invalid', 422)
    return f'{host}:{number}'


def _public_url(value):
    """The address people open the cabinet at: scheme, host and port only."""
    if not isinstance(value, str):
        raise AccessDenied('cabinet_url_invalid', 422)
    try:
        parts = urlsplit(value.strip())
        port = parts.port
    except ValueError:
        raise AccessDenied('cabinet_url_invalid', 422) from None
    host = (parts.hostname or '').lower()
    if (parts.scheme not in ('https', 'http') or parts.path not in ('', '/') or parts.query or parts.fragment
            or parts.username or parts.password or not host):
        raise AccessDenied('cabinet_url_invalid', 422)
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if not _HOST.fullmatch(host):
            raise AccessDenied('cabinet_url_invalid', 422) from None
    default = 443 if parts.scheme == 'https' else 80
    shown = f'[{host}]' if ':' in host else host
    return f'{parts.scheme}://{shown}' + ('' if port in (None, default) else f':{port}')


def _relay(conn):
    return conn.execute("SELECT * FROM backend_relays WHERE status != 'removing' ORDER BY created_at LIMIT 1").fetchone()


def _row(conn):
    return conn.execute('SELECT * FROM backend_web_cabinet WHERE id=1').fetchone()


def origin(conn):
    """The cabinet's public origin, or None while it is not set up."""
    row = _row(conn)
    if row is None or row['status'] == 'removing':
        return None
    if row['mode'] == 'local':
        return row['public_url']
    from .relays import public
    relay = _relay(conn)
    return public(relay)['url'] if relay else None


def public(row, conn):
    if row is None:
        return None
    return {'mode': row['mode'], 'node_key': row['node_key'], 'listen': row['listen'],
            'url': origin(conn) if row['status'] != 'removing' else None, 'public_url': row['public_url'],
            'status': row['status'], 'state': STATES[row['status']], 'revision': row['revision'],
            'applied_revision': row['applied_revision'], 'applied_version': row['applied_version'],
            'version': APP_SEMVER, 'error_code': row['error_code'], 'updated_at': row['updated_at']}


def _write_local(settings):
    """The local server's settings, replaced at once; None removes them."""
    path = local_settings_path()
    if settings is None:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(settings, sort_keys=True) + '\n')
    temporary.replace(path)


class WebCabinetService:
    def __init__(self, db, driver=None):
        self.db, self.driver = db, driver

    def initialize_schema(self):
        from db.migration_revisions.r0050_web_cabinet import create_schema
        with self.db.transaction() as conn:
            create_schema(conn)

    def get(self, actor):
        require_permission(actor, CABINET_PERMISSION)
        with self.db.connect() as conn:
            relay = _relay(conn)
            return {'cabinet': public(_row(conn), conn), 'available': available(),
                    'relay_node_key': relay['node_key'] if relay else None}

    def configure(self, actor, mode, *, listen=None, public_url=None):
        """Serve the cabinet from the relay's node or from the controller."""
        require_permission(actor, CABINET_PERMISSION)
        if mode not in MODES:
            raise AccessDenied('invalid_input', 422)
        if not available():
            raise AccessDenied('cabinet_build_missing', 409)
        if mode == 'local':
            listen, public_url = _listen(listen), _public_url(public_url)
        elif listen is not None or public_url is not None:
            # On the relay's node the cabinet shares the relay's domain.
            raise AccessDenied('invalid_input', 422)
        now = _now().isoformat()
        with self.db.transaction() as conn:
            row = _row(conn)
            if row is not None and row['status'] == 'removing':
                raise AccessDenied('cabinet_removing', 409)
            if row is not None and row['mode'] != mode:
                # The old place gives the cabinet up first.
                raise AccessDenied('cabinet_mode_change', 409)
            node_key = None
            if mode == 'relay':
                relay = _relay(conn)
                if relay is None:
                    raise AccessDenied('cabinet_needs_relay', 409)
                node_key = relay['node_key']
            if row is None:
                conn.execute('''INSERT INTO backend_web_cabinet(id,mode,node_key,listen,public_url,status,revision,
                    actor_id,created_at,updated_at) VALUES (1,?,?,?,?,'applying',1,?,?,?)''',
                    (mode, node_key, listen, public_url, actor.account.id, now, now))
            else:
                conn.execute('''UPDATE backend_web_cabinet SET node_key=?, listen=?, public_url=?,
                    status='applying', revision=revision+1, error_code=NULL, attempted_at=NULL, actor_id=?,
                    updated_at=? WHERE id=1''', (node_key, listen, public_url, actor.account.id, now))
            return {'cabinet': public(_row(conn), conn)}

    def remove(self, actor):
        require_permission(actor, CABINET_PERMISSION)
        with self.db.transaction() as conn:
            if _row(conn) is None:
                raise AccessDenied('resource_not_found', 404)
            conn.execute('''UPDATE backend_web_cabinet SET status='removing', error_code=NULL, attempted_at=NULL,
                actor_id=?, updated_at=? WHERE id=1''', (actor.account.id, _now().isoformat()))
            return {'cabinet': public(_row(conn), conn)}

    def recover(self):
        """At worker start: apply the files again, which may be a new
        release's; a node that has this version already changes nothing."""
        with self.db.transaction() as conn:
            conn.execute("""UPDATE backend_web_cabinet SET status='applying', attempted_at=NULL
                WHERE status IN ('active','failed')""")

    def _apply(self, row):
        if row['mode'] == 'local':
            _write_local(None if row['status'] == 'removing' else
                         {'listen': row['listen'], 'root': str(build_dir()), 'version': APP_SEMVER})
        elif row['status'] == 'removing':
            self.driver.remove_cabinet(row['node_key'])
        else:
            self.driver.apply_cabinet(row['node_key'], str(build_dir()), APP_SEMVER)

    def run_one(self):
        now = _now()
        with self.db.connect() as conn:
            row = conn.execute('''SELECT * FROM backend_web_cabinet WHERE status IN ('applying','removing')
                AND (attempted_at IS NULL OR attempted_at<=?)''', ((now - RETRY).isoformat(),)).fetchone()
        if row is None or (row['mode'] == 'relay' and self.driver is None):
            return False
        import grpc
        error = None
        try:
            if row['status'] == 'applying' and not available():
                raise AccessDenied('cabinet_build_missing', 409)
            self._apply(row)
        except AccessDenied as refused:
            error = (refused.code, True)
        except grpc.RpcError as failure:
            details = failure.details() or ''
            if failure.code() == grpc.StatusCode.INVALID_ARGUMENT and re.fullmatch(r'cabinet_[a-z0-9_]+', details):
                error = (details, True)
            elif failure.code() in (grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED):
                error = ('agent_unavailable', False)
            else:
                error = ('cabinet_apply_failed', True)
        except Exception:
            error = ('cabinet_apply_failed', True)
        with self.db.transaction() as conn:
            current = _row(conn)
            if current is None or (current['status'], current['revision']) != (row['status'], row['revision']):
                return True
            stamp = now.isoformat()
            if error is not None:
                code, final = error
                status = 'failed' if final and row['status'] == 'applying' else row['status']
                conn.execute('UPDATE backend_web_cabinet SET status=?, error_code=?, attempted_at=? WHERE id=1',
                             (status, code, stamp))
            elif row['status'] == 'removing':
                conn.execute('DELETE FROM backend_web_cabinet WHERE id=1')
            else:
                conn.execute('''UPDATE backend_web_cabinet SET status='active', applied_revision=revision,
                    applied_version=?, error_code=NULL, attempted_at=?, updated_at=? WHERE id=1''',
                    (APP_SEMVER, stamp, stamp))
        return True
