import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from eksperimen.kemdiktisaintek import preview_real_announcement as preview


PDF = b'%PDF-preview-fixture'
PDF_NAME = preview.PDF_NAME


class Response:
    status_code = 200

    def __init__(self, body):
        self.body = body

    def json(self):
        return self.body


class Credentials:
    token = '123456:fixture'
    chat_id = '-1001234567890'
    chat_sha256 = 'a' * 64


class Proof:
    def validate(self, credentials, *, at):
        return None


class Session:
    def __init__(self, outcomes=()):
        self.outcomes = list(outcomes)
        self.posts = []
        self.trust_env = False

    def post(self, url, **kwargs):
        self.posts.append((url.rsplit('/', 1)[-1], kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return Response(outcome)

    def close(self):
        pass


def _plan():
    return {
        'article_id': preview.ARTICLE_ID,
        'article_url': preview.ARTICLE_URL,
        'title': preview.TITLE,
        'published_at': '2026-09-30T00:00:00+07:00',
        'category': 'Umum',
        'deadline_evidence': ['paling lambat 01 Maret 2027'],
        'formatter_sha': preview.FORMATTER_SHA,
        'message': {'text': '<b>UJI</b>', 'parse_mode': 'HTML', 'disable_web_page_preview': True},
        'destination_alias': 'EXPERIMENT-aaaaaaaaaaaa',
        'pdf_url': 'https://kemdiktisaintek.go.id/example.pdf',
        'pdf_filename': PDF_NAME,
        'pdf_size': len(PDF),
        'pdf_sha256': hashlib.sha256(PDF).hexdigest(),
    }


def _download(session, url, filename):
    handle = tempfile.NamedTemporaryFile(delete=False, suffix='.pdf')
    handle.write(PDF)
    handle.close()
    return Path(handle.name), filename


def _env(root, **extra):
    return {
        'GITHUB_REPOSITORY': preview.REPOSITORY,
        'GITHUB_REF': preview.FEATURE_REF,
        'GITHUB_SHA': '1' * 40,
        'KEMDIKTISAINTEK_REVIEWED_CODE_SHA': '1' * 40,
        'KEMDIKTISAINTEK_FORMATTER_SHA': preview.FORMATTER_SHA,
        'KEMDIKTISAINTEK_FORMATTER_ROOT': str(root),
        'KEMDIKTISAINTEK_EXPERIMENT_ENABLED': 'false',
        'KEMDIKTISAINTEK_EXPERIMENT_AUTO_SEND': 'false',
        'KEMDIKTISAINTEK_PREVIEW_CONFIRMATION': preview.CONFIRMATION,
        **extra,
    }


class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.plan = _plan()
        self.digest = 'b' * 64
        self.prepare = patch.object(preview, '_prepare', return_value=(
            self.plan, self.digest, Credentials(), Proof(), _download))
        self.prepare.start()
        self.git = patch.object(preview.subprocess, 'run', return_value=type(
            'Result', (), {'stdout': preview.FORMATTER_SHA + '\n'})())
        self.git.start()
        self.addCleanup(self.prepare.stop)
        self.addCleanup(self.git.stop)
        self.addCleanup(self.temp.cleanup)

    def test_plan_never_posts(self):
        session = Session()
        result = preview.run('PREVIEW_PLAN', _env(self.root), session=session)
        self.assertEqual(result['status'], 'READY_FOR_OPERATOR_APPROVAL')
        self.assertEqual(session.posts, [])

    def test_send_requires_explicit_confirmation(self):
        session = Session()
        env = _env(self.root)
        env.pop('KEMDIKTISAINTEK_PREVIEW_CONFIRMATION')
        with self.assertRaisesRegex(preview.PreviewError, 'EXPLICIT_PREVIEW_CONFIRMATION_REQUIRED'):
            preview.run('SEND_PREVIEW', env, session=session)
        self.assertEqual(session.posts, [])

    def test_automatic_delivery_switches_block_preview(self):
        session = Session()
        env = _env(self.root, KEMDIKTISAINTEK_EXPERIMENT_AUTO_SEND='true')
        with self.assertRaisesRegex(preview.PreviewError, 'AUTOMATIC_EXPERIMENT_DELIVERY_MUST_BE_OFF'):
            preview.run('PREVIEW_PLAN', env, session=session)
        self.assertEqual(session.posts, [])

    def test_plan_hash_mismatch_prevents_posts(self):
        session = Session()
        env = _env(self.root, KEMDIKTISAINTEK_PREVIEW_PLAN_SHA256='c' * 64)
        with self.assertRaisesRegex(preview.PreviewError, 'PREVIEW_PLAN_HASH_MISMATCH'):
            preview.run('SEND_PREVIEW', env, session=session)
        self.assertEqual(session.posts, [])

    def test_delivery_uses_one_message_then_one_document(self):
        session = Session([
            {'ok': True, 'result': {'message_id': 12, 'chat': {'id': Credentials.chat_id}}},
            {'ok': True, 'result': {'message_id': 13, 'chat': {'id': Credentials.chat_id},
                                    'document': {'file_id': 'fixture', 'file_name': PDF_NAME,
                                                 'file_size': len(PDF)}}},
        ])
        env = _env(self.root, KEMDIKTISAINTEK_PREVIEW_PLAN_SHA256=self.digest)
        result = preview.run('SEND_PREVIEW', env, session=session)
        self.assertEqual(result['status'], 'DELIVERED')
        self.assertEqual([entry[0] for entry in session.posts], ['sendMessage', 'sendDocument'])
        self.assertEqual(result['post_attempts'], 2)

    def test_ambiguous_message_stops_before_document_without_retry(self):
        session = Session([TimeoutError('timeout')])
        env = _env(self.root, KEMDIKTISAINTEK_PREVIEW_PLAN_SHA256=self.digest)
        result = preview.run('SEND_PREVIEW', env, session=session)
        self.assertEqual(result['status'], 'AMBIGUOUS')
        self.assertEqual([entry[0] for entry in session.posts], ['sendMessage'])

    def test_ambiguous_document_keeps_partial_receipt_without_retry(self):
        session = Session([
            {'ok': True, 'result': {'message_id': 12, 'chat': {'id': Credentials.chat_id}}},
            TimeoutError('timeout'),
        ])
        env = _env(self.root, KEMDIKTISAINTEK_PREVIEW_PLAN_SHA256=self.digest)
        result = preview.run('SEND_PREVIEW', env, session=session)
        self.assertEqual(result['status'], 'PARTIAL_AMBIGUOUS')
        self.assertEqual(result['sendMessage']['message_id'], 12)
        self.assertEqual([entry[0] for entry in session.posts], ['sendMessage', 'sendDocument'])


if __name__ == '__main__':
    unittest.main()
