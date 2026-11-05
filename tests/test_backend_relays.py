"""The web cabinet's relay: settings, its instance on a node, and the worker
that applies it through the driver."""
from types import SimpleNamespace
from uuid import uuid4
import unittest

import grpc

from backend.authorization import AccessDenied, Account, Actor, Principal, PrincipalKind
from backend.nodes import NodeService
from backend.relays import RelayService
from tests import test_backend_node_jobs as fixtures

KEY = 'ab' * 32


class Failure(grpc.RpcError):
    def __init__(self, code, details=''):
        self._code, self._details = code, details

    def code(self):
        return self._code

    def details(self):
        return self._details


class Driver:
    def __init__(self):
        self.calls = []
        self.failure = None

    def apply_relay(self, node_key, settings):
        self.calls.append(('apply', node_key, settings['domain'], settings['port'], settings['tls']))
        if self.failure:
            raise self.failure
        return {'relay_key': KEY, 'active': True}

    def remove_relay(self, node_key):
        self.calls.append(('remove', node_key))
        if self.failure:
            raise self.failure
        return {}

    def relay_status(self, node_key):
        return {'agent_reachable': True, 'installed': True, 'active': True, 'relay_key': KEY, 'linked': True,
                'link_connected': True, 'link_error': None, 'link_since': 1_700_000_000,
                'url': 'https://cab.example.com'}


