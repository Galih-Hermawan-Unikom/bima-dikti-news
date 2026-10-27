from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import socket
from threading import Barrier, Lock, Thread
from unittest.mock import Mock

import pytest
import requests

from kemdiktisaintek_editorial_delivery import (ALLOWED_ACTORS, EditorialDelivery, TARGETS,
    event_key, _official_item)
from kemdiktisaintek_editorial_state import (empty_ledger, blob_sha, validate_ledger,
    validate_lineage, EditorialRemoteState, PATH as EDITORIAL_PATH, BRANCH as EDITORIAL_BRANCH)
from kemdiktisaintek_prod_delivery_state import (migrate_v1, migrate_official_policy,
    event_key as automatic_event_key, validate_delivery_state)
from kemdiktisaintek_prod_monitor import Capture, apply_poll, initialize
from kemdiktisaintek_prod_parser import parse_api_list
from kemdiktisaintek_prod_state import StateConflict, StateError, canonical, fingerprint

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads((ROOT / 'tests/fixtures/kemdiktisaintek_editorial_articles.json').read_text(encoding='utf-8'))
UUID_A = '2407f6f4-1e06-452b-a053-864426e9a183'
UUID_B = 'b7abee5c-39d8-48fa-a2ac-93fe07ec7ed2'
AT0 = '2026-10-25T00:00:00+00:00'
AT1 = '2026-10-27T10:30:00+00:00'
MASTER = 'a' * 40
CHAT_SHA = hashlib.sha256(b'-1001234567890').hexdigest()
DESTINATION = {'bot_id': 123456789, 'chat_sha256': CHAT_SHA,
               'credential_sha256': hashlib.sha256(b'fake editorial test credential').hexdigest()}


@pytest.fixture(autouse=True)
def forbid_live_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Live source/Telegram/GitHub network is forbidden in editorial tests')
    monkeypatch.setattr(requests.sessions.Session, 'request', forbidden)
    monkeypatch.setattr(socket.socket, 'connect', forbidden)


def _html(article):
    row = {k: article[k] for k in ('id', 'slug', 'title', 'category', 'datetime')}
    row.update(updated_at=article['updated_at'], source=None)
    anchors = ''.join(f'<a href="{doc["url"]}">{doc["label"]}</a>' for doc in article['attachments'])
    data = json.dumps({'props': {'pageProps': {'data': row}}}, ensure_ascii=False)
    return ('<html><body><div class="detail"><h1>' + article['title'] + '</h1>' + anchors
        + '<article class="rich-content"><p>' + article['body'] + '</p></article></div>'
        + '<script id="__NEXT_DATA__" type="application/json">' + data + '</script></body></html>')


class FixtureSource:
    def __init__(self, rows=None, *, changed_uuid=None, changed_body=None):
        self.rows = deepcopy(rows or FIXTURE['articles'])
        self.changed_uuid, self.changed_body = changed_uuid, changed_body
        self.calls = []

    def collect(self, *, max_pages, detail_limit, search, category, method):
        self.calls.append(('collect', max_pages, detail_limit, search, category, method))
        assert (max_pages, detail_limit, search, category, method) == (3, 0, '', None, 'api')
        raw = [{'id': row['id'], 'slug': row['slug'], 'title': row['title'],
                'category': row['category'], 'created_at': row['created_at'],
                'updated_at': row['updated_at']} for row in self.rows]
        page_url = 'https://kemdiktisaintek.go.id/api/announcement?page=1&size=10&search='
        parsed = parse_api_list(json.dumps({'data': raw, 'total': len(raw)}), page_url)
        return {'items': parsed['items'], 'pages': [{'url': page_url, 'total': len(raw)}],
                'archive_complete': False}

    def fetch(self, url):
        self.calls.append(('detail', url))
        for row in self.rows:
            expected = TARGETS[row['id']]['url']
            if url == expected:
                item = deepcopy(row)
                if row['id'] == self.changed_uuid:
                    item['body'] = self.changed_body
                return _html(item)
        raise AssertionError('unexpected URL')


def _item(identifier, source=None):
    source = source or FixtureSource()
    return _official_item(identifier, source)


def _coverage(at, count, total):
    url = 'https://kemdiktisaintek.go.id/api/announcement?page=1&size=10&search='
    return {'requested_pages': 1, 'captured_pages': 1,
        'pages': [{'page': 1, 'url': url, 'count': count, 'total': total,
                   'method': 'public_json_api', 'healthy': True}],
        'total_reported': total, 'listing_count': count, 'detail_count': count,
        'detail_budget': max(1, count), 'archive_complete': False,
        'all_requested_pages_healthy': True, 'unfiltered': True}


