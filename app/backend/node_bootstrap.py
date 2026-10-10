"""Durable installation coordinator; each mutation belongs to an existing journal."""
from uuid import UUID, uuid4, uuid5

from .authorization import (AccessDenied, Account, Actor, Principal, PrincipalKind,
                            ADMIN_PERMISSIONS, require_permission)
from .node_settings import _key, _snapshot


def require_idle(conn, node_key, bootstrap_id=None):
    if conn.execute("""SELECT 1 FROM backend_node_bootstraps WHERE node_key=?
        AND status IN ('awaiting_executor','running','blocked')
        AND (CAST(? AS TEXT) IS NULL OR id != ?) LIMIT 1""", (node_key, bootstrap_id, bootstrap_id)).fetchone():
        raise AccessDenied('node_operation_pending', 409)


class NodeBootstrapService:
    def __init__(self, db, driver=None):
        self.db, self.driver = db, driver

    def initialize_schema(self):
        from db.migration_revisions.r0004_node_bootstrap import upgrade
        with self.db.transaction() as conn:
            upgrade(conn)

    @staticmethod
    def public(row):
        return {key: row[key] for key in ('id', 'node_key', 'revision', 'phase',
                'status', 'child_id', 'child_kind', 'error_code')}

    def get(self, actor, identity):
        require_permission(actor, 'nodes.manage')
        with self.db.connect() as conn:
            row = conn.execute('SELECT * FROM backend_node_bootstraps WHERE id=?', (identity,)).fetchone()
        if row is None:
            raise AccessDenied('resource_not_found', 404)
        result = self.public(row)
        result['progress'] = None
        if row['child_kind'] == 'agent-rollouts' and row['child_id']:
            from .agent_rollout import AgentRolloutService
            result['progress'] = AgentRolloutService(self.db).get(actor, row['child_id']).get('progress')
        elif row['child_kind'] == 'node-jobs' and row['child_id']:
            from .node_operations import NodeOperations
            result['progress'] = NodeOperations(self.db).get(actor, row['child_id']).get('progress')
        return result

    def queue(self, actor, node_key, revision, command_key):
        require_permission(actor, 'nodes.manage')
        key = _key(command_key)
        if type(revision) is not int or revision < 1:
            raise AccessDenied('invalid_revision', 422)
        with self.db.transaction() as conn:
            from .maintenance_gate import admit
            admit(conn)
            current = conn.execute('SELECT role,status FROM backend_accounts WHERE id=?', (actor.account.id,)).fetchone()
            if not current or current['role'] != 'admin' or current['status'] != 'approved':
                raise AccessDenied('permission_denied')
            previous = conn.execute('SELECT * FROM backend_node_bootstraps WHERE actor_id=? AND command_key=?',
                                    (actor.account.id, key)).fetchone()
            if previous:
                if previous['node_key'] != node_key or previous['revision'] != revision:
                    raise AccessDenied('idempotency_conflict', 409)
                return self.public(previous)
            node = conn.execute('UPDATE backend_nodes SET enabled=enabled WHERE key=? RETURNING *', (node_key,)).fetchone()
            if node is None:
                raise AccessDenied('resource_not_found', 404)
            if node['desired_revision'] != revision:
                raise AccessDenied('revision_conflict', 412)
            _snapshot(node)
            if conn.execute('SELECT 1 FROM backend_node_drains WHERE node_key=?', (node_key,)).fetchone():
                raise AccessDenied('node_already_draining', 409)
            require_idle(conn, node_key)
            for table in ('backend_node_jobs', 'backend_node_settings_tasks', 'backend_agent_rollouts',
                          'backend_operation_tasks'):
                if conn.execute(f"SELECT 1 FROM {table} WHERE node_key=? AND status IN ('awaiting_executor','running','blocked') LIMIT 1", (node_key,)).fetchone():
                    raise AccessDenied('node_operation_pending', 409)
            identity = str(uuid4())
            conn.execute('''INSERT INTO backend_node_bootstraps
                (id,node_key,actor_id,command_key,revision,phase,status)
                VALUES (?,?,?,?,?,'agent','awaiting_executor')''',
                (identity, node_key, actor.account.id, key, revision))
            return self.public(conn.execute('SELECT * FROM backend_node_bootstraps WHERE id=?', (identity,)).fetchone())

    def _change(self, row, *, phase=None, status='awaiting_executor', child=None, kind=None, error=None):
        with self.db.transaction() as conn:
            conn.execute('''UPDATE backend_node_bootstraps SET phase=?,status=?,child_id=?,child_kind=?,error_code=?
                WHERE id=? AND status IN ('awaiting_executor','running','blocked')''',
                (phase or row['phase'], status, child, kind, error, row['id']))

    def _advance(self, row):
        key = str(uuid5(UUID(row['id']), row['phase']))
        with self.db.connect() as conn:
            node = conn.execute('SELECT * FROM backend_nodes WHERE key=?', (row['node_key'],)).fetchone()
            account = conn.execute('SELECT role,status FROM backend_accounts WHERE id=?', (row['actor_id'],)).fetchone()
            child = None
            # The child transaction may have committed before the parent link
            # was saved. Adopt that exact command before checking revisions.
            if not row['child_id']:
                table = 'backend_agent_rollouts' if row['phase'] == 'agent' else 'backend_node_jobs'
                actor_column = 'actor_account_id' if row['phase'] == 'agent' else 'actor_id'
                recorded = conn.execute(f'SELECT id,node_key FROM {table} WHERE {actor_column}=? AND command_key=?',
                                        (row['actor_id'], key)).fetchone()
                if recorded and recorded['node_key'] == row['node_key']:
                    row = dict(row, child_id=recorded['id'],
                               child_kind='agent-rollouts' if row['phase'] == 'agent' else 'node-jobs')
                    adopted = True
                else:
                    adopted = False
            else:
                adopted = False
            if row['child_id']:
                table = 'backend_agent_rollouts' if row['child_kind'] == 'agent-rollouts' else 'backend_node_jobs'
                child = conn.execute(f'SELECT status FROM {table} WHERE id=?', (row['child_id'],)).fetchone()
        if adopted:
            self._change(row, status='running', child=row['child_id'], kind=row['child_kind'])
            return True
        if node is None:
            self._change(row, status='superseded', error='resource_not_found')
            return True
        if not account or account['role'] != 'admin' or account['status'] != 'approved':
            if row['status'] != 'blocked':
                self._change(row, status='blocked', child=row['child_id'], kind=row['child_kind'], error='permission_denied')
                return True
            return False
        if row['child_id']:
            if child is None:
                if row['status'] != 'blocked':
                    self._change(row, status='blocked', child=row['child_id'], kind=row['child_kind'], error='child_operation_missing')
                    return True
                return False
            if child['status'] in {'awaiting_executor', 'running'}:
                return False
            if child['status'] == 'blocked':
                if row['status'] != 'blocked':
                    self._change(row, status='blocked', child=row['child_id'], kind=row['child_kind'], error='child_operation_blocked')
                    return True
                return False
            if child['status'] != 'succeeded':
                self._change(row, status='superseded', error='child_operation_superseded')
            elif row['phase'] == 'protocols':
                self._change(row, phase='done', status='succeeded')
            else:
                self._change(row, phase='docker' if row['phase'] == 'agent' else 'protocols')
            return True
        if row['status'] == 'blocked':
            return False
        if node['desired_revision'] != row['revision']:
            self._change(row, status='blocked', error='revision_conflict')
            return True
        actor = Actor(Principal(row['actor_id'], PrincipalKind.ACCOUNT, ADMIN_PERMISSIONS),
                      Account(row['actor_id'], account['role'], account['status']))
        if row['phase'] in {'agent', 'docker'}:
            try:
                facts = self.driver.inspect_node_services(row['node_key'])
            except Exception as error:
                import grpc
                missing = (isinstance(error, grpc.RpcError)
                           and error.code() == grpc.StatusCode.FAILED_PRECONDITION
                           and error.details() in {'no node-agent target configured', 'agent not configured'})
                if not missing or row['phase'] != 'agent':
                    raise AccessDenied('node_agent_unavailable', 503) from None
                facts = None
            if row['phase'] == 'agent' and facts is not None:
                self._change(row, phase='docker')
                return True
            if row['phase'] == 'docker' and facts.get('docker') is True:
                self._change(row, phase='protocols')
                return True
        if row['phase'] == 'protocols' and node['applied_revision'] == node['desired_revision']:
            facts = self.driver.inspect_node_services(row['node_key'])
            import json
            protocols = json.loads(node['protocols_json'])
            if protocols and all(facts.get(p + '_config_valid') is True and
                                 facts.get(p + '_running') is True for p in protocols):
                self._change(row, phase='done', status='succeeded')
                return True
        if row['phase'] == 'agent':
            from .agent_rollout import AgentRolloutService
            with self.db.connect() as conn:
                connection = conn.execute('SELECT transport,ssh_target FROM backend_node_connections WHERE node_key=?', (row['node_key'],)).fetchone()
            if not connection:
                raise AccessDenied('node_agent_unconfigured', 409)
            child = AgentRolloutService(self.db).request(actor, row['node_key'], key,
                transport=connection['transport'], ssh_target=connection['ssh_target'], bootstrap_id=row['id'])
            kind = 'agent-rollouts'
        else:
            from .node_operations import NodeOperations
            action = 'install_docker' if row['phase'] == 'docker' else 'bootstrap'
            child = NodeOperations(self.db).queue(actor, row['node_key'], action,
                revision=row['revision'], command_key=key, bootstrap_id=row['id'])
            kind = 'node-jobs'
        self._change(row, status='running', child=child['id'], kind=kind)
        return True

    def resume(self, actor, identity):
        """Resume a blocked coordinator before dispatch; never reset a child."""
        require_permission(actor, 'maintenance.manage')
        require_permission(actor, 'nodes.manage')
        with self.db.transaction() as conn:
            from .maintenance_gate import admit
            admit(conn)
            row = conn.execute('SELECT * FROM backend_node_bootstraps WHERE id=?', (identity,)).fetchone()
            if row is None:
                raise AccessDenied('resource_not_found', 404)
            if row['status'] != 'blocked' or row['child_id']:
                raise AccessDenied('repair_conflict', 409)
            node = conn.execute('UPDATE backend_nodes SET enabled=enabled WHERE key=? RETURNING *', (row['node_key'],)).fetchone()
            if node is None:
                raise AccessDenied('resource_not_found', 404)
            account = conn.execute('SELECT role,status FROM backend_accounts WHERE id=?', (row['actor_id'],)).fetchone()
            if not account or account['role'] != 'admin' or account['status'] != 'approved':
                raise AccessDenied('permission_denied')
            if conn.execute('SELECT 1 FROM backend_node_drains WHERE node_key=?', (row['node_key'],)).fetchone():
                raise AccessDenied('node_already_draining', 409)
            _snapshot(node)
            # A crash may have hidden a committed child from the parent. Adopt
            # its identity rather than permitting another dispatch.
            key = str(uuid5(UUID(identity), row['phase']))
            table = 'backend_agent_rollouts' if row['phase'] == 'agent' else 'backend_node_jobs'
            owner = 'actor_account_id' if row['phase'] == 'agent' else 'actor_id'
            child = conn.execute(f'SELECT id,node_key FROM {table} WHERE {owner}=? AND command_key=?', (row['actor_id'], key)).fetchone()
            if child:
                if child['node_key'] != row['node_key']:
                    raise AccessDenied('repair_conflict', 409)
                conn.execute("UPDATE backend_node_bootstraps SET child_id=?,child_kind=?,status='running',error_code=NULL WHERE id=?", (child['id'], 'agent-rollouts' if row['phase']=='agent' else 'node-jobs', identity))
            else:
                if node['desired_revision'] != row['revision']:
                    raise AccessDenied('revision_conflict', 412)
                for table in ('backend_node_jobs', 'backend_node_settings_tasks', 'backend_agent_rollouts', 'backend_operation_tasks'):
                    if conn.execute(f"SELECT 1 FROM {table} WHERE node_key=? AND status IN ('awaiting_executor','running','blocked') LIMIT 1", (row['node_key'],)).fetchone():
                        raise AccessDenied('node_operation_pending', 409)
                conn.execute("UPDATE backend_node_bootstraps SET status='awaiting_executor',error_code=NULL WHERE id=?", (identity,))
        return self.get(actor, identity)

    def run_one(self):
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM backend_node_bootstraps WHERE status IN ('awaiting_executor','running','blocked') ORDER BY id").fetchall()
        for row in rows:
            try:
                if self._advance(row):
                    return True
            except Exception as error:
                code = error.code if isinstance(error, AccessDenied) else 'bootstrap_unconfirmed'
                self._change(row, status='blocked', child=row['child_id'], kind=row['child_kind'], error=code)
                return True
        return False
