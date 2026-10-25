"""Prepare or send one explicitly approved, state-isolated announcement preview."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

import requests

REPOSITORY = 'Galih-Hermawan-Unikom/bima-dikti-news'
FEATURE_REF = 'refs/heads/feat/kemdiktisaintek-telegram-stage3'
ARTICLE_URL = ('https://kemdiktisaintek.go.id/announcement/article/'
               'pendaftaran-beasiswa-indonesia-austria-scholarship-programme-iasp-tahun-2027')
ARTICLE_ID = 'b63cc6de-f84c-4737-b7d9-9f47f6deb583'
TITLE = 'Pendaftaran Beasiswa Indonesia-Austria Scholarship Programme (IASP) Tahun 2027'
PDF_NAME = '3414-Tawaran_Pendaftaran_Program_IASP_2027.pdf'
PDF_SIZE = 339392
PDF_SHA256 = '52fd6c2793fad926b4857775722605cc9168668d2cb94234cfff5c1e34e704a1'
FORMATTER_SHA = 'd022dab6c18c000cbff9b44c020ef498d580940f'
LABEL = 'UJI TAMPILAN — BUKAN PENGUMUMAN BARU'
CONFIRMATION = 'SEND_ONE_REAL_ARTICLE_PREVIEW'


class PreviewError(RuntimeError):
    pass


def _context(env, *, sending):
    if (env.get('GITHUB_REPOSITORY') != REPOSITORY
            or env.get('GITHUB_REF') != FEATURE_REF
            or not re.fullmatch(r'[a-f0-9]{40}', env.get('GITHUB_SHA', ''))
            or env.get('KEMDIKTISAINTEK_REVIEWED_CODE_SHA') != env.get('GITHUB_SHA')
            or env.get('KEMDIKTISAINTEK_FORMATTER_SHA') != FORMATTER_SHA):
        raise PreviewError('PREVIEW_CONTEXT_OR_PIN_INVALID')
    if sending and env.get('KEMDIKTISAINTEK_PREVIEW_CONFIRMATION') != CONFIRMATION:
        raise PreviewError('EXPLICIT_PREVIEW_CONFIRMATION_REQUIRED')
    root = Path(env.get('KEMDIKTISAINTEK_FORMATTER_ROOT', '')).resolve()
    if not root.is_dir():
        raise PreviewError('PINNED_FORMATTER_CHECKOUT_MISSING')
    actual = subprocess.run(['git', '-C', str(root), 'rev-parse', 'HEAD'],
                            check=True, capture_output=True, text=True).stdout.strip()
    if actual != FORMATTER_SHA:
        raise PreviewError('PINNED_FORMATTER_CHECKOUT_MISMATCH')
    if (env.get('KEMDIKTISAINTEK_EXPERIMENT_ENABLED', 'false') == 'true'
            or env.get('KEMDIKTISAINTEK_EXPERIMENT_AUTO_SEND', 'false') == 'true'):
        raise PreviewError('AUTOMATIC_EXPERIMENT_DELIVERY_MUST_BE_OFF')
    return root


def _modules(formatter_root):
    source_root = Path(__file__).resolve().parent
    sys.path.insert(0, str(source_root))
    sys.path.insert(0, str(formatter_root))
    from kemdiktisaintek_parser import AnnouncementClient, parse_detail
    from kemdiktisaintek_prod_telegram import (
        download_pdf, format_announcement, safe_pdf_url,
    )
    from kemdiktisaintek_telegram import Credentials, payload, preflight
    return (AnnouncementClient, parse_detail, download_pdf, format_announcement,
            safe_pdf_url, Credentials, payload, preflight)


def _article(client_type, parse_detail):
    client = client_type()
    try:
        item = parse_detail(client.fetch(ARTICLE_URL), ARTICLE_URL)
    finally:
        client.session.close()
    if (item.get('id') != ARTICLE_ID or item.get('title') != TITLE
            or item.get('url') != ARTICLE_URL
            or item.get('published_at') != '2026-09-30T00:00:00+07:00'
            or item.get('category') != 'Umum'
            or 'paling lambat 01 Maret 2027' not in item.get('deadline_evidence', [])):
        raise PreviewError('FIXED_ARTICLE_IDENTITY_OR_METADATA_MISMATCH')
    attachments = item.get('attachments', [])
    if (len(attachments) != 1 or attachments[0].get('kind') != 'pdf'
            or attachments[0].get('url', '').rsplit('/', 1)[-1] != PDF_NAME):
        raise PreviewError('FIXED_OFFICIAL_PDF_MISMATCH')
    return item, attachments[0]


def _prepare(env, http):
    formatter_root = _context(env, sending=False)
    (client_type, parse_detail, download_pdf, format_announcement, safe_pdf_url,
     credentials_type, make_payload, preflight) = _modules(formatter_root)
    item, attachment = _article(client_type, parse_detail)
    url = attachment['url']
    if not safe_pdf_url(url):
        raise PreviewError('OFFICIAL_PDF_URL_NOT_ALLOWLISTED')
    formatted = format_announcement(item)
    message = make_payload(LABEL + '\n\n' + formatted['text'])
    credentials = credentials_type.from_environment(env)
    proof = preflight(credentials, at=datetime.now(timezone.utc).isoformat(), get=http.get)
    proof.validate(credentials, at=datetime.now(timezone.utc).isoformat())
    if not proof.identity or len(proof.identity.get('chat_sha256', '')) != 64:
        raise PreviewError('EXPERIMENT_DESTINATION_PROOF_INVALID')
    path = None
    try:
        path, filename = download_pdf(http, url, PDF_NAME)
        raw = path.read_bytes()
        if (filename != PDF_NAME or len(raw) != PDF_SIZE
                or hashlib.sha256(raw).hexdigest() != PDF_SHA256
                or not raw.startswith(b'%PDF-')):
            raise PreviewError('OFFICIAL_PDF_VALIDATION_FAILED')
    finally:
        if path is not None:
            path.unlink(missing_ok=True)
    plan = {
        'article_id': item['id'], 'article_url': item['url'], 'title': item['title'],
        'published_at': item['published_at'], 'category': item['category'],
        'deadline_evidence': item['deadline_evidence'], 'formatter_sha': FORMATTER_SHA,
        'message': message, 'destination_alias': 'EXPERIMENT-' + proof.identity['chat_sha256'][:12],
        'pdf_url': url, 'pdf_filename': filename, 'pdf_size': len(raw),
        'pdf_sha256': hashlib.sha256(raw).hexdigest(),
    }
    digest = hashlib.sha256(json.dumps(plan, ensure_ascii=False, sort_keys=True,
                                      separators=(',', ':')).encode()).hexdigest()
    return plan, digest, credentials, proof, download_pdf


def _summary(plan, digest, *, plan_only):
    path = os.environ.get('GITHUB_STEP_SUMMARY')
    content = ('## Preview Kemdiktisaintek — belum dikirim\n\n'
               f"- Tujuan: `{plan['destination_alias']}`\n"
               f"- Judul: {plan['title']}\n"
               f"- Tanggal: {plan['published_at']}\n"
               f"- Kategori: {plan['category']}\n"
               f"- PDF: `{plan['pdf_filename']}` ({plan['pdf_size']} bytes)\n"
               f"- SHA-256 rencana: `{digest}`\n"
               '- Preflight: PASS; state produksi tidak dibaca/ditulis\n\n'
               '```html\n' + plan['message']['text'] + '\n```\n')
    print('PREVIEW_DESTINATION=' + plan['destination_alias'])
    print('PREVIEW_PREFLIGHT=PASS')
    print('PREVIEW_FORMATTER_SHA=' + FORMATTER_SHA)
    print('PREVIEW_PLAN_SHA256=' + digest)
    print('PREVIEW_PDF=' + plan['pdf_filename'] + ' bytes=' + str(plan['pdf_size'])
          + ' signature=%PDF- sha256=' + plan['pdf_sha256'])
    print('PREVIEW_MESSAGE_BEGIN\n' + plan['message']['text'] + '\nPREVIEW_MESSAGE_END')
    if plan_only:
        print('PREVIEW_POST_ATTEMPTS=0')
    if path:
        with open(path, 'a', encoding='utf-8') as output:
            output.write(content)


def _send_once(http, method, credentials, *, message=None, pdf_path=None,
               filename=None, expected_size=None):
    url = 'https://api.telegram.org/bot' + credentials.token + '/' + method
    try:
        if method == 'sendMessage':
            response = http.post(url, json={**message, 'chat_id': credentials.chat_id},
                                 timeout=(10, 20), allow_redirects=False)
        else:
            with pdf_path.open('rb') as document:
                response = http.post(url, data={'chat_id': credentials.chat_id},
                                     files={'document': (filename, document, 'application/pdf')},
                                     timeout=(10, 30), allow_redirects=False)
    except Exception:
        return None, 'AMBIGUOUS'
    try:
        body = response.json()
    except Exception:
        return None, 'AMBIGUOUS'
    if response.status_code != 200:
        return None, 'AMBIGUOUS'
    if not isinstance(body, dict) or body.get('ok') is not True:
        return None, 'REJECTED'
    receipt = body.get('result')
    if (not isinstance(receipt, dict) or type(receipt.get('message_id')) is not int
            or receipt['message_id'] <= 0
            or str(receipt.get('chat', {}).get('id')) != credentials.chat_id):
        return None, 'AMBIGUOUS'
    result = {'message_id': receipt['message_id'], 'method': method,
              'destination_alias': 'EXPERIMENT-' + credentials.chat_sha256[:12]}
    if method == 'sendDocument':
        document = receipt.get('document')
        if (not isinstance(document, dict) or not isinstance(document.get('file_id'), str)
                or document.get('file_name') != filename
                or document.get('file_size') != expected_size):
            return None, 'AMBIGUOUS'
        result['filename'] = filename
    return result, 'DELIVERED'


def run(mode, env=None, *, session=None):
    env = os.environ if env is None else env
    if mode not in {'PREVIEW_PLAN', 'SEND_PREVIEW'}:
        raise PreviewError('INVALID_PREVIEW_MODE')
    _context(env, sending=(mode == 'SEND_PREVIEW'))
    http = session or requests.Session()
    if session is None:
        http.trust_env = False
    try:
        plan, digest, credentials, proof, download_pdf = _prepare(env, http)
        _summary(plan, digest, plan_only=(mode == 'PREVIEW_PLAN'))
        if mode == 'PREVIEW_PLAN':
            return {'status': 'READY_FOR_OPERATOR_APPROVAL', 'plan_sha256': digest,
                    'destination_alias': plan['destination_alias'], 'post_attempts': 0}
        if env.get('KEMDIKTISAINTEK_PREVIEW_PLAN_SHA256') != digest:
            raise PreviewError('PREVIEW_PLAN_HASH_MISMATCH')
        proof.validate(credentials, at=datetime.now(timezone.utc).isoformat())
        path = None
        try:
            path, filename = download_pdf(http, plan['pdf_url'], plan['pdf_filename'])
            raw = path.read_bytes()
            if (filename != plan['pdf_filename'] or len(raw) != plan['pdf_size']
                    or hashlib.sha256(raw).hexdigest() != plan['pdf_sha256']
                    or not raw.startswith(b'%PDF-')):
                raise PreviewError('PDF_CHANGED_AFTER_PREVIEW')
            first, status = _send_once(http, 'sendMessage', credentials, message=plan['message'])
            print('SENDMESSAGE_STATUS=' + status + ((' message_id=' + str(first['message_id'])) if first else ''))
            if status != 'DELIVERED':
                return {'status': status, 'post_attempts': 1, 'sendMessage': status}
            second, status = _send_once(http, 'sendDocument', credentials,
                                        pdf_path=path, filename=filename,
                                        expected_size=plan['pdf_size'])
            print('SENDDOCUMENT_STATUS=' + status + ((' message_id=' + str(second['message_id'])) if second else ''))
            return {'status': 'DELIVERED' if status == 'DELIVERED' else 'PARTIAL_' + status,
                    'post_attempts': 2, 'sendMessage': first, 'sendDocument': second}
        finally:
            if path is not None:
                path.unlink(missing_ok=True)
    finally:
        if session is None:
            http.close()


def main():
    try:
        result = run(os.environ.get('KEMDIKTISAINTEK_PREVIEW_MODE', ''), os.environ)
        print(json.dumps(result, ensure_ascii=True, sort_keys=True))
        return 0 if result.get('status') in {'READY_FOR_OPERATOR_APPROVAL', 'DELIVERED'} else 1
    except Exception as exc:
        code = exc.args[0] if exc.args else 'PREVIEW_FAILED'
        if not isinstance(code, str) or not re.fullmatch(r'[A-Z0-9_]{1,100}', code):
            code = 'PREVIEW_FAILED'
        print('PREVIEW_ABORTED=' + code)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