class RelayTests(unittest.TestCase):
    def setUp(self):
        fixtures.BackendNodeJobTests.setUp(self)
        with self.db.transaction() as conn:
            from backend.agent_installations import record
            record(conn, 'n1', 'token')
        self.driver = Driver()
        self.relays = RelayService(self.db, self.driver)

    def configure(self, **values):
        values = {'domain': 'Cab.Example.com', 'tls': 'proxy', 'port': 9443, **values}
        return self.relays.configure(self.actor, values.pop('node_key', 'n1'), values)['relay']

    def node_instances(self):
        return NodeService(self.db).get(self.actor, 'n1')['service_instances']

    def test_settings_are_checked(self):
        for values, code in [
            ({'domain': 'localhost'}, 'relay_domain_invalid'),
            ({'domain': 'a b.example'}, 'relay_domain_invalid'),
            ({'tls': 'none'}, 'relay_tls_invalid'),
            ({'port': 0}, 'relay_port_invalid'),
            ({'tls': 'acme', 'port': 8443}, 'relay_acme_needs_port_443'),
            ({'tls': 'acme', 'port': 443, 'acme_contact': 'nobody'}, 'relay_acme_contact_invalid'),
            ({'tls': 'files', 'certificate_path': 'cert.pem', 'key_path': '/k'}, 'relay_certificate_path_invalid'),
            ({'tls': 'files', 'certificate_path': '/etc/../c', 'key_path': '/k'}, 'relay_certificate_path_invalid'),
        ]:
            with self.subTest(values=values), self.assertRaises(AccessDenied) as raised:
                self.configure(**values)
            self.assertEqual(raised.exception.code, code)

    def test_the_relay_cannot_take_a_vpn_port(self):
        # Xray listens on 443 and 8443 on this node.
        for port in (443, 8443):
            with self.assertRaises(AccessDenied) as raised:
                self.configure(port=port)
            self.assertEqual(raised.exception.code, 'relay_port_in_use')
        self.configure(port=9443)
        # Nor may the node's ports move onto the relay's later.
        node = NodeService(self.db).get(self.actor, 'n1')
        with self.assertRaises(AccessDenied) as raised:
            NodeService(self.db).command(self.actor, action='edit', node_key='n1', command_key=str(uuid4()),
                                         revision=node['desired_revision'],
                                         values={'settings': {'xray_xhttp_port': 9443}})
        self.assertEqual(raised.exception.code, 'relay_port_in_use')

    def test_a_relay_is_an_instance_applied_by_the_worker(self):
        relay = self.configure()
        self.assertEqual((relay['status'], relay['revision'], relay['domain']), ('applying', 1, 'cab.example.com'))
        self.assertEqual(relay['url'], 'https://cab.example.com')
        self.assertEqual([i['state'] for i in self.node_instances() if i['service'] == 'relay'], ['changes_pending'])

        self.assertTrue(self.relays.run_one())
        self.assertEqual(self.driver.calls, [('apply', 'n1', 'cab.example.com', 9443, 'proxy')])
        relay = self.relays.get(self.actor)['relay']
        self.assertEqual((relay['status'], relay['applied_revision'], relay['relay_key']), ('active', 1, KEY))
        self.assertEqual(relay['link']['connected'], True)
        self.assertEqual(relay['link']['since'], '2023-11-14T22:13:20+00:00')
        self.assertEqual([i['state'] for i in self.node_instances() if i['service'] == 'relay'], ['active'])
        self.assertFalse(self.relays.run_one())

        # A change is applied again; removal takes the instance away.
        self.assertEqual(self.configure(tls='files', port=9443, certificate_path='/etc/ssl/c.pem',
                                        key_path='/etc/ssl/c.key')['revision'], 2)
        self.assertTrue(self.relays.run_one())
        self.assertEqual(self.relays.remove(self.actor)['relay']['status'], 'removing')
        self.assertTrue(self.relays.run_one())
        self.assertEqual(self.driver.calls[-1], ('remove', 'n1'))
        self.assertIsNone(self.relays.get(self.actor)['relay'])
        self.assertEqual([i for i in self.node_instances() if i['service'] == 'relay'], [])

    def test_one_relay_per_installation(self):
        self.configure()
        with self.db.transaction() as conn:
            conn.execute('''INSERT INTO backend_nodes(key,title,region,flag,enabled,protocols_json,
                xray_transports_json,settings_json) VALUES ('n2','Two','EU','',1,'[]','[]','{}')''')
            from backend.agent_installations import record
            record(conn, 'n2', 'token')
        with self.assertRaises(AccessDenied) as raised:
            self.configure(node_key='n2')
        self.assertEqual(raised.exception.code, 'relay_exists_on_another_node')

    def test_a_node_needs_its_agent(self):
        with self.db.transaction() as conn:
            conn.execute('DELETE FROM backend_agent_installations')
            conn.execute("UPDATE backend_nodes SET applied_revision=0 WHERE key='n1'")
        with self.assertRaises(AccessDenied) as raised:
            self.configure()
        self.assertEqual(raised.exception.code, 'relay_node_without_agent')

    def test_failures(self):
        self.configure()
        # An unreachable agent is retried; the relay keeps applying.
        self.driver.failure = Failure(grpc.StatusCode.UNAVAILABLE)
        self.assertTrue(self.relays.run_one())
        relay = self.relays.get(self.actor, live=False)['relay']
        self.assertEqual((relay['status'], relay['error_code']), ('applying', 'agent_unavailable'))
        self.assertFalse(self.relays.run_one())  # not before the retry delay
        # What the agent refuses needs the administrator.
        with self.db.transaction() as conn:
            conn.execute('UPDATE backend_relays SET attempted_at=NULL')
        self.driver.failure = Failure(grpc.StatusCode.INVALID_ARGUMENT, 'relay_start_failed')
        self.assertTrue(self.relays.run_one())
        relay = self.relays.get(self.actor, live=False)['relay']
        self.assertEqual((relay['status'], relay['error_code']), ('failed', 'relay_start_failed'))
        self.assertEqual([i['state'] for i in self.node_instances() if i['service'] == 'relay'], ['failed'])
        # Worker start applies it again.
        self.relays.recover()
        self.driver.failure = None
        self.assertTrue(self.relays.run_one())
        self.assertEqual(self.relays.get(self.actor, live=False)['relay']['status'], 'active')

    def test_a_node_with_the_relay_is_not_removed(self):
        from backend.node_lifecycle import NodeLifecycle
        self.configure()
        with self.assertRaises(AccessDenied) as raised:
            NodeLifecycle(self.db).start_drain(self.actor, 'n1')
        self.assertEqual(raised.exception.code, 'node_has_relay')

    def test_only_owners_and_administrators_manage_the_relay(self):
        operator = Actor(Principal('op', PrincipalKind.ACCOUNT, frozenset({'nodes.manage'})),
                         Account(self.actor.account.id, 'admin', 'approved', frozenset({'operator'})))
        with self.assertRaises(AccessDenied):
            self.relays.configure(operator, 'n1', {'domain': 'cab.example.com', 'tls': 'proxy', 'port': 9443})
        with self.assertRaises(AccessDenied):
            self.relays.get(operator)


if __name__ == '__main__':
    unittest.main()
