import json
import hashlib
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests

from kemdiktisaintek_prod_state import fingerprint, StateError
from kemdiktisaintek_prod_telegram import (Credentials, Proof, TelegramGateway, download_pdf,
    format_announcement, format_test, payload, safe_filename, safe_pdf_url, MAX_PDF_BYTES)

ROOT = Path(__file__).resolve().parents[1]
IASP = json.loads((ROOT / 'tests/fixtures/kemdiktisaintek_iasp_2027.json').read_text(encoding='utf-8'))
CHAT = '-1001234567890'
TOKEN = '123456789:MOCK_TOKEN'


def test_compact_formatter_iasp_preview_excludes_body_and_category():
    msg = format_announcement(IASP)['text']
    assert '<b>📢 PENGUMUMAN BARU KEMDIKTISAINTEK</b>' in msg
    assert '30 September 2026' in msg
    assert '1. Pendaftaran Beasiswa Indonesia-Austria Scholarship Programme (IASP) Tahun 2027' in msg
    assert 'paling lambat 01 Maret 2027' in msg
    assert '3414-Tawaran_Pendaftaran_Program_IASP_2027.pdf' in msg
    assert 'Sumber:' in msg and IASP['url'] in msg
    assert 'Teks panjang artikel' not in msg and 'Umum' not in msg
    assert msg.endswith('━━━━━━━━━━━━━━━━━━━━\n<i>Bot Dikti News</i>')


def test_formatter_without_attachments_or_optional_fields():
    item = {**IASP, 'attachments': [], 'deadline_evidence': [], 'letter_number': None}
    text = format_announcement(item)['text']
    assert 'Dokumen:' not in text and 'Tenggat:' not in text and '3414/' not in text


def test_formatter_letter_number_and_html_escaping():
    item = {**IASP, 'title': 'Hibah <Riset> & Mitra', 'letter_number': '3414/DT.2.1/2026',
            'attachments': []}
    text = format_announcement(item)['text']
    assert 'Hibah &lt;Riset&gt; &amp; Mitra' in text
    assert '3414/DT.2.1/2026' in text
    assert '<Riset>' not in text


def test_multiple_documents_are_listed_and_counted():
    item = {**IASP, 'attachments': IASP['attachments'] + [
        {'kind': 'external_document', 'label': 'Panduan & Formulir',
         'url': 'https://drive.google.com/file/d/example/view'}]}
    text = format_announcement(item)['text']
    assert 'Dokumen: 2 file' in text
    assert 'Panduan &amp; Formulir' in text
    assert 'href="https://drive.google.com/file/d/example/view"' in text


def test_title_is_kept_full_until_telegram_limit_requires_clipping():
    ordinary = {**IASP, 'title': 'Judul lengkap ' * 80, 'attachments': []}
    rendered = format_announcement(ordinary)['text']
    assert ('Judul lengkap ' * 80).strip() in rendered
    long = {**ordinary, 'title': 'Judul panjang ' * 500}
    clipped = format_announcement(long)['text']
    assert len(clipped.encode('utf-16-le')) // 2 <= 3900
    assert '...' in clipped


def test_safe_filename_and_url_allowlist():
    assert safe_filename('../../A:<B>.PDF') == 'A_B_.PDF'
    assert safe_pdf_url(IASP['attachments'][0]['url'])
    for url in ('http://kemdiktisaintek.go.id/api/file/humas-production/a.pdf',
                'https://evil.example/api/file/humas-production/a.pdf',
                'https://kemdiktisaintek.go.id.evil/api/file/humas-production/a.pdf',
                'https://kemdiktisaintek.go.id/api/file/other/a.pdf',
                'https://kemdiktisaintek.go.id/api/file/humas-production/a.pdf?x=1#frag'):
        assert not safe_pdf_url(url)


class DownloadResponse:
    status_code = 200
    url = IASP['attachments'][0]['url']
    headers = {'Content-Type': 'application/pdf'}
    is_redirect = False

    def __init__(self, chunks=(b'%PDF-1.4\nmock',)):
        self.chunks = chunks
        self.closed = False

    def iter_content(self, _):
        yield from self.chunks

    def close(self):
        self.closed = True


class DownloadSession:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        return self.response


def test_download_pdf_is_bounded_and_redirects_disabled(tmp_path, monkeypatch):
    monkeypatch.setattr('kemdiktisaintek_prod_telegram.tempfile.gettempdir', lambda: str(tmp_path))
    response = DownloadResponse()
    session = DownloadSession(response)
    path, filename = download_pdf(session, IASP['attachments'][0]['url'], IASP['attachments'][0]['label'])
    assert path.read_bytes().startswith(b'%PDF-')
    assert filename == IASP['attachments'][0]['label']
    assert session.calls[0][1]['allow_redirects'] is False
    path.unlink()
    assert response.closed


