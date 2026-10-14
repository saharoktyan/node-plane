from unittest import TestCase
from telegram_client.release_notes import preview
from telegram_client.alert_delivery import alert_screen


class ReleaseNotesTests(TestCase):
    def test_native_headers_code_and_table_links_replace_raw_markdown(self):
        markdown = "## Install\n```sh\ncurl https://example.test/install.sh | sh\n```\n| File | Platform |\n|---|---|\n| [Download](https://example.test/archive.zip) | Windows |"
        blocks,lines = preview(markdown)
        self.assertEqual([b.type for b in blocks],['heading','paragraph','paragraph'])
        self.assertEqual(blocks[1].text.type,'code')
        self.assertTrue(any(getattr(p,'type',None)=='url' for p in blocks[2].text))
        text = '\n'.join(lines)
        self.assertIn('Platform: Windows',text)
        self.assertNotIn('```',text)
        self.assertNotIn('|---|',text)
        screen = alert_screen({'event':{'kind':'update_available','payload':{'version':'v1','branch':'dev','changelog':markdown}}})
        details = next(b for b in screen.rich().blocks if b.type=='details')
        self.assertEqual(details.blocks[1].text.type,'code')

    def test_budget_drops_whole_links_and_code_blocks(self):
        url = 'https://example.test/' + 'x'*400
        blocks,lines = preview('# Release\n' + 'text '*300 + '\n[Download](' + url + ')\n```sh\n' + 'command '*400 + '\n```')
        text = '\n'.join(lines)
        self.assertLessEqual(len(text),1800)
        self.assertNotIn('https://example.test/',text)
        self.assertFalse(any(getattr(b.text,'type',None)=='code' for b in blocks))
