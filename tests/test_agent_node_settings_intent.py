import importlib.util
import json
import os
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