@pytest.mark.parametrize('response,error,expected', [
    (DownloadResponse(chunks=(b'%PDF-' + b'x' * (MAX_PDF_BYTES + 1),)), None, 'PDF_TOO_LARGE'),
    (None, requests.Timeout(), 'NETWORK_OR_INVALID_RESPONSE'),
    (DownloadResponse(chunks=(b'not-pdf',)), None, 'PDF_SIGNATURE_INVALID'),
])
def test_download_failures_leave_no_temp_files(tmp_path, monkeypatch, response, error, expected):
    monkeypatch.setattr('kemdiktisaintek_prod_telegram.tempfile.gettempdir', lambda: str(tmp_path))
    session = DownloadSession(response, error)
    with pytest.raises((StateError, requests.Timeout)) as caught:
        download_pdf(session, IASP['attachments'][0]['url'], 'test.pdf')
    assert expected in str(caught.value) or isinstance(caught.value, requests.Timeout)
    assert list(tmp_path.iterdir()) == []


class TelegramResponse:
    status_code = 200

    def __init__(self, method, ok=True):
        self.method, self.ok = method, ok

    def json(self):
        result = {'message_id': 101 if self.method == 'sendMessage' else 102,
                  'chat': {'id': int(CHAT)}}
        if self.method == 'sendDocument':
            result['document'] = {'file_id': 'mock-file-id', 'file_name': 'IASP.pdf'}
        return {'ok': self.ok, 'result': result}


class TelegramSession(DownloadSession):
    def __init__(self):
        super().__init__(DownloadResponse())
        self.posts = []
        self.reject = False

    def post(self, url, **kwargs):
        method = url.rsplit('/', 1)[-1]
        self.posts.append((method, kwargs))
        return TelegramResponse(method, ok=not self.reject)


def test_mock_preview_sendmessage_and_senddocument_receipts(tmp_path, monkeypatch):
    monkeypatch.setattr('kemdiktisaintek_prod_telegram.tempfile.gettempdir', lambda: str(tmp_path))
    creds = Credentials(TOKEN, CHAT, hashlib.sha256(CHAT.encode()).hexdigest(), 123456789)
    proof = Proof(creds.identity(), '2026-10-25T08:30:00+00:00')
    session = TelegramSession()
    gateway = TelegramGateway(creds, session=session)
    summary = format_announcement(IASP)
    summary_row = {'status': 'SENDING', 'destination': proof.identity, 'payload': summary,
                   'payload_hash': fingerprint(summary), 'operation': {'kind': 'SUMMARY'}}
    summary_receipt, error = gateway.post_claimed(summary_row, proof, at=proof.checked_at)
    doc_meta = {'url': IASP['attachments'][0]['url'],
                'filename': IASP['attachments'][0]['label'], 'caption': ''}
    doc_row = {'status': 'SENDING', 'destination': proof.identity, 'payload': doc_meta,
               'payload_hash': fingerprint(doc_meta), 'operation': {'kind': 'ATTACHMENT', **doc_meta}}
    document_receipt, doc_error = gateway.post_claimed(doc_row, proof, at=proof.checked_at)
    assert error is doc_error is None
    assert summary_receipt['method'] == 'sendMessage' and summary_receipt['message_id'] == 101
    assert document_receipt['method'] == 'sendDocument' and document_receipt['message_id'] == 102
    assert len(session.posts) == 2
    assert [entry[0] for entry in session.posts] == ['sendMessage', 'sendDocument']
    assert list(tmp_path.iterdir()) == []


def test_attachment_http_error_never_posts_to_telegram():
    creds = Credentials(TOKEN, CHAT, hashlib.sha256(CHAT.encode()).hexdigest(), 123456789)
    proof = Proof(creds.identity(), '2026-10-25T08:30:00+00:00')
    session = TelegramSession()
    session.response.status_code = 503
    gateway = TelegramGateway(creds, session=session)
    meta = {'url': IASP['attachments'][0]['url'], 'filename': 'IASP.pdf', 'caption': ''}
    row = {'status': 'SENDING', 'destination': proof.identity, 'payload': meta,
           'payload_hash': fingerprint(meta), 'operation': {'kind': 'ATTACHMENT', **meta}}
    receipt, error = gateway.post_claimed(row, proof, at=proof.checked_at)
    assert receipt is None and error == 'PREPOST_PDF_DOWNLOAD_FAILED'
    assert session.posts == []


def test_telegram_document_rejection_is_reported_without_retry():
    creds = Credentials(TOKEN, CHAT, hashlib.sha256(CHAT.encode()).hexdigest(), 123456789)
    proof = Proof(creds.identity(), '2026-10-25T08:30:00+00:00')
    session = TelegramSession()
    session.reject = True
    gateway = TelegramGateway(creds, session=session)
    meta = {'url': IASP['attachments'][0]['url'], 'filename': 'IASP.pdf', 'caption': ''}
    row = {'status': 'SENDING', 'destination': proof.identity, 'payload': meta,
           'payload_hash': fingerprint(meta), 'operation': {'kind': 'ATTACHMENT', **meta}}
    receipt, error = gateway.post_claimed(row, proof, at=proof.checked_at)
    assert receipt is None and error == 'TELEGRAM_REJECTED'
    assert [method for method, _ in session.posts] == ['sendDocument']
