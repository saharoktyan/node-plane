"""Account name and language belong to the account, not to its Telegram identity."""

import unittest

from backend.identity_repository import SQLIdentityRepository
from db.migration_revisions import r0028_account_details

from tests.test_backend_identity import Database


class AccountDetailsTests(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        self.addCleanup(self.db.connection.close)
        self.identities = SQLIdentityRepository(self.db)
        self.identities.initialize_schema()

    def account(self, account_id):
        return dict(self.db.connection.execute(
            'SELECT display_name, locale, locale_selected FROM backend_accounts WHERE id=?',
            (account_id,)).fetchone())

    def test_telegram_details_fill_empty_account_details_once(self):
        account = self.identities.resolve_telegram(101)
        self.identities.update_telegram_details(101, first_name='Ann', last_name='Lee', language_code='ru-RU')
        self.assertEqual(self.account(account.id), {'display_name': 'Ann Lee', 'locale': 'ru', 'locale_selected': 0})
        self.identities.update_telegram_details(101, username='ann', language_code='en')
        self.assertEqual(self.account(account.id)['display_name'], 'Ann Lee')
        details = self.identities.account_details(account.id)
        self.assertEqual((details['display_name'], details['username'], details['locale']), ('Ann Lee', 'ann', 'ru'))

    def test_username_is_the_name_when_telegram_has_no_first_name(self):
        self.identities.update_telegram_details(102, username='ops')
        account = self.identities.resolve_telegram(102)
        self.assertEqual(self.account(account.id)['display_name'], '@ops')

    def test_account_without_telegram_has_its_own_language(self):
        account = self.identities.create_account()
        self.identities.set_locale_for_account(account.id, 'ru')
        self.assertEqual(self.account(account.id), {'display_name': None, 'locale': 'ru', 'locale_selected': 1})
        self.assertEqual(self.identities.account_details(account.id), {'locale': 'ru', 'locale_selected': True})

    def test_language_choice_is_kept_on_the_telegram_identity_too(self):
        account = self.identities.resolve_telegram(103)
        self.identities.set_locale_for_account(account.id, 'ru')
        row = self.db.connection.execute(
            "SELECT locale, locale_selected FROM backend_telegram_identity_details WHERE subject='103'").fetchone()
        self.assertEqual(tuple(row), ('ru', 1))

    def test_migration_fills_existing_accounts_from_telegram(self):
        account = self.identities.resolve_telegram(104)
        self.identities.update_telegram_details(104, first_name='Bo')
        self.db.connection.execute("UPDATE backend_accounts SET display_name=NULL, locale=NULL")
        self.db.connection.execute(
            "UPDATE backend_telegram_identity_details SET locale='ru', locale_selected=1 WHERE subject='104'")
        r0028_account_details.upgrade(self.db.connection)
        self.assertEqual(self.account(account.id), {'display_name': 'Bo', 'locale': 'ru', 'locale_selected': 1})


if __name__ == '__main__':
    unittest.main()
