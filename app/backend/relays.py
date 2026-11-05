"""The web cabinet's relay: a system service on a node.

The administrator picks a node and gives the relay a domain, a port and a
TLS mode; the worker asks the driver to apply it. The driver has the node's
agent install the relay, pins the relay's key and keeps a link to it, over
which the cabinet's requests reach this API. One relay per installation for
now. The relay's own settings and how far they are applied live in
`backend_relays`; its instance on the node is a service instance like the
VPN engines'.
"""
from datetime import datetime, timedelta, timezone
import json
import re
from uuid import uuid4

from .authorization import RELAY_PERMISSION, AccessDenied, require_permission

TLS_MODES = ('acme', 'files', 'proxy')
# An unreachable agent or driver is retried no more often than this.
RETRY = timedelta(seconds=30)
_LABEL = r'(?!-)[a-z0-9-]{1,63}(?<!-)'
_DOMAIN = re.compile(rf'(?=.{{4,253}}\Z){_LABEL}(?:\.{_LABEL})+\Z')
_PATH = re.compile(r'/[A-Za-z0-9._/+@:=-]{1,255}\Z')
_CONTACT = re.compile(r'[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,63}\Z')


def _now():
    return datetime.now(timezone.utc)


def settings(values):
    """Checked relay settings, or AccessDenied with the reason."""
    if not isinstance(values, dict):
        raise AccessDenied('invalid_input', 422)
    domain = values.get('domain')
    if not isinstance(domain, str) or not _DOMAIN.fullmatch(domain.strip().lower()):
        raise AccessDenied('relay_domain_invalid', 422)
    tls = values.get('tls')
    if tls not in TLS_MODES:
        raise AccessDenied('relay_tls_invalid', 422)
    port = values.get('port', 443)
    if type(port) is not int or not 1 <= port <= 65535:
        raise AccessDenied('relay_port_invalid', 422)
    if tls == 'acme' and port != 443:
        # Let's Encrypt validates the domain over TLS on port 443.
        raise AccessDenied('relay_acme_needs_port_443', 422)
    result = {'domain': domain.strip().lower(), 'port': port, 'tls': tls,
              'acme_contact': None, 'certificate_path': None, 'key_path': None}
    if tls == 'acme':
        contact = values.get('acme_contact') or None
        if contact is not None and (not isinstance(contact, str) or not _CONTACT.fullmatch(contact)):
            raise AccessDenied('relay_acme_contact_invalid', 422)
        result['acme_contact'] = contact
    if tls == 'files':
        for field in ('certificate_path', 'key_path'):
            value = values.get(field)
            if not isinstance(value, str) or not _PATH.fullmatch(value) or '/../' in value + '/':
                raise AccessDenied('relay_certificate_path_invalid', 422)
            result[field] = value
    return result


def tcp_ports(protocols, node_settings):
    """The TCP ports the node's VPN engines listen on; AWG uses UDP."""
    if 'xray' not in protocols:
        return set()
    return {node_settings.get('xray_tcp_port'), node_settings.get('xray_xhttp_port')} - {None}


def port_conflict(conn, node_key, protocols, node_settings):
    """True when the node's relay listens on one of these engine ports."""
    row = conn.execute("SELECT port FROM backend_relays WHERE node_key=? AND status<>'removing'",
                       (node_key,)).fetchone()
    return row is not None and row['port'] in tcp_ports(protocols, node_settings)


def public(row, live=None):
    if row is None:
        return None
    result = {'node_key': row['node_key'], 'instance_id': row['instance_id'], 'domain': row['domain'],
              'port': row['port'], 'tls': row['tls'], 'acme_contact': row['acme_contact'],
              'certificate_path': row['certificate_path'], 'key_path': row['key_path'],
              'status': row['status'], 'revision': row['revision'], 'applied_revision': row['applied_revision'],
              'relay_key': row['relay_key'], 'error_code': row['error_code'],
              'url': f"https://{row['domain']}" if row['tls'] == 'proxy' or row['port'] == 443
              else f"https://{row['domain']}:{row['port']}",
              'updated_at': row['updated_at'], 'link': None}
    if live is not None:
        result['link'] = {'agent_reachable': live['agent_reachable'], 'running': live['active'],
                          'connected': live['link_connected'], 'error': live['link_error'],
                          'since': (datetime.fromtimestamp(live['link_since'], timezone.utc).isoformat()
                                    if live['link_since'] else None)}
    return result


