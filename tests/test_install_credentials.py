from pathlib import Path
import subprocess
import unittest


class InstallationCredentialTests(unittest.TestCase):
    def valid(self, function, value):
        source = Path(__file__).resolve().parents[1] / 'scripts/lib/install_credentials.sh'
        result = subprocess.run(['bash', '-c', 'source "$1"; "$2" "$3"',
            'validate', str(source), function, value], capture_output=True, timeout=5)
        self.assertEqual(result.stdout, b'')
        self.assertEqual(result.stderr, b'')
        return result.returncode == 0

    def test_token_format(self):
        for value in ('123:abcdefghijklmnopqrstuvwx', '123:abcdefghijklmnop_QRST-123'):
            self.assertTrue(self.valid('valid_telegram_bot_token', value))
        for value in ('', 'replace_me', '123456', '123:short',
                '0:abcdefghijklmnopqrstuvwx', '-123:abcdefghijklmnopqrstuvwx',
                '123:abcdefghijklmnop qrstuvwx', '123:abcdefghijklmnopqrstuvwx\n',
                '9223372036854775808:abcdefghijklmnopqrstuvwx'):
            self.assertFalse(self.valid('valid_telegram_bot_token', value))

    def test_administrator_ids_format(self):
        for value in ('42', '42, 78', ' 42 , 78 ', '9223372036854775807'):
            self.assertTrue(self.valid('valid_telegram_admin_ids', value))
        for value in ('', '0', '42,0', '-42', '@admin', '42,', ',42', '42,,78',
                '42:token', '42\n78', '42,\t78', '9223372036854775808'):
            self.assertFalse(self.valid('valid_telegram_admin_ids', value))
