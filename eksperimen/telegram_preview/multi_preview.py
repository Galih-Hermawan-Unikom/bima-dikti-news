"""Pinned production formatting and isolated, operator-approved Telegram previews."""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timezone
import hashlib
from html import escape
from html.parser import HTMLParser
import importlib
import json
import os
from pathlib import Path
import re
import ssl
import subprocess
import sys
from urllib.parse import urlsplit
import urllib.request
import urllib.error
from unittest.mock import patch
from uuid import uuid4
from zoneinfo import ZoneInfo

import requests

REPOSITORY = 'Galih-Hermawan-Unikom/bima-dikti-news'
FORMATTER_SHA = 'd022dab6c18c000cbff9b44c020ef498d580940f'
FEATURE_REF = 'refs/heads/feat/kemdiktisaintek-telegram-stage3'
STATE_BRANCH = 'kemdiktisaintek-experiment-state'
LEDGER_PREFIX = '.bot-state/telegram-multi-preview/'
CONFIRMATION = 'SEND_MULTI_PREVIEW_APPROVED_PLAN'
LABEL = 'UJI TAMPILAN — BUKAN BERITA BARU'
PLAN_PATH = Path(__file__).with_name('multi_preview_plan.json')
SOURCE_ROOT = Path(__file__).resolve().parents[1] / 'kemdiktisaintek'
BIMA_LETTERS = ['1236/DST/C3/DT.06.01/2026', '1099/DST/C3/DT.06.01/2026']
VIDEO_IDS = ['QFqYYjg51RA', 'X_4PalxnlWk']
ARTICLE_SLUGS = [
    'pendaftaran-beasiswa-indonesia-austria-scholarship-programme-iasp-tahun-2027',
    'akpt-tahap-iii-2026-dibuka-di-itb-pemimpin-perguruan-tinggi-dibekali-strategi-transformasi-dan-inovasi-institusi',
]


