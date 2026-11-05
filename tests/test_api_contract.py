"""The committed API contract matches the application."""
import unittest

from backend import api_contract


class ApiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.document = api_contract.schema()

    def test_the_committed_schema_is_current(self):
        self.assertEqual(api_contract.SCHEMA.read_text(), api_contract.text(self.document),
                         'The API changed: run scripts/export_openapi.py and commit api/openapi.json.')

    def test_new_operations_declare_their_response(self):
        allowed = set(api_contract.UNTYPED.read_text().split('\n')) - {''}
        found = set(api_contract.untyped_operations(self.document))
        self.assertFalse(found - allowed, 'Give these operations a response_model.')
        self.assertFalse(allowed - found,
                         'These operations are typed now: run scripts/export_openapi.py --untyped.')

    def test_the_contract_has_its_own_version(self):
        self.assertEqual(self.document['info']['version'], api_contract.VERSION)
