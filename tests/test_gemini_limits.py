"""
Unit tests for finish_reason handling around the Gemini calls.

Everything Google is mocked: no request leaves the machine. Run from the repo root:

    python -m unittest discover -s tests -v
"""

import os
import sys
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'v2-cloud-run'))

import account_context  # noqa: E402
import main  # noqa: E402
from google.genai import types  # noqa: E402


def _resp(text, reason='STOP'):
    fr = getattr(types.FinishReason, reason) if isinstance(reason, str) else reason
    return SimpleNamespace(text=text, candidates=[SimpleNamespace(finish_reason=fr)])


def _sent_budget(call):
    return call.kwargs['config'].max_output_tokens


class _GeminiCase(unittest.TestCase):
    def setUp(self):
        self.client = mock.Mock()
        p = mock.patch.object(main.genai, 'Client', return_value=self.client)
        p.start()
        self.addCleanup(p.stop)
        q = mock.patch('builtins.print')
        self.printed = q.start()
        self.addCleanup(q.stop)

    def gen(self):
        return self.client.models.generate_content

    def logged(self):
        return '\n'.join(' '.join(map(str, c.args)) for c in self.printed.call_args_list)


class FinishReasonTests(_GeminiCase):

    def test_clean_stop_is_one_call(self):
        self.gen().return_value = _resp('full digest')
        self.assertEqual(main._invoke_gemini('p'), 'full digest')
        self.assertEqual(self.gen().call_count, 1)
        self.assertEqual(_sent_budget(self.gen().call_args), main.MAX_OUTPUT_TOKENS)

    def test_truncated_then_complete_on_retry(self):
        self.gen().side_effect = [_resp('half', 'MAX_TOKENS'), _resp('whole')]
        self.assertEqual(main._invoke_gemini('p'), 'whole')
        budgets = [_sent_budget(c) for c in self.gen().call_args_list]
        self.assertEqual(budgets, [main.MAX_OUTPUT_TOKENS, main.RETRY_MAX_OUTPUT_TOKENS])
        self.assertIn('retrying once', self.logged())

    def test_still_truncated_after_retry_raises(self):
        self.gen().side_effect = [_resp('half', 'MAX_TOKENS'), _resp('more', 'MAX_TOKENS')]
        with self.assertRaises(main.TruncatedOutputError) as ctx:
            main._invoke_gemini('p')
        self.assertIn('cut off', str(ctx.exception))
        self.assertEqual(self.gen().call_count, 2)

    def test_other_finish_reason_raises_without_retry(self):
        self.gen().return_value = _resp('partial', 'SAFETY')
        with self.assertRaises(RuntimeError) as ctx:
            main._invoke_gemini('p')
        self.assertIn('SAFETY', str(ctx.exception))
        self.assertEqual(self.gen().call_count, 1)

    def test_empty_text_raises(self):
        self.gen().return_value = _resp('   ')
        with self.assertRaises(RuntimeError):
            main._invoke_gemini('p')

    def test_missing_finish_reason_with_text_is_accepted(self):
        self.gen().return_value = SimpleNamespace(text='ok', candidates=[])
        self.assertEqual(main._invoke_gemini('p'), 'ok')

    def test_text_property_raising_is_handled(self):
        class Blocked:
            candidates = [SimpleNamespace(finish_reason=types.FinishReason.PROHIBITED_CONTENT)]
            @property
            def text(self):
                raise ValueError('no parts')
        self.gen().return_value = Blocked()
        with self.assertRaises(RuntimeError) as ctx:
            main._invoke_gemini('p')
        self.assertIn('PROHIBITED_CONTENT', str(ctx.exception))