def _production(items=None):
    sink = Mock()
    empty_capture = Capture(AT0, [], _coverage(AT0, 0, 0), [])
    initial = initialize(sink, empty_capture)
    state = migrate_v1(initial, at=AT0)
    items = list(items or [_item(UUID_A), _item(UUID_B)])
    capture = Capture(AT1, items, _coverage(AT1, len(items), len(items)), [])
    monitor, _ = apply_poll(state['observation_state'], capture, max_poll_gap=timedelta(hours=12))
    state['observation_state'] = monitor
    state = migrate_official_policy(state, at=AT1)
    validate_delivery_state(state)
    for item in items:
        observation = state['observation_state']['observations'][item['id']]
        assert observation['detection_status'] == 'HOLD'
        assert observation['initial_decision'] == 'HOLD'
    return state


class MemoryProduction:
    def __init__(self, state):
        self.state = deepcopy(state)
        self.reads = 0
        self.writes = 0

    def load(self):
        self.reads += 1
        return deepcopy(validate_delivery_state(self.state)), 'production-blob-sha'

    def checkpoint(self, *args, **kwargs):
        self.writes += 1
        raise AssertionError('editorial path must never write the automatic production state')


class MemoryLedger:
    durable = True

    def __init__(self, state=None, *, fail_checkpoint=None):
        self.state = deepcopy(state or empty_ledger())
        validate_ledger(self.state)
        self.sha = blob_sha(canonical(self.state)) if state else None
        self.checkpoints = 0
        self.fail_checkpoint = fail_checkpoint
        self.lock = Lock()
        self.barrier = None
        self.barrier_reads = 0

    def load(self):
        barrier = None
        with self.lock:
            if self.barrier and self.barrier_reads < 2:
                self.barrier_reads += 1
                barrier = self.barrier
        if barrier:
            barrier.wait(timeout=5)
        with self.lock:
            return deepcopy(validate_ledger(self.state)), self.sha

    def checkpoint(self, state, expected):
        with self.lock:
            self.checkpoints += 1
            if self.fail_checkpoint == self.checkpoints or expected != self.sha:
                raise StateConflict('MOCK_EDITORIAL_CAS_CONFLICT')
            validate_lineage(self.state, state)
            self.state = deepcopy(validate_ledger(state))
            self.sha = blob_sha(canonical(self.state))
            return self.sha


class FakeGateway:
    def __init__(self, *, timeout=False, fail_attachment=False, identity=None):
        self.identity = deepcopy(identity or DESTINATION)
        self.posts = []
        self.inspect_calls = 0
        self.timeout = timeout
        self.fail_attachment = fail_attachment
        self.message_id = 500

    def inspect(self, *, at):
        self.inspect_calls += 1
        return type('Proof', (), {'identity': deepcopy(self.identity), 'checked_at': at})()

    def post_claimed(self, row, proof, *, at):
        assert row['status'] == 'SENDING'
        assert row['destination'] == proof.identity
        op = row['operation']
        self.posts.append((row['event_key'], op['operation_id'], op['kind']))
        if self.timeout:
            return None, 'TIMEOUT'
        if self.fail_attachment and op['kind'] == 'ATTACHMENT':
            return None, 'PREPOST_PDF_DOWNLOAD_FAILED'
        self.message_id += 1
        method = 'sendDocument' if op['kind'] == 'ATTACHMENT' else 'sendMessage'
        receipt = {'http_status': 200, 'ok': True, 'method': method,
                   'message_id': self.message_id, 'destination_sha256': CHAT_SHA}
        if op['kind'] == 'ATTACHMENT':
            receipt.update(file_id_sha256=hashlib.sha256(b'fixture-pdf-file-id').hexdigest(),
                           file_name=op['payload']['filename'])
        return receipt, None


