"""Production entrypoints and restart guards on disposable journals/configs."""
import fcntl
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from uuid import uuid4

SPEC = importlib.util.spec_from_file_location('agent_intents', Path(__file__).resolve().parents[1] / 'runtime_assets/apply-profile-intent.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


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
        self.assertIn('ExecStart=/usr/local/bin/node-plane-agent lease supervise xray',unit)
        self.assertNotIn('node-plane-agent.service',unit)
        self.assertIn(['docker','update','--restart','no','a'*64],self.calls)
        self.calls.clear()
        MODULE.stop_leased_runtime_units(('xray',),self.execute,units)
        self.assertEqual(self.calls,[['systemctl','disable','--now','node-plane-leased-xray.service']])
        (units/'node-plane-leased-xray.service').write_text('unrelated unit')
        with self.assertRaisesRegex(ValueError,'ownership'):
            MODULE.stop_leased_runtime_units(('xray',),self.execute,units)

    def test_restarting_or_stopped_expiry_worker_cannot_report_ready(self):
        self.running = True
        for state in ('activating', 'inactive'):
            def execute(args, **kwargs):
                if args == ['systemctl','is-active','--quiet','node-plane-lease-expiry.service']:
                    raise subprocess.CalledProcessError(3,args,output=state)
                return self.execute(args,**kwargs)
            self.assertFalse(MODULE.lease_enforcement_ready('xray',execute))
