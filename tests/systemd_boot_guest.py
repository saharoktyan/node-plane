"""Manual checks inside the disposable QEMU guest described in SYSTEMD_BOOT_CHECK.md.

Never run against a production installation. This script changes lease deadlines
in its disposable journal; product durations remain 12h / 1d / 3d.
"""
import importlib.util
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
from datetime import datetime,timedelta,timezone
from uuid import uuid4

ROOT=Path('/opt/node-plane-runtime')
JOURNAL=Path('/etc/node-plane/profile-intents.sqlite3')
STATE=Path('/root/boot-test-state.json')
def run(*args):
    return subprocess.run(args,check=True,capture_output=True,text=True,timeout=90).stdout.strip()
def load(name,file):
    spec=importlib.util.spec_from_file_location(name,ROOT/file)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module
if (subprocess.run(['systemd-detect-virt'],capture_output=True,text=True).stdout.strip()!='qemu'
        or Path('/etc/hostname').read_text().strip()!='np-boot-test'
        or not Path('/root/boot-fixture-ready').is_file()):
    raise SystemExit('This check requires the explicitly prepared disposable np-boot-test QEMU guest')
engine=load('boot_intents','apply-profile-intent.py')
xray=load('boot_xray','xray-user-api.py')
def issue(protocol,name,temporary=False):
    intent=dict(command_id=str(uuid4()),protocol=protocol,runtime_name=name,revision=1,
                action='ensure',uuid=str(uuid4()) if protocol=='xray' else '',
                short_id='0123456789abcdef' if protocol=='xray' else '')
    if temporary:intent['lease_seconds']=43200
    engine.apply(intent,JOURNAL,engine.run)
    return name
def setup():
    ROOT.joinpath('xray').mkdir(exist_ok=True)
    Path('/etc/node-plane').mkdir(exist_ok=True)
    Path('/etc/node-plane/node.env').write_text('SERVER_KEY=bootnode\nAWG_SERVER_IP=10.0.2.15\nAWG_SERVER_PORT=51820\nAWG_I1_PRESET=quic\n')
    config={'log':{'loglevel':'warning'},'api':{'tag':'api','services':['HandlerService','StatsService']},
        'inbounds':[{'tag':tag,'port':port,'protocol':'vless','settings':{'decryption':'none','clients':[]},
          'streamSettings':{'network':network}} for tag,port,network in [('reality-tcp',443,'tcp'),('reality-xhttp',8443,'xhttp')]]+
          [{'tag':'api','listen':'127.0.0.1','port':10085,'protocol':'dokodemo-door','settings':{'address':'127.0.0.1'}}],
        'outbounds':[{'protocol':'freedom','tag':'direct'}],
        'routing':{'rules':[{'type':'field','inboundTag':['api'],'outboundTag':'api'}]}}
    ROOT.joinpath('xray/config.json').write_text(json.dumps(config))
    run('docker','run','-d','--name','xray','--restart','unless-stopped','--user','0:0','--network','host',
        '-v',str(ROOT/'xray')+':/etc/xray:ro','ghcr.io/xtls/xray-core:26.3.27','run','-c','/etc/xray/config.json')
    run('bash',str(ROOT/'init-awg.sh'))
    run('modprobe','tun')
    run('docker','run','-d','--name','amnezia-awg','--restart','unless-stopped','--cap-add','NET_ADMIN',
        '--device','/dev/net/tun:/dev/net/tun','--sysctl','net.ipv4.ip_forward=1','-e','AWG_IFACE=wg0',
        '-e','AWG_NETWORK=10.8.1.0/24','-p','51820:51820/udp',
        '-v',str(ROOT/'amnezia-awg/data')+':/opt/amnezia/awg','node-plane-awg:boot-test')
    time.sleep(3)
    state={'boot_before':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),'protocols':{}}
    for protocol in ('xray','awg'):
        issue(protocol,'permanent')
        engine.ensure_lease_scheduler(protocol=protocol)
        state['protocols'][protocol]={'due':issue(protocol,'tmp_'+uuid4().hex,True),
                                     'valid':issue(protocol,'tmp_'+uuid4().hex,True)}
    STATE.write_text(json.dumps(state))
    print(json.dumps({'setup':'passed','boot_id':state['boot_before']}))
def arm():
    state=json.loads(STATE.read_text())
    run('systemctl','stop','node-plane-lease-expiry.timer','node-plane-lease-expiry.service')
    with sqlite3.connect(JOURNAL) as conn:
        for protocol,values in state['protocols'].items():
            conn.execute('UPDATE temporary_leases SET expires_at=? WHERE protocol=? AND profile=?',
                 ((datetime.now(timezone.utc)+timedelta(seconds=2)).isoformat(),protocol,values['due']))
    state['boot_before']=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    STATE.write_text(json.dumps(state))
    print(json.dumps({'armed':'passed','boot_id':state['boot_before']}))
