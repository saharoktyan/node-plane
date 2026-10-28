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
REMOVE_UNSELECTED = MODULE.remove_unselected_protocols


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
        env.write_text(f'SERVER_KEY=n1\nXRAY_CONFIG={root / "xray.json"}\nAWG_CONFIG={awg}\nXRAY_CONTAINER_NAME=xray\nAWG_CONTAINER_NAME=awg\n')
        (root / 'awg_ports.py').write_text('def select_port(*args):\n    return 8443\n')
        (root / 'init-awg.sh').write_text(f'#!/bin/bash\nsource "{env}"\nprintf "[Interface]\\nListenPort = %s\\nI1 = <b 0xc000000001><rc 8><r 900>\\n" "$AWG_SERVER_PORT" > "{awg}"\n')
        (root / 'apply-node-settings.sh').write_text('#!/bin/sh\nexit 0\n')
        for name in ('init-awg.sh', 'apply-node-settings.sh'):
            (root / name).chmod(0o755)
        intent = self.intent()
        intent['protocols'] = ['awg']
        intent['settings'].update(awg_port=443, awg_port_mode='auto', awg_i1_preset='quic', ipv6_outbound='on')
        real_run = subprocess.run
        firewall_calls = []

        def run(args, **kwargs):
            if args[0] in {'ufw', 'docker'}:
                if args[0] == 'ufw':
                    firewall_calls.append(args)
                return subprocess.CompletedProcess(args, 0, 'true\n' if args[0] == 'docker' else '')
            return real_run(args, **kwargs)

        with patch.object(MODULE.subprocess, 'run', side_effect=run), \
                patch.object(MODULE.shutil, 'which', return_value='/fake/ufw'):
            result = MODULE.run_node_settings(intent, 0, env_path=env, root=root)
        self.assertEqual(firewall_calls, [['ufw', 'allow', '8443/udp'], ['ufw', 'reload']])
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
        # These runner tests fake protocol deployment and Docker. Exercise the
        # cleanup boundary separately with ownership/path checks in operation tests.
        def cleanup(intent, lock_fd, root):
            env = root / 'node.env'
            if env.exists():
                for line in env.read_text().splitlines():
                    key, _, value = line.partition('=')
                    protocol = {'XRAY_CONFIG': 'xray', 'AWG_CONFIG': 'awg'}.get(key)
                    if protocol and protocol in intent.get('protocol_plan', {}).get('remove_protocols', []):
                        Path(shlex.split(value)[0]).unlink(missing_ok=True)
        mocked = patch.object(MODULE, 'remove_unselected_protocols', side_effect=cleanup)
        self.cleanup = mocked.start()
        self.addCleanup(mocked.stop)

    def test_empty_protocol_intent_is_durable_and_retains_the_agent_journal(self):
        intent = dict(self.intent(), protocols=[], settings={}, protocol_plan={'remove_protocols': ['awg', 'xray']})
        first = MODULE.apply_node_settings(intent, self.journal, self.runner)
        self.assertEqual(first, MODULE.lookup_node_settings(intent, self.journal))
        self.assertEqual(first, MODULE.apply_node_settings(intent, self.journal, self.runner))
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(self.journal.exists())

    def test_empty_command_without_explicit_plan_never_touches_runtime(self):
        intent = dict(self.intent(), protocols=[], settings={})
        with patch.object(MODULE.subprocess, 'run') as run:
            with self.assertRaisesRegex(ValueError, 'no VPN settings'):
                MODULE.run_node_settings(intent, 0, env_path=Path(self.temp.name)/'foreign.env')
            self.assertEqual(MODULE.inspect_node_settings_for_repair(intent),
                {'config_matches': False, 'containers_running': False})
        run.assert_not_called()
        self.cleanup.assert_not_called()

    def test_nonselected_foreign_protocol_is_not_a_cleanup_instruction(self):
        for protocol_plan in (None, {'remove_protocols': []}):
            intent = dict(self.intent(), protocols=['awg'])
            if protocol_plan is not None:
                intent['protocol_plan'] = protocol_plan
            with patch.object(MODULE, 'settings_cleanup_module') as load:
                REMOVE_UNSELECTED(intent, 0, Path(self.temp.name))
            load.assert_not_called()

    def test_removal_plan_refuses_a_runtime_owned_by_another_node(self):
        root = Path(self.temp.name)
        env = root / 'node.env'
        config = root / 'xray.json'
        config.write_text('foreign configuration')
        env.write_text(f'SERVER_KEY=other-node\nXRAY_CONFIG={config}\n')
        (root / 'apply-node-settings.sh').write_text('#!/bin/sh\nexit 0\n')
        original = env.read_text()
        intent = dict(self.intent(), protocols=[], protocol_plan={'remove_protocols': ['xray']})
        with self.assertRaisesRegex(ValueError, 'ownership is not confirmed'):
            MODULE.run_node_settings(intent, 0, env_path=env, root=root)
        self.cleanup.assert_not_called()
        self.assertEqual(config.read_text(), 'foreign configuration')
        self.assertEqual(env.read_text(), original)

    def test_agent_only_repair_requires_absence_of_removed_containers(self):
        from unittest.mock import Mock
        intent = dict(self.intent(), protocols=[], settings={}, protocol_plan={'remove_protocols': ['awg', 'xray']})
        cleanup = Mock()
        for leftovers in ([], ['a' * 64]):
            cleanup.owned_protocol_containers.return_value = leftovers
            with self.subTest(leftovers=bool(leftovers)), \
                 patch.object(MODULE, 'settings_cleanup_module', return_value=cleanup), \
                 patch.object(MODULE, 'verify_node_settings_config'), \
                 patch.object(MODULE, 'verify_node_settings_environment'), \
                 patch.object(MODULE.shutil, 'which', return_value='/usr/bin/docker'), \
                 patch.object(MODULE.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '/tmp/xray.json\n/tmp/wg0.conf\nxray\nawg\n')):
                result = MODULE.inspect_node_settings_for_repair(intent)
            self.assertEqual(result, {'config_matches': not leftovers, 'containers_running': not leftovers})

    def test_agent_only_apply_cleans_both_protocols_and_opens_no_ports(self):
        root = Path(self.temp.name)
        env = root / 'node.env'
        env.write_text(f'SERVER_KEY=n1\nXRAY_CONFIG={root / "xray.json"}\nAWG_CONFIG={root / "wg0.conf"}\n')
        (root / 'xray.json').write_text('{}')
        (root / 'wg0.conf').write_text('[Interface]')
        (root / 'apply-node-settings.sh').write_text('#!/bin/sh\nexit 0\n')
        (root / 'apply-node-settings.sh').chmod(0o755)
        intent = dict(self.intent(), protocols=[], settings={}, protocol_plan={'remove_protocols': ['awg', 'xray']})
        real_run = subprocess.run
        firewall = []
        def run(args, **kwargs):
            if args[0] == 'ufw':
                firewall.append(args)
                return subprocess.CompletedProcess(args, 0, '')
            return real_run(args, **kwargs)
        with patch.object(MODULE.subprocess, 'run', side_effect=run), patch.object(MODULE.shutil, 'which', return_value='/fake/ufw'):
            result = MODULE.run_node_settings(intent, 0, env_path=env, root=root)
        self.cleanup.assert_called_once_with(intent, 0, root)
        self.assertEqual(firewall, [['ufw', 'reload']])
        self.assertEqual(json.loads(result['payload_json'])['settings_sha256'], MODULE.node_settings_digest(intent))
        self.assertTrue(env.exists())
        self.assertTrue((root / 'apply-node-settings.sh').exists())

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
        data = {'outbounds': [{'protocol': 'freedom', 'tag': 'direct'}], 'inbounds': [
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
        env.write_text(f'SERVER_KEY=n1\nXRAY_CONFIG={xray}\nAWG_CONFIG={awg}\nXRAY_CONTAINER_NAME=xray\nAWG_CONTAINER_NAME=awg\n')
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
        firewall_calls = []

        def run(args, **kwargs):
            if args[0] in {'ufw', 'docker'}:
                if args[0] == 'ufw':
                    firewall_calls.append(args)
                return subprocess.CompletedProcess(args, 0, 'true\n' if args[0] == 'docker' else '')
            launched.append(Path(args[0]).name)
            return real_run(args, **kwargs)

        with patch.object(MODULE.subprocess, 'run', side_effect=run), \
                patch.object(MODULE.shutil, 'which', return_value='/fake/ufw'):
            first = MODULE.run_node_settings(self.intent(), 0, env_path=env, root=scripts)
            second = MODULE.run_node_settings(self.intent(), 0, env_path=env, root=scripts)
        self.assertEqual(first, second)
        self.assertEqual(launched.count('init-xray.sh'), 1)
        self.assertEqual(launched.count('init-awg.sh'), 1)
        self.assertEqual(launched.count('apply-node-settings.sh'), 2)
        expected = [['ufw', 'allow', '443/tcp'], ['ufw', 'allow', '8443/tcp'],
                    ['ufw', 'allow', '51820/udp'], ['ufw', 'reload']]
        self.assertEqual(firewall_calls, expected + expected)

    def test_applying_settings_opens_current_ports_for_only_enabled_protocols(self):
        root = Path(self.temp.name)
        env = root / 'node.env'
        xray, awg = root / 'xray.json', root / 'wg0.conf'
        (root / 'apply-node-settings.sh').write_text('#!/bin/sh\nexit 0\n')
        (root / 'apply-node-settings.sh').chmod(0o755)
        real_run = subprocess.run
        for protocols, expected in (
                (['awg'], ['51999/udp']),
                (['xray'], ['4443/tcp', '9443/tcp']),
                (['awg', 'xray'], ['4443/tcp', '9443/tcp', '51999/udp'])):
            with self.subTest(protocols=protocols):
                env.write_text(f'SERVER_KEY=n1\nXRAY_CONFIG={xray}\nAWG_CONFIG={awg}\nXRAY_CONTAINER_NAME=xray\nAWG_CONTAINER_NAME=awg\n')
                awg.write_text('[Interface]\nListenPort = 51999\n')
                xray.write_text(json.dumps({'inbounds': [
                    {'tag': 'reality-tcp', 'port': 4443, 'streamSettings': {
                        'realitySettings': {'serverNames': ['www.cloudflare.com'], 'dest': 'www.cloudflare.com:443'}}},
                    {'tag': 'reality-xhttp', 'port': 9443, 'streamSettings': {
                        'realitySettings': {'serverNames': ['www.cloudflare.com'], 'dest': 'www.cloudflare.com:443'},
                        'xhttpSettings': {'path': '/assets'}}}]}))
                intent = self.intent()
                intent['protocols'] = protocols
                intent['settings'].update(awg_port=51999, xray_tcp_port=4443, xray_xhttp_port=9443)
                firewall = []
                def run(args, **kwargs):
                    if args[0] == 'ufw':
                        firewall.append(args)
                        return subprocess.CompletedProcess(args, 0, '')
                    if args[0] == 'docker':
                        return subprocess.CompletedProcess(args, 0, 'true\n')
                    return real_run(args, **kwargs)
                with patch.object(MODULE.subprocess, 'run', side_effect=run), \
                        patch.object(MODULE.shutil, 'which', return_value='/fake/ufw'):
                    MODULE.run_node_settings(intent, 0, env_path=env, root=root)
                self.assertEqual(firewall, [['ufw', 'allow', port] for port in expected] + [['ufw', 'reload']])

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
        env.write_text(f'SERVER_KEY=n1\nXRAY_CONFIG={xray}\nAWG_CONFIG={awg}\nXRAY_CONTAINER_NAME=xray\nAWG_CONTAINER_NAME=awg\n')
        data = {'outbounds': [{'protocol': 'freedom', 'tag': 'direct'}], 'inbounds': [
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
            awg_interface='vpn0', awg_port=5300, awg_i1_preset='dns', awg_port_mode='manual', ipv6_outbound='off')
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
        direct = next(item for item in updated['outbounds'] if item['tag'] == 'direct')
        self.assertEqual(direct['settings']['domainStrategy'], 'ForceIPv4')
        self.assertEqual(updated['routing']['rules'][0]['ip'], ['::/0'])
        intent['settings']['ipv6_outbound'] = 'on'
        with patch.object(MODULE.subprocess, 'run', side_effect=run):
            MODULE.run_node_settings(intent, 0, env_path=env, root=root)
        enabled = json.loads(xray.read_text())
        direct = next(item for item in enabled['outbounds'] if item['tag'] == 'direct')
        self.assertEqual(direct['settings']['domainStrategy'], 'AsIs')
        self.assertFalse(any(item.get('outboundTag') == 'node-plane-ipv6-disabled'
            for item in enabled['routing']['rules']))
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
