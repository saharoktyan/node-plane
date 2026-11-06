"""Texts in several languages: what is kept, what each language shows."""
import unittest

from backend import localized
from backend.authorization import AccessDenied


class LocalizedTests(unittest.TestCase):
    def code(self, value, **kwargs):
        with self.assertRaises(AccessDenied) as caught:
            localized.checked(value, max_length=kwargs.pop('max_length', 20), **kwargs)
        return caught.exception.code

    def test_only_the_source_is_required_and_others_fall_back(self):
        value = localized.checked({'source': 'ru', 'texts': {'ru': ' Привет ', 'en': ''}}, max_length=20)
        self.assertEqual(value, {'source': 'ru', 'texts': {'ru': 'Привет'}, 'fallback': {}})
        self.assertEqual(localized.resolve(value, 'en'), 'Привет')
        self.assertEqual(localized.resolve(value, 'de-DE'), 'Привет')
        self.assertEqual(localized.public(value)['shown'], {'en': 'ru', 'ru': 'ru'})

    def test_a_language_without_its_text_shows_the_chosen_one(self):
        both = {'source': 'en', 'texts': {'en': 'Hi', 'ru': 'Привет'}}
        self.assertEqual(localized.resolve(localized.checked(both, max_length=20), 'ru_RU'), 'Привет')
        # A fallback to the source is the default and is not kept.
        value = localized.checked({'source': 'en', 'texts': {'en': 'Hi'}, 'fallback': {'ru': 'en'}}, max_length=20)
        self.assertEqual(value['fallback'], {})

    def test_refusals(self):
        for value in ({'source': 'ru', 'texts': {'en': 'Hi'}}, {'source': 'xx', 'texts': {'xx': 'Hi'}},
                      {'source': 'en', 'texts': {'en': 'Hi'}, 'fallback': {'ru': 'de'}},
                      {'source': 'en', 'texts': {'en': 'Hi', 'ru': 'Привет'}, 'fallback': {'ru': 'en'}},
                      {'source': 'en', 'texts': {'en': 'x' * 21}}, {'source': 'en', 'texts': {'en': 'Hi'}, 'extra': 1}):
            self.assertEqual(self.code(value), 'invalid_localized_text', value)
        self.assertEqual(self.code({'source': 'en', 'texts': {'en': 'a\nb'}}, multiline=False), 'invalid_localized_text')

    def test_plain_strings_from_before_are_texts_in_the_default_language(self):
        self.assertEqual(localized.loads('Hello'), localized.plain('Hello'))
        self.assertIsNone(localized.loads(None))


if __name__ == '__main__':
    unittest.main()
