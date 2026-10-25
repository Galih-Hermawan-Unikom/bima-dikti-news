from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

from eksperimen.telegram_preview import multi_preview as multi


class MemoryLedger:
    def __init__(self, fail_on=None):
        self.state, self.sha, self.saves = None, None, 0
        self.fail_on = fail_on

    def load(self):
        return deepcopy(self.state), self.sha

    def save(self, state, sha):
        self.saves += 1
        if self.saves == self.fail_on or sha != self.sha:
            raise multi.PreviewError('CAS_CONFLICT')
        self.state = deepcopy(state)
        self.sha = str(self.saves)
        return self.sha


def plan(count=7):
    return {'destination_alias': 'EXPERIMENT-e73e971297b7',
            'operations': [{'id': str(index), 'method': 'sendMessage',
                            'payload': {'text': 'fixture'}} for index in range(count)]}


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        import sys
        sys.path.insert(0, str(multi.SOURCE_ROOT))
        import preview_real_announcement
        self.sender_module = preview_real_announcement
        self.credentials = SimpleNamespace(chat_sha256='a' * 64)
        self.proof = SimpleNamespace(validate=Mock())
        self.ledger = MemoryLedger()
        self.calls = []

    def sender(self, http, method, credentials, **kwargs):
        rows = self.ledger.state['operations']
        claimed = [row for row in rows if row['status'] == 'SENDING']
        self.assertEqual(len(claimed), 1)
        self.assertTrue(claimed[0]['attempt_id'])
        self.calls.append(claimed[0]['id'])
        return {'message_id': 100 + len(self.calls), 'method': method}, 'DELIVERED'

    def run_plan(self, value=None):
        with patch.object(self.sender_module, '_send_once', side_effect=self.sender):
            return multi.execute(value or plan(), 'b' * 64, self.credentials, self.proof,
                                 Mock(), {}, self.ledger)

    def test_seven_operations_claimed_before_post_and_receipted(self):
        result = self.run_plan()
        self.assertEqual(result['post_attempts'], 7)
        self.assertEqual(result['status'], 'DELIVERED')
        self.assertEqual(len(self.calls), 7)
        self.assertTrue(all(row['status'] == 'DELIVERED' and row['receipt']
                            for row in self.ledger.state['operations']))

    def test_ledger_creation_cas_conflict_prevents_post(self):
        self.ledger.fail_on = 1
        with self.assertRaises(multi.PreviewError):
            self.run_plan()
        self.assertEqual(self.calls, [])

    def test_claim_cas_conflict_prevents_post(self):
        self.ledger.fail_on = 2
        with self.assertRaises(multi.PreviewError):
            self.run_plan()
        self.assertEqual(self.calls, [])

    def test_receipt_cas_failure_preserves_sending_and_no_replay(self):
        self.ledger.fail_on = 3
        with self.assertRaisesRegex(multi.PreviewError, 'RECEIPT_CHECKPOINT_FAILED'):
            self.run_plan()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.ledger.state['operations'][0]['status'], 'SENDING')
        with self.assertRaisesRegex(multi.PreviewError, 'NO_REPLAY'):
            self.run_plan()
        self.assertEqual(len(self.calls), 1)

    def test_completed_plan_never_replayed_after_restart(self):
        self.run_plan()
        with self.assertRaisesRegex(multi.PreviewError, 'NO_REPLAY'):
            self.run_plan()
        self.assertEqual(len(self.calls), 7)

    def test_reserved_plan_from_another_workflow_blocks_concurrent_execution(self):
        self.ledger.state = {'operations': [{'status': 'RESERVED'}]}
        with self.assertRaisesRegex(multi.PreviewError, 'NO_REPLAY'):
            self.run_plan()
        self.assertEqual(self.calls, [])

    def test_timeout_stops_sequence_without_retry(self):
        with patch.object(self.sender_module, '_send_once', return_value=(None, 'AMBIGUOUS')) as sender:
            result = multi.execute(plan(), 'b' * 64, self.credentials, self.proof, Mock(), {}, self.ledger)
        self.assertEqual(result['post_attempts'], 1)
        self.assertEqual(sender.call_count, 1)
        self.assertEqual(self.ledger.state['operations'][0]['status'], 'AMBIGUOUS')
        self.assertTrue(all(row['status'] == 'RESERVED' for row in self.ledger.state['operations'][1:]))

    def test_more_than_eight_posts_rejected_before_checkpoint(self):
        with self.assertRaisesRegex(multi.PreviewError, 'MAXIMUM_EIGHT_POSTS'):
            self.run_plan(plan(9))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.ledger.saves, 0)

    def test_document_operation_has_separate_claim_and_receipt(self):
        value = plan(1)
        value['operations'].append({'id': 'D-pdf-1', 'method': 'sendDocument',
                                    'document': {'filename': 'fixture.pdf', 'size': 123}})
        with patch.object(self.sender_module, '_send_once', side_effect=self.sender) as sender:
            result = multi.execute(value, 'b' * 64, self.credentials, self.proof,
                                   Mock(), {'D-pdf-1': Path('fixture.pdf')}, self.ledger)
        self.assertEqual(result['post_attempts'], 2)
        self.assertEqual(sender.call_args_list[1].args[1], 'sendDocument')
        self.assertEqual(sender.call_args_list[1].kwargs['expected_size'], 123)
        self.assertEqual(len(self.ledger.state['operations']), 2)


