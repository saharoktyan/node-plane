from unittest import TestCase
from domain_names import valid_sni


class SniTests(TestCase):
    def test_domain_names_and_punycode(self):
        for name in ('example.com', 'WWW.Cloudflare.COM', 'a-b.example', 'xn--e1afmkfd.xn--p1ai'):
            with self.subTest(name=name):
                self.assertTrue(valid_sni(name))

    def test_rejects_urls_ips_paths_ports_and_malformed_labels(self):
        for name in ('', 'localhost', 'https://example.com', 'example.com/path', 'example.com:443',
                     '1.2.3.4', 'example..com', '-a.example', 'a-.example', 'a_b.example',
                     '*.example.com', 'example.com.', ' example.com', 'пример.рф',
                     'a'*64+'.com', '.'.join(['a'*63]*4)):
            with self.subTest(name=name):
                self.assertFalse(valid_sni(name))
