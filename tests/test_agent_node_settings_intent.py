import importlib.util
import json
import os
import shlex
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location('agent_node_settings',
    Path(__file__).resolve().parents[1] / 'runtime_assets/apply-profile-intent.py')
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class AgentNodeSettingsIntentTests(unittest.TestCase):
    def test_settings_command_publishes_progress_without_changing_its_identity(self):
        from uuid import uuid4
        identity = str(uuid4())
        intent = self.intent(command_id=identity)
        def runner(value, fd):
            self.assertEqual(value['command_id'], identity)
            MODULE.installation_step('protocol_deploy')
            receipt = self.journal.parent / 'operation-progress' / (identity + '.json')
            self.assertEqual(json.loads(receipt.read_text()), {'operation_id': identity, 'stage': 'protocol_deploy'})
            return self.runner(value, fd)
        with patch.dict(os.environ, {'NODE_PLANE_OPERATION_PROGRESS_ID': 'previous'}):
            MODULE.apply_node_settings(intent, self.journal, runner)
            self.assertEqual(os.environ['NODE_PLANE_OPERATION_PROGRESS_ID'], 'previous')

    def test_selected_fallback_port_is_durable_and_recovered_without_replay(self):
        intent = self.intent()
        intent['settings'].update(awg_port=443, awg_i1_preset='quic', awg_port_mode='auto')
        calls = []

        def runner(original, lock_fd):
            calls.append(True)
            effective = dict(original, settings=dict(original['settings'], awg_port=8443))
            payload = {'node_key': 'n1', 'revision': 1, 'awg_port': 8443,
                'settings_sha256': MODULE.node_settings_digest(effective)}
            return {'summary': 'verified', 'payload_json': json.dumps(payload)}

        first = MODULE.apply_node_settings(intent, self.journal, runner)
        self.assertEqual(first, MODULE.lookup_node_settings(intent, self.journal))
        self.assertEqual(first, MODULE.apply_node_settings(intent, self.journal, runner))
        self.assertEqual(len(calls), 1)
        self.assertEqual(intent['settings']['awg_port'], 443)

    def test_selected_port_reaches_environment_config_and_verified_result(self):
        root = Path(self.temp.name)
        env = root / 'node.env'
        awg = root / 'wg0.conf'
        env.write_text(f'XRAY_CONFIG={root / "xray.json"}\nAWG_CONFIG={awg}\nXRAY_CONTAINER_NAME=xray\nAWG_CONTAINER_NAME=awg\n')
        (root / 'awg_ports.py').write_text('def select_port(*args):\n    return 8443\n')
        (root / 'init-awg.sh').write_text(f'#!/bin/bash\nsource "{env}"\nprintf "[Interface]\\nListenPort = %s\\nI1 = <b 0xc000000001><rc 8><r 900>\\n" "$AWG_SERVER_PORT" > "{awg}"\n')
        (root / 'apply-node-settings.sh').write_text('#!/bin/sh\nexit 0\n')
        for name in ('init-awg.sh', 'apply-node-settings.sh'):
            (root / name).chmod(0o755)
        intent = self.intent()
        intent['protocols'] = ['awg']
        intent['settings'].update(awg_port=443, awg_port_mode='auto', awg_i1_preset='quic')
        real_run = subprocess.run

        def run(args, **kwargs):
            if args[0] in {'ufw', 'docker'}:
                return subprocess.CompletedProcess(args, 0, 'true\n' if args[0] == 'docker' else '')
            return real_run(args, **kwargs)

        with patch.object(MODULE.subprocess, 'run', side_effect=run):
            result = MODULE.run_node_settings(intent, 0, env_path=env, root=root)
        self.assertIn("AWG_SERVER_PORT=8443", env.read_text())
        self.assertIn('ListenPort = 8443', awg.read_text())
        payload = json.loads(result['payload_json'])
        effective = dict(intent, settings=dict(intent['settings'], awg_port=8443))
        self.assertEqual(payload['settings_sha256'], MODULE.node_settings_digest(effective))
        self.assertEqual(payload['awg_port'], 8443)
        self.assertEqual(intent['settings']['awg_port'], 443)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.journal = Path(self.temp.name) / 'commands.sqlite3'
        self.calls = []

    def intent(self, revision=1, command_id='node-task-1'):
        return {'kind': 'node_settings', 'node_key': 'n1', 'command_id': command_id,
                'revision': revision, 'protocols': ['awg', 'xray'],
                'settings': {'public_host': 'node.example', 'xray_sni': 'www.cloudflare.com',
                             'xray_tcp_port': 443, 'xray_xhttp_port': 8443,
                             'xray_xhttp_path': '/assets', 'awg_port': 51820}}

    def runner(self, intent, lock_fd):
        self.calls.append(intent.copy())
        payload = {'node_key': intent['node_key'], 'revision': intent['revision'],
                   'settings_sha256': MODULE.node_settings_digest(intent)}
        return {'summary': 'applied', 'payload_json': json.dumps(payload)}

    def test_idempotent_revision_and_read_only_recovery(self):
        intent = self.intent()
        first = MODULE.apply_node_settings(intent, self.journal, self.runner)
        self.assertEqual(first, MODULE.apply_node_settings(intent, self.journal, self.runner))
        self.assertEqual(first, MODULE.lookup_node_settings(intent, self.journal))
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.journal.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(ValueError):
            MODULE.apply_node_settings(self.intent(command_id='other'), self.journal, self.runner)
        with self.assertRaises(ValueError):
            MODULE.apply_node_settings(self.intent(revision=2, command_id='node-task-1'), self.journal, self.runner)
        second = MODULE.apply_node_settings(self.intent(revision=2, command_id='node-task-2'), self.journal, self.runner)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(second['summary'], 'applied')
        with self.assertRaises(ValueError):
            MODULE.guard_legacy(self.journal)

    def test_failed_or_unverified_mutation_blocks_later_commands(self):
        def bad_result(intent, lock_fd):
            return {'summary': 'claimed', 'payload_json': '{}'}

        with self.assertRaises(ValueError):
            MODULE.apply_node_settings(self.intent(), self.journal, bad_result)
        with self.assertRaises(ValueError):
            MODULE.lookup_node_settings(self.intent(), self.journal)
        with self.assertRaises(ValueError):
            MODULE.apply_node_settings(self.intent(revision=2, command_id='new'), self.journal, self.runner)
        profile = {'command_id': 'profile-task', 'protocol': 'awg', 'runtime_name': 'alice',
                   'revision': 1, 'action': 'ensure', 'uuid': '', 'short_id': ''}
        with self.assertRaises(ValueError):
            MODULE.apply(profile, self.journal, lambda intent, fd: {'summary': '', 'payload_json': ''})
        self.assertEqual(self.calls, [])

    def test_interrupted_settings_require_restart_then_fresh_revision(self):
        intent = self.intent()
        before = os.environ.get('NODE_PLANE_AGENT_INSTANCE_ID')
        try:
            os.environ['NODE_PLANE_AGENT_INSTANCE_ID'] = 'old-agent'
            with self.assertRaises(RuntimeError):
                MODULE.apply_node_settings(intent, self.journal,
                    lambda _intent, _fd: (_ for _ in ()).throw(RuntimeError('interrupted')))
            with self.assertRaises(ValueError):
                MODULE.resolve_node_settings(intent, self.journal,
                    lambda _intent: {'config_matches': False, 'containers_running': False})
            os.environ['NODE_PLANE_AGENT_INSTANCE_ID'] = 'new-agent'
            observations = []
            def inspect(_intent):
                observations.append(True)
                return {'config_matches': False, 'containers_running': True}
            result = MODULE.resolve_node_settings(intent, self.journal, inspect)
            self.assertEqual(result['observation'], {'config_matches': False, 'containers_running': True})
            self.assertEqual(result, MODULE.resolve_node_settings(intent, self.journal, inspect))
            self.assertEqual(len(observations), 1)
            with self.assertRaises(ValueError):
                MODULE.lookup_node_settings(intent, self.journal)
            MODULE.apply_node_settings(self.intent(revision=2, command_id='fresh'), self.journal, self.runner)
            self.assertEqual(len(self.calls), 1)
        finally:
            if before is None:
                os.environ.pop('NODE_PLANE_AGENT_INSTANCE_ID', None)
            else:
                os.environ['NODE_PLANE_AGENT_INSTANCE_ID'] = before

    def test_incomplete_or_malformed_settings_rejected_before_journal(self):
        bad = self.intent()
        del bad['settings']['xray_sni']
        with self.assertRaises(ValueError):
            MODULE.apply_node_settings(bad, self.journal, self.runner)
        injected = self.intent()
        injected['settings']['xray_sni'] = 'example.com";print(1)'
        with self.assertRaises(ValueError):
            MODULE.apply_node_settings(injected, self.journal, self.runner)
        self.assertFalse(self.journal.exists())

    def test_effective_protocol_configs_are_checked_before_acknowledgment(self):
        intent = self.intent()
        xray = Path(self.temp.name) / 'xray.json'
        awg = Path(self.temp.name) / 'wg0.conf'
        data = {'inbounds': [
            {'tag': 'reality-tcp', 'port': 443, 'streamSettings': {
                'realitySettings': {'serverNames': ['www.cloudflare.com'], 'dest': 'www.cloudflare.com:443'}}},
            {'tag': 'reality-xhttp', 'port': 8443, 'streamSettings': {
                'realitySettings': {'serverNames': ['www.cloudflare.com'], 'dest': 'www.cloudflare.com:443'},
                'xhttpSettings': {'path': '/assets'}}}]}
        xray.write_text(json.dumps(data))
        awg.write_text('[Interface]\nListenPort = 51820\n')
        MODULE.verify_node_settings_config(intent, xray, awg)
        data['inbounds'][0]['port'] = 444
        xray.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            MODULE.verify_node_settings_config(intent, xray, awg)
        data['inbounds'][0]['port'] = 443
        xray.write_text(json.dumps(data))
        awg.write_text('[Interface]\nListenPort = 51821\n')
        with self.assertRaises(ValueError):
            MODULE.verify_node_settings_config(intent, xray, awg)

    def test_clean_runtime_initializes_configs_once_before_verification(self):
        root = Path(self.temp.name)
        scripts = root / 'scripts'
        scripts.mkdir()
        xray = root / 'xray.json'
        awg = root / 'wg0.conf'
        env = root / 'node.env'
        env.write_text(f'XRAY_CONFIG={xray}\nAWG_CONFIG={awg}\nXRAY_CONTAINER_NAME=xray\nAWG_CONTAINER_NAME=awg\n')
        (scripts / 'init-xray.sh').write_text('''#!/bin/sh
cat > "$1" <<'EOF'
{"inbounds":[{"tag":"reality-tcp","port":443,"streamSettings":{"realitySettings":{"serverNames":["www.cloudflare.com"],"dest":"www.cloudflare.com:443"}}},{"tag":"reality-xhttp","port":8443,"streamSettings":{"realitySettings":{"serverNames":["www.cloudflare.com"],"dest":"www.cloudflare.com:443"},"xhttpSettings":{"path":"/assets"}}}]}
EOF
''')
        (scripts / 'init-awg.sh').write_text(f'''#!/bin/sh
printf '[Interface]\\nListenPort = 51820\\n' > '{awg}'
''')
        (scripts / 'apply-node-settings.sh').write_text('#!/bin/sh\nexit 0\n')
        for script in scripts.iterdir():
            script.chmod(0o755)
        real_run = subprocess.run
        launched = []

        def run(args, **kwargs):
            if args[0] in {'ufw', 'docker'}:
                return subprocess.CompletedProcess(args, 0, 'true\n' if args[0] == 'docker' else '')
            launched.append(Path(args[0]).name)
            return real_run(args, **kwargs)

        with patch.object(MODULE.subprocess, 'run', side_effect=run):
            first = MODULE.run_node_settings(self.intent(), 0, env_path=env, root=scripts)
            second = MODULE.run_node_settings(self.intent(), 0, env_path=env, root=scripts)
        self.assertEqual(first, second)
        self.assertEqual(launched.count('init-xray.sh'), 1)
        self.assertEqual(launched.count('init-awg.sh'), 1)
        self.assertEqual(launched.count('apply-node-settings.sh'), 2)

    def test_environment_verification_covers_endpoint_port_interface_preset_and_fingerprint(self):
        intent = self.intent()
        intent['settings'].update(awg_public_host='awg.example', awg_interface='vpn0',
            awg_i1_preset='dns', xray_fingerprint='firefox')
        env = Path(self.temp.name) / 'node.env'
        values = {'SERVER_KEY': 'n1', 'AWG_SERVER_IP': 'awg.example', 'AWG_SERVER_PORT': '51820',
                  'AWG_IFACE': 'vpn0', 'AWG_I1_PRESET': 'dns', 'XRAY_FP': 'firefox'}
        def write(settings):
            env.write_text(''.join(f'{key}={value}\n' for key, value in settings.items()))
        write(values)
        MODULE.verify_node_settings_environment(intent, env)
        for field in values:
            with self.subTest(field=field):
                write({**values, field: 'old'})
                with self.assertRaises(ValueError):
                    MODULE.verify_node_settings_environment(intent, env)

    def test_config_verification_rejects_old_preset_or_interface(self):
        intent = self.intent()
        intent['protocols'] = ['awg']
        intent['settings'].update(awg_i1_preset='dns', awg_interface='vpn0')
        path = Path(self.temp.name) / 'vpn0.conf'
        path.write_text('[Interface]\nListenPort = 51820\nI1 = <b 0xc000000001><rc 8><r 900>\n')
        with self.assertRaises(ValueError):
            MODULE.verify_node_settings_config(intent, '', path)
        path.write_text('[Interface]\nListenPort = 51820\nI1 = <rc 2><b 0x01000001000000000000><r 32>\n')
        MODULE.verify_node_settings_config(intent, '', path)
        old = path.with_name('wg0.conf')
        old.write_text(path.read_text())
        with self.assertRaises(ValueError):
            MODULE.verify_node_settings_config(intent, '', old)

    def test_repair_does_not_accept_matching_configs_with_old_endpoint(self):
        intent = self.intent()
        env = Path(self.temp.name) / 'node.env'
        env.write_text('SERVER_KEY=n1\nAWG_SERVER_IP=old.example\nAWG_SERVER_PORT=51820\n'
                       'AWG_IFACE=wg0\nAWG_I1_PRESET=quic\nXRAY_FP=chrome\n')
        with patch.object(MODULE, 'verify_node_settings_config') as verify, patch.object(
                MODULE.subprocess, 'run', side_effect=[
                    subprocess.CompletedProcess([], 0, '/tmp/xray.json\n/tmp/wg0.conf\nxray\nawg\n'),
                    subprocess.CompletedProcess([], 0, 'n1\nold.example\n51820\nwg0\nquic\nchrome\n'),
                    subprocess.CompletedProcess([], 0, 'true\n'),
                    subprocess.CompletedProcess([], 0, 'true\n')]):
            result = MODULE.inspect_node_settings_for_repair(intent, env_path=env)
        self.assertFalse(result['config_matches'])
        self.assertTrue(result['containers_running'])
        verify.assert_not_called()

    def test_real_settings_script_preserves_identities_and_updates_protocol_parameters(self):
        root = Path(self.temp.name)
        assets = Path(__file__).resolve().parents[1] / 'runtime_assets'
        env = root / 'node.env'
        xray = root / 'xray.json'
        awg = root / 'wg0.conf'
        env.write_text(f'XRAY_CONFIG={xray}\nAWG_CONFIG={awg}\nXRAY_CONTAINER_NAME=xray\nAWG_CONTAINER_NAME=awg\n')
        data = {'inbounds': [
            {'tag': tag, 'port': port, 'settings': {'clients': [{'id': 'existing-user'}]},
             'streamSettings': {'realitySettings': {'privateKey': 'existing-server-key',
                 'shortIds': ['existing-short-id'], 'serverNames': ['old.example'], 'dest': 'old.example:443'},
                 **({'xhttpSettings': {'path': '/old'}} if tag == 'reality-xhttp' else {})}}
            for tag, port in [('reality-tcp', 443), ('reality-xhttp', 8443)]]}
        xray.write_text(json.dumps(data))
        profile = subprocess.check_output(['python3', str(assets / 'awg_profile.py'), 'init', str(awg), 'quic'], text=True)
        peers = '\n# existing-device\n[Peer]\nPublicKey = existing-peer\nPresharedKey = existing-psk\nAllowedIPs = 10.8.1.2/32\n'
        awg.write_text('[Interface]\nPrivateKey = existing-server\nListenPort = 51820\n' + profile + peers)
        script = (assets / 'apply-node-settings.sh').read_text().replace(
            'source /etc/node-plane/node.env', f'source {shlex.quote(str(env))}').replace(
            '/opt/node-plane-runtime/', str(root) + '/')
        (root / 'apply-node-settings.sh').write_text(script)
        (root / 'awg_profile.py').write_text((assets / 'awg_profile.py').read_text())
        for name in ('deploy-xray.sh', 'deploy-awg.sh'):
            (root / name).write_text('#!/bin/sh\nexit 0\n')
            (root / name).chmod(0o755)
        (root / 'apply-node-settings.sh').chmod(0o755)
        intent = self.intent()
        intent['settings'].update(public_host='new.example', xray_host='vless.example',
            xray_sni='www.example.com', xray_fingerprint='firefox', xray_tcp_port=1443,
            xray_xhttp_port=2443, xray_xhttp_path='/new', awg_public_host='awg.example',
            awg_interface='vpn0', awg_port=5300, awg_i1_preset='dns', awg_port_mode='manual')
        real_run = subprocess.run
        def run(args, **kwargs):
            if args[0] in {'ufw', 'docker'}:
                return subprocess.CompletedProcess(args, 0, 'true\n' if args[0] == 'docker' else '')
            return real_run(args, **kwargs)
        with patch.object(MODULE.subprocess, 'run', side_effect=run):
            result = MODULE.run_node_settings(intent, 0, env_path=env, root=root)
        self.assertEqual(json.loads(result['payload_json'])['settings_sha256'], MODULE.node_settings_digest(intent))
        updated = json.loads(xray.read_text())
        for inbound in updated['inbounds']:
            self.assertEqual(inbound['settings']['clients'], [{'id': 'existing-user'}])
            self.assertEqual(inbound['streamSettings']['realitySettings']['privateKey'], 'existing-server-key')
            self.assertEqual(inbound['streamSettings']['realitySettings']['shortIds'], ['existing-short-id'])
        new_awg = root / 'vpn0.conf'
        self.assertIn('PrivateKey = existing-server', new_awg.read_text())
        self.assertIn(peers, new_awg.read_text())
        MODULE.verify_node_settings_environment(intent, env)
        MODULE.verify_node_settings_config(intent, xray, new_awg)
        # Explicit profiles may use custom CPS patterns unrelated to the preset.
        manual = MODULE.awg_profile_module().new_profile('quic')
        manual['I1'] = '<r 80>'
        intent['settings']['awg_parameters'] = manual
        with patch.object(MODULE.subprocess, 'run', side_effect=run):
            MODULE.run_node_settings(intent, 0, env_path=env, root=root)
        self.assertIn(peers, new_awg.read_text())
        self.assertIn('PrivateKey = existing-server', new_awg.read_text())
        MODULE.verify_node_settings_config(intent, xray, new_awg)
        modified = dict(manual, Jc=str(int(manual['Jc']) + 1))
        wrong = dict(intent, settings=dict(intent['settings'], awg_parameters=modified))
        with self.assertRaises(ValueError):
            MODULE.verify_node_settings_config(wrong, xray, new_awg)
        self.assertEqual((root / '.node-plane-i1-preset').read_text().strip(), 'manual')
        intent['settings']['awg_parameters'] = {}
        with patch.object(MODULE.subprocess, 'run', side_effect=run):
            MODULE.run_node_settings(intent, 0, env_path=env, root=root)
        self.assertEqual((root / '.node-plane-i1-preset').read_text().strip(), 'dns')
        self.assertIn('I1 = <rc 2><b 0x01000001000000000000>', new_awg.read_text())
        self.assertIn(peers, new_awg.read_text())
