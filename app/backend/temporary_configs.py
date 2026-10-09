"""Independent admin-issued leases; unknown ensures are never replayed."""
from datetime import datetime, timedelta, timezone
import json
from urllib.parse import quote
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from .authorization import AccessDenied, require_permission

DURATIONS = {43200, 86400, 259200}
TERMINAL = {'expired', 'revoked', 'cancelled'}


def now():
    return datetime.now(timezone.utc)


class TemporaryConfigService:
    def __init__(self, db, driver=None):
        self.db, self.driver = db, driver

    def initialize_schema(self):
        # Isolated repository fixtures; production uses migration revision 3.
        from db.migration_revisions.r0003_temporary_configs import upgrade
        with self.db.transaction() as conn:
            upgrade(conn)

    @staticmethod
    def authorize(conn, actor):
        require_permission(actor, 'settings.manage')
        account = conn.execute('SELECT role,status FROM backend_accounts WHERE id=?', (actor.account.id,)).fetchone()
        if not account or account['role'] != 'admin' or account['status'] != 'approved':
            raise AccessDenied('permission_denied')

    @staticmethod
    def event(conn, row, action, status, actor=None):
        conn.execute('INSERT INTO backend_temporary_config_events VALUES (?,?,?,?,?,?,?,?,?)',
            (str(uuid4()), row['id'], actor.account.id if actor else row['actor_id'],
             actor.principal.id if actor else row['principal_id'], row['node_key'], row['protocol'],
             action, status, now().isoformat()))

    @staticmethod
    def public(row, conn=None):
        result = {k: row[k] for k in ('id','node_key','protocol','transport','duration_seconds',
                                      'status','created_at','expires_at','error_code')}
        if row['status'] == 'active' and datetime.fromisoformat(row['expires_at']) <= now():
            result['status'] = 'expiry_pending'
        result['revocation_mode'] = 'new_connections_only' if row['protocol'] == 'xray' else 'peer_removed'
        if conn is not None:
            node = conn.execute('SELECT title FROM backend_nodes WHERE key=?', (row['node_key'],)).fetchone()
            result['node_title'] = node['title'] if node else row['node_key']
        return result

    @staticmethod
    def ready_node(conn, node_key, protocol, transport):
        node = conn.execute('SELECT * FROM backend_nodes WHERE key=?', (node_key,)).fetchone()
        if node is None:
            raise AccessDenied('resource_not_found',404)
        if not node['enabled'] or node['desired_revision'] != node['applied_revision']:
            raise AccessDenied('node_settings_pending',409)
        if protocol not in json.loads(node['protocols_json']) or (protocol == 'xray'
                and transport not in json.loads(node['xray_transports_json'])):
            raise AccessDenied('config_not_supported',422)
        for table in ('backend_node_drains','backend_node_cleanup','backend_node_retirements'):
            if conn.execute(f'SELECT 1 FROM {table} WHERE node_key=?', (node_key,)).fetchone():
                raise AccessDenied('node_draining',409)
        for table in ('backend_node_jobs','backend_node_settings_tasks'):
            if conn.execute(f"SELECT 1 FROM {table} WHERE node_key=? AND status IN ('awaiting_executor','running','blocked')",
                            (node_key,)).fetchone():
                raise AccessDenied('node_settings_pending',409)
        return node

    def create(self, actor, node_key, protocol, transport, duration_seconds, command_key):
        if type(duration_seconds) is not int or duration_seconds not in DURATIONS or (protocol,transport) not in {
                ('awg','vpn'),('awg','conf'),('xray','tcp'),('xray','xhttp')}:
            raise AccessDenied('invalid_input',422)
        try:
            key = str(UUID(command_key))
        except (ValueError,TypeError,AttributeError):
            raise AccessDenied('invalid_idempotency_key',422) from None
        with self.db.transaction() as conn:
            from .maintenance_gate import admit
            admit(conn)
            self.authorize(conn,actor)
            previous = conn.execute('SELECT * FROM backend_temporary_configs WHERE actor_id=? AND command_key=?',
                                    (actor.account.id,key)).fetchone()
            if previous:
                if (previous['node_key'],previous['protocol'],previous['transport'],previous['duration_seconds']) != (
                        node_key,protocol,transport,duration_seconds):
                    raise AccessDenied('idempotency_conflict',409)
                return self.public(previous, conn)
            conn.execute('UPDATE backend_nodes SET enabled=enabled WHERE key=?',(node_key,))
            node = self.ready_node(conn,node_key,protocol,transport)
            identity = str(uuid4())
            intent = {'node_key':node_key,'command_id':identity,'protocol':protocol,
                'runtime_name':'tmp_'+uuid4().hex,'revision':1,'action':'ensure',
                'uuid':str(uuid4()) if protocol=='xray' else '',
                'short_id':uuid4().hex[:16] if protocol=='xray' else '', 'lease_seconds':duration_seconds}
            conn.execute('''INSERT INTO backend_temporary_configs
                (id,actor_id,principal_id,command_key,node_key,node_revision,protocol,transport,
                 duration_seconds,runtime_name,intent_json,status,created_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,'queued',?)''',
                (identity,actor.account.id,actor.principal.id,key,node_key,node['desired_revision'],protocol,
                 transport,duration_seconds,intent['runtime_name'],json.dumps(intent,sort_keys=True),now().isoformat()))
            row = conn.execute('SELECT * FROM backend_temporary_configs WHERE id=?',(identity,)).fetchone()
            self.event(conn,row,'create','queued',actor)
            return self.public(row, conn)

    def list(self, actor, *, node_key=None, page=0, page_size=20):
        if type(page) is not int or page < 0 or type(page_size) is not int or not 1 <= page_size <= 100:
            raise AccessDenied('invalid_input',422)
        with self.db.connect() as conn:
            self.authorize(conn,actor)
            condition = "status NOT IN ('expired','revoked','cancelled')" + (' AND node_key=?' if node_key else '')
            args = (node_key,) if node_key else ()
            count = conn.execute(f'SELECT COUNT(*) AS n FROM backend_temporary_configs WHERE {condition}',args).fetchone()['n']
            rows = conn.execute(f'''SELECT * FROM backend_temporary_configs WHERE {condition}
                ORDER BY COALESCE(expires_at,created_at),id LIMIT ? OFFSET ?''',(*args,page_size,page*page_size)).fetchall()
            return {'items':[self.public(r,conn) for r in rows],'total':count,'page':page,'page_size':page_size}

    def get(self, actor, config_id):
        with self.db.connect() as conn:
            self.authorize(conn,actor)
            row = conn.execute('SELECT * FROM backend_temporary_configs WHERE id=?',(config_id,)).fetchone()
            if row is None:
                raise AccessDenied('resource_not_found',404)
            return self.public(row, conn)

    def revoke(self, actor, config_id):
        with self.db.transaction() as conn:
            from .maintenance_gate import admit
            admit(conn)
            self.authorize(conn,actor)
            row = conn.execute('SELECT * FROM backend_temporary_configs WHERE id=?',(config_id,)).fetchone()
            if row is None:
                raise AccessDenied('resource_not_found',404)
            if row['status'] in TERMINAL | {'revoking'}:
                return self.public(row, conn)
            status = 'cancelled' if row['status']=='queued' else 'revoking'
            conn.execute('''UPDATE backend_temporary_configs SET status=?,revoke_reason='manual',error_code=NULL WHERE id=?''',
                         (status,config_id))
            self.event(conn,row,'revoke',status,actor)
            return self.public(conn.execute('SELECT * FROM backend_temporary_configs WHERE id=?',(config_id,)).fetchone(), conn)

    def recover(self):
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_temporary_configs SET status='blocked',error_code='outcome_unknown' WHERE status='issuing'")

    def scheduled(self):
        with self.db.transaction() as conn:
            from .maintenance_gate import active
            conn.execute('UPDATE backend_account_guard SET revision=revision+1 WHERE id=1')
            if active(conn) or conn.execute("SELECT 1 FROM backend_backup_jobs WHERE action='restore' AND status IN ('awaiting_executor','running')").fetchone():
                return
            rows = conn.execute("SELECT * FROM backend_temporary_configs WHERE status NOT IN ('expired','revoked','cancelled')").fetchall()
            for row in rows:
                retired = conn.execute('SELECT mode FROM backend_node_retirements WHERE node_key=?',(row['node_key'],)).fetchone()
                if retired:
                    status = 'revoked' if retired['mode']=='verified' else 'cancelled'
                    conn.execute("UPDATE backend_temporary_configs SET status=?,error_code=NULL WHERE id=?",(status,row['id']))
                    self.event(conn,row,'node_retired',status)
                elif row['status']=='active' and datetime.fromisoformat(row['expires_at']) <= now():
                    conn.execute("UPDATE backend_temporary_configs SET status='revoking',revoke_reason='expiry' WHERE id=?",(row['id'],))
                    self.event(conn,row,'expiry','revoking')

    def _finish_issue(self, row, receipt):
        if not isinstance(receipt,dict):
            raise ValueError('invalid lease receipt')
        expiry = datetime.fromisoformat(receipt['expires_at'])
        expected_mode = 'new_connections_only' if row['protocol']=='xray' else 'peer_removed'
        if (expiry.tzinfo is None or expiry <= now() or expiry > now()+timedelta(seconds=row['duration_seconds']+5)
                or receipt.get('revocation_mode') != expected_mode):
            raise ValueError('invalid lease expiry')
        intent = json.loads(row['intent_json'])
        if row['protocol']=='xray' and receipt.get('xray_uuid') != intent['uuid']:
            raise ValueError('invalid temporary identity')
        if row['protocol']=='awg' and (not receipt.get('wg_conf','').startswith('[Interface]')
                or not receipt.get('vpn_key','').startswith('vpn://')):
            raise ValueError('invalid temporary artifact')
        state = self.driver.node_action(row['id'],'temporary_status',intent,recover=True)
        if (state.get('status') != 'active' or state.get('enforcement_ready') is not True
                or datetime.fromisoformat(state['expires_at']) != expiry):
            raise ValueError('temporary expiry enforcement is unavailable')
        with self.db.transaction() as conn:
            conn.execute('UPDATE backend_account_guard SET revision=revision+1 WHERE id=1')
            current = conn.execute('SELECT * FROM backend_temporary_configs WHERE id=?',(row['id'],)).fetchone()
            if current['status'] not in {'issuing','blocked'}:
                return  # A concurrent revoke must never be overwritten by issuance.
            node = conn.execute('SELECT enabled,desired_revision,applied_revision FROM backend_nodes WHERE key=?',(row['node_key'],)).fetchone()
            usable = node and node['enabled'] and node['desired_revision']==row['node_revision']==node['applied_revision']
            status = 'active' if usable else 'revoking'
            conn.execute('UPDATE backend_temporary_configs SET status=?,expires_at=?,revoke_reason=?,error_code=NULL WHERE id=?',
                         (status,expiry.astimezone(timezone.utc).isoformat(),None if usable else 'node_changed',row['id']))
            self.event(conn,row,'issue',status)

    def reconcile(self):
        with self.db.connect() as conn:
            from .maintenance_gate import active
            if active(conn) or conn.execute("SELECT 1 FROM backend_backup_jobs WHERE action='restore' AND status IN ('awaiting_executor','running')").fetchone():
                return
        with self.db.connect() as conn:
            rows = conn.execute("SELECT * FROM backend_temporary_configs WHERE status IN ('blocked','revoke_blocked') ORDER BY created_at,id LIMIT 100").fetchall()
        for row in rows:
            try:
                intent = json.loads(row['intent_json'])
                state = self.driver.node_action(row['id'],'temporary_status',intent,recover=True)
                if state['status'] in {'expired','revoked'}:
                    with self.db.transaction() as conn:
                        conn.execute('UPDATE backend_account_guard SET revision=revision+1 WHERE id=1')
                        changed = conn.execute("UPDATE backend_temporary_configs SET status=?,error_code=NULL WHERE id=? AND status IN ('blocked','revoke_blocked') RETURNING id",
                                               (state['status'],row['id'])).fetchone()
                        if changed:
                            self.event(conn,row,'reconcile',state['status'])
                elif row['status']=='blocked':
                    self._finish_issue(row,self.driver.node_action(row['id'],'temporary_ensure',intent,recover=True))
            except Exception:
                continue

    def run_one(self):
        with self.db.transaction() as conn:
            from .maintenance_gate import active
            conn.execute('UPDATE backend_account_guard SET revision=revision+1 WHERE id=1')
            if active(conn) or conn.execute("SELECT 1 FROM backend_backup_jobs WHERE action='restore' AND status IN ('awaiting_executor','running')").fetchone():
                return False
            row = conn.execute("SELECT * FROM backend_temporary_configs WHERE status IN ('queued','revoking') ORDER BY created_at,id LIMIT 1").fetchone()
            if row is None:
                return False
            if row['status']=='queued':
                try:
                    node = self.ready_node(conn,row['node_key'],row['protocol'],row['transport'])
                    if node['desired_revision'] != row['node_revision']:
                        raise AccessDenied('config_stale',409)
                except AccessDenied:
                    conn.execute("UPDATE backend_temporary_configs SET status='cancelled',error_code='node_changed' WHERE id=?",(row['id'],))
                    self.event(conn,row,'issue','cancelled')
                    return True
                conn.execute("UPDATE backend_temporary_configs SET status='issuing' WHERE id=?",(row['id'],))
        issuing = row['status']=='queued'
        try:
            intent = json.loads(row['intent_json'])
            if issuing:
                self._finish_issue(row,self.driver.node_action(row['id'],'temporary_ensure',intent))
            else:
                command_id = str(uuid5(NAMESPACE_URL,'node-plane:temporary-revoke:'+row['id']))
                intent.update(command_id=command_id,action='delete',revision=2,uuid='',short_id='')
                intent.pop('lease_seconds')
                result = self.driver.node_action(command_id,'temporary_revoke',intent)
                expected_mode = 'new_connections_only' if row['protocol']=='xray' else 'peer_removed'
                if result.get('revocation_mode') != expected_mode:
                    raise ValueError('invalid revocation receipt')
                with self.db.transaction() as conn:
                    status = 'expired' if row['revoke_reason']=='expiry' else 'revoked'
                    changed = conn.execute("UPDATE backend_temporary_configs SET status=?,error_code=NULL WHERE id=? AND status='revoking' RETURNING id",(status,row['id'])).fetchone()
                    if changed:
                        self.event(conn,row,'revoke',status)
        except Exception:
            with self.db.transaction() as conn:
                status = 'blocked' if issuing else 'revoke_blocked'
                changed = conn.execute('UPDATE backend_temporary_configs SET status=?,error_code=? WHERE id=? AND status=? RETURNING id',
                    (status,'outcome_unknown',row['id'],'issuing' if issuing else 'revoking')).fetchone()
                if changed:
                    self.event(conn,row,'issue' if issuing else 'revoke',status)
        return True

    def _readable(self, conn, actor, config_id):
        self.authorize(conn,actor)
        from .maintenance_gate import active
        if active(conn) or conn.execute("SELECT 1 FROM backend_backup_jobs WHERE action='restore' AND status IN ('awaiting_executor','running')").fetchone():
            raise AccessDenied('maintenance_busy',409)
        row = conn.execute('SELECT * FROM backend_temporary_configs WHERE id=?',(config_id,)).fetchone()
        if row is None:
            raise AccessDenied('resource_not_found',404)
        if row['status'] != 'active':
            raise AccessDenied('config_not_ready',409)
        if datetime.fromisoformat(row['expires_at']) <= now():
            raise AccessDenied('config_expired',410)
        node = self.ready_node(conn,row['node_key'],row['protocol'],row['transport'])
        if node['desired_revision'] != row['node_revision']:
            raise AccessDenied('config_stale',409)
        return row,node

    def artifact(self, actor, config_id):
        from contextlib import nullcontext
        from .config_issuance import ConfigIssuanceService
        with (nullcontext(self.driver) if self.driver is not None else ConfigIssuanceService(self.db)._driver()) as driver:
            try:
                return self._artifact(actor,config_id,driver)
            except AccessDenied:
                raise
            except Exception:
                raise AccessDenied('config_unavailable',503) from None

    def _artifact(self, actor, config_id, driver):
        with self.db.connect() as conn:
            row,node = self._readable(conn,actor,config_id)
        intent = json.loads(row['intent_json'])
        state = driver.node_action(row['id'],'temporary_status',intent,recover=True)
        if (state.get('status') != 'active' or state.get('enforcement_ready') is not True
                or datetime.fromisoformat(state['expires_at']) != datetime.fromisoformat(row['expires_at'])):
            raise AccessDenied('config_unavailable',503)
        receipt = driver.node_action(row['id'],'temporary_ensure',intent,recover=True)
        if receipt.get('revocation_mode') != self.public(row)['revocation_mode']:
            raise AccessDenied('config_stale',409)
        inspection = {'node_key':row['node_key'],'runtime_name':row['runtime_name'],
                      'protocol':row['protocol'],'action':'ensure','desired_revision':1}
        if row['protocol']=='xray':
            inspection['xray'] = {'uuid':intent['uuid'],'short_id':intent['short_id']}
            if receipt.get('xray_uuid') != intent['uuid']:
                raise AccessDenied('config_stale',409)
        observation = driver.inspect(inspection)
        if not all(observation[k] for k in ('disk_present','live_present','identity_matches','config_available')):
            raise AccessDenied('config_unavailable',503)
        if receipt.get('expires_at') != row['expires_at']:
            # datetime equality permits equivalent UTC encodings, but never a renewed lease.
            if datetime.fromisoformat(receipt['expires_at']) != datetime.fromisoformat(row['expires_at']):
                raise AccessDenied('config_stale',409)
        if row['protocol']=='awg':
            config = driver.refresh_awg_config(row['node_key'],receipt['wg_conf'],node['title']+' · Temporary')
            files = [{'filename':'Temporary AmneziaWG.'+ext,'content':config[key]} for ext,key in (('vpn','vpn_key'),('conf','wg_conf'))]
            content = config['wg_conf'] if row['transport']=='conf' else config['vpn_key']
        else:
            metadata = driver.read_xray_public(row['node_key'])
            settings = json.loads(node['settings_json'])
            if (metadata['sni'] != settings['xray_sni'] or
                    metadata['tcp_port'] != settings['xray_tcp_port'] or
                    metadata['xhttp_port'] != settings['xray_xhttp_port'] or
                    metadata['xhttp_path'] != settings['xray_xhttp_path']):
                raise AccessDenied('config_stale',409)
            host = settings.get('xray_host',settings['public_host'])
            port = metadata['tcp_port'] if row['transport']=='tcp' else metadata['xhttp_port']
            params = {'encryption':'none','security':'reality','sni':metadata['sni'],
                'fp':settings.get('xray_fingerprint','chrome'),'pbk':metadata['public_key'],'sid':metadata['short_id'],
                'type':row['transport']}
            if row['transport']=='tcp':
                params['flow']='xtls-rprx-vision'
            else:
                params.update(path=metadata['xhttp_path'],mode='auto',extra='{"xmux":{"maxConcurrency":"16-32"}}')
            content = 'vless://'+intent['uuid']+'@'+host+':'+str(port)+'?'+ '&'.join(k+'='+quote(v,safe='') for k,v in params.items())+'#'+quote(node['title']+' · Temporary',safe='')
            files = [{'filename':'Temporary VLESS.txt','content':content}]
        with self.db.transaction() as conn:
            conn.execute('UPDATE backend_account_guard SET revision=revision+1 WHERE id=1')
            self._readable(conn,actor,config_id)
            self.event(conn,row,'download','active',actor)
        return {'content':content,'files':files,'expires_at':row['expires_at'],
                'revocation_mode':self.public(row)['revocation_mode']}