class GuardTests(unittest.TestCase):
    def env(self, **changes):
        return {'GITHUB_REPOSITORY': multi.REPOSITORY, 'GITHUB_REF': multi.FEATURE_REF,
                'GITHUB_EVENT_NAME': 'workflow_dispatch', 'GITHUB_SHA': 'a' * 40,
                'KEMDIKTISAINTEK_REVIEWED_CODE_SHA': 'a' * 40,
                'KEMDIKTISAINTEK_APPROVED_CODE_SHA': 'a' * 40, **changes}

    def test_operator_confirmation_required(self):
        with self.assertRaisesRegex(multi.PreviewError, 'OPERATOR_PLAN_APPROVAL_REQUIRED'):
            multi.workflow_context(self.env(), True)

    def test_plan_mode_allows_switches_off(self):
        multi.workflow_context(self.env(), False)

    def test_automatic_send_on_blocks_preview(self):
        with self.assertRaisesRegex(multi.PreviewError, 'AUTOMATIC_DELIVERY_MUST_REMAIN_OFF'):
            multi.workflow_context(self.env(KEMDIKTISAINTEK_EXPERIMENT_AUTO_SEND='true'), False)

    def test_sha_mismatch_blocks(self):
        with self.assertRaisesRegex(multi.PreviewError, 'SHA_GATE_FAILED'):
            multi.workflow_context(self.env(KEMDIKTISAINTEK_APPROVED_CODE_SHA='b' * 40), False)

    def test_master_ref_blocks(self):
        with self.assertRaises(multi.PreviewError):
            multi.workflow_context(self.env(GITHUB_REF='refs/heads/master'), False)

    def test_approved_hash_mismatch_blocks_before_network(self):
        env = self.env(KEMDIKTISAINTEK_MULTI_PREVIEW_CONFIRMATION=multi.CONFIRMATION,
                       KEMDIKTISAINTEK_MULTI_PLAN_SHA256='0' * 64)
        with patch.object(multi, 'verify_sources') as network:
            with self.assertRaisesRegex(multi.PreviewError, 'APPROVED_PLAN_HASH_MISMATCH'):
                multi.run('SEND_MULTI_PREVIEW', env)
        network.assert_not_called()

    def test_preparation_never_posts_or_constructs_ledger(self):
        import sys
        sys.path.insert(0, str(multi.SOURCE_ROOT))
        import kemdiktisaintek_telegram as telegram
        http = MagicMock()
        http.__enter__.return_value = http
        credentials = SimpleNamespace(chat_sha256='e73e971297b7' + '0' * 52)
        with patch.object(multi, 'validate_plan'), patch.object(multi, 'verify_sources', return_value=[]), \
                patch.object(multi, 'verify_pdfs', return_value={}), \
                patch.object(multi.requests, 'Session', return_value=http), \
                patch.object(telegram.Credentials, 'from_environment', return_value=credentials), \
                patch.object(telegram, 'preflight'), patch.object(multi, 'Ledger') as ledger:
            result = multi.run('MULTI_PREVIEW_PLAN', self.env())
        self.assertEqual(result['post_attempts'], 0)
        http.post.assert_not_called()
        http.put.assert_not_called()
        ledger.assert_not_called()

    def test_destination_mismatch_prevents_post_and_ledger(self):
        import sys
        sys.path.insert(0, str(multi.SOURCE_ROOT))
        import kemdiktisaintek_telegram as telegram
        http = MagicMock()
        http.__enter__.return_value = http
        credentials = SimpleNamespace(chat_sha256='a' * 64)
        with patch.object(multi, 'validate_plan'), patch.object(multi, 'verify_sources', return_value=[]), \
                patch.object(multi.requests, 'Session', return_value=http), \
                patch.object(telegram.Credentials, 'from_environment', return_value=credentials), \
                patch.object(telegram, 'preflight'), patch.object(multi, 'Ledger') as ledger:
            with self.assertRaisesRegex(multi.PreviewError, 'DESTINATION_MISMATCH_NO_POST'):
                multi.run('MULTI_PREVIEW_PLAN', self.env())
        http.post.assert_not_called()
        ledger.assert_not_called()

    def test_html_escaping_and_long_message(self):
        multi.validate_message(multi.LABEL + '\n\nSkenario: A\n\n<b>A &amp; B &lt;C&gt;</b>')
        for value in ['<script>x</script>', '<b>unclosed', '<b><i>x</b></i>', 'x' * 4100]:
            with self.assertRaises(multi.PreviewError):
                multi.validate_message(multi.LABEL + '\n\nSkenario: A\n\n' + value)

    def test_public_sources_exclude_telegram_and_credentials_in_url(self):
        for url in ['https://api.telegram.org/bot/whatever', 'http://kemdiktisaintek.go.id/a',
                    'https://user@kemdiktisaintek.go.id/a', 'https://127.0.0.1/a']:
            with self.assertRaises(multi.PreviewError):
                multi.require_public_url(url)

    def test_frozen_plan_budget_and_source_data(self):
        envelope = json.loads(multi.PLAN_PATH.read_text(encoding='utf-8'))
        self.assertEqual(multi.digest(envelope['plan']), envelope['plan_sha256'])
        ops = envelope['plan']['operations']
        self.assertEqual([op['id'] for op in ops],
                         ['A-summary', 'B-summary', 'C-summary', 'D-summary', 'D-pdf-1', 'E-summary', 'E-pdf-1'])
        self.assertEqual(sum(op['method'] == 'sendMessage' for op in ops), 5)
        self.assertEqual(sum(op['method'] == 'sendDocument' for op in ops), 2)
        for op in ops:
            if op['method'] == 'sendMessage':
                multi.validate_message(op['payload']['text'])

    def test_ledger_isolated_branch_path_and_cas(self):
        http = Mock()
        ledger = multi.Ledger('mock-github-token', 'b' * 64, session=http)
        http.get.return_value = SimpleNamespace(status_code=404)
        self.assertEqual(ledger.load(), (None, None))
        self.assertEqual(http.get.call_args.kwargs['params'], {'ref': multi.STATE_BRANCH})
        self.assertIn('/contents/.bot-state/telegram-multi-preview/', ledger.url)
        http.put.return_value = SimpleNamespace(status_code=409)
        with self.assertRaisesRegex(multi.PreviewError, 'CAS_FAILED'):
            ledger.save({'schema_version': 1}, 'c' * 40)
        body = http.put.call_args.kwargs['json']
        self.assertEqual(body['branch'], 'kemdiktisaintek-experiment-state')
        self.assertEqual(body['sha'], 'c' * 40)
        self.assertEqual(http.put.call_count, 1)


class ArchivedProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.inputs = json.loads(multi.PLAN_PATH.read_text(encoding='utf-8'))['plan']['inputs']
        self.blobs = {}
        for key, filename in [('bima', 'bima_history_89.json'), ('youtube', 'youtube_history_125.json')]:
            relative = 'tests/fixtures/' + filename
            raw = (json.dumps(self.inputs[key], ensure_ascii=False, indent=2) + '\n').encode()
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
            self.blobs[relative] = raw
        inputs, evidence = multi.archived_inputs(self.root)
        self.plan = {'inputs': inputs, 'provenance': evidence}
        for row in evidence.values():
            raw = self.blobs[row['fixture']].replace(b'\n', b'\r\n')
            row['file_sha256'] = hashlib.sha256(raw).hexdigest()

    def pinned_blob(self, command):
        self.assertTrue(command[-1].startswith(multi.FORMATTER_SHA + ':'))
        return self.blobs[command[-1].split(':', 1)[1]]

    def verify(self):
        with patch.object(multi.subprocess, 'check_output', side_effect=self.pinned_blob):
            multi.verify_archived_provenance(self.plan, self.root)

    def test_windows_approved_crlf_hash_matches_linux_lf_checkout(self):
        before = deepcopy(self.plan)
        self.verify()
        self.assertEqual(self.plan, before)

    def test_native_git_lf_hash_also_verifies(self):
        for row in self.plan['provenance'].values():
            row['file_sha256'] = hashlib.sha256(self.blobs[row['fixture']]).hexdigest()
        self.verify()

    def test_arbitrary_checksum_is_not_accepted(self):
        self.plan['provenance']['bima']['file_sha256'] = 'f' * 64
        with self.assertRaisesRegex(multi.PreviewError, 'CHECKSUM_NOT_DERIVED_FROM_PINNED_BLOB'):
            self.verify()

    def test_non_newline_checkout_change_is_rejected(self):
        path = self.root / self.plan['provenance']['bima']['fixture']
        path.write_bytes(path.read_bytes() + b' ')
        with self.assertRaisesRegex(multi.PreviewError, 'CHECKOUT_DIFFERS_FROM_PINNED_BLOB'):
            self.verify()

    def test_changed_record_is_not_hidden_by_newline_handling(self):
        self.plan['inputs']['bima'][0]['title'] += ' altered'
        with self.assertRaisesRegex(multi.PreviewError, 'INPUT_OR_PROVENANCE_CHANGED'):
            self.verify()


