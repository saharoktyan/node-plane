"""Administrator access remains permanent across profile commands and node changes."""
import unittest
from uuid import uuid4

from backend.authorization import ADMIN_PERMISSIONS, Actor, Principal, PrincipalKind
from backend.profile_commands import ProfileCommands
from backend.grant_policies import reconcile
from tests import test_backend_http as http_fixture, test_backend_nodes as node_fixture


class AdminProtectionTests(unittest.TestCase):
    setUp = http_fixture.BackendHTTPTests.setUp
    create_node = node_fixture.BackendNodeTests.create
    admin_headers = node_fixture.BackendNodeTests.admin_headers

    def profile(self):
        response = self.client.post('/api/v1/profiles', headers=self.admin_headers(**{'Idempotency-Key':str(uuid4())}),
            json={'display_name':'Administrator', 'owner_account_id':self.admin.id})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()['profile']['id']

    def actor(self):
        return Actor(Principal('test', PrincipalKind.ACCOUNT, ADMIN_PERMISSIONS, self.admin.id), self.admin)

    def test_admin_mutations_are_rejected_without_erasing_devices_or_grants(self):
        profile = self.profile()
        from backend.devices import DeviceRepository
        with self.db.transaction() as conn:
            DeviceRepository.ensure_default(conn, conn.execute('SELECT * FROM backend_profiles WHERE id=?', (profile,)).fetchone())
        before = [tuple(row) for row in self.db.connection.execute('SELECT * FROM backend_devices').fetchall()]
        path = '/api/v1/profiles/' + profile
        def headers():
            return self.admin_headers(**{'If-Match':'"1"','Idempotency-Key':str(uuid4())})
        for method, suffix, body, status in [('DELETE','',None,409), ('PATCH','',{'frozen':True},422),
            ('PATCH','/grants',{'grants':[]},422), ('PUT','/access-policy',{'explicit_grants':[], 'rules':[], 'exclusions':[]},422)]:
            response = self.client.request(method,path+suffix,headers=headers(),json=body)
            self.assertEqual(response.status_code,status,response.text)
        self.assertEqual([tuple(row) for row in self.db.connection.execute('SELECT * FROM backend_devices').fetchall()],before)
        self.assertEqual(self.db.connection.execute('SELECT COUNT(*) FROM backend_profile_deletions').fetchone()[0],0)
        self.assertEqual(self.client.get(path,headers=self.admin_headers()).json()['desired_revision'],1)

    def test_current_and_future_nodes_apply_only_after_installation(self):
        profile = self.profile()
        response = self.create_node()
        self.assertEqual(response.status_code,201,response.text)
        self.assertEqual(self.db.connection.execute('SELECT COUNT(*) FROM backend_operation_tasks').fetchone()[0],0)
        policy = self.client.get(f'/api/v1/profiles/{profile}/access-policy',headers=self.admin_headers()).json()
        self.assertTrue(policy['permanent'])
        self.assertEqual(policy['rules'],[{'scope':'all','region_id':None,'protocols':['awg','xray']}])
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_nodes SET applied_revision=desired_revision, enabled=1 WHERE key='lv1'")
            reconcile(conn,self.actor())
        self.assertEqual({tuple(row) for row in self.db.connection.execute('SELECT node_key,protocol FROM backend_grants WHERE profile_id=?',(profile,))}, {('lv1','awg'),('lv1','xray')})
        body={'key':'future','title':'Future','region':'Other','protocols':['xray'],'xray_transports':['tcp'], 'transport':'ssh','ssh_target':'root@future.example.test','settings':{'public_host':'future.example.test'}}
        self.assertEqual(self.create_node(body).status_code,201)
        with self.db.transaction() as conn:
            conn.execute("UPDATE backend_nodes SET applied_revision=desired_revision, enabled=1 WHERE key='future'")
            reconcile(conn,self.actor())
        self.assertIn(('future','xray'),{tuple(row) for row in self.db.connection.execute('SELECT node_key,protocol FROM backend_grants WHERE profile_id=?',(profile,))})

    def test_legacy_admin_freeze_is_repaired_once(self):
        profile = self.profile()
        with self.db.transaction() as conn:
            conn.execute('UPDATE backend_profiles SET frozen=1 WHERE id=?',(profile,))
            ProfileCommands.restore_admin_expiries(conn,self.actor())
        row=self.client.get('/api/v1/profiles/'+profile,headers=self.admin_headers()).json()
        self.assertFalse(row['frozen'])
        revision=row['desired_revision']
        with self.db.transaction() as conn:
            ProfileCommands.restore_admin_expiries(conn,self.actor())
        self.assertEqual(self.client.get('/api/v1/profiles/'+profile,headers=self.admin_headers()).json()['desired_revision'],revision)

    def test_promotion_and_demotion_preserve_original_member_policy(self):
        member = self.identities.resolve_telegram(102)
        self.db.connection.execute("UPDATE backend_accounts SET status='approved' WHERE id=?", (member.id,))
        self.assertEqual(self.create_node().status_code,201)
        self.db.connection.execute("UPDATE backend_nodes SET applied_revision=desired_revision, enabled=1 WHERE key='lv1'")
        created=self.client.post('/api/v1/profiles',headers=self.admin_headers(**{'Idempotency-Key':str(uuid4())}),
            json={'display_name':'Member','owner_account_id':member.id,'grants':[{'node_key':'lv1','protocol':'xray'}]})
        self.assertEqual(created.status_code,201,created.text)
        profile=created.json()['profile']['id']
        path='/api/v1/accounts/'+member.id
        def headers(revision):
            return self.admin_headers(**{'If-Match':'"'+str(revision)+'"','Idempotency-Key':str(uuid4())})
        promoted=self.client.patch(path,headers=headers(1),json={'role':'admin'})
        self.assertEqual(promoted.status_code,200,promoted.text)
        self.assertEqual(self.client.patch(path,headers=headers(2),json={'status':'disabled'}).status_code,409)
        self.assertEqual({tuple(r) for r in self.db.connection.execute('SELECT node_key,protocol FROM backend_grants WHERE profile_id=?',(profile,))}, {('lv1','xray'),('lv1','awg')})
        demoted=self.client.patch(path,headers=headers(2),json={'role':'member'})
        self.assertEqual(demoted.status_code,200,demoted.text)
        self.assertEqual({tuple(r) for r in self.db.connection.execute('SELECT node_key,protocol FROM backend_grants WHERE profile_id=?',(profile,))}, {('lv1','xray')})
