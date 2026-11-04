"""Token installation of node agents: no SSH key or sudo for the controller.

The administrator runs the shown command on the server as root. The agent
installs itself and waits; the worker asks the driver to dial it, prove the
one-time token and issue its certificate (see the agent's `enrollment`
module). Server setup is queued together with the command, and its agent
phase waits for the enrollment.

The token stays readable until it is used or expires: the driver proves it
with an HMAC, so a hash would not do. Backups leave the table out and a
restore clears it.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shlex
from uuid import UUID

from .authorization import AccessDenied, require_permission

TOKEN_LIFETIME = timedelta(hours=1)
# Unreachable agents are retried no more often than this.
RETRY = timedelta(seconds=10)
REPOSITORY = 'saharoktyan/node-plane'
AGENT_ASSET = 'node-plane-agent-linux-amd64.tar.gz'
INSTALLER_ASSET = 'node-plane-agent-installer.sh'
HOST = re.compile(r'(?=.{1,253}\Z)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*\Z')


def _now():
    return datetime.now(timezone.utc)


def agent_port():
    value = os.environ.get('NODE_AGENT_PORT', '50061')
    if not value.isdecimal() or not 1 <= int(value) <= 65535:
        raise AccessDenied('agent_port_invalid', 500)
    return int(value)


def target(host, port):
    """`host:port` for the driver; the agent certificate names the host."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if not HOST.fullmatch(host or ''):
            raise AccessDenied('agent_host_invalid', 422) from None
        return f'{host}:{port}'
    return f'[{address}]:{port}' if address.version == 6 else f'{address}:{port}'


_release_cache = {}


def release_assets(app_root=None, fetch=None):
    """The release this controller runs, with the checksums the command pins.

    The installer must be byte-identical to the controller's own copy, so a
    changed release asset cannot run as root on the node.
    """
    root = Path(app_root or os.environ.get('NODE_PLANE_APP_DIR') or Path(__file__).resolve().parents[2])
    try:
        version = (root / 'VERSION').read_text().strip()
        installer = (root / 'scripts/install_agent.sh').read_bytes()
    except OSError:
        raise AccessDenied('agent_installer_unavailable', 503) from None
    if not re.fullmatch(r'[0-9A-Za-z.+-]{1,64}', version):
        raise AccessDenied('agent_installer_unavailable', 503)
    tag = 'v' + version
    if tag not in _release_cache:
        base = f'https://github.com/{REPOSITORY}/releases/download/{tag}'
        if fetch is None:
            import sys
            sys.path.insert(0, str(root / 'scripts'))
            from controller_release import fetch
        try:
            sums = fetch(base + '/SHA256SUMS.txt', 1024 * 1024).decode()
        except Exception:
            raise AccessDenied('agent_release_unavailable', 503) from None
        found = {}
        for line in sums.splitlines():
            parts = line.split()
            if len(parts) == 2 and re.fullmatch(r'[0-9a-f]{64}', parts[0]):
                found.setdefault(parts[1].lstrip('*'), []).append(parts[0])
        if any(len(found.get(name, ())) != 1 for name in (AGENT_ASSET, INSTALLER_ASSET)):
            raise AccessDenied('agent_release_unavailable', 503)
        _release_cache[tag] = {'tag': tag, 'base': base, 'agent_sha256': found[AGENT_ASSET][0],
                               'installer_sha256': found[INSTALLER_ASSET][0]}
    release = _release_cache[tag]
    if hashlib.sha256(installer).hexdigest() != release['installer_sha256']:
        raise AccessDenied('agent_installer_mismatch', 503)
    return release


def installation(release, node_key, token, port):
    """What runs on the server: the installer to check and its arguments."""
    return {'installer': {'url': f"{release['base']}/{INSTALLER_ASSET}",
                          'sha256': release['installer_sha256']},
            'arguments': ['--node-key', node_key, '--token', token, '--release', release['tag'],
                          '--agent-sha256', release['agent_sha256'], '--port', str(port)]}


def command(release, node_key, token, port):
    """One line to paste on the server: check the installer, then run it."""
    path = '/tmp/node-plane-agent-installer.sh'
    steps = installation(release, node_key, token, port)
    return (f"curl -fsSLo {path} {steps['installer']['url']} && "
            f"echo '{steps['installer']['sha256']}  {path}' | sha256sum -c - && "
            f"sudo sh {path} " + ' '.join(shlex.quote(arg) for arg in steps['arguments']))