class SourceTransportTests(unittest.TestCase):
    def test_bima_certificate_failure_uses_archive_without_disabling_tls_or_retry(self):
        error = multi.urllib.error.URLError(multi.ssl.SSLCertVerificationError(1, 'fixture'))
        with patch.object(multi.urllib.request, 'urlopen', side_effect=error) as get:
            warnings = multi.verify_bima_live_url()
        self.assertEqual(warnings, ['BIMA_LIVE_TLS_UNVERIFIED_USING_VERIFIED_PINNED_ARCHIVE'])
        get.assert_called_once_with('https://bima.kemdiktisaintek.go.id/pengumuman', timeout=25)

    def test_unexpected_network_failure_remains_blocked(self):
        with patch.object(multi.urllib.request, 'urlopen',
                          side_effect=multi.urllib.error.URLError(TimeoutError())):
            with self.assertRaisesRegex(multi.PreviewError, 'BIMA_SOURCE_URL_UNAVAILABLE'):
                multi.verify_bima_live_url()

    def test_http_access_denial_is_not_a_certificate_fallback(self):
        error = multi.urllib.error.HTTPError('https://bima.kemdiktisaintek.go.id/pengumuman',
                                             403, 'Forbidden', {}, None)
        with patch.object(multi.urllib.request, 'urlopen', side_effect=error):
            with self.assertRaisesRegex(multi.PreviewError, 'BIMA_SOURCE_URL_UNAVAILABLE'):
                multi.verify_bima_live_url()

    def test_archive_provenance_is_required_before_live_transport_fallback(self):
        with patch.object(multi, 'verify_archived_provenance',
                          side_effect=multi.PreviewError('ARCHIVE_INVALID')), \
                patch.object(multi, 'verify_bima_live_url') as live:
            with self.assertRaisesRegex(multi.PreviewError, 'ARCHIVE_INVALID'):
                multi.verify_sources({}, 'fixture-root', Mock())
        live.assert_not_called()

    def test_healthy_live_bima_url_has_no_warning(self):
        response = MagicMock()
        response.__enter__.return_value = SimpleNamespace(
            status=200, url='https://bima.kemdiktisaintek.go.id/pengumuman')
        with patch.object(multi.urllib.request, 'urlopen', return_value=response):
            self.assertEqual(multi.verify_bima_live_url(), [])

    def test_redirected_or_wrong_bima_url_is_rejected(self):
        response = MagicMock()
        response.__enter__.return_value = SimpleNamespace(status=200, url='https://example.org/')
        with patch.object(multi.urllib.request, 'urlopen', return_value=response):
            with self.assertRaisesRegex(multi.PreviewError, 'BIMA_SOURCE_URL_UNVERIFIED'):
                multi.verify_bima_live_url()


if __name__ == '__main__':
    unittest.main()
