"""Production entrypoints and restart guards on disposable journals/configs."""
import fcntl
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from uuid import uuid4

from tests.test_agent_profile_intents import MODULE


class LeasedRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / 'journal.sqlite3'
        self.config = self.root / 'config.json'
        self.running = False
        self.policy = 'no'
        self.failed = False
        self.bad_mount = False
        self.calls = []

    def intent(self, protocol='xray'):
        return dict(command_id=str(uuid4()), protocol=protocol, runtime_name='tmp_'+uuid4().hex,
                    revision=1, action='ensure', uuid=str(uuid4()) if protocol=='xray' else '',
                    short_id='0123456789abcdef' if protocol=='xray' else '', lease_seconds=86400)

    def lease(self, protocol='xray', overdue=False):
        intent = self.intent(protocol)
        MODULE.apply(intent, self.path, lambda *_: {'summary':'OK','payload_json':''})
        if overdue:
            with sqlite3.connect(self.path) as conn:
                conn.execute('UPDATE temporary_leases SET expires_at=? WHERE command_id=?',
                    ((datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(),intent['command_id']))
        return intent

    def execute(self, args, **kwargs):
        self.calls.append(args)
        output = ''
        code = 0
        if args[0]=='bash':
            output = str(self.config)+'\ntest\nlv1\n'
        elif args[:3]==['docker','container','inspect']:
            protocol = 'awg' if self.config.suffix=='.conf' else 'xray'
            output = json.dumps([{'Id':'a'*64,'Name':'/test','State':{'Running':self.running},
                'HostConfig':{'RestartPolicy':{'Name':self.policy}},'Mounts':[{'Type':'bind',
                'Source':'/unrelated' if self.bad_mount else str(self.config.parent),
                'Destination':'/opt/amnezia/awg' if protocol=='awg' else '/etc/xray'}]}])
        elif args[:2]==['docker','update']:
            self.policy = 'no'
        elif args[:2]==['docker','start']:
            self.started_config = self.config.read_text()
            self.running = True
        elif args[:2]==['docker','wait']:
            # Supervision must not monopolize the mutation/expiry lock.
            with open(str(self.path)+'.lock','a') as other:
                fcntl.flock(other,fcntl.LOCK_EX|fcntl.LOCK_NB)
        elif args[:2]==['systemctl','is-failed']:
            code = 0 if self.failed else 1
        return subprocess.CompletedProcess(args,code,stdout=output,stderr='')

    def test_start_prunes_overdue_and_uncertain_vless_but_keeps_other_clients(self):
        due, active, uncertain = self.lease(overdue=True), self.lease(), self.lease()
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE temporary_leases SET status='provisioning' WHERE command_id=?",(uncertain['command_id'],))
        clients = [{'id':i['uuid'],'email':i['runtime_name']} for i in (due,active,uncertain)]
        clients.append({'id':'permanent-secret','email':'regular'})
        self.config.write_text(json.dumps({'inbounds':[{'protocol':'vless','settings':{'clients':clients}}]}))
        MODULE.start_leased_runtime('xray',self.path,self.execute)
        names = [c['email'] for c in json.loads(self.started_config)['inbounds'][0]['settings']['clients']]
        self.assertEqual(names,[active['runtime_name'],'regular'])
        self.assertEqual(self.config.stat().st_mode&0o777,0o600)
        with sqlite3.connect(self.path) as conn:
            statuses = dict(conn.execute('SELECT profile,status FROM temporary_leases'))
        self.assertEqual(statuses[due['runtime_name']],'revoking')
        self.assertEqual(statuses[active['runtime_name']],'active')
        self.assertEqual(statuses[uncertain['runtime_name']],'revoking')
        self.assertEqual(self.calls[-1],['docker','wait','a'*64])

    def test_awg_restart_removes_only_the_expired_device_peer(self):
        due, active = self.lease('awg',True), self.lease('awg')
        self.config = self.root/'wg0.conf'
        peer = lambda name,key: '# '+name+'\n[Peer]\nPublicKey = '+key+'\nAllowedIPs = 10.8.1.2/32\n\n'
        self.config.write_text('[Interface]\nPrivateKey = server-secret\n\n'+
            peer('lv1-'+due['runtime_name'],'expired')+peer('lv1-'+active['runtime_name'],'active')+peer('permanent','regular'))
        MODULE.start_leased_runtime('awg',self.path,self.execute)
        self.assertNotIn('PublicKey = expired',self.started_config)
        self.assertIn('PublicKey = active',self.started_config)
        self.assertIn('PublicKey = regular',self.started_config)
        self.assertIn('PrivateKey = server-secret',self.started_config)

    def test_unknown_config_ownership_and_cleanup_fence_never_start_container(self):
        self.lease(overdue=True)
        self.config.write_text('invalid json')
        with self.assertRaises(ValueError):
            MODULE.start_leased_runtime('xray',self.path,self.execute)
        self.assertFalse(any(a[:2]==['docker','start'] for a in self.calls))
        self.bad_mount = True
        with self.assertRaisesRegex(ValueError,'ownership'):
            MODULE.start_leased_runtime('xray',self.path,self.execute)
        Path(str(self.path)+'.disabled').touch()
        self.calls.clear()
        with self.assertRaisesRegex(ValueError,'removal'):
            MODULE.start_leased_runtime('xray',self.path,self.execute)
        self.assertFalse(self.calls)

    def test_installing_guard_does_not_restart_a_running_shared_protocol(self):
        self.running = True
        MODULE.start_leased_runtime('xray',self.path,self.execute)
        self.assertFalse(any(a[:2] in (['docker','stop'],['docker','start']) for a in self.calls))
        self.assertEqual(self.calls[-1],['docker','wait','a'*64])

    def test_health_requires_enabled_timer_guard_and_owned_running_container(self):
        self.running = True
        self.assertTrue(MODULE.lease_enforcement_ready('xray',self.execute))
        for field,value in [('running',False),('policy','unless-stopped'),('failed',True),('bad_mount',True)]:
            previous = getattr(self,field)
            setattr(self,field,value)
            self.assertFalse(MODULE.lease_enforcement_ready('xray',self.execute))
            setattr(self,field,previous)
        def unavailable(args,**kwargs):
            raise subprocess.CalledProcessError(1,args)
        self.assertFalse(MODULE.lease_enforcement_ready('xray',unavailable))

    def test_supervisor_is_persistent_and_only_its_owned_unit_is_stopped(self):
        units = self.root/'units'
        units.mkdir()
        self.running = True
        MODULE.ensure_lease_scheduler(units,self.execute,protocol='xray')
        unit = (units/'node-plane-leased-xray.service').read_text()
        self.assertIn('WantedBy=docker.service',unit)
        self.assertIn('Restart=always',unit)
        self.assertIn('start-leased-runtime xray',unit)
        self.assertNotIn('node-plane-agent.service',unit)
        self.assertIn(['docker','update','--restart','no','a'*64],self.calls)
        self.calls.clear()
        MODULE.stop_leased_runtime_units(('xray',),self.execute,units)
        self.assertEqual(self.calls,[['systemctl','disable','--now','node-plane-leased-xray.service']])
        (units/'node-plane-leased-xray.service').write_text('unrelated unit')
        with self.assertRaisesRegex(ValueError,'ownership'):
            MODULE.stop_leased_runtime_units(('xray',),self.execute,units)

    def test_inherited_lock_stays_held_and_rejects_another_file(self):
        with open(str(self.path)+'.lock','a') as parent:
            fcntl.flock(parent,fcntl.LOCK_EX)
            with patch.dict(os.environ,{'NODE_PLANE_PROFILE_LOCK_FD':str(parent.fileno())}):
                with MODULE.lease_restart_lock(self.path):
                    pass
            with open(str(self.path)+'.lock','a') as other:
                with self.assertRaises(BlockingIOError):
                    fcntl.flock(other,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with open(self.root/'other.lock','a') as unrelated:
                with patch.dict(os.environ,{'NODE_PLANE_PROFILE_LOCK_FD':str(unrelated.fileno())}):
                    with self.assertRaises(ValueError):
                        with MODULE.lease_restart_lock(self.path):
                            pass

    def test_timer_executes_the_real_cli_and_cleans_an_overdue_stopped_peer(self):
        intent = self.lease('awg',True)
        self.config = self.root/'wg0.conf'
        self.config.write_text('[Interface]\nPrivateKey = server\n\n# lv1-'+intent['runtime_name']+
                              '\n[Peer]\nPublicKey = expired\n\n# regular\n[Peer]\nPublicKey = keep\n')
        script = self.root/'apply-profile-intent.py'
        source = Path(MODULE.__file__).read_text().replace('/etc/node-plane/profile-intents.sqlite3',str(self.path))
        script.write_text(source)
        shutil.copy(Path(MODULE.__file__).with_name('awg-peer-state.py'),self.root)
        fake_bin = self.root/'bin'
        fake_bin.mkdir()
        record = {'Id':'a'*64,'Name':'/test','State':{'Running':False},
            'Mounts':[{'Type':'bind','Source':str(self.config.parent),'Destination':'/opt/amnezia/awg'}]}
        for name,output in [('bash',str(self.config)+'\ntest\nlv1\n'),('docker',json.dumps([record]))]:
            executable = fake_bin/name
            executable.write_text('#!'+sys.executable+'\nimport sys\nsys.stdout.write('+repr(output)+')\n')
            executable.chmod(0o700)
        result = subprocess.run([sys.executable,str(script),'expire-leases'],
            capture_output=True,text=True,timeout=10,env=dict(os.environ,PATH=str(fake_bin)))
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout),{'expired':1,'pending':0})
        self.assertNotIn('expired',self.config.read_text())
        self.assertIn('PublicKey = keep',self.config.read_text())
        with sqlite3.connect(self.path) as conn:
            self.assertEqual(conn.execute('SELECT status FROM temporary_leases').fetchone()[0],'expired')

