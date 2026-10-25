"""Production-only credential mapping, official-channel preflight and formatter."""
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from html import escape
import hashlib
import re
import tempfile
from pathlib import Path
from urllib.parse import unquote, urlsplit
import unicodedata

import requests

from production_preflight import verify_production, PreflightError
from kemdiktisaintek_prod_state import StateError, timestamp, fingerprint
from kemdiktisaintek_prod_parser import normalize_url, WIB, MONTHS


@dataclass(frozen=True)
class Credentials:
    token: str = field(repr=False)
    chat_id: str = field(repr=False)
    chat_sha256: str
    bot_id: int

    @classmethod
    def from_environment(cls, env):
        token = env.get('TELEGRAM_BOT_TOKEN', '')
        chat = env.get('TELEGRAM_CHAT_ID', '')
        digest = env.get('PRODUCTION_TELEGRAM_CHAT_ID_SHA256', '')
        valid_chat = (chat == '@dikti_news' or
                      (re.fullmatch(r'-[0-9]{5,20}', chat) is not None
                       and hashlib.sha256(chat.encode()).hexdigest() == digest))
        if (not re.fullmatch(r'[1-9][0-9]*:[A-Za-z0-9_-]+', token)
                or not valid_chat or not re.fullmatch('[a-f0-9]{64}', digest)):
            raise StateError('PRODUCTION_CREDENTIALS_OR_ALLOWLIST_INVALID')
        return cls(token, chat, digest, int(token.split(':')[0]))

    def mapping(self):
        return {'TELEGRAM_BOT_TOKEN': self.token,
                'TELEGRAM_CHAT_ID': self.chat_id,
                'PRODUCTION_TELEGRAM_CHAT_ID_SHA256': self.chat_sha256}

    def identity(self):
        return {'chat_sha256': self.chat_sha256, 'bot_id': self.bot_id,
                'credential_sha256': hashlib.sha256(self.token.encode()).hexdigest()}


@dataclass(frozen=True)
class Proof:
    identity: dict
    checked_at: str

    def validate(self, credentials, *, at):
        if self.identity != credentials.identity() or not timedelta(0) <= timestamp(at) - timestamp(self.checked_at) <= timedelta(minutes=5):
            raise StateError('PREFLIGHT_MISSING_STALE_OR_DIFFERENT_DESTINATION')


def preflight(credentials, *, at, get):
    verify_production(credentials.mapping(), get=get)
    return Proof(credentials.identity(), at)


def switches(env):
    if (env.get('KEMDIKTISAINTEK_PRODUCTION_ENABLED') != 'true'
            or env.get('KEMDIKTISAINTEK_PRODUCTION_AUTO_SEND') != 'true'):
        raise StateError('PRODUCTION_SEND_SWITCH_OFF')


def length(value):
    return len(value.encode('utf-16-le')) // 2


def payload(text):
    if not isinstance(text, str) or not text.strip() or length(text) > 3900:
        raise StateError('TELEGRAM_PAYLOAD_TOO_LONG_OR_EMPTY')
    return {'text': text, 'parse_mode': 'HTML', 'disable_web_page_preview': True}


def _clip(value, maximum):
    output = ''
    for char in ' '.join((value or '').split()):
        if length(escape(output + char)) > maximum - 3:
            return output + '...'
        output += char
    return output


