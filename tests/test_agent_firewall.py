"""Run the remote firewall script with isolated command doubles only."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class AgentFirewallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.log = self.root / 'calls.jsonl'
        self.script = subprocess.run(['bash', '-c',
            'source "$1"; agent_firewall_script', 'test',
            str(ROOT / 'scripts/lib/agent_ssh.sh')], check=True,
            capture_output=True, text=True).stdout
        for name in ('bash', 'python3', 'env'):
            (self.root / name).symlink_to(shutil.which(name))
        self.executable('sudo', '#!/bin/bash\nexec "$@"\n')
        self.executable('ufw', '''#!/usr/bin/env python3
import json, os, sys
with open(os.environ['CALL_LOG'], 'a') as output:
    output.write(json.dumps(sys.argv[1:]) + '\\n')
if sys.argv[1:] == ['status']:
    print(os.environ.get('UFW_STATUS', 'Status: active'))
    sys.exit(int(os.environ.get('STATUS_EXIT', '0')))
sys.exit(int(os.environ.get('ALLOW_EXIT', '0')))
''')

    def executable(self, name, text):
        path = self.root / name
        path.write_text(text)
        path.chmod(0o755)

    def run_script(self, port='50061', source='192.0.2.7 1234 198.51.100.8 22', **settings):
        return subprocess.run([str(self.root / 'bash'), '-s', '--', port],
            input=self.script, capture_output=True, text=True, timeout=10,
            env={**os.environ, 'PATH': str(self.root), 'CALL_LOG': str(self.log),
                 'SSH_CONNECTION': source, **settings})

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_active_ufw_allows_only_observed_controller_ipv4_or_ipv6(self):
        for address in ('192.0.2.7', '2001:db8::7'):
            with self.subTest(address=address):
                self.log.unlink(missing_ok=True)
                result = self.run_script(source=f'{address} 1234 198.51.100.8 22')
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.calls(), [['status'], ['allow', 'proto', 'tcp',
                    'from', address, 'to', 'any', 'port', '50061', 'comment', 'Node Plane agent']])

    def test_inactive_or_absent_ufw_is_never_enabled(self):
        result = self.run_script(UFW_STATUS='Status: inactive')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), [['status']])
        self.log.unlink()
        (self.root / 'ufw').unlink()
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), [])

    def test_untrusted_source_never_becomes_unrestricted_allow(self):
        for source in ('', 'any 1234 node 22', '0.0.0.0 1234 node 22',
                       ':: 1234 node 22', '224.0.0.1 1234 node 22',
                       '192.0.2.7;echo 1234 node 22', 'fe80::1%eth0 1234 node 22'):
            with self.subTest(source=source):
                self.log.unlink(missing_ok=True)
                result = self.run_script(source=source)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('AGENT_FIREWALL_FAILED:', result.stderr)
                self.assertEqual(self.calls(), [['status']])

    def test_invalid_port_fails_before_firewall_access(self):
        for port in ('0', '65536', '9' * 100, '-1', '50061;echo', '５００６１'):
            with self.subTest(port=port):
                self.log.unlink(missing_ok=True)
                result = self.run_script(port=port)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('AGENT_FIREWALL_FAILED:', result.stderr)
                self.assertEqual(self.calls(), [])

    def test_status_and_rule_failures_are_reported(self):
        for settings in ({'STATUS_EXIT': '1'}, {'UFW_STATUS': 'unknown'}, {'ALLOW_EXIT': '1'}):
            with self.subTest(settings=settings):
                result = self.run_script(**settings)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('AGENT_FIREWALL_FAILED:', result.stderr)

    def test_repeated_install_uses_same_narrow_rule_and_custom_port(self):
        for _ in range(2):
            result = self.run_script(port='50062')
            self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertEqual(calls[:2], calls[2:])
        self.assertIn('50062', calls[1])

if __name__ == '__main__':
    unittest.main()
