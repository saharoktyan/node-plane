"""Regression tests for custom access expiry parsing."""
import unittest
from datetime import datetime, timezone

from services.access_expiry import parse_expiry


class AccessExpiryTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)

    def test_specific_date_today_and_future_allowed(self):
        # Today allowed, resolves to 23:59:59 UTC
        self.assertEqual(parse_expiry('2026-10-25', now=self.now), '2026-10-25T23:59:59+00:00')
        # Future date allowed
        self.assertEqual(parse_expiry('2026-10-26', now=self.now), '2026-10-26T23:59:59+00:00')
        self.assertEqual(parse_expiry('2030-01-01', now=self.now), '2030-01-01T23:59:59+00:00')

    def test_specific_date_past_or_invalid_rejected(self):
        # Past date rejected
        with self.assertRaises(ValueError):
            parse_expiry('2026-10-24', now=self.now)
        with self.assertRaises(ValueError):
            parse_expiry('1999-12-31', now=self.now)
        # Invalid dates (non-leap year Feb 29)
        with self.assertRaises(ValueError):
            parse_expiry('2025-02-29', now=self.now)
        # Invalid format / month
        with self.assertRaises(ValueError):
            parse_expiry('2026-13-01', now=self.now)
        with self.assertRaises(ValueError):
            parse_expiry('2026-00-10', now=self.now)

    def test_single_and_combined_durations(self):
        # 12h
        self.assertEqual(parse_expiry('12h', now=self.now), '2026-10-26T00:00:00+00:00')
        self.assertEqual(parse_expiry('12H', now=self.now), '2026-10-26T00:00:00+00:00')
        # 7d
        self.assertEqual(parse_expiry('7d', now=self.now), '2026-11-01T12:00:00+00:00')
        # 1m (30 days)
        self.assertEqual(parse_expiry('1m', now=self.now), '2026-11-24T12:00:00+00:00')
        # 1y (365 days)
        self.assertEqual(parse_expiry('1y', now=self.now), '2027-10-25T12:00:00+00:00')
        # Combined: 3d5m1y, arbitrary order and case-insensitive
        # 3*86400 + 5*30*86400 + 365*86400 = (3 + 150 + 365)*86400 = 518 days
        expected = datetime(2028, 3, 26, 12, 0, 0, tzinfo=timezone.utc).isoformat()
        self.assertEqual(parse_expiry('3d5m1y', now=self.now), expected)
        self.assertEqual(parse_expiry('1Y5M3D', now=self.now), expected)
        self.assertEqual(parse_expiry('5m1y3d', now=self.now), expected)

    def test_durations_invalid_and_overflow_rejected(self):
        # Duplicate units
        with self.assertRaises(ValueError):
            parse_expiry('3d4d', now=self.now)
        with self.assertRaises(ValueError):
            parse_expiry('1h2h', now=self.now)
        # Zero or negative
        with self.assertRaises(ValueError):
            parse_expiry('0d', now=self.now)
        with self.assertRaises(ValueError):
            parse_expiry('-1d', now=self.now)
        # Malformed tokens (including internal spaces)
        for bad in ('', 'abc', '1.5d', '3x', '10', '10 d', '1d 2h', '3d5'):
            with self.assertRaises(ValueError):
                parse_expiry(bad, now=self.now)
        # Overflow
        with self.assertRaises(ValueError):
            parse_expiry('99999y', now=self.now)


if __name__ == '__main__':
    unittest.main()