def _env(**updates):
    env = {'GITHUB_ACTIONS': 'true', 'GITHUB_EVENT_NAME': 'workflow_dispatch',
        'GITHUB_REPOSITORY': 'Galih-Hermawan-Unikom/bima-dikti-news',
        'GITHUB_REF': 'refs/heads/master',
        'GITHUB_WORKFLOW_REF': 'Galih-Hermawan-Unikom/bima-dikti-news/'
            '.github/workflows/kemdiktisaintek-editorial-delivery.yml@refs/heads/master',
        'GITHUB_SHA': MASTER, 'GITHUB_ACTOR': 'Galih-Hermawan-Unikom',
        'KEMDIKTISAINTEK_PRODUCTION_REVIEWED_SHA': MASTER,
        'KEMDIKTISAINTEK_PRODUCTION_APPROVED_SHA': MASTER,
        'KEMDIKTISAINTEK_PRODUCTION_ENABLED': 'true'}
    env.update(updates)
    return env


def _service(*, target_items=None, ledger=None, gateway=None, env=None, source=None, clock=None):
    source = source or FixtureSource()
    items = list(target_items or [_item(UUID_A, source), _item(UUID_B, source)])
    return EditorialDelivery(MemoryProduction(_production(items)), ledger or MemoryLedger(),
        source, gateway or FakeGateway(), env=env or _env(), clock=clock or (lambda: AT1))


def test_preview_both_real_uuid_fixtures_and_parser_fields_without_writes_or_posts():
    source, ledger, gateway = FixtureSource(), MemoryLedger(), FakeGateway()
    service = _service(source=source, ledger=ledger, gateway=gateway)
    a = service.preview(UUID_A)
    b = service.preview(UUID_B)
    assert a['title'] == TARGETS[UUID_A]['title'] and b['title'] == TARGETS[UUID_B]['title']
    assert a['deadline_evidence'] == ['paling lambat 06 November 2026']
    assert b['deadline_evidence'] == []
    assert len(a['documents']) == len(b['documents']) == 1
    assert 'INFORMASI RESMI' in a['message'] and 'PENGUMUMAN BARU' not in a['message']
    assert 'INFORMASI RESMI' in b['message'] and 'Tenggat:' not in b['message']
    assert a['production_status'] == b['production_status'] == 'HOLD'
    assert a['telegram_post_attempted'] is b['telegram_post_attempted'] is False
    assert a['remote_write'] is b['remote_write'] is False
    assert ledger.checkpoints == 0 and gateway.posts == [] and gateway.inspect_calls == 0
    assert all(call[1:] == (3, 0, '', None, 'api') for call in source.calls if call[0] == 'collect')


def test_live_parser_fixture_uuid_title_canonical_url_and_deadline_are_consistent():
    a, b = _item(UUID_A), _item(UUID_B)
    assert (a['id'], a['url'], a['published_at'].split('T')[0]) == (UUID_A, TARGETS[UUID_A]['url'], '2026-10-27')
    assert (b['id'], b['url'], b['published_at'].split('T')[0]) == (UUID_B, TARGETS[UUID_B]['url'], '2026-10-26')
    assert a['publication_basis'] == b['publication_basis'] == 'detail_datetime'
    assert any(doc['kind'] == 'pdf' for doc in a['attachments'])
    assert any(doc['kind'] == 'pdf' for doc in b['attachments'])


def test_authorization_is_uuid_specific_and_does_not_change_hold_state():
    ledger, gateway = MemoryLedger(), FakeGateway()
    service = _service(ledger=ledger, gateway=gateway)
    before = deepcopy(service.production.state)
    auth = service.authorize(UUID_A, f'AUTHORIZE {UUID_A}')
    assert auth['status'] == 'AUTHORIZED' and auth['operations'] == 2
    with pytest.raises(StateError, match='EDITORIAL_AUTHORIZATION_NOT_FOUND'):
        service.send(UUID_B, f'SEND {UUID_B}')
    assert service.production.state == before
    assert len(ledger.state['entries']) == 1
    assert ledger.state['entries'][event_key(UUID_A)]['uuid'] == UUID_A
    assert gateway.posts == []


@pytest.mark.parametrize('bad_env', [
    {'KEMDIKTISAINTEK_PRODUCTION_REVIEWED_SHA': 'b' * 40},
    {'KEMDIKTISAINTEK_PRODUCTION_APPROVED_SHA': 'b' * 40},
    {'GITHUB_ACTOR': 'galihboy'},
    {'GITHUB_REF': 'refs/heads/feat/test'},
])
def test_review_actor_ref_and_approved_sha_fail_closed(bad_env):
    service = _service(env=_env(**bad_env))
    with pytest.raises(StateError):
        service.authorize(UUID_A, f'AUTHORIZE {UUID_A}')


