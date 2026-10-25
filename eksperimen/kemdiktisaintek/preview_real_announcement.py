"""Send one explicitly labelled preview of a fixed official announcement."""
from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
import subprocess
import sys

import requests


REPOSITORY = 'Galih-Hermawan-Unikom/bima-dikti-news'
FEATURE_REF = 'refs/heads/feat/kemdiktisaintek-telegram-stage3'
CONFIRMATION = 'SEND_ONE_REAL_ARTICLE_PREVIEW'
ANNOUNCEMENT_UUID = 'b63cc6de-f84c-4737-b7d9-9f47f6deb583'
EXPECTED_TITLE = 'Pendaftaran Beasiswa Indonesia-Austria Scholarship Programme (IASP) Tahun 2027'
LIST_URL = 'https://kemdiktisaintek.go.id/api/announcement?page=1&size=10&search='
LABEL = 'UJI TAMPILAN — BUKAN PENGUMUMAN BARU'


class PreviewError(RuntimeError):
    pass


def _append_summary(text):
    path = os.environ.get('GITHUB_STEP_SUMMARY')
    if path:
        with open(path, 'a', encoding='utf-8') as output:
            output.write(text.rstrip() + '\n')


def _require_context(env):
    if (env.get('GITHUB_REPOSITORY') != REPOSITORY
            or env.get('GITHUB_REF') != FEATURE_REF
            or env.get('KEMDIKTISAINTEK_PREVIEW_CONFIRMATION') != CONFIRMATION
            or env.get('KEMDIKTISAINTEK_PREVIEW_UUID') != ANNOUNCEMENT_UUID
            or env.get('KEMDIKTISAINTEK_REVIEWED_CODE_SHA') != env.get('GITHUB_SHA')):
        raise PreviewError('PREVIEW_CONTEXT_OR_CONFIRMATION_INVALID')
    formatter_root = Path(env.get('KEMDIKTISAINTEK_FORMATTER_ROOT', '')).resolve()
    expected_formatter_sha = env.get('KEMDIKTISAINTEK_FORMATTER_SHA', '')
    actual = subprocess.run(['git', '-C', str(formatter_root), 'rev-parse', 'HEAD'],
                            check=True, capture_output=True, text=True).stdout.strip()
    if actual != expected_formatter_sha or len(actual) != 40:
        raise PreviewError('PRODUCTION_FORMATTER_CHECKOUT_MISMATCH')
    return formatter_root


def _article():
    source_root = Path(__file__).resolve().parent
    sys.path.insert(0, str(source_root))
    sys.path.insert(0, str(source_root.parents[2]))
    from kemdiktisaintek_parser import AnnouncementClient, parse_api_list, parse_detail

    client = AnnouncementClient()
    try:
        listing = parse_api_list(client.fetch(LIST_URL), LIST_URL)
        row = next((item for item in listing['items'] if item.get('id') == ANNOUNCEMENT_UUID), None)
        if row is None:
            raise PreviewError('APPROVED_ARTICLE_UUID_NOT_IN_CURRENT_LIST_PAGE')
        item = parse_detail(client.fetch(row['url']), row['url'], row)
    finally:
        client.session.close()
    if (item.get('id') != ANNOUNCEMENT_UUID or item.get('title') != EXPECTED_TITLE
            or item.get('publication_basis') != 'detail_datetime'
            or not item.get('published_at') or not item.get('body_text')
            or not item.get('url', '').startswith('https://kemdiktisaintek.go.id/announcement/article/')):
        raise PreviewError('APPROVED_ARTICLE_DETAIL_VALIDATION_FAILED')
    return item


def _payload(item, formatter_root):
    sys.path.insert(0, str(formatter_root))
    from kemdiktisaintek_prod_telegram import format_announcement
    from kemdiktisaintek_telegram import payload

    formatted = format_announcement(item)
    if not isinstance(formatted, dict) or not isinstance(formatted.get('text'), str):
        raise PreviewError('PRODUCTION_FORMATTER_RETURN_INVALID')
    return payload(LABEL + '\n\n' + formatted['text'])


