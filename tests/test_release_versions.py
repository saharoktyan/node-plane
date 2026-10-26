"""Prerelease comparisons shared by update policy and administrator overview."""
import unittest
from services.release_versions import _parse_semver, _compare_versions


class ReleaseVersionTests(unittest.TestCase):
    def test_numeric_identifiers_and_stage_order(self):
        versions = ['1.0.0-alpha.2', '1.0.0-alpha.10', '1.0.0-beta.1',
                    '1.0.0-beta.2', '1.0.0-beta.11', '1.0.0-rc.1',
                    '1.0.0-rc.2', '1.0.0-rc.10', '1.0.0', '1.0.1-alpha.1']
        for i, left in enumerate(versions):
            for j, right in enumerate(versions):
                with self.subTest(left=left, right=right):
                    self.assertEqual(_compare_versions(left, right), (i > j) - (i < j))

    def test_semver_identifiers_and_invalid_versions(self):
        self.assertGreater(_compare_versions('1.0.0-alpha.beta', '1.0.0-alpha.9'), 0)
        for value in ['', 'main', '1.0', '1.0.0-beta.', '1.0.0-rc..1', '1.0.0-beta.01']:
            with self.subTest(value=value):
                self.assertIsNone(_parse_semver(value))