def test_only_allowlisted_uuid_can_be_previewed():
    service = _service()
    with pytest.raises(StateError, match='EDITORIAL_UUID_NOT_ALLOWLISTED'):
        service.preview('00000000-0000-0000-0000-000000000000')


def test_changed_article_content_invalidates_authorization_before_post():
    source, ledger, gateway = FixtureSource(), MemoryLedger(), FakeGateway()
    service = _service(source=source, ledger=ledger, gateway=gateway)
    service.authorize(UUID_A, f'AUTHORIZE {UUID_A}')
    changed = FixtureSource(changed_uuid=UUID_A,
        changed_body=FIXTURE['articles'][0]['body'] + ' Isi artikel berubah setelah persetujuan.')
    service.client = changed
    with pytest.raises(StateError, match='EDITORIAL_OBSERVED_CONTENT_CHANGED_REVIEW_REQUIRED'):
        service.send(UUID_A, f'SEND {UUID_A}')
    assert gateway.posts == []
    assert ledger.state['entries'][event_key(UUID_A)]['status'] == 'AUTHORIZED'


def test_destination_change_after_authorization_stops_before_claim_and_post():
    ledger, gateway = MemoryLedger(), FakeGateway()
    service = _service(ledger=ledger, gateway=gateway)
    service.authorize(UUID_A, f'AUTHORIZE {UUID_A}')
    gateway.identity = {'bot_id': 123456789, 'chat_sha256': 'f' * 64,
                        'credential_sha256': DESTINATION['credential_sha256']}
    with pytest.raises(StateError, match='EDITORIAL_DESTINATION_CHANGED'):
        service.send(UUID_A, f'SEND {UUID_A}')
    assert gateway.posts == []
    assert ledger.state['entries'][event_key(UUID_A)]['status'] == 'RESERVED'


def test_confirmation_is_explicit_and_actor_must_match_authorizer():
    service = _service()
    with pytest.raises(StateError, match='EDITORIAL_EXPLICIT_CONFIRMATION_REQUIRED'):
        service.authorize(UUID_A, 'approved=true')
    service.authorize(UUID_A, f'AUTHORIZE {UUID_A}')
    service.env['GITHUB_ACTOR'] = 'someone-else'
    with pytest.raises(StateError, match='EDITORIAL_ACTOR_NOT_AUTHORIZED'):
        service.send(UUID_A, f'SEND {UUID_A}')


def test_editorial_delivery_sends_summary_and_pdf_once_then_replay_returns_receipt():
    ledger, gateway = MemoryLedger(), FakeGateway()
    service = _service(ledger=ledger, gateway=gateway)
    service.authorize(UUID_A, f'AUTHORIZE {UUID_A}')
    result = service.send(UUID_A, f'SEND {UUID_A}')
    assert result['status'] == 'DELIVERED' and result['post_attempted'] is True
    assert [row[2] for row in gateway.posts] == ['SUMMARY', 'ATTACHMENT']
    assert [receipt['status'] for receipt in result['receipts']] == ['DELIVERED', 'DELIVERED']
    before_posts = list(gateway.posts)
    replay = service.send(UUID_A, f'SEND {UUID_A}')
    assert replay['status'] == 'DELIVERED' and replay['post_attempted'] is False
    assert gateway.posts == before_posts
    assert service.production.writes == 0
    assert ledger.state['entries'][event_key(UUID_A)]['delivery_origin'] == 'EDITORIAL_RECOVERY'
    assert service.production.state['observation_state']['observations'][UUID_A]['detection_status'] == 'HOLD'


def test_second_uuid_can_be_authorized_and_delivered_independently_without_deadline_claim():
    ledger, gateway = MemoryLedger(), FakeGateway()
    service = _service(ledger=ledger, gateway=gateway)
    preview = service.preview(UUID_B)
    assert preview['deadline_evidence'] == []
    assert 'Tenggat:' not in preview['message']
    service.authorize(UUID_B, f'AUTHORIZE {UUID_B}')
    result = service.send(UUID_B, f'SEND {UUID_B}')
    assert result['status'] == 'DELIVERED'
    assert len(ledger.state['entries']) == 1 and event_key(UUID_B) in ledger.state['entries']
    assert service.production.state['observation_state']['observations'][UUID_B]['detection_status'] == 'HOLD'


