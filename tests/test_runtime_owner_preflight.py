"""A server running another node's VPN runtime is refused before any change."""
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class RuntimeOwnerPreflightTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.script = subprocess.run(['bash', '-c', 'source "$1"; runtime_owner_script', 'test',
            str(ROOT / 'scripts/lib/agent_ssh.sh')], check=True, capture_output=True, text=True).stdout

    def check(self, node_key, content=None):
        env = self.root / 'node.env'
        if content is not None:
            env.write_text(content)
        # sudo is a plain pass-through here; the script never needs a shell.
        return subprocess.run(['bash', '-c', 'sudo() { "$@"; }; export -f sudo; bash -s -- "$1" "$2"',
                               'test', node_key, str(env)], input=self.script, capture_output=True, text=True)

    def test_only_the_same_node_or_a_fresh_server_passes(self):
        self.assertEqual(self.check('de1').returncode, 0)
        self.assertEqual(self.check('de1', "SERVER_KEY='de1'\n").returncode, 0)
        refused = self.check('de1', 'SERVER_KEY=ger2\nXRAY_CONFIG=/x\n')
        self.assertEqual((refused.returncode, refused.stdout.strip()), (3, 'ger2'))
        unknown = self.check('de1', 'XRAY_CONFIG=/x\n')
        self.assertEqual((unknown.returncode, unknown.stdout.strip()), (3, 'unknown'))

    def test_node_env_is_never_run(self):
        marker = self.root / 'ran'
        result = self.check('de1', f'SERVER_KEY=ger2\nX=$(touch {marker})\n')
        self.assertEqual(result.returncode, 3)
        self.assertFalse(marker.exists())


if __name__ == '__main__':
    unittest.main()