class PreviewError(RuntimeError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


class TelegramHTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []

    def handle_starttag(self, tag, attrs):
        if tag not in {'b', 'i', 'a'} or (tag != 'a' and attrs):
            raise PreviewError('INVALID_TELEGRAM_HTML')
        if tag == 'a':
            if len(attrs) != 1 or attrs[0][0] != 'href':
                raise PreviewError('INVALID_TELEGRAM_LINK')
            require_public_url(attrs[0][1])
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack.pop() != tag:
            raise PreviewError('UNBALANCED_TELEGRAM_HTML')

    def handle_startendtag(self, tag, attrs):
        raise PreviewError('INVALID_TELEGRAM_HTML')


def require_public_url(url):
    p = urlsplit(url)
    if (p.scheme != 'https' or p.hostname not in {
            'bima.kemdiktisaintek.go.id', 'www.youtube.com', 'kemdiktisaintek.go.id'}
            or p.username or p.password or p.port not in (None, 443)):
        raise PreviewError('SOURCE_URL_NOT_ALLOWLISTED')


def validate_message(text):
    if not text.startswith(LABEL + '\n\nSkenario: ') or len(text.encode('utf-16-le')) // 2 > 4096:
        raise PreviewError('PREVIEW_LABEL_OR_LENGTH_INVALID')
    parser = TelegramHTML()
    parser.feed(text)
    parser.close()
    if parser.stack:
        raise PreviewError('UNBALANCED_TELEGRAM_HTML')


def formatter(root):
    root = Path(root).resolve()
    actual = subprocess.run(['git', '-C', str(root), 'rev-parse', 'HEAD'],
                            check=True, capture_output=True, text=True).stdout.strip()
    if actual != FORMATTER_SHA:
        raise PreviewError('FORMATTER_SHA_MISMATCH')
    tracked = ['notifier.py', 'kemdiktisaintek_prod_telegram.py', 'tests/fixtures/bima_history_89.json',
               'tests/fixtures/youtube_history_125.json']
    subprocess.run(['git', '-C', str(root), 'diff', '--exit-code', 'HEAD', '--', *tracked],
                   check=True, capture_output=True)
    sys.path.insert(0, str(root))
    sys.path.insert(0, str(SOURCE_ROOT))
    return importlib.import_module('notifier'), importlib.import_module('kemdiktisaintek_prod_telegram')


def archived_inputs(root):
    inputs, evidence = {}, {}
    for name, key, field, values in [
        ('bima_history_89.json', 'bima', 'surat', BIMA_LETTERS),
        ('youtube_history_125.json', 'youtube', 'video_id', VIDEO_IDS),
    ]:
        path = Path(root) / 'tests' / 'fixtures' / name
        raw = path.read_bytes()
        rows = json.loads(raw)
        selected = []
        for value in values:
            matches = [row for row in rows if row.get(field) == value]
            if len(matches) != 1:
                raise PreviewError('ARCHIVED_SOURCE_IDENTITY_CONFLICT')
            selected.append(matches[0])
        inputs[key] = selected
        evidence[key] = {'fixture': 'tests/fixtures/' + name, 'commit': FORMATTER_SHA,
                         'file_sha256': hashlib.sha256(raw).hexdigest(),
                         'basis': 'previously_captured_historical_record',
                         'record_sha256': [digest(row) for row in selected]}
    return inputs, evidence


def article_records():
    sys.path.insert(0, str(SOURCE_ROOT))
    from kemdiktisaintek_parser import AnnouncementClient, parse_detail
    client = AnnouncementClient()
    try:
        return [parse_detail(client.fetch(url), url) for url in
                ['https://kemdiktisaintek.go.id/announcement/article/' + slug for slug in ARTICLE_SLUGS]]
    finally:
        client.session.close()


def render(inputs, prepared_at, notifier_module, telegram_module):
    notifier = notifier_module.Notifier(config={'notification_method': 'console'})
    stamp = datetime.fromisoformat(prepared_at).astimezone(ZoneInfo('Asia/Jakarta'))
    groups = {'A': inputs['bima'], 'B': inputs['youtube'],
              'C': [inputs['bima'][0], inputs['youtube'][0]],
              'D': [inputs['kemdiktisaintek'][0]], 'E': [inputs['kemdiktisaintek'][1]]}
    operations, scenarios = [], []
    for scenario, items in groups.items():
        if scenario in 'ABC':
            with patch.object(notifier_module, 'now_wib', return_value=stamp):
                chunks = notifier._build_summary_chunks(items)
            if len(chunks) != 1:
                raise PreviewError('SCENARIO_REQUIRES_MULTIPLE_SUMMARIES')
            payload = {'text': chunks[0], 'parse_mode': 'HTML', 'disable_web_page_preview': True}
        else:
            payload = telegram_module.format_announcement(items[0])
        payload = {**payload, 'text': LABEL + '\n\nSkenario: ' + scenario + '\n\n' + payload['text']}
        validate_message(payload['text'])
        operations.append({'id': scenario + '-summary', 'scenario': scenario,
                           'method': 'sendMessage', 'payload': payload})
        for index, doc in enumerate(inputs.get('pdfs', {}).get(scenario, []), 1):
            operations.append({'id': scenario + '-pdf-' + str(index), 'scenario': scenario,
                               'method': 'sendDocument', 'document': doc})
        scenarios.append({'id': scenario, 'item_count': len(items),
                          'titles': [row['title'] for row in items],
                          'source_urls': [row['url'] for row in items],
                          'uploaded_pdf_count': len(inputs.get('pdfs', {}).get(scenario, [])),
                          'utf16_units': len(payload['text'].encode('utf-16-le')) // 2})
    return operations, scenarios


def verify_archived_provenance(plan, root):
    archived, evidence = archived_inputs(root)
    for key in ('bima', 'youtube'):
        reviewed = plan['provenance'][key]
        expected = dict(evidence[key], file_sha256=reviewed.get('file_sha256'))
        if plan['inputs'][key] != archived[key] or reviewed != expected:
            raise PreviewError('ARCHIVED_INPUT_OR_PROVENANCE_CHANGED')
        # Git checkouts may materialize this exact pinned text blob as LF or CRLF.
        # Verify the original approved checksum against those bytes, not a new capture.
        fixture = evidence[key]['fixture']
        blob = subprocess.check_output(['git', '-C', str(root), 'show', FORMATTER_SHA + ':' + fixture])
        lf = blob.replace(b'\r\n', b'\n')
        actual = (Path(root) / fixture).read_bytes()
        if actual.replace(b'\r\n', b'\n') != lf:
            raise PreviewError('ARCHIVED_CHECKOUT_DIFFERS_FROM_PINNED_BLOB')
        representations = {
            hashlib.sha256(blob).hexdigest(): 'pinned_git_blob',
            hashlib.sha256(lf.replace(b'\n', b'\r\n')).hexdigest(): 'CRLF_checkout_of_pinned_blob',
        }
        if reviewed.get('file_sha256') not in representations:
            raise PreviewError('ARCHIVED_CHECKSUM_NOT_DERIVED_FROM_PINNED_BLOB')
        print('ARCHIVED_PROVENANCE_PASS source=' + key + ' representation='
              + representations[reviewed['file_sha256']])


def verify_bima_live_url():
    # Live availability is supplementary to the pinned historical preview records.
    # Never disable TLS or use an unverified response to obtain source data.
    try:
        with urllib.request.urlopen('https://bima.kemdiktisaintek.go.id/pengumuman', timeout=25) as response:
            if response.status != 200 or response.url != 'https://bima.kemdiktisaintek.go.id/pengumuman':
                raise PreviewError('BIMA_SOURCE_URL_UNVERIFIED')
    except urllib.error.URLError as exc:
        if not isinstance(exc.reason, ssl.SSLCertVerificationError):
            raise PreviewError('BIMA_SOURCE_URL_UNAVAILABLE') from None
        warning = 'BIMA_LIVE_TLS_UNVERIFIED_USING_VERIFIED_PINNED_ARCHIVE'
        print('SOURCE_WARNING=' + warning)
        return [warning]
    return []


def verify_sources(plan, root, http):
    verify_archived_provenance(plan, root)  # Required even if live BIMA TLS is unavailable.
    warnings = verify_bima_live_url()
    for item in plan['inputs']['youtube']:
        require_public_url(item['url'])
        response = http.get('https://www.youtube.com/oembed',
                            params={'url': item['url'], 'format': 'json'}, timeout=20, allow_redirects=False)
        data = response.json()
        if (response.status_code != 200 or data.get('title') != item['title']
                or data.get('author_url') != 'https://www.youtube.com/@kemdiktisaintek'):
            raise PreviewError('YOUTUBE_TITLE_OR_CHANNEL_MISMATCH')
    current = article_records()
    if current != plan['inputs']['kemdiktisaintek']:
        raise PreviewError('OFFICIAL_ARTICLE_CHANGED_REBUILD_PLAN')
    return warnings


def validate_plan(plan, root):
    if (plan.get('schema_version') != 1 or plan.get('formatter_sha') != FORMATTER_SHA
            or plan.get('destination_alias') != 'EXPERIMENT-e73e971297b7'):
        raise PreviewError('PLAN_SCHEMA_OR_DESTINATION_INVALID')
    notifier_module, telegram_module = formatter(root)
    operations, scenarios = render(plan['inputs'], plan['prepared_at'], notifier_module, telegram_module)
    if plan['operations'] != operations or plan['scenarios'] != scenarios or not 1 <= len(operations) <= 8:
        raise PreviewError('PLAN_RENDER_OR_POST_BUDGET_MISMATCH')
    if len({op['id'] for op in operations}) != len(operations):
        raise PreviewError('DUPLICATE_OPERATION_ID')
    for scenario in plan['scenarios']:
        for url in scenario['source_urls']:
            require_public_url(url)
    for op in operations:
        if op['method'] == 'sendDocument' and not telegram_module.safe_pdf_url(op['document']['url']):
            raise PreviewError('PDF_URL_NOT_ALLOWLISTED')
    return telegram_module


def verify_pdfs(plan, telegram_module, http):
    paths = {}
    try:
        for op in plan['operations']:
            if op['method'] != 'sendDocument':
                continue
            doc = op['document']
            path, filename = telegram_module.download_pdf(http, doc['url'], doc['filename'])
            paths[op['id']] = path
            raw = path.read_bytes()
            if (filename != doc['filename'] or len(raw) != doc['size'] or raw[:5] != b'%PDF-'
                    or hashlib.sha256(raw).hexdigest() != doc['sha256']):
                raise PreviewError('PDF_HASH_SIZE_OR_SIGNATURE_MISMATCH')
        return paths
    except Exception:
        for path in paths.values():
            path.unlink(missing_ok=True)
        raise


class Ledger:
    """Separate per-plan file on existing experiment branch, never production state."""
    def __init__(self, token, plan_hash, *, session=None):
        if not token or not re.fullmatch('[a-f0-9]{64}', plan_hash):
            raise PreviewError('PREVIEW_LEDGER_CONFIGURATION_INVALID')
        self.plan_hash = plan_hash
        self.http = session or requests.Session()
        self.http.trust_env = False
        self.headers = {'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json'}
        self.url = ('https://api.github.com/repos/' + REPOSITORY + '/contents/'
                    + LEDGER_PREFIX + plan_hash + '.json')

    def load(self):
        response = self.http.get(self.url, headers=self.headers,
                                 params={'ref': STATE_BRANCH}, timeout=20, allow_redirects=False)
        if response.status_code == 404:
            return None, None
        if response.status_code != 200:
            raise PreviewError('PREVIEW_LEDGER_READ_FAILED')
        body = response.json()
        state = json.loads(base64.b64decode(body['content'], validate=False))
        if (state.get('plan_sha256') != self.plan_hash or state.get('schema_version') != 1
                or not re.fullmatch('[a-f0-9]{40}', body.get('sha', ''))):
            raise PreviewError('PREVIEW_LEDGER_INVALID')
        return state, body['sha']

    def save(self, state, sha):
        body = {'message': 'Checkpoint isolated Telegram preview ' + self.plan_hash[:12],
                'branch': STATE_BRANCH, 'content': base64.b64encode(canonical(state)).decode()}
        if sha is not None:
            body['sha'] = sha
        response = self.http.put(self.url, headers=self.headers, json=body,
                                 timeout=20, allow_redirects=False)
        if response.status_code not in (200, 201):
            raise PreviewError('PREVIEW_LEDGER_CAS_FAILED_NO_RETRY')
        saved, saved_sha = self.load()
        if saved != state:
            raise PreviewError('PREVIEW_LEDGER_ACK_MISMATCH')
        return saved_sha


def execute(plan, plan_hash, credentials, proof, http, paths, ledger):
    if not 1 <= len(plan['operations']) <= 8:
        raise PreviewError('MAXIMUM_EIGHT_POSTS')
    sys.path.insert(0, str(SOURCE_ROOT))
    from preview_real_announcement import _send_once
    old, _ = ledger.load()
    if old is not None:
        raise PreviewError('PLAN_ALREADY_HAS_LEDGER_NO_REPLAY')
    state = {'schema_version': 1, 'plan_sha256': plan_hash,
             'destination_alias': plan['destination_alias'], 'created_at': now(),
             'operations': [{**op, 'payload_sha256': digest(op), 'status': 'RESERVED',
                             'attempt_id': None, 'receipt': None, 'error': None}
                            for op in plan['operations']]}
    sha = ledger.save(state, None)
    count, receipts = 0, []
    for index, op in enumerate(plan['operations']):
        proof.validate(credentials, at=now())
        state['operations'][index].update(status='SENDING', attempt_id=uuid4().hex, claimed_at=now())
        sha = ledger.save(state, sha)  # Durable ACK required before Telegram POST.
        verified, read_sha = ledger.load()
        if verified != state or read_sha != sha:
            raise PreviewError('DURABLE_CLAIM_LOST_NO_POST')
        proof.validate(credentials, at=now())
        count += 1
        kwargs = ({'message': op['payload']} if op['method'] == 'sendMessage' else
                  {'pdf_path': paths[op['id']], 'filename': op['document']['filename'],
                   'expected_size': op['document']['size']})
        receipt, status = _send_once(http, op['method'], credentials, **kwargs)
        result = {'operation_id': op['id'], 'status': status, 'receipt': receipt,
                  'post_attempts_so_far': count}
        print(json.dumps(result, sort_keys=True))  # Preserve evidence even if receipt CAS fails.
        receipts.append(result)
        state['operations'][index].update(status=status, receipt=receipt,
                                           error=None if receipt else status, completed_at=now())
        try:
            sha = ledger.save(state, sha)
        except Exception:
            raise PreviewError('RECEIPT_CHECKPOINT_FAILED_OPERATOR_RECONCILIATION_NO_RETRY') from None
        if status != 'DELIVERED':
            return {'status': status, 'post_attempts': count, 'operations': receipts}
    return {'status': 'DELIVERED', 'post_attempts': count, 'operations': receipts}


def workflow_context(env, sending):
    if (env.get('GITHUB_REPOSITORY') != REPOSITORY or env.get('GITHUB_REF') != FEATURE_REF
            or env.get('GITHUB_EVENT_NAME') != 'workflow_dispatch'
            or not re.fullmatch('[a-f0-9]{40}', env.get('GITHUB_SHA', ''))
            or env.get('GITHUB_SHA') != env.get('KEMDIKTISAINTEK_REVIEWED_CODE_SHA')
            or env.get('GITHUB_SHA') != env.get('KEMDIKTISAINTEK_APPROVED_CODE_SHA')):
        raise PreviewError('WORKFLOW_CONTEXT_OR_INDEPENDENT_SHA_GATE_FAILED')
    if (env.get('KEMDIKTISAINTEK_EXPERIMENT_ENABLED', 'false') != 'false'
            or env.get('KEMDIKTISAINTEK_EXPERIMENT_AUTO_SEND', 'false') != 'false'):
        raise PreviewError('AUTOMATIC_DELIVERY_MUST_REMAIN_OFF')
    if sending and env.get('KEMDIKTISAINTEK_MULTI_PREVIEW_CONFIRMATION') != CONFIRMATION:
        raise PreviewError('OPERATOR_PLAN_APPROVAL_REQUIRED')


def run(mode, env):
    sending = mode == 'SEND_MULTI_PREVIEW'
    if mode not in {'MULTI_PREVIEW_PLAN', 'SEND_MULTI_PREVIEW'}:
        raise PreviewError('INVALID_MULTI_PREVIEW_MODE')
    workflow_context(env, sending)
    envelope = json.loads(PLAN_PATH.read_text(encoding='utf-8'))
    plan, plan_hash = envelope['plan'], envelope['plan_sha256']
    if digest(plan) != plan_hash or (sending and env.get('KEMDIKTISAINTEK_MULTI_PLAN_SHA256') != plan_hash):
        raise PreviewError('APPROVED_PLAN_HASH_MISMATCH')
    root = env.get('KEMDIKTISAINTEK_FORMATTER_ROOT', '')
    telegram_module = validate_plan(plan, root)
    sys.path.insert(0, str(SOURCE_ROOT))
    from kemdiktisaintek_telegram import Credentials, preflight
    with requests.Session() as http:
        http.trust_env = False
        warnings = verify_sources(plan, root, http) or []
        credentials = Credentials.from_environment(env)
        proof = preflight(credentials, at=now(), get=http.get)
        alias = 'EXPERIMENT-' + credentials.chat_sha256[:12]
        if alias != plan['destination_alias']:
            raise PreviewError('DESTINATION_MISMATCH_NO_POST')
        paths = verify_pdfs(plan, telegram_module, http)
        try:
            print('MULTI_PREVIEW_PREFLIGHT=PASS destination=' + alias)
            print('MULTI_PREVIEW_PLAN_SHA256=' + plan_hash)
            if not sending:
                return {'status': 'READY_FOR_OPERATOR_APPROVAL', 'post_attempts': 0,
                        'planned_posts': len(plan['operations']), 'plan_sha256': plan_hash,
                        'warnings': warnings}
            ledger = Ledger(env.get('KEMDIKTISAINTEK_PREVIEW_STATE_TOKEN', ''), plan_hash)
            result = execute(plan, plan_hash, credentials, proof, http, paths, ledger)
            return {**result, 'warnings': warnings}
        finally:
            for path in paths.values():
                path.unlink(missing_ok=True)


def build(root, output=PLAN_PATH):
    notifier_module, telegram_module = formatter(root)
    inputs, evidence = archived_inputs(root)
    inputs['kemdiktisaintek'] = article_records()
    inputs['pdfs'] = {}
    with requests.Session() as http:
        http.trust_env = False
        for scenario, item in zip('DE', inputs['kemdiktisaintek']):
            docs = []
            for attachment in item['attachments']:
                if attachment['kind'] != 'pdf':
                    continue
                filename = telegram_module.document_filename(attachment['url'], attachment['label'])
                path, safe_name = telegram_module.download_pdf(http, attachment['url'], filename)
                try:
                    raw = path.read_bytes()
                    docs.append({'url': attachment['url'], 'filename': safe_name, 'size': len(raw),
                                 'sha256': hashlib.sha256(raw).hexdigest(), 'signature': '%PDF-'})
                finally:
                    path.unlink(missing_ok=True)
            inputs['pdfs'][scenario] = docs
        plan = {'schema_version': 1, 'formatter_sha': FORMATTER_SHA,
                'prepared_at': now(), 'destination_alias': 'EXPERIMENT-e73e971297b7',
                'inputs': inputs, 'provenance': evidence,
                'scope': 'New labelled A-E comparison; prior successful IASP single-preview operations are never replayed.'}
        plan['operations'], plan['scenarios'] = render(inputs, plan['prepared_at'], notifier_module, telegram_module)
        validate_plan(plan, root)
        verify_sources(plan, root, http)
    envelope = {'plan_sha256': digest(plan), 'plan': plan}
    Path(output).write_text(json.dumps(envelope, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    write_report(envelope, Path(output).with_name('README_MULTI_SOURCE_PREVIEW.md'))
    print(json.dumps({'plan_sha256': envelope['plan_sha256'], 'posts': len(plan['operations']),
                      'scenarios': plan['scenarios']}, ensure_ascii=True, indent=2))
    return envelope


def write_report(envelope, output):
    plan, plan_hash = envelope['plan'], envelope['plan_sha256']
    lines = ['# Multi-source Telegram preview — awaiting approval', '',
             '- Formatter commit: `' + FORMATTER_SHA + '`',
             '- Plan SHA-256: `' + plan_hash + '`',
             '- Prepared UTC: ' + plan['prepared_at'],
             '- Destination alias: `' + plan['destination_alias'] + '`',
             '- Planned Telegram POSTs: ' + str(len(plan['operations'])) + ' (maximum 8)',
             '- Actual Telegram POSTs: 0; no message IDs for this plan.', '',
             '## Evidence and scope', '',
             'BIMA records and YouTube publication/description metadata are unmodified historical records '
             'from fixtures at the pinned formatter commit. BIMA source URL is verified with system TLS; '
             'individual historical cards are not claimed to be visible on the current first page. '
             'BIMA document names have no download URL in the archive, so they are listed without upload. '
             'YouTube oEmbed confirms both exact titles and the official channel. '
             'Both Kemdiktisaintek details and official PDFs are fetched read-only and validated.', '',
             'The notifier header clock is frozen at the actual preparation time so the operator-approved '
             'message remains identical; publication dates in source records are unchanged.', '',
             'Scenario D is a new, explicitly labelled multi-source comparison payload. '
             'Previous single-preview receipts (38, 39; run 40189458449) remain historical evidence; '
             'that workflow is never rerun. Approval of this new plan is required before any new D operation.', '',
             '## Operation order', '', '| Order | Scenario | Operation | Details |',
             '|---|---|---|---|']
    for index, op in enumerate(plan['operations'], 1):
        detail = (str(next(s['item_count'] for s in plan['scenarios'] if s['id'] == op['scenario'])) + ' items'
                  if op['method'] == 'sendMessage' else
                  op['document']['filename'] + ' (' + str(op['document']['size']) + ' bytes)')
        lines.append(f"| {index} | {op['scenario']} | {op['method']} | {detail} |")
    for op in plan['operations']:
        lines.extend(['', '## ' + op['id'], ''])
        if op['method'] == 'sendMessage':
            lines.extend(['```html', op['payload']['text'], '```'])
        else:
            doc = op['document']
            lines.extend(['- URL: ' + doc['url'], '- Filename: `' + doc['filename'] + '`',
                          '- Bytes: ' + str(doc['size']), '- Signature: `%PDF-`',
                          '- SHA-256: `' + doc['sha256'] + '`'])
    lines.extend(['', '## Delivery and result recording', '',
                  '`MULTI_PREVIEW_PLAN` performs GET/preflight only. `SEND_MULTI_PREVIEW` requires '
                  'independent approved code SHA, exact plan hash, and confirmation '
                  '`SEND_MULTI_PREVIEW_APPROVED_PLAN`. Both automatic delivery switches must remain false.', '',
                  'A separate ledger is created only after SEND approval at '
                  '`kemdiktisaintek-experiment-state:' + LEDGER_PREFIX + plan_hash + '.json`. '
                  'No production state or existing experiment observation/outbox file is written. '
                  'Every operation is durably RESERVED, then CAS-claimed SENDING before POST, '
                  'then checkpointed with its individual receipt. Any attempted/previous plan ledger blocks replay. '
                  'CAS failure, timeout, destination mismatch, and receipt checkpoint failure stop the sequence '
                  'without automatic retries.', '',
                  '## Reader experience review', '',
                  '- A/B/C use native source-specific headers and consistent numbering 1–2. '
                  'Mixed C retains per-item source identity.',
                  '- BIMA/YouTube footer is italic; Kemdiktisaintek footer is plain. '
                  'Recommend one shared visual style in a separate approved production change.',
                  '- BIMA/YouTube show generation time and item counts; Kemdiktisaintek shows publication date '
                  'without count. Keep date labels explicit if later aligned.',
                  '- YouTube appends an ellipsis even to short descriptions; recommend adding it only when truncated.',
                  '- Titles remain complete and messages are below Telegram limits. '
                  'E exercises a longer title and no deadline; D has an explicit deadline.',
                  '- BIMA source is a shared listing URL. No item-specific URL or missing document link is fabricated.',
                  '- PDFs immediately follow their D/E summary. Visual appearance in Telegram remains unverified '
                  'until operator-approved sending; current review is of actual formatter HTML.', ''])
    Path(output).write_text('\n'.join(lines), encoding='utf-8')
    cards = []
    for scenario in plan['scenarios']:
        op = next(op for op in plan['operations'] if op['id'] == scenario['id'] + '-summary')
        docs = [op['document'] for op in plan['operations']
                if op['scenario'] == scenario['id'] and op['method'] == 'sendDocument']
        attachments = ''.join('<div class="attachment">PDF: ' + escape(doc['filename'])
                              + ' · ' + str(doc['size']) + ' byte</div>' for doc in docs)
        cards.append('<section><h2>Skenario ' + scenario['id'] + '</h2><div class="message">'
                     + op['payload']['text'] + '</div>' + attachments + '</section>')
    html = ('<!doctype html><html lang="id"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>Multi-source Telegram preview</title><style>'
            'body{font:15px system-ui;background:#edf2f5;color:#152536;margin:24px}'
            'main{display:flex;gap:20px;align-items:flex-start;flex-wrap:wrap}'
            'section{width:360px}h2{font-size:18px}.message{white-space:pre-wrap;overflow-wrap:anywhere;'
            'background:white;padding:18px;border-radius:12px;line-height:1.5;box-shadow:0 2px 6px #0001}'
            '.attachment{background:#deedf6;padding:16px;border-radius:10px;margin-top:8px;overflow-wrap:anywhere}'
            'code{overflow-wrap:anywhere}</style><h1>Preview HTML formatter produksi</h1>'
            '<p>Rendering lokal untuk peninjauan; tampilan aktual Telegram belum diuji untuk rencana ini.</p>'
            '<p>Plan SHA-256: <code>' + plan_hash + '</code></p><main>' + ''.join(cards) + '</main></html>')
    Path(output).with_name('multi_preview_comparison.html').write_text(html, encoding='utf-8')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('mode', choices=['build', 'MULTI_PREVIEW_PLAN', 'SEND_MULTI_PREVIEW'])
    parser.add_argument('--formatter-root')
    args = parser.parse_args()
    try:
        if args.mode == 'build':
            build(args.formatter_root)
            return 0
        result = run(args.mode, os.environ)
        print(json.dumps(result, sort_keys=True))
        return 0 if result['status'] in {'READY_FOR_OPERATOR_APPROVAL', 'DELIVERED'} else 1
    except Exception as exc:
        code = str(exc) if isinstance(exc, PreviewError) else type(exc).__name__
        print('MULTI_PREVIEW_BLOCKED=' + code)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
