import unittest

from awg_parameters import parse_parameters, profile_module, validate_parameters


class ManualAwgParametersTests(unittest.TestCase):
    def test_block_and_json_preserve_exact_values_and_auto_resets(self):
        import json
        values = profile_module().new_profile('quic')
        block = '\n'.join(f'{key} = {value}' for key, value in values.items())
        self.assertEqual(parse_parameters(block), values)
        self.assertEqual(parse_parameters(json.dumps(values)), values)
        self.assertEqual(parse_parameters('auto'), {})

    def test_invalid_combination_identity_keys_duplicates_and_injection_are_rejected(self):
        values = profile_module().new_profile('quic')
        for invalid in ({**values, 'PrivateKey': 'secret'}, {**values, 'H2': values['H1']},
                        {**values, 'S2': str(int(values['S1']) + 56)},
                        {**values, 'RekeyAfterTime': '300', 'RejectAfterTime': '200'},
                        {**values, 'I1': '<r 80>\n[Peer]'}, {'Jc': '3'}):
            with self.subTest(keys=list(invalid)):
                with self.assertRaises(ValueError):
                    validate_parameters(invalid)
        for block in ('Jc = 3\nJc = 4', '{"Jc":"3","Jc":"4"}', ''):
            with self.assertRaises(ValueError):
                parse_parameters(block)