def run(env=None, *, session=None):
    env = os.environ if env is None else env
    formatter_root = _require_context(env)
    item = _article()
    message = _payload(item, formatter_root)

    from kemdiktisaintek_telegram import Credentials, preflight

    http = session or requests.Session()
    http.trust_env = False
    credentials = Credentials.from_environment(env)
    proof = preflight(credentials, at=datetime.now(timezone.utc).isoformat(), get=http.get)
    proof.validate(credentials, at=datetime.now(timezone.utc).isoformat())
    if not (proof.identity and len(proof.identity.get('chat_sha256', '')) == 64):
        raise PreviewError('EXPERIMENT_DESTINATION_PROOF_INVALID')

    alias = 'EXPERIMENT-' + credentials.chat_sha256[:12]
    summary = (
        '## Preview pengumuman Kemdiktisaintek\n\n'
        f"- Judul: {item['title']}\n"
        f"- Publikasi: {item['published_at']}\n"
        f"- Kategori: {item.get('category') or 'Tidak tersedia'}\n"
        f"- Sumber: {item['url']}\n"
        f'- Tujuan: {alias}\n'
        '- Preflight: PASS (bot, kanal eksperimen, izin posting, allowlist, pemisahan produksi)\n'
        '- State berita: tidak dibaca atau ditulis\n\n'
        '````html\n' + message['text'] + '\n````\n'
    )
    print('PREVIEW_DESTINATION=' + alias)
    print('PREVIEW_PREFLIGHT=PASS')
    print('PREVIEW_ARTICLE_UUID=' + ANNOUNCEMENT_UUID)
    print('PREVIEW_MESSAGE_BEGIN')
    print(message['text'])
    print('PREVIEW_MESSAGE_END')
    _append_summary(summary)

    body = {**message, 'chat_id': credentials.chat_id}
    print('TELEGRAM_POST_ATTEMPTS=1')
    try:
        response = http.post(
            'https://api.telegram.org/bot' + credentials.token + '/sendMessage',
            json=body, timeout=(10, 20), allow_redirects=False)
    except Exception:
        print('PREVIEW_DELIVERY=AMBIGUOUS; no retry')
        _append_summary('- Hasil: AMBIGUOUS; tidak ada retry otomatis.\n')
        return 2

    try:
        result = response.json()
    except Exception:
        print('PREVIEW_DELIVERY=AMBIGUOUS; no retry')
        _append_summary('- Hasil: AMBIGUOUS; respons Telegram tidak dapat diverifikasi, tanpa retry.\n')
        return 2
    if response.status_code != 200:
        print('PREVIEW_DELIVERY=AMBIGUOUS; HTTP response; no retry')
        _append_summary('- Hasil: AMBIGUOUS; respons HTTP tidak dapat memastikan delivery, tanpa retry.\n')
        return 2
    if not isinstance(result, dict) or result.get('ok') is not True:
        print('PREVIEW_DELIVERY=REJECTED; no retry')
        _append_summary('- Hasil: ditolak Telegram; tanpa retry.\n')
        return 1
    receipt = result.get('result')
    if (not isinstance(receipt, dict) or type(receipt.get('message_id')) is not int
            or receipt['message_id'] <= 0
            or str(receipt.get('chat', {}).get('id')) != credentials.chat_id):
        print('PREVIEW_DELIVERY=AMBIGUOUS; receipt invalid; no retry')
        _append_summary('- Hasil: AMBIGUOUS; receipt tidak valid, tanpa retry.\n')
        return 2

    message_id = receipt['message_id']
    print('PREVIEW_DELIVERY=DELIVERED message_id=' + str(message_id))
    _append_summary(f'- Hasil: DELIVERED\n- Message ID: {message_id}\n')
    return 0


def main():
    try:
        return run()
    except Exception as exc:
        code = exc.args[0] if exc.args else 'PREVIEW_FAILED'
        if not isinstance(code, str) or len(code) > 120 or ' ' in code:
            code = 'PREVIEW_FAILED'
        print('PREVIEW_ABORTED=' + code)
        _append_summary('- Hasil: dibatalkan sebelum pengiriman (' + code + ').\n')
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