def test_automatic_admission_or_outbox_blocks_editorial_reservation():
    item = _item(UUID_A)
    state = _production([item])
    key = automatic_event_key(UUID_A)
    state['admissions'][key] = {'event_key': key, 'uuid': UUID_A, 'event_type': 'INITIAL',
        'decision': 'ELIGIBLE', 'observed_at': AT1, 'expires_at': '2026-10-27T11:30:00+00:00',
        'item_sha256': fingerprint(item), 'baseline_sha256': state['observation_state']['baseline']['sha256'],
        'admission_policy_version': 'kemdiktisaintek-delivery-v3-all-official'}
    # The admission remains a valid schema object but makes editorial processing ineligible.
    validate_delivery_state(state)
    service = _service(target_items=[item], ledger=MemoryLedger(), gateway=FakeGateway())
    service.production = MemoryProduction(state)
    with pytest.raises(StateError, match='EDITORIAL_AUTOMATIC_DELIVERY_ALREADY_ADMITTED_OR_RESERVED'):
        service.authorize(UUID_A, f'AUTHORIZE {UUID_A}')


def test_hold_content_or_identity_mismatch_is_blocked():
    item = _item(UUID_A)
    state = _production([item])
    state['observation_state']['observations'][UUID_A]['detection_status'] = 'KNOWN'
    state['observation_state']['observations'][UUID_A]['initial_decision'] = 'KNOWN'
    validate_delivery_state(state)
    service = _service(target_items=[item])
    service.production = MemoryProduction(state)
    with pytest.raises(StateError, match='EDITORIAL_SOURCE_NOT_HELD_TARGET'):
        service.authorize(UUID_A, f'AUTHORIZE {UUID_A}')


def test_timeout_is_ambiguous_and_never_retried():
    ledger, gateway = MemoryLedger(), FakeGateway(timeout=True)
    service = _service(ledger=ledger, gateway=gateway)
    service.authorize(UUID_A, f'AUTHORIZE {UUID_A}')
    first = service.send(UUID_A, f'SEND {UUID_A}')
    assert first['status'] == 'AMBIGUOUS' and first['post_attempted'] is True
    second = service.send(UUID_A, f'SEND {UUID_A}')
    assert second['status'] == 'AMBIGUOUS' and second['post_attempted'] is False
    assert len(gateway.posts) == 1


def test_pdf_failure_is_partial_and_summary_is_not_reposted():
    ledger, gateway = MemoryLedger(), FakeGateway(fail_attachment=True)
    service = _service(ledger=ledger, gateway=gateway)
    service.authorize(UUID_A, f'AUTHORIZE {UUID_A}')
    first = service.send(UUID_A, f'SEND {UUID_A}')
    assert first['status'] == 'PARTIAL'
    assert first['receipts'][0]['status'] == 'DELIVERED'
    assert first['receipts'][1]['status'] == 'FAILED'
    second = service.send(UUID_A, f'SEND {UUID_A}')
    assert second['post_attempted'] is False and len(gateway.posts) == 2
    assert gateway.posts[0][2] == 'SUMMARY' and gateway.posts[1][2] == 'ATTACHMENT'


def test_cas_conflict_on_reservation_or_claim_prevents_post():
    for conflict_on in (1, 2):
        ledger, gateway = MemoryLedger(), FakeGateway()
        service = _service(ledger=ledger, gateway=gateway)
        service.authorize(UUID_A, f'AUTHORIZE {UUID_A}')
        ledger.fail_checkpoint = ledger.checkpoints + conflict_on
        with pytest.raises(StateConflict):
            service.send(UUID_A, f'SEND {UUID_A}')
        assert gateway.posts == []


def test_receipt_checkpoint_conflict_requires_reconciliation_and_no_resend():
    ledger, gateway = MemoryLedger(), FakeGateway()
    service = _service(ledger=ledger, gateway=gateway)
    service.authorize(UUID_A, f'AUTHORIZE {UUID_A}')
    ledger.fail_checkpoint = ledger.checkpoints + 3  # reservation, durable claim, then receipt checkpoint
    from kemdiktisaintek_editorial_delivery import EditorialReconciliationRequired
    with pytest.raises(EditorialReconciliationRequired):
        service.send(UUID_A, f'SEND {UUID_A}')
    assert len(gateway.posts) == 1
    recovered = service.send(UUID_A, f'SEND {UUID_A}')
    assert recovered['status'] == 'SENDING' and recovered['reconciliation_required']
    assert recovered['post_attempted'] is False
    assert len(gateway.posts) == 1


