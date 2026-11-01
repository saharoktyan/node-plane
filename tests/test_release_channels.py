"""Release lanes and migration from the experimental 1.0 prereleases."""
import unittest

from services.release_versions import branch_channel, parse_release_metadata, release_channel


class ReleaseChannelTests(unittest.TestCase):
    def metadata(self, tag='v0.5.0-beta.1', channel='beta', commit='a' * 40):
        return (f'format=2\nchannel={channel}\ntag={tag}\nversion={tag[1:]}\ncommit={commit}\n').encode()

    def test_channels_are_disjoint(self):
        for tag, channel in [('v0.5.0-alpha.12', 'dev'), ('v0.5.0-beta.1', 'beta'),
                             ('v0.5.0-rc.3', 'beta'), ('v0.5.0', 'stable')]:
            self.assertEqual(release_channel(tag), channel)
            self.assertEqual(branch_channel('main' if channel == 'stable' else channel), channel)
        for invalid in ['legacy/v1.0.0-beta.11', 'v01.0.0', 'v0.5.0-beta.01',
                        'v0.5.0-preview.1', 'v0.5.0-beta.1+build', '0.5.0', 'HEAD']:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                release_channel(invalid)
        with self.assertRaises(ValueError):
            branch_channel('feature/updates')

    def test_metadata_requires_explicit_matching_channel_and_identity(self):
        good = self.metadata()
        parsed = parse_release_metadata(good, tag='v0.5.0-beta.1', channel='beta', commit='a' * 40)
        self.assertEqual(parsed['commit'], 'a' * 40)
        for data in [good.replace(b'format=2\n', b''), good.replace(b'channel=beta\n', b''),
                     good + b'channel=beta\n', good + b'invalid-line\n',
                     self.metadata(commit='a' * 12), self.metadata(channel='dev'),
                     self.metadata(tag='v1.0.0-beta.11'), b'x' * 4097]:
            with self.subTest(data=data[:80]), self.assertRaises(ValueError):
                parse_release_metadata(data, tag='v0.5.0-beta.1', channel='beta', commit='a' * 40)
        with self.assertRaises(ValueError):
            parse_release_metadata(good, tag='v0.5.0-beta.1', channel='beta', commit='b' * 40)

    def test_old_beta_without_channel_metadata_is_excluded(self):
        old = b'tag=v1.0.0-beta.11\nversion=1.0.0-beta.11\ncommit=ce88545\n'
        with self.assertRaises(ValueError):
            parse_release_metadata(old, tag='v1.0.0-beta.11', channel='beta')