def format_announcement(item):
    title = item.get('title')
    if not isinstance(title, str) or not title.strip():
        raise StateError('ANNOUNCEMENT_TITLE_REQUIRED')
    rendered_date = None
    published = item.get('published_at')
    if isinstance(published, str):
        try:
            stamp = datetime.fromisoformat(published.replace('Z', '+00:00'))
            if stamp.tzinfo:
                stamp = stamp.astimezone(WIB)
                rendered_date = f'{stamp.day} {MONTHS[stamp.month-1].title()} {stamp.year}'
            elif len(published) == 10:
                stamp_date = date.fromisoformat(published)
                rendered_date = f'{stamp_date.day} {MONTHS[stamp_date.month-1].title()} {stamp_date.year}'
        except ValueError:
            pass
    message = '<b>PENGUMUMAN BARU KEMDIKTISAINTEK</b>'
    if rendered_date:
        message += '\n\n' + rendered_date
    title_marker = '<KEMDIKTISAINTEK_TITLE_MARKER>'
    while title_marker in title:
        title_marker += '_'
    message += '\n\n<b>1. ' + title_marker + '</b>'
    letter = item.get('letter_number')
    if isinstance(letter, str) and re.fullmatch(r'(?i)(?:nomor\s*:?\s*)?[A-Z0-9./_-]{4,80}', letter.strip()):
        message += '\n\n' + escape(letter.strip())
    evidence = item.get('deadline_evidence')
    if isinstance(evidence, list):
        safe_evidence = [v.strip() for v in evidence if isinstance(v, str) and v.strip()]
        if safe_evidence:
            message += '\n⏳ Tenggat: ' + escape(_clip('; '.join(safe_evidence), 500))
    docs = item.get('attachments') if isinstance(item.get('attachments'), list) else []
    docs = [d for d in docs if isinstance(d, dict) and isinstance(d.get('url'), str)]
    if docs:
        message += f'\n\nDokumen: {len(docs)} file'
        for doc in docs:
            label = (doc.get('label') or doc.get('filename') or 'Dokumen resmi').strip()
            try:
                url = normalize_url(doc['url'])
            except Exception:
                continue
            if doc.get('kind') == 'pdf':
                label = document_filename(url, label)
            shown = escape(_clip(label, 180))
            if doc.get('kind') != 'pdf' and length(url) <= 450:
                shown = f'<a href="{escape(url, quote=True)}">{shown}</a>'
            message += '\n' + shown
    source = normalize_url(item['url'], article=True)
    message += '\n\nSumber:\n' + escape(source)
    message += '\n\n━━━━━━━━━━━━━━━━━━━━\nBot Dikti News'
    if length(message.replace(title_marker, escape(title))) > 3900:
        available = 3900 - length(message.replace(title_marker, ''))
        if available < 4:
            raise StateError('TELEGRAM_FIXED_ANNOUNCEMENT_FIELDS_TOO_LONG')
        message = message.replace(title_marker, escape(_clip(title, available)))
    else:
        message = message.replace(title_marker, escape(title))
    return payload(message)


def format_test(test_id):
    if not re.fullmatch('[A-Za-z0-9_-]{8,64}', test_id):
        raise StateError('INVALID_TEST_ID')
    return payload('<b>PENGUJIAN — KEMDIKTISAINTEK EKSPERIMEN</b>\n\n'
                   'Pesan uji jalur preflight, durable claim, dan receipt. '
                   'Ini bukan pengumuman baru atau pengiriman arsip.\n\nID pengujian: ' + escape(test_id))


PDF_HOST = 'kemdiktisaintek.go.id'
PDF_PATH_PREFIX = '/api/file/humas-production/announcements/'
MAX_PDF_BYTES = 20 * 1024 * 1024


def safe_pdf_url(value):
    try:
        parts = urlsplit(value)
        expected_path = re.fullmatch(
            r'/api/file/humas-production/announcements/\d{4}/\d{1,2}/files/[0-9a-fA-F-]{36}/[^/]+\.pdf',
            parts.path, re.I)
        return (parts.scheme == 'https' and parts.hostname == PDF_HOST and parts.port in (None, 443)
                and not parts.username and not parts.password and bool(expected_path)
                and not parts.query and not parts.fragment and '%2f' not in parts.path.lower()
                and '\\' not in parts.path and '..' not in parts.path)
    except (TypeError, ValueError):
        return False


def safe_filename(value):
    name = unquote(Path(str(value or '')).name).replace('\\', '_')
    name = ''.join(char for char in name if not unicodedata.category(char).startswith('C'))
    name = re.sub(r'[\x00-\x1f<>:"/\\|?*]+', '_', name).strip(' ._')
    extension = name[-4:] if name.lower().endswith('.pdf') else '.pdf'
    stem = (name[:-4] if name.lower().endswith('.pdf') else name)[:176].rstrip(' .')
    return (stem or 'dokumen-resmi') + extension


def document_filename(url, label):
    label = str(label or '').strip()
    chosen = label if label.lower().endswith('.pdf') else unquote(urlsplit(url).path.rsplit('/', 1)[-1])
    return safe_filename(chosen)