class RelayService:
    def __init__(self, db, driver=None):
        self.db, self.driver = db, driver

    def initialize_schema(self):
        from db.migration_revisions.r0038_relays import create_schema
        with self.db.transaction() as conn:
            create_schema(conn)

    def _row(self, conn):
        return conn.execute('SELECT * FROM backend_relays ORDER BY created_at LIMIT 1').fetchone()

    def get(self, actor, *, live=True):
        """The relay, with what the driver sees of it now when it can tell."""
        require_permission(actor, RELAY_PERMISSION)
        with self.db.connect() as conn:
            row = self._row(conn)
        status = None
        if row is not None and live and self.driver is not None and row['status'] != 'removing':
            try:
                status = self.driver.relay_status(row['node_key'])
            except Exception:
                status = None
        return {'relay': public(row, status)}

    def configure(self, actor, node_key, values):
        """Set up the relay on the node, or change its settings."""
        require_permission(actor, RELAY_PERMISSION)
        checked = settings(values)
        now = _now().isoformat()
        with self.db.transaction() as conn:
            from .nodes import NodeService, agent_installed, protocol_defaults
            node = conn.execute(NodeService._select() + ' WHERE n.key=?', (node_key,)).fetchone()
            if node is None:
                raise AccessDenied('resource_not_found', 404)
            if not agent_installed(node):
                raise AccessDenied('relay_node_without_agent', 409)
            protocols = json.loads(node['protocols_json'])
            if checked['port'] in tcp_ports(protocols, protocol_defaults(json.loads(node['settings_json']), protocols)):
                raise AccessDenied('relay_port_in_use', 409)
            row = self._row(conn)
            if row is not None and row['node_key'] != node_key:
                raise AccessDenied('relay_exists_on_another_node', 409)
            if row is not None and row['status'] == 'removing':
                raise AccessDenied('relay_removing', 409)
            if row is None:
                instance = str(uuid4())
                conn.execute('''INSERT INTO backend_service_instances(id,node_key,service,created_at)
                    VALUES (?,?,'relay',?)''', (instance, node_key, now))
                conn.execute('''INSERT INTO backend_relays(instance_id,node_key,domain,port,tls,acme_contact,
                    certificate_path,key_path,status,revision,actor_id,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,'applying',1,?,?,?)''',
                    (instance, node_key, checked['domain'], checked['port'], checked['tls'],
                     checked['acme_contact'], checked['certificate_path'], checked['key_path'],
                     actor.account.id, now, now))
            else:
                conn.execute('''UPDATE backend_relays SET domain=?, port=?, tls=?, acme_contact=?,
                    certificate_path=?, key_path=?, status='applying', revision=revision+1,
                    error_code=NULL, attempted_at=NULL, actor_id=?, updated_at=? WHERE instance_id=?''',
                    (checked['domain'], checked['port'], checked['tls'], checked['acme_contact'],
                     checked['certificate_path'], checked['key_path'], actor.account.id, now,
                     row['instance_id']))
            row = self._row(conn)
        return {'relay': public(row)}

    def remove(self, actor):
        require_permission(actor, RELAY_PERMISSION)
        with self.db.transaction() as conn:
            row = self._row(conn)
            if row is None:
                raise AccessDenied('resource_not_found', 404)
            conn.execute('''UPDATE backend_relays SET status='removing', error_code=NULL, attempted_at=NULL,
                actor_id=?, updated_at=? WHERE instance_id=?''', (actor.account.id, _now().isoformat(),
                                                                  row['instance_id']))
            row = self._row(conn)
        return {'relay': public(row)}

    def recover(self):
        """At worker start: apply the relay again. The driver may have lost
        its pinned key (a new driver state); applying unchanged settings
        restarts nothing on the node."""
        with self.db.transaction() as conn:
            conn.execute("""UPDATE backend_relays SET status='applying', attempted_at=NULL
                WHERE status IN ('active','failed')""")

    def run_one(self):
        now = _now()
        with self.db.connect() as conn:
            row = conn.execute('''SELECT * FROM backend_relays WHERE status IN ('applying','removing')
                AND (attempted_at IS NULL OR attempted_at<=?) ORDER BY updated_at LIMIT 1''',
                ((now - RETRY).isoformat(),)).fetchone()
        if row is None or self.driver is None:
            return False
        import grpc
        error, result = None, None
        try:
            if row['status'] == 'removing':
                self.driver.remove_relay(row['node_key'])
            else:
                result = self.driver.apply_relay(row['node_key'], dict(row))
        except grpc.RpcError as failure:
            details = failure.details() or ''
            if failure.code() == grpc.StatusCode.INVALID_ARGUMENT and re.fullmatch(r'relay_[a-z0-9_]+', details):
                error = (details, True)
            elif failure.code() in (grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED):
                # The agent or the driver is away for now: try again later.
                error = ('agent_unavailable', False)
            else:
                error = ('relay_apply_failed', True)
        except Exception:
            error = ('relay_apply_failed', True)
        with self.db.transaction() as conn:
            current = conn.execute('SELECT status,revision FROM backend_relays WHERE instance_id=?',
                                   (row['instance_id'],)).fetchone()
            if current is None or (current['status'], current['revision']) != (row['status'], row['revision']):
                # Changed meanwhile; the next run applies the new state.
                return True
            stamp = now.isoformat()
            if error is not None:
                code, final = error
                status = 'failed' if final and row['status'] == 'applying' else row['status']
                conn.execute('''UPDATE backend_relays SET status=?, error_code=?, attempted_at=?
                    WHERE instance_id=?''', (status, code, stamp, row['instance_id']))
            elif row['status'] == 'removing':
                conn.execute('DELETE FROM backend_service_instances WHERE id=?', (row['instance_id'],))
                conn.execute('DELETE FROM backend_relays WHERE instance_id=?', (row['instance_id'],))
            else:
                conn.execute('''UPDATE backend_relays SET status='active', applied_revision=revision,
                    relay_key=?, error_code=NULL, attempted_at=?, updated_at=? WHERE instance_id=?''',
                    (result['relay_key'], stamp, stamp, row['instance_id']))
        return True