def verify():
    state=json.loads(STATE.read_text())
    boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    assert boot!=state['boot_before'],'Guest kernel did not reboot'
    deadline=time.monotonic()+40
    while True:
        with sqlite3.connect(JOURNAL) as conn:
            settled=all(conn.execute('SELECT status FROM temporary_leases WHERE protocol=? AND profile=?',(p,v['due'])).fetchone()[0]=='expired' for p,v in state['protocols'].items())
        if settled and all(engine.lease_enforcement_ready(p) for p in state['protocols']):break
        if time.monotonic()>deadline:raise AssertionError('Lease services did not become healthy')
        time.sleep(.5)
    for protocol,values in state['protocols'].items():
        assert engine.lease_enforcement_ready(protocol)
        with sqlite3.connect(JOURNAL) as conn:
            leases=dict(conn.execute('SELECT profile,status FROM temporary_leases WHERE protocol=?',(protocol,)))
        assert leases[values['due']]=='expired',leases
        assert leases[values['valid']]=='active',leases
        if protocol=='xray':
            for tag in ('reality-tcp','reality-xhttp'):
                users=xray.users('xray',tag)
                assert 'permanent' in users and values['valid'] in users,users
                assert values['due'] not in users,users
        else:
            config=ROOT.joinpath('amnezia-awg/data/wg0.conf').read_text()
            assert 'bootnode-permanent' in config and 'bootnode-'+values['valid'] in config
            assert 'bootnode-'+values['due'] not in config
            assert len(run('docker','exec','amnezia-awg','wg','show','wg0','peers').splitlines())==2
    print(json.dumps({'boot_check':'passed','boot_id':boot,'old_boot_id':state['boot_before'],
                      'protocols':['AWG','VLESS'],'expired_absent':True,'permanent_preserved':True,
                      'valid_temporary_preserved':True,'agent':run('systemctl','is-active','node-plane-agent')}))
def offline():
    run('systemctl','stop','node-plane-agent')
    created={p:issue(p,'tmp_'+uuid4().hex,True) for p in ('awg','xray')}
    with sqlite3.connect(JOURNAL) as conn:
        for protocol,name in created.items():
            conn.execute('UPDATE temporary_leases SET expires_at=? WHERE protocol=? AND profile=?',
                ((datetime.now(timezone.utc)+timedelta(seconds=2)).isoformat(),protocol,name))
    deadline=time.monotonic()+25
    while True:
        with sqlite3.connect(JOURNAL) as conn:
            statuses=[conn.execute('SELECT status FROM temporary_leases WHERE protocol=? AND profile=?',(p,n)).fetchone()[0]
                      for p,n in created.items()]
        if statuses==['expired','expired']:break
        if time.monotonic()>deadline:raise AssertionError(statuses)
        time.sleep(.5)
    assert subprocess.run(['systemctl','is-active','--quiet','node-plane-agent']).returncode!=0
    print(json.dumps({'agent_offline_expiry':'passed','protocols':['AWG','VLESS']}))
def docker_restart():
    run('systemctl','restart','docker')
    deadline=time.monotonic()+40
    while not all(engine.lease_enforcement_ready(p) for p in ('awg','xray')):
        if time.monotonic()>deadline:raise AssertionError('Docker restart did not recover supervisors')
        time.sleep(.5)
    print(json.dumps({'docker_restart':'passed'}))
def arm_guard():
    state=json.loads(STATE.read_text())
    for protocol,values in state['protocols'].items():
        values['due']=issue(protocol,'tmp_'+uuid4().hex,True)
    STATE.write_text(json.dumps(state))
    run('systemctl','disable','--now','node-plane-lease-expiry.timer')
    arm()
def verify_guard():
    state=json.loads(STATE.read_text())
    boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    assert boot!=state['boot_before']
    assert subprocess.run(['systemctl','is-active','--quiet','node-plane-lease-expiry.timer']).returncode!=0
    deadline=time.monotonic()+40
    for protocol,values in state['protocols'].items():
        config,container,_=engine.leased_runtime_settings(protocol)
        while True:
            try:
                if engine.leased_container(protocol,config,container)['State']['Running']:break
            except Exception:pass
            if time.monotonic()>deadline:raise AssertionError('Guard did not start '+protocol)
            time.sleep(.5)
        assert not engine.lease_enforcement_ready(protocol),'Disabled expiry timer reported healthy'
        with sqlite3.connect(JOURNAL) as conn:
            assert conn.execute('SELECT status FROM temporary_leases WHERE protocol=? AND profile=?',
                                (protocol,values['due'])).fetchone()[0]=='revoking'
        if protocol=='xray':
            for tag in ('reality-tcp','reality-xhttp'):
                users=xray.users('xray',tag)
                assert values['due'] not in users
                assert 'permanent' in users and values['valid'] in users
        else:
            text=config.read_text()
            assert 'bootnode-'+values['due'] not in text
            assert 'bootnode-permanent' in text and 'bootnode-'+values['valid'] in text
            assert len(run('docker','exec',container,'wg','show','wg0','peers').splitlines())==2
    print(json.dumps({'guard_without_timer':'passed','boot_id':boot,
                      'expired_absent_before_scan':True,'enforcement_ready':False}))
    run('systemctl','enable','--now','node-plane-lease-expiry.timer')
    verify()
{'setup':setup,'arm':arm,'verify':verify,'offline':offline,'docker-restart':docker_restart,
 'arm-guard':arm_guard,'verify-guard':verify_guard}[sys.argv[1]]()
