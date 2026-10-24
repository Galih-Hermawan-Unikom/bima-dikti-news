"""Experiment-only mapping and reused preflight, deterministic one-message formatter."""
from dataclasses import dataclass, field
from datetime import timedelta
from html import escape
import hashlib
import re
from pathlib import Path
import sys

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from youtube_experiment_preflight import verify_experiment_destination, PreflightError
from kemdiktisaintek_state import StateError, timestamp, fingerprint
from kemdiktisaintek_parser import normalize_url, WIB, MONTHS


@dataclass(frozen=True)
class Credentials:
    token: str = field(repr=False)
    chat_id: str = field(repr=False)
    chat_sha256: str
    bot_id: int

    @classmethod
    def from_environment(cls, env):
        token = env.get('YOUTUBE_EXPERIMENT_BOT_TOKEN', '')
        chat = env.get('YOUTUBE_EXPERIMENT_CHAT_ID', '')
        digest = env.get('YOUTUBE_EXPERIMENT_CHAT_ID_SHA256', '')
        if (not re.fullmatch(r'[1-9][0-9]*:[A-Za-z0-9_-]+', token)
                or not re.fullmatch(r'-[0-9]{5,20}', chat)
                or not re.fullmatch('[a-f0-9]{64}', digest)
                or hashlib.sha256(chat.encode()).hexdigest() != digest):
            raise StateError('EXPERIMENT_CREDENTIALS_MISSING_OR_INVALID')
        return cls(token, chat, digest, int(token.split(':')[0]))

    def mapping(self):
        return {'YOUTUBE_EXPERIMENT_BOT_TOKEN': self.token,
                'YOUTUBE_EXPERIMENT_CHAT_ID': self.chat_id,
                'YOUTUBE_EXPERIMENT_CHAT_ID_SHA256': self.chat_sha256}

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
    def guarded_get(url, **kwargs):
        response = get(url, **kwargs)
        if url.endswith('/getMe'):
            try:
                bot = response.json()['result']
                if type(bot.get('id')) is not int or bot['id'] != credentials.bot_id:
                    raise PreflightError('EXPERIMENT_BOT_MISMATCH')
            except (KeyError, TypeError, ValueError):
                raise PreflightError('EXPERIMENT_BOT_MISMATCH') from None
        return response
    verify_experiment_destination(credentials.mapping(), get=guarded_get)
    return Proof(credentials.identity(), at)


def switches(env):
    if (env.get('KEMDIKTISAINTEK_EXPERIMENT_ENABLED') != 'true'
            or env.get('KEMDIKTISAINTEK_EXPERIMENT_AUTO_SEND') != 'true'):
        raise StateError('EXPERIMENT_SEND_SWITCH_OFF')


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
    stamp = timestamp(item.get('published_at')).astimezone(WIB)
    date = f'{stamp.day} {MONTHS[stamp.month-1].title()} {stamp.year}, {stamp:%H:%M} WIB'
    message = '<b>PENGUMUMAN KEMDIKTISAINTEK</b>\n\n<b>' + escape(_clip(item['title'], 450)) + '</b>'
    message += '\n\nTanggal publikasi: ' + date + '\nKategori: ' + escape(_clip(item.get('category') or 'Tidak tersedia', 100))
    message += '\n\n<b>Ringkasan:</b>\n' + escape(_clip(item.get('body_text'), 1000))
    # Only parser-extracted explicit deadline evidence; no inference from event dates.
    if item.get('deadline_evidence'):
        message += '\n\n<b>Tenggat (bukti artikel):</b>\n' + escape(_clip('; '.join(item['deadline_evidence']), 260))
    source = normalize_url(item['url'], article=True)
    source_block = '\n\n<b>Sumber:</b>\n' + escape(source)
    documents = []
    for doc in item.get('attachments', [])[:3]:
        url = normalize_url(doc['url'])
        if length(url) > 450:
            continue
        kind = {'pdf': 'PDF', 'external_document': 'Google Drive/Dokumen eksternal'}.get(doc.get('kind'), 'Dokumen')
        line = f'{kind}: <a href="{escape(url, quote=True)}">{escape(_clip(doc.get("label") or kind, 90))}</a>'
        if length(message + '\n\n<b>Dokumen:</b>\n' + '\n'.join(documents + [line]) + source_block) <= 3850:
            documents.append(line)
    if documents:
        message += '\n\n<b>Dokumen:</b>\n' + '\n'.join(documents)
    if item.get('external_links'):
        link = item['external_links'][0]
        url = normalize_url(link['url'])
        if length(url) <= 300:
            addition = '\n\nTautan eksternal: <a href="' + escape(url, quote=True) + '">Lihat tautan artikel</a>'
            if length(message + addition + source_block) <= 3850:
                message += addition
    message += source_block
    return payload(message)


def format_test(test_id):
    if not re.fullmatch('[A-Za-z0-9_-]{8,64}', test_id):
        raise StateError('INVALID_TEST_ID')
    return payload('<b>PENGUJIAN — KEMDIKTISAINTEK EKSPERIMEN</b>\n\n'
                   'Pesan uji jalur preflight, durable claim, dan receipt. '
                   'Ini bukan pengumuman baru atau pengiriman arsip.\n\nID pengujian: ' + escape(test_id))


class TelegramGateway:
    def __init__(self, credentials, *, session=None):
        self.credentials = credentials
        self.session = session or requests.Session()
        if session is None:
            self.session.trust_env = False

    def inspect(self, *, at):
        return preflight(self.credentials, at=at, get=self.session.get)

    def post_claimed(self, row, proof, *, at):
        proof.validate(self.credentials, at=at)
        if row['status'] != 'SENDING' or row['destination'] != proof.identity or fingerprint(row['payload']) != row['payload_hash']:
            raise StateError('INVALID_DURABLE_SEND_CLAIM')
        payload(row['payload']['text'])
        body = {**row['payload'], 'chat_id': self.credentials.chat_id}
        try:
            response = self.session.post('https://api.telegram.org/bot' + self.credentials.token + '/sendMessage',
                                         json=body, timeout=15, allow_redirects=False)
            result = response.json()
            if response.status_code != 200:
                return None, 'HTTP_ERROR'
            if not isinstance(result, dict) or result.get('ok') is not True:
                return None, 'TELEGRAM_REJECTED'
            message = result.get('result', {})
            if (type(message.get('message_id')) is not int or message['message_id'] <= 0
                    or str(message.get('chat', {}).get('id')) != self.credentials.chat_id):
                return None, 'INVALID_OR_WRONG_DESTINATION_RECEIPT'
            return {'http_status': 200, 'ok': True, 'message_id': message['message_id'],
                    'destination_sha256': self.credentials.chat_sha256}, None
        except requests.Timeout:
            return None, 'TIMEOUT'
        except Exception:
            return None, 'NETWORK_OR_INVALID_RESPONSE'