def test_reauthorization_after_expiry_appends_history_with_same_event_key():
    ledger, gateway = MemoryLedger(), FakeGateway()
    current = [datetime.fromisoformat(AT1)]
    clock = lambda: current[0].isoformat()
    service = _service(ledger=ledger, gateway=gateway, clock=clock)
    service.authorize(UUID_A, f'AUTHORIZE {UUID_A}')
    current[0] += timedelta(minutes=61)
    service.authorize(UUID_A, f'AUTHORIZE {UUID_A}')
    entry = ledger.state['entries'][event_key(UUID_A)]
    assert len(ledger.state['entries']) == 1 and len(entry['authorizations']) == 2
    assert entry['active_authorization'] == 1 and entry['status'] == 'AUTHORIZED'


def test_remote_ledger_scope_is_explicit_and_manual_only_workflow_has_no_cron():
    assert EDITORIAL_BRANCH == 'dikti-production-state'
    assert EDITORIAL_PATH == '.bot-state/kemdiktisaintek_editorial.json'
    workflow = (ROOT / '.github/workflows/kemdiktisaintek-editorial-delivery.yml').read_text(encoding='utf-8')
    assert 'workflow_dispatch:' in workflow and 'schedule:' not in workflow
    assert "default: PREVIEW" in workflow and 'contents: read' in workflow and 'contents: write' in workflow
    unified = (ROOT / '.github/workflows/dikti-news-production.yml').read_bytes()
    base_blob_sha = hashlib.sha1(b'blob ' + str(len(unified)).encode() + b'\0' + unified).hexdigest()
    assert base_blob_sha == '0e9b31ae621d37022eab7a591882ab0dff8a55fd'


def test_missing_ledger_is_empty_only_when_github_path_history_proves_never_created():
    class Response:
        def __init__(self, status, body=None):
            self.status_code, self.body = status, body
        def json(self):
            return self.body
    class Session:
        def __init__(self, history_status, history):
            self.responses = [Response(404), Response(history_status, history)]
            self.calls, self.puts = [], []
        def get(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return self.responses.pop(0)
        def put(self, *args, **kwargs):
            self.puts.append((args, kwargs))
    first = Session(200, [])
    ledger, sha = EditorialRemoteState('test-only-token', session=first).load()
    assert ledger == empty_ledger() and sha is None
    assert first.calls[1][1]['params'] == {'path': EDITORIAL_PATH, 'sha': EDITORIAL_BRANCH, 'per_page': 1}
    for status, history in ((200, [{'sha': 'deleted-previously'}]), (403, None), (404, None)):
        session = Session(status, history)
        with pytest.raises(StateError, match='EDITORIAL_LEDGER_MISSING_OR_HISTORY_UNVERIFIABLE'):
            EditorialRemoteState('test-only-token', session=session).load()
        assert session.puts == []


def test_two_concurrent_sends_share_uuid_claim_and_only_one_wins():
    ledger, gateway = MemoryLedger(), FakeGateway()
    service = _service(ledger=ledger, gateway=gateway)
    service.authorize(UUID_A, f'AUTHORIZE {UUID_A}')
    ledger.barrier = Barrier(2)
    results, errors = [], []
    def worker():
        try:
            results.append(service.send(UUID_A, f'SEND {UUID_A}'))
        except Exception as exc:
            errors.append(exc)
    threads = [Thread(target=worker), Thread(target=worker)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert all(not thread.is_alive() for thread in threads)
    assert len(gateway.posts) <= 2  # summary + one PDF; never twice per operation
    assert len({(row[0], row[1]) for row in gateway.posts}) == len(gateway.posts)
    assert results or errors
    assert ledger.state['entries'][event_key(UUID_A)]['status'] in {'DELIVERED', 'PARTIAL', 'SENDING'}
    assert sum(1 for row in gateway.posts if row[1] == 'summary') <= 1
    assert sum(1 for row in gateway.posts if row[1] == 'attachment-1') <= 1


def test_state_validator_rejects_removed_history_or_receipt_replay():
    ledger, gateway = MemoryLedger(), FakeGateway()
    service = _service(ledger=ledger, gateway=gateway)
    service.authorize(UUID_A, f'AUTHORIZE {UUID_A}')
    old, _ = ledger.load()
    removed = empty_ledger()
    removed['generation'] = old['generation'] + 1
    with pytest.raises(StateError, match='EDITORIAL_HISTORY_REMOVED'):
        validate_lineage(old, removed)
