"""The controller checks manual AWG parameters as the node agent does: both
decide the cases in tests/fixtures/awg_profile_cases.json, which the agent's
Rust tests (protocols/awg/profile) run too."""
import json
import pathlib
import unittest

import awg_profile

CASES = json.loads((pathlib.Path(__file__).parent / 'fixtures/awg_profile_cases.json').read_text())


def valid_profile():
    """A valid AWG 3.1 profile of the shared cases."""
    case = CASES['validate'][0]
    assert case['valid']
    return dict(case['values'])


class AwgProfileCaseTests(unittest.TestCase):
    def test_validation_matches_the_agent(self):
        for case in CASES['validate']:
            for require, expected in ((True, case['valid']), (False, case['valid_any'])):
                with self.subTest(values=case['values'], require=require):
                    try:
                        awg_profile.validate(case['values'], require_31=require)
                        valid = True
                    except ValueError:
                        valid = False
                    self.assertEqual(valid, expected)

    def test_interfaces_are_read_as_the_agent_reads_them(self):
        for case in CASES['replace']:
            self.assertEqual(list(awg_profile.interface_values(case['config']).items()),
                             [tuple(pair) for pair in case['interface']])


if __name__ == '__main__':
    unittest.main()