class AgentEnrollmentService:
    def __init__(self, db, driver=None, release=release_assets):
        self.db, self.driver, self.release = db, driver, release

    def initialize_schema(self):
        from db.migration_revisions.r0025_agent_enrollments import upgrade
        with self.db.transaction() as conn:
            upgrade(conn)

    @staticmethod
    def public(row):
        if row is None:
            return {'status': 'none'}
        return {'node_key': row['node_key'], 'status': row['status'], 'target': row['target'],
                'restrict_to_controller': bool(row['restrict_to_controller']),
                'error_code': row['error_code'], 'firewall': row['firewall'],
                'expires_at': row['expires_at'], 'completed_at': row['completed_at']}

    def get(self, actor, node_key):
        require_permission(actor, 'nodes.manage')
        with self.db.connect() as conn:
            if not conn.execute('SELECT 1 FROM backend_nodes WHERE key=?', (node_key,)).fetchone():
                raise AccessDenied('resource_not_found', 404)
            row = conn.execute('SELECT * FROM backend_agent_enrollments WHERE node_key=?', (node_key,)).fetchone()
        return self.public(row)

    def issue(self, actor, node_key, revision, command_key, *, restrict_to_controller=False, agent_host=None,
              install_services=True):
        """A new one-time command for the node, replacing an unused one.

        With `install_services`, server setup is queued too and continues
        once the agent enrolls; otherwise the services are installed later.
        """
        require_permission(actor, 'nodes.manage')
        if type(restrict_to_controller) is not bool or type(install_services) is not bool:
            raise AccessDenied('invalid_input', 422)
        try:
            key = str(UUID(command_key))
        except (TypeError, ValueError, AttributeError):
            raise AccessDenied('invalid_idempotency_key', 422) from None
        with self.db.connect() as conn:
            node = conn.execute('SELECT * FROM backend_nodes WHERE key=?', (node_key,)).fetchone()
        if node is None:
            raise AccessDenied('resource_not_found', 404)
        host = agent_host or json.loads(node['settings_json'] or '{}').get('public_host')
        if not host:
            raise AccessDenied('node_settings_incomplete', 422)
        port = agent_port()
        address = target(host, port)
        release = self.release()
        now = _now()
        expires = now + TOKEN_LIFETIME
        token = f'{int(expires.timestamp())}.{secrets.token_hex(32)}'
        from .node_bootstrap import NodeBootstrapService
        bootstraps = NodeBootstrapService(self.db, self.driver)
        with self.db.connect() as conn:
            pending = conn.execute('''SELECT * FROM backend_node_bootstraps WHERE node_key=?
                AND status IN ('awaiting_executor','running','blocked') ORDER BY id LIMIT 1''',
                (node_key,)).fetchone()
        if pending is not None and pending['phase'] != 'agent':
            raise AccessDenied('node_operation_pending', 409)
        with self.db.transaction() as conn:
            conn.execute('''INSERT INTO backend_agent_enrollments
                (node_key,token,target,restrict_to_controller,status,actor_id,expires_at,created_at)
                VALUES (?,?,?,?,'waiting',?,?,?)
                ON CONFLICT(node_key) DO UPDATE SET token=excluded.token, target=excluded.target,
                    restrict_to_controller=excluded.restrict_to_controller, status='waiting',
                    error_code=NULL, firewall=NULL, journal_archive=NULL, actor_id=excluded.actor_id,
                    expires_at=excluded.expires_at, attempted_at=NULL, created_at=excluded.created_at,
                    completed_at=NULL''',
                (node_key, token, address, int(restrict_to_controller), actor.account.id,
                 expires.isoformat(), now.isoformat()))
        if pending is None and not install_services:
            bootstrap = None
        elif pending is None:
            bootstrap = bootstraps.queue(actor, node_key, revision, key)
        elif pending['status'] == 'blocked' and not pending['child_id']:
            bootstrap = bootstraps.resume(actor, pending['id'])
        else:
            bootstrap = bootstraps.public(pending)
        return {'command': command(release, node_key, token, port),
                **installation(release, node_key, token, port), 'expires_at': expires.isoformat(),
                'target': address, 'restrict_to_controller': restrict_to_controller,
                'bootstrap': bootstrap}

    def pending(self, node_key):
        """The enrollment server setup waits for: waiting, expired or none."""
        with self.db.connect() as conn:
            row = conn.execute('SELECT status FROM backend_agent_enrollments WHERE node_key=?',
                               (node_key,)).fetchone()
        return row['status'] if row else None

    def _retired(self, conn):
        return [row['node_key'] for row in conn.execute('''SELECT r.node_key FROM backend_node_retirements r
            WHERE NOT EXISTS (SELECT 1 FROM backend_nodes n WHERE n.key=r.node_key) ORDER BY r.node_key''')]

    def run_one(self):
        now = _now()
        with self.db.transaction() as conn:
            expired = conn.execute('''UPDATE backend_agent_enrollments SET status='expired', token=NULL
                WHERE status='waiting' AND expires_at<=? RETURNING node_key''', (now.isoformat(),)).fetchall()
        if expired:
            return True
        with self.db.connect() as conn:
            row = conn.execute('''SELECT * FROM backend_agent_enrollments WHERE status='waiting'
                AND (attempted_at IS NULL OR attempted_at<=?) ORDER BY attempted_at NULLS FIRST, node_key
                LIMIT 1''', ((now - RETRY).isoformat(),)).fetchone()
            if row is None:
                return False
            retired = self._retired(conn)
        from .installation_identity import controller_identity
        controller = controller_identity(self.db)
        import grpc
        error = None
        try:
            result = self.driver.enroll_agent(row['node_key'], row['target'], row['token'], controller,
                                              retired, restrict_to_controller=bool(row['restrict_to_controller']))
        except grpc.RpcError as failure:
            # Not installed yet, another token, or another active node's agent:
            # the administrator can still fix it before the token expires.
            error = {grpc.StatusCode.UNAVAILABLE: None,
                     grpc.StatusCode.PERMISSION_DENIED: 'agent_enrollment_rejected',
                     grpc.StatusCode.FAILED_PRECONDITION: 'agent_owned_by_another_node'}.get(
                         failure.code(), 'agent_enrollment_failed')
            result = None
        with self.db.transaction() as conn:
            if result is None:
                conn.execute('''UPDATE backend_agent_enrollments SET attempted_at=?, error_code=?
                    WHERE node_key=? AND token=? AND status='waiting' ''',
                    (now.isoformat(), error, row['node_key'], row['token']))
                return False
            conn.execute('''UPDATE backend_agent_enrollments SET status='enrolled', token=NULL, error_code=NULL,
                firewall=?, journal_archive=?, completed_at=? WHERE node_key=? AND token=?''',
                (result['firewall'], result['journal_archive'] or None, now.isoformat(),
                 row['node_key'], row['token']))
        return True
