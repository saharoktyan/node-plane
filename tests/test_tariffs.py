"""Tariffs: linked limits, copied term and nodes, and their commands."""
from datetime import datetime, timedelta, timezone
import unittest

from backend.speed_limits import SpeedLimits, node_snapshot
from backend.vpn_policies import effective
from tests import test_connection_limits as fixture


def titles(text, source='en', **others):
    return {'source': source, 'texts': {source: text, **others}}


class TariffTests(fixture.Fixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        SpeedLimits(self.db).initialize_schema()

    def revision(self):
        return self.db.connection.execute('SELECT desired_revision FROM backend_profiles WHERE id=?',
                                          (self.profile,)).fetchone()[0]

    def tariff(self, **fields):
        response = self.client.post('/api/v1/tariffs', headers=self.headers_for(),
                                    json={'titles': titles('Basic'), **fields})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def assign(self, tariff_id, **fields):
        return self.client.put(f'/api/v1/profiles/{self.profile}/tariff', headers=self.headers_for(self.revision()),
                               json={'tariff_id': tariff_id, **fields})

    def resolved(self, *keys):
        with self.db.connect() as conn:
            return effective(conn, profile_id=self.profile, keys=keys)

    def expiry(self):
        value = self.db.connection.execute('SELECT expires_at FROM backend_profiles WHERE id=?',
                                           (self.profile,)).fetchone()[0]
        return value and datetime.fromisoformat(value)

    def test_tariff_limits_sit_between_installation_and_profile_and_stay_linked(self):
        self.installation('speed.down_kbit', 50000)
        tariff = self.tariff(policies={'speed.down_kbit': 10000, 'devices.max': 2})
        response = self.assign(tariff['id'])
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.resolved('speed.down_kbit', 'devices.max'),
                         {'speed.down_kbit': 10000, 'devices.max': 2})
        with self.db.connect() as conn:
            clients = node_snapshot(conn, 'node', ['xray'])
        self.assertEqual(clients[0]['down_kbit'], 10000)
        # Editing the tariff changes its profiles at once.
        edited = self.client.patch(f"/api/v1/tariffs/{tariff['id']}", headers=self.headers_for(tariff['revision']),
                                   json={'policies': {'speed.down_kbit': 20000}})
        self.assertEqual(edited.status_code, 200, edited.text)
        self.assertEqual(edited.json()['profiles'], 1)
        self.assertEqual(self.resolved('speed.down_kbit', 'devices.max'),
                         {'speed.down_kbit': 20000, 'devices.max': None})
        # The profile's own value wins; removing it falls back to the tariff.
        path = f'/api/v1/profiles/{self.profile}/vpn-policies'
        self.set_policy(path, 'speed.down_kbit', 0)
        self.assertEqual(self.resolved('speed.down_kbit')['speed.down_kbit'], 0)
        keys = {item['key']: item for item in self.client.get(path, headers=self.headers_for()).json()['keys']}
        self.assertEqual((keys['speed.down_kbit']['tariff'], keys['speed.down_kbit']['effective']), (20000, 0))
        # Unlinking returns to the installation value.
        self.set_policy(path, 'speed.down_kbit', None)
        self.assertEqual(self.assign(None).status_code, 200)
        self.assertEqual(self.resolved('speed.down_kbit')['speed.down_kbit'], 50000)

    def test_term_starts_or_extends_and_nodes_are_copied_on_assignment(self):
        self.db.connection.execute('''INSERT INTO backend_nodes(key,title,region,protocols_json,enabled,applied_revision)
            VALUES ('second','Second','test','["xray"]',1,1)''')
        tariff = self.tariff(duration_days=30, access_policy={
            'explicit_grants': [{'node_key': 'second', 'protocol': 'xray'}], 'rules': [], 'exclusions': []})
        before = datetime.now(timezone.utc)
        response = self.assign(tariff['id'], expiry='start', apply_access=True)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['grants'], [{'node_key': 'second', 'protocol': 'xray', 'service_instance_id': None}])
        self.assertIsNotNone(response.json()['operation_id'])
        started = self.expiry()
        self.assertTrue(before + timedelta(days=30) <= started <= datetime.now(timezone.utc) + timedelta(days=30))
        # Renewal adds the term after the remaining time.
        self.assertEqual(self.assign(tariff['id'], expiry='extend').status_code, 200)
        self.assertEqual(self.expiry(), started + timedelta(days=30))
        # An expired profile renews from now.
        self.db.connection.execute('UPDATE backend_profiles SET expires_at=? WHERE id=?',
            ((datetime.now(timezone.utc) - timedelta(days=3)).isoformat(), self.profile))
        self.assertEqual(self.assign(tariff['id'], expiry='extend').status_code, 200)
        self.assertGreater(self.expiry(), datetime.now(timezone.utc) + timedelta(days=29))
        summary = self.client.get(f'/api/v1/profiles/{self.profile}/summary', headers=self.headers_for()).json()
        self.assertEqual((summary['tariff']['title'], summary['tariff']['duration_days']), ('Basic', 30))
        # Keeping the term changes neither expiry nor nodes.
        kept = self.expiry()
        self.assertEqual(self.assign(tariff['id']).json()['grants'], [{'node_key': 'second', 'protocol': 'xray', 'service_instance_id': None}])
        self.assertEqual(self.expiry(), kept)

    def test_assignment_refuses_what_the_tariff_does_not_define(self):
        tariff = self.tariff()
        self.assertEqual(self.assign(tariff['id'], expiry='start').json()['error']['code'], 'tariff_without_term')
        self.assertEqual(self.assign(tariff['id'], apply_access=True).json()['error']['code'],
                         'tariff_without_access')
        self.assertEqual(self.assign(None, expiry='start').status_code, 422)
        missing = self.assign('00000000-0000-4000-8000-000000000000')
        self.assertEqual(missing.status_code, 404)
        self.assertIsNone(self.expiry())

    def test_administrator_profiles_keep_their_term_and_access(self):
        self.db.connection.execute('''UPDATE backend_accounts SET role='admin'
            WHERE id=(SELECT owner_account_id FROM backend_profiles WHERE id=?)''', (self.profile,))
        tariff = self.tariff(duration_days=30, policies={'speed.up_kbit': 1000})
        refused = self.assign(tariff['id'], expiry='start')
        self.assertEqual(refused.json()['error']['code'], 'admin_access_permanent')
        self.assertEqual(self.assign(tariff['id']).status_code, 200)
        self.assertEqual(self.resolved('speed.up_kbit')['speed.up_kbit'], 1000)

    def test_tariffs_are_validated_unique_and_kept_while_in_use(self):
        for body in ({'titles': titles(' ')}, {'titles': titles('X', ru='a\nb')},
                     {'titles': {'source': 'ru', 'texts': {'en': 'X'}}},
                     {'titles': titles('X'), 'duration_days': 0},
                     {'titles': titles('X'), 'policies': {'quota.timezone': 'UTC'}},
                     {'titles': titles('X'), 'policies': {'speed.up_kbit': -1}},
                     {'titles': titles('X'), 'policies': {'unknown': 1}}):
            response = self.client.post('/api/v1/tariffs', headers=self.headers_for(), json=body)
            self.assertEqual(response.status_code, 422, body)
        tariff = self.tariff()
        duplicate = self.client.post('/api/v1/tariffs', headers=self.headers_for(), json={'titles': titles(' basic ', 'ru')})
        self.assertEqual(duplicate.json()['error']['code'], 'tariff_exists')
        stale = self.client.patch(f"/api/v1/tariffs/{tariff['id']}", headers=self.headers_for(5),
                                  json={'duration_days': 7})
        self.assertEqual(stale.status_code, 412)
        self.assertEqual(self.assign(tariff['id']).status_code, 200)
        path = f"/api/v1/tariffs/{tariff['id']}"
        self.assertEqual(self.client.delete(path, headers=self.headers_for(1)).json()['error']['code'], 'tariff_in_use')
        self.assertEqual(self.assign(None).status_code, 200)
        self.assertEqual(self.client.delete(path, headers=self.headers_for(1)).status_code, 200)
        listed = self.client.get('/api/v1/tariffs', headers=self.headers_for()).json()
        self.assertEqual(listed['items'], [])
        self.assertIn('speed.up_kbit', listed['keys'])
        self.assertNotIn('quota.timezone', listed['keys'])

    def test_titles_and_descriptions_in_several_languages(self):
        tariff = self.tariff(titles=titles('Month', ru='Месяц'), duration_days=30,
                             descriptions={'source': 'en', 'texts': {'en': 'Two devices.'}})
        self.assertEqual((tariff['title'], tariff['description']), ('Month', 'Two devices.'))
        self.assertEqual(tariff['titles']['shown'], {'en': 'en', 'ru': 'ru'})
        self.assertEqual(tariff['descriptions']['shown'], {'en': 'en', 'ru': 'en'})
        self.assertEqual(self.assign(tariff['id']).status_code, 200)
        # The owner reads it in their language.
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_accounts SET locale='ru' WHERE id=(SELECT owner_account_id FROM backend_profiles WHERE id=?)",
                         (self.profile,))
        from backend.profiles import ProfileRepository
        summary = ProfileRepository(self.db).summary(self.profile, 'ru')
        self.assertEqual((summary['tariff']['title'], summary['tariff']['description']), ('Месяц', 'Two devices.'))
        edited = self.client.patch(f"/api/v1/tariffs/{tariff['id']}", headers=self.headers_for(1),
                                   json={'descriptions': None})
        self.assertIsNone(edited.json()['descriptions'], edited.text)


if __name__ == '__main__':
    unittest.main()
