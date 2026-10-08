"""
Unit tests for Markdown links in the HTML email body.

The model writes official links as [text](url) or [url](url). Before this fix
the email showed that syntax verbatim.

    python -m unittest discover -s tests -v
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'v2-cloud-run'))

import main  # noqa: E402

URL = 'https://cloud.google.com/blog/products/databases/spanner-queues-provide-native-transactional-messaging/'


class MarkdownLinkTest(unittest.TestCase):

    def test_named_link_keeps_its_text(self):
        # The shape the 2026-10-08 digest actually used.
        out = main.markdown_to_html(f'**官方連結**：[連結]({URL})', link_label='官方公告 ↗')
        self.assertIn(f'<a href="{URL}"', out)
        self.assertIn('>連結</a>', out)
        self.assertIn('<strong>官方連結</strong>', out)
        self.assertNotIn('](', out)

    def test_bare_url_link_uses_label(self):
        out = main.markdown_to_html(f'**官方連結**：[{URL}]({URL})', link_label='官方公告 ↗')
        self.assertIn('>官方公告 ↗</a>', out)
        self.assertEqual(out.count(URL), 1)

    def test_default_label_is_english(self):
        out = main.markdown_to_html(f'[{URL}]({URL})')
        self.assertIn('>Read more ↗</a>', out)

    def test_blog_arrow_line(self):
        out = main.markdown_to_html(f'**Some blog post**：summary → [連結]({URL})')
        self.assertIn('<strong>Some blog post</strong>', out)
        self.assertIn(f'href="{URL}"', out)

    def test_links_in_headings_and_list_items(self):
        out = main.markdown_to_html(f'### [Feature]({URL})\n- see [docs](https://example.com/x)')
        self.assertIn('<h3', out)
        self.assertIn('>Feature</a>', out)
        self.assertIn('href="https://example.com/x"', out)

    def test_multiple_links_on_one_line(self):
        out = main.markdown_to_html(f'[a]({URL}) and [b](https://example.com/x)')
        self.assertIn('>a</a>', out)
        self.assertIn('>b</a>', out)

    def test_ampersand_in_url_is_escaped(self):
        out = main.markdown_to_html('[q](https://example.com/?a=1&b=2)')
        self.assertIn('href="https://example.com/?a=1&amp;b=2"', out)

    def test_quote_in_url_cannot_break_out_of_href(self):
        out = main.markdown_to_html('[x](https://example.com/a"onmouseover="y)')
        self.assertNotIn('"onmouseover="', out)

    def test_non_http_brackets_are_left_alone(self):
        out = main.markdown_to_html('[TODO](not-a-url) stays as text')
        self.assertIn('[TODO](not-a-url)', out)
        self.assertNotIn('<a ', out)

    def test_both_languages_define_link_label(self):
        for lang, strings in main._EMAIL_STRINGS.items():
            self.assertIn('link_label', strings, lang)

    def test_send_email_uses_language_label(self):
        sent = {}
        with mock.patch.dict(main.CONFIG, {'DIGEST_LANGUAGE': 'zh-TW'}), \
             mock.patch.object(main, '_dispatch_send',
                                        side_effect=lambda **kw: sent.update(kw)):
            main.send_email(f'[{URL}]({URL})', None, 1, 1)
        self.assertIn('>官方公告 ↗</a>', sent['html_body'])


if __name__ == '__main__':
    unittest.main()