def download_pdf(session, url, label):
    """Download one allowlisted PDF, bounded and without following redirects."""
    if not safe_pdf_url(url):
        raise StateError('PDF_DOWNLOAD_URL_NOT_ALLOWLISTED')
    response = None
    path = None
    try:
        response = session.get(url, timeout=(5, 20), allow_redirects=False, stream=True)
        if response.status_code != 200 or response.is_redirect:
            raise StateError('PDF_DOWNLOAD_HTTP_ERROR')
        if not safe_pdf_url(getattr(response, 'url', url)):
            raise StateError('PDF_DOWNLOAD_REDIRECT_REJECTED')
        content_type = response.headers.get('Content-Type', '').split(';', 1)[0].strip().lower()
        if content_type not in {'application/pdf', 'application/octet-stream'}:
            raise StateError('PDF_CONTENT_TYPE_INVALID')
        try:
            if int(response.headers.get('Content-Length', '0') or 0) > MAX_PDF_BYTES:
                raise StateError('PDF_TOO_LARGE')
        except ValueError:
            raise StateError('PDF_CONTENT_LENGTH_INVALID') from None
        handle = tempfile.NamedTemporaryFile(prefix='kemdiktisaintek-', suffix='.pdf', delete=False)
        path = Path(handle.name)
        total = 0
        prefix = b''
        try:
            for chunk in response.iter_content(64 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_PDF_BYTES:
                    raise StateError('PDF_TOO_LARGE')
                if len(prefix) < 5:
                    prefix += chunk[:5-len(prefix)]
                handle.write(chunk)
            handle.flush()
        finally:
            handle.close()
        if prefix != b'%PDF-' or total == 0:
            raise StateError('PDF_SIGNATURE_INVALID')
        return path, safe_filename(label)
    except Exception:
        if path is not None:
            path.unlink(missing_ok=True)
        raise
    finally:
        if response is not None:
            response.close()


class TelegramGateway:
    def __init__(self, credentials, *, session=None):
        self.credentials = credentials
        self.session = session or requests.Session()
        if session is None:
            self.session.trust_env = False

    def inspect(self, *, at):
        def guarded_get(url, **kwargs):
            response = self.session.get(url, **kwargs)
            if url.endswith('/getMe'):
                try:
                    body = response.json()
                    bot = body['result']
                    if type(bot.get('id')) is not int or bot['id'] != self.credentials.bot_id:
                        raise PreflightError('PRODUCTION_BOT_ID_MISMATCH')
                except (KeyError, TypeError, ValueError):
                    raise PreflightError('PRODUCTION_BOT_ID_MISMATCH') from None
            return response
        verified = verify_production(self.credentials.mapping(), get=guarded_get)
        self.credentials = Credentials(self.credentials.token, verified['telegram_chat_id'],
            self.credentials.chat_sha256, self.credentials.bot_id)
        return Proof(self.credentials.identity(), at)

    def post_claimed(self, row, proof, *, at):
        proof.validate(self.credentials, at=at)
        if row['status'] != 'SENDING' or row['destination'] != proof.identity or fingerprint(row['payload']) != row['payload_hash']:
            raise StateError('INVALID_DURABLE_SEND_CLAIM')
        try:
            operation = row.get('operation', {'kind': 'SUMMARY'})
            method = 'sendDocument' if operation['kind'] == 'ATTACHMENT' else 'sendMessage'
            if method == 'sendMessage':
                payload(row['payload']['text'])
                response = self.session.post('https://api.telegram.org/bot' + self.credentials.token + '/' + method,
                    json={**row['payload'], 'chat_id': self.credentials.chat_id}, timeout=15, allow_redirects=False)
                temp_path = None
            else:
                document_operation = operation.get('payload') or operation
                try:
                    temp_path, filename = download_pdf(self.session, document_operation['url'], document_operation['filename'])
                except Exception:
                    return None, 'PREPOST_PDF_DOWNLOAD_FAILED'
                try:
                    with temp_path.open('rb') as document:
                        response = self.session.post('https://api.telegram.org/bot' + self.credentials.token + '/' + method,
                            data={'chat_id': self.credentials.chat_id, 'caption': document_operation.get('caption', '')},
                            files={'document': (filename, document, 'application/pdf')}, timeout=30, allow_redirects=False)
                finally:
                    temp_path.unlink(missing_ok=True)
            result = response.json()
            if response.status_code != 200:
                return None, 'HTTP_ERROR'
            if not isinstance(result, dict) or result.get('ok') is not True:
                return None, 'TELEGRAM_REJECTED'
            message = result.get('result', {})
            if (type(message.get('message_id')) is not int or message['message_id'] <= 0
                    or str(message.get('chat', {}).get('id')) != self.credentials.chat_id):
                return None, 'INVALID_OR_WRONG_DESTINATION_RECEIPT'
            receipt = {'http_status': 200, 'ok': True, 'method': method,
                       'message_id': message['message_id'],
                       'destination_sha256': self.credentials.chat_sha256}
            if method == 'sendDocument':
                document = message.get('document') or {}
                if not isinstance(document.get('file_id'), str) or not document['file_id']:
                    return None, 'INVALID_DOCUMENT_RECEIPT'
                receipt.update(file_id_sha256=hashlib.sha256(document['file_id'].encode()).hexdigest(),
                               file_name=safe_filename(document_operation['filename']))
            return receipt, None
        except requests.Timeout:
            return None, 'TIMEOUT'
        except Exception:
            return None, 'NETWORK_OR_INVALID_RESPONSE'
