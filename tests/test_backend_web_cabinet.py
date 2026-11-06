"""The web cabinet: served by the relay's node or the controller, applied by
the worker, its address the passkeys' domain."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import grpc

from backend import web_cabinet
from backend.authorization import AccessDenied
from backend.passkeys import relying_party
from backend.web_cabinet import WebCabinetService
from tests import test_backend_relays as relays
from tests.test_backend_relays import Failure


class Driver(relays.Driver):
    def apply_cabinet(self, node_key, root, version):
        self.calls.append(('cabinet', node_key, root, version))
        if self.failure:
            raise self.failure
        return {}

    def remove_cabinet(self, node_key):
        self.calls.append(('cabinet-remove', node_key))
        if self.failure:
            raise self.failure
        return {}


class WebCabinetTests(unittest.TestCase):
    def setUp(self):
        relays.RelayTests.setUp(self)
        self.driver = Driver()
        self.relays.driver = self.driver
        self.cabinet = WebCabinetService(self.db, self.driver)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.build = Path(temporary.name) / 'cabinet'
        self.build.mkdir()
        (self.build / 'index.html').write_text('<html></html>')
        self.settings = Path(temporary.name) / 'cabinet-local.json'
        environment = patch.dict(os.environ, {'NODE_PLANE_CABINET_DIR': str(self.build),
                                              'NODE_PLANE_CABINET_LOCAL_SETTINGS': str(self.settings)})
        environment.start()
        self.addCleanup(environment.stop)

    def relay(self):
        self.relays.configure(self.actor, 'n1', {'domain': 'cab.example.com', 'tls': 'proxy', 'port': 9443})
        self.relays.run_one()

    def refused(self, code, work):
        with self.assertRaises(AccessDenied) as raised:
            work()
        self.assertEqual(raised.exception.code, code)

    def test_served_by_the_relays_node_on_its_domain(self):
        self.refused('cabinet_needs_relay', lambda: self.cabinet.configure(self.actor, 'relay'))
        self.relay()
        cabinet = self.cabinet.configure(self.actor, 'relay')['cabinet']
        self.assertEqual((cabinet['node_key'], cabinet['url'], cabinet['state']),
                         ('n1', 'https://cab.example.com', 'changes_pending'))
        self.assertTrue(self.cabinet.run_one())
        self.assertEqual(self.driver.calls[-1], ('cabinet', 'n1', str(self.build), web_cabinet.APP_SEMVER))
        cabinet = self.cabinet.get(self.actor)['cabinet']
        self.assertEqual((cabinet['status'], cabinet['applied_version']), ('active', web_cabinet.APP_SEMVER))
        with self.db.connect() as conn:
            self.assertEqual(relying_party(conn), ('cab.example.com', 'https://cab.example.com'))
        # The relay stays while the cabinet is served beside it.
        self.refused('relay_serves_cabinet', lambda: self.relays.remove(self.actor))
        self.cabinet.remove(self.actor)
        self.cabinet.run_one()
        self.assertEqual(self.driver.calls[-1], ('cabinet-remove', 'n1'))
        self.assertIsNone(self.cabinet.get(self.actor)['cabinet'])
        with self.db.connect() as conn:
            self.refused('passkeys_unavailable', lambda: relying_party(conn))
        self.relays.remove(self.actor)

    def test_served_by_the_controller(self):
        self.refused('cabinet_listen_invalid',
                     lambda: self.cabinet.configure(self.actor, 'local', listen='cab:80', public_url='https://a.b'))
        self.refused('cabinet_listen_invalid', lambda: self.cabinet.configure(
            self.actor, 'local', listen='127.0.0.1:8080', public_url='https://a.b'))
        for url in ('ftp://a.b', 'https://a.b/path', 'https://user@a.b', 'https://', 'https://a b'):
            with self.subTest(url=url):
                self.refused('cabinet_url_invalid', lambda: self.cabinet.configure(
                    self.actor, 'local', listen='0.0.0.0:8090', public_url=url))
        cabinet = self.cabinet.configure(self.actor, 'local', listen='0.0.0.0:8090',
                                         public_url='HTTPS://Cab.Home.Example:443/')['cabinet']
        self.assertEqual(cabinet['url'], 'https://cab.home.example')
        self.cabinet.run_one()
        self.assertEqual(json.loads(self.settings.read_text()),
                         {'listen': '0.0.0.0:8090', 'root': str(self.build), 'version': web_cabinet.APP_SEMVER})
        self.assertEqual(self.driver.calls, [])
        with self.db.connect() as conn:
            self.assertEqual(relying_party(conn), ('cab.home.example', 'https://cab.home.example'))
        # Moving to the relay's node needs the controller to give it up first.
        self.relay()
        self.refused('cabinet_mode_change', lambda: self.cabinet.configure(self.actor, 'relay'))
        self.cabinet.remove(self.actor)
        self.cabinet.run_one()
        self.assertFalse(self.settings.exists())
        self.cabinet.configure(self.actor, 'relay')

    def test_needs_the_build_and_retries_an_absent_agent(self):
        (self.build / 'index.html').unlink()
        self.refused('cabinet_build_missing', lambda: self.cabinet.configure(self.actor, 'relay'))
        (self.build / 'index.html').write_text('<html></html>')
        self.relay()
        self.cabinet.configure(self.actor, 'relay')
        self.driver.failure = Failure(grpc.StatusCode.UNAVAILABLE)
        self.cabinet.run_one()
        cabinet = self.cabinet.get(self.actor)['cabinet']
        self.assertEqual((cabinet['status'], cabinet['error_code']), ('applying', 'agent_unavailable'))
        # Not again before the pause.
        self.assertFalse(self.cabinet.run_one())
        with self.db.transaction() as conn:
            conn.execute('UPDATE backend_web_cabinet SET attempted_at=NULL')
        self.driver.failure = Failure(grpc.StatusCode.INVALID_ARGUMENT, 'cabinet_files_invalid')
        self.cabinet.run_one()
        cabinet = self.cabinet.get(self.actor)['cabinet']
        self.assertEqual((cabinet['status'], cabinet['error_code']), ('failed', 'cabinet_files_invalid'))
        # A new release applies the files again when the worker starts.
        self.driver.failure = None
        self.cabinet.recover()
        self.cabinet.run_one()
        self.assertEqual(self.cabinet.get(self.actor)['cabinet']['status'], 'active')

    def test_only_owners_and_administrators(self):
        from backend.authorization import Account, Actor, Principal, PrincipalKind
        operator = Actor(Principal('op', PrincipalKind.ACCOUNT, frozenset({'nodes.manage'})),
                         Account(self.actor.account.id, 'admin', 'approved', frozenset({'operator'})))
        self.refused('permission_denied', lambda: self.cabinet.get(operator))
        self.refused('permission_denied', lambda: self.cabinet.configure(operator, 'relay'))


if __name__ == '__main__':
    unittest.main()
