"""Worker-driven full removal using the independently verified lifecycle saga."""
import os
import logging
import grpc
from pathlib import Path

from .authorization import Actor, Account, Principal, PrincipalKind, ADMIN_PERMISSIONS, require_permission, AccessDenied
from .node_lifecycle import NodeLifecycle
from .removal_verifier import RemovalVerifier, RemovalVerificationError

logger = logging.getLogger(__name__)


class NodeRemovalService:
    def __init__(self, db, driver=None, verifier_factory=None):
        self.db, self.driver = db, driver
        self.verifier_factory = verifier_factory

    def initialize_schema(self):
        with self.db.transaction() as conn:
            conn.execute('''CREATE TABLE IF NOT EXISTS backend_node_removals (
                node_key TEXT PRIMARY KEY, actor_id TEXT NOT NULL,
                status TEXT NOT NULL, error_code TEXT
            )''')

    def request(self, actor, node_key, retry=False):
        require_permission(actor, 'maintenance.manage')
        with self.db.transaction() as conn:
            prior = conn.execute('SELECT status FROM backend_node_removals WHERE node_key = ?', (node_key,)).fetchone()
            if prior is None:
                if conn.execute('SELECT 1 FROM backend_nodes WHERE key = ?', (node_key,)).fetchone() is None:
                    raise AccessDenied('resource_not_found', 404)
                conn.execute("INSERT INTO backend_node_removals VALUES (?, ?, 'queued', NULL)", (node_key, actor.account.id))
            elif prior['status'] == 'blocked' and retry:
                conn.execute("UPDATE backend_node_removals SET status = 'queued', error_code = NULL WHERE node_key = ?", (node_key,))
        return self.get(actor, node_key)

    def get(self, actor, node_key):
        require_permission(actor, 'maintenance.manage')
        with self.db.connect() as conn:
            row = conn.execute('SELECT * FROM backend_node_removals WHERE node_key = ?', (node_key,)).fetchone()
        if row is None:
            raise AccessDenied('resource_not_found', 404)
        if row['status'] in {'succeeded', 'abandoned'}:
            return {'node_key': node_key, 'status': 'removed' if row['status'] == 'succeeded' else 'removed_registry_only'}
        return {**NodeLifecycle(self.db).overview(actor, node_key),
                'removal_status': row['status'], 'error_code': row['error_code']}

    def verifier(self, target, final=False):
        if self.verifier_factory:
            return self.verifier_factory(target, final)
        local = target == 'local'
        bot_key_path = os.environ.get('NODE_PLANE_BOT_PUBLIC_KEY_FILE') or (os.environ.get('SSH_KEY', '') + '.pub')
        bot_key = Path(bot_key_path).read_text().strip() if Path(bot_key_path).is_file() else None
        if not local and bot_key is None:
            raise AccessDenied('verification_key_unavailable', 503)
        if local:
            return RemovalVerifier(local=True, bot_public_key=bot_key)
        independent = os.environ.get('NODE_PLANE_REMOVAL_SSH_KEY')
        original = os.environ.get('SSH_KEY')
        if not independent:
            if not original or not Path(original).is_file():
                raise AccessDenied('verification_key_unavailable', 503)
            from .installation_identity import controller_identity
            from .removal_credentials import ManagedRemovalVerifier, credential_directory
            return ManagedRemovalVerifier(original_key=original,
                directory=credential_directory(original, controller_identity(self.db), target),
                ssh_target=target, bot_public_key=bot_key)
        if not Path(independent).is_file():
            raise AccessDenied('independent_verification_key_required', 503)
        if original and Path(original).is_file() and os.path.samefile(independent, original):
            raise AccessDenied('independent_verification_key_required', 503)
        return RemovalVerifier(ssh_target=target, ssh_identity_file=independent,
                              bot_public_key=bot_key)

    def run_one(self):
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM backend_node_removals WHERE status IN ('queued', 'running') ORDER BY node_key").fetchall()
        for row in rows:
            key = row['node_key']
            with self.db.connect() as conn:
                account = conn.execute('SELECT role, status FROM backend_accounts WHERE id = ?', (row['actor_id'],)).fetchone()
                node = conn.execute('SELECT transport, ssh_target FROM backend_node_connections WHERE node_key = ?', (key,)).fetchone()
            if account is None:
                continue
            actor = Actor(Principal('node-removal-worker', PrincipalKind.SERVICE, ADMIN_PERMISSIONS),
                          Account(row['actor_id'], account['role'], account['status']))
            lifecycle = NodeLifecycle(self.db)
            phase = 'verify_identity'
            try:
                if node is None:
                    raise AccessDenied('verification_target_required', 409)
                target = 'local' if node['transport'] == 'local' else node['ssh_target']
                # Verify credentials before revoking access or removing anything.
                verifier = self.verifier(target)
                state = lifecycle.overview(actor, key)
                phase = state['cleanup_phase'] or 'start_drain'
                if state['status'] == 'active':
                    if hasattr(verifier, 'prepare'):
                        verifier.prepare(key)
                    lifecycle.bind_verification_target(actor, key, verifier)
                    lifecycle.start_drain(actor, key)
                    with self.db.transaction() as conn:
                        conn.execute("UPDATE backend_node_removals SET status = 'running' WHERE node_key = ?", (key,))
                    return True
                if not state['revocations_complete']:
                    continue
                if state['cleanup_phase'] in {'uninstall_uncertain', 'uninstall_scheduled'}:
                    final_verifier = self.verifier(target, final=True)
                    lifecycle.retire_verified(actor, key, final_verifier)
                    with self.db.transaction() as conn:
                        conn.execute("UPDATE backend_node_removals SET status = 'succeeded' WHERE node_key = ?", (key,))
                    if hasattr(final_verifier, 'discard'):
                        try:
                            final_verifier.discard()
                        except OSError:
                            logger.exception('Could not discard verification credentials: node=%s', key)
                else:
                    lifecycle.cleanup(actor, key, self.driver, expected_phase=state['cleanup_phase'] or 'not_started')
                return True
            except Exception as exc:
                if isinstance(exc, AccessDenied):
                    code = exc.code
                elif isinstance(exc, RemovalVerificationError):
                    code = 'host_verification_failed'
                elif isinstance(exc, grpc.RpcError):
                    code = ('node_agent_unavailable' if exc.code() in {
                        grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED}
                        else 'node_cleanup_failed')
                else:
                    code = 'node_cleanup_failed'
                logger.exception('Node removal blocked: node=%s phase=%s code=%s', key, phase, code)
                with self.db.transaction() as conn:
                    conn.execute("UPDATE backend_node_removals SET status = 'blocked', error_code = ? WHERE node_key = ?", (code, key))
        return False