class AdviceSectionNeverRaisesTests(unittest.TestCase):
    ITEMS = [('閒置的靜態 IP 位址', object())]

    def setUp(self):
        for name, value in (('fetch_recommendations', self.ITEMS),
                            ('format_recommendations', ('listing', 0))):
            p = mock.patch.object(account_context, name, return_value=value)
            p.start()
            self.addCleanup(p.stop)

    def test_truncated_model_reply_becomes_a_warning(self):
        def cut(prompt):
            raise main.TruncatedOutputError('cut off at 32768')
        section, warn = account_context.build_advice_section('en', 'proj', cut)
        self.assertEqual(section, '')
        self.assertIn('account advice skipped', warn)
        self.assertIn('TruncatedOutputError', warn)

    def test_model_error_becomes_a_warning(self):
        def boom(prompt):
            raise RuntimeError('503 UNAVAILABLE')
        section, warn = account_context.build_advice_section('zh-TW', 'proj', boom)
        self.assertEqual(section, '')
        self.assertIn('503', warn)

    def test_success_returns_the_section(self):
        section, warn = account_context.build_advice_section('en', 'proj', lambda p: 'do X')
        self.assertIn('do X', section)
        self.assertEqual(warn, '')

    def test_recommender_error_is_still_a_warning(self):
        with mock.patch.object(account_context, 'fetch_recommendations',
                               side_effect=RuntimeError('403')):
            section, warn = account_context.build_advice_section('en', 'proj', lambda p: 'x')
        self.assertEqual(section, '')
        self.assertIn('403', warn)


class MainEndToEndTests(_GeminiCase):
    """Through main(): which failures cost the digest, and which only the advice section."""

    def _run_main(self):
        features = dict(main.CONFIG['FEATURES'], ACCOUNT_ADVICE=True, SAVE_TO_GCS=False,
                        SEND_EMAIL=True, POST_TO_LINKEDIN=False, POST_TO_WEBHOOK=False)
        with mock.patch.dict(main.CONFIG, {'FEATURES': features, 'DIGEST_LANGUAGE': 'en',
                                           'GCP_PROJECT_ID': 'proj'}), \
             mock.patch.object(main, 'fetch_gcp_release_notes',
                               return_value=[{'title': 't', 'summary': 's', 'link': 'l'}]), \
             mock.patch.object(main, 'fetch_gcp_blog_posts', return_value=[]), \
             mock.patch.object(account_context, 'fetch_recommendations',
                               return_value=AdviceSectionNeverRaisesTests.ITEMS), \
             mock.patch.object(account_context, 'format_recommendations',
                               return_value=('listing', 0)), \
             mock.patch.object(main, 'send_email') as send_email, \
             mock.patch.object(main, '_send_error_email') as send_error:
            error = None
            try:
                main.main()
            except Exception as e:                              # noqa: BLE001
                error = e
        return send_email, send_error, error

    def test_truncated_advice_drops_section_and_digest_still_goes_out(self):
        self.gen().side_effect = [
            _resp('# digest'),                                  # digest
            _resp('advice part', 'MAX_TOKENS'),                 # advice
            _resp('advice more', 'MAX_TOKENS'),                 # advice retry
        ]
        send_email, send_error, error = self._run_main()
        self.assertIsNone(error)
        send_email.assert_called_once()
        self.assertEqual(send_email.call_args.args[0], '# digest')
        send_error.assert_not_called()
        self.assertIn('WARNING: account advice skipped', self.logged())

    def test_truncated_digest_is_not_mailed(self):
        self.gen().side_effect = [_resp('# dig', 'MAX_TOKENS'), _resp('# dige', 'MAX_TOKENS')]
        send_email, send_error, error = self._run_main()
        self.assertIsInstance(error, main.TruncatedOutputError)
        send_email.assert_not_called()
        send_error.assert_called_once()
        self.assertIn('cut off', send_error.call_args.args[0])

    def test_clean_run_appends_advice(self):
        self.gen().side_effect = [_resp('# digest'), _resp('do X')]
        send_email, send_error, error = self._run_main()
        self.assertIsNone(error)
        self.assertIn('do X', send_email.call_args.args[0])
        send_error.assert_not_called()


if __name__ == '__main__':
    unittest.main()
