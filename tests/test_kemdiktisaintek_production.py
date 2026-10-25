from copy import deepcopy
import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import socket
from unittest.mock import Mock
from uuid import uuid4

import pytest
import requests

from kemdiktisaintek_prod_automation import run_poll
from kemdiktisaintek_prod_delivery import Delivery
from kemdiktisaintek_prod_delivery_state import migrate_v1, validate_delivery_state, validate_delivery_lineage
from kemdiktisaintek_prod_monitor import Capture, initialize
from kemdiktisaintek_prod_remote_state import REPOSITORY, RemoteState, blob_sha
from kemdiktisaintek_prod_runtime import run
from kemdiktisaintek_prod_state import STATE_BRANCH, STATE_PATH, StateConflict, StateError, canonical
from kemdiktisaintek_prod_telegram import Credentials, Proof, switches

ROOT = Path(__file__).resolve().parents[1]
NOW = '2026-10-25T08:30:00+00:00'
FENCE = '2026-10-25T03:41:19.866405+00:00'
TARGET = '-1001234567890'
TOKEN = '123456789:PRODUCTION_TEST_TOKEN_ONLY'


@pytest.fixture(autouse=True)
def forbid_live_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Live network/Telegram calls are forbidden in this test suite')
    monkeypatch.setattr(requests.sessions.Session, 'request', forbidden)
    monkeypatch.setattr(socket.socket, 'connect', forbidden)


class MemoryRemote:
    durable = True

    def __init__(self, state, *, conflict_on=None):
        self.state = validate_delivery_state(deepcopy(state))
        self.sha = blob_sha(canonical(self.state))
        self.checkpoints = 0
        self.conflict_on = conflict_on

    def load(self):
        return deepcopy(validate_delivery_state(self.state)), self.sha

    def checkpoint(self, state, expected):
        self.checkpoints += 1
        if self.checkpoints == self.conflict_on:
            raise StateConflict('MOCK_CAS_CONFLICT')
        if expected != self.sha:
            raise StateConflict('MOCK_CAS_CONFLICT')
        validate_delivery_lineage(self.state, state)
        self.state = deepcopy(validate_delivery_state(state))
        self.sha = blob_sha(canonical(self.state))
        return self.sha


class FakeClient:
    def __init__(self, item=None):
        self.item = item
        self.calls = []

    def fetch(self, url):
        self.calls.append(url)
        if '/api/announcement?' in url:
            data = [] if self.item is None else [{
                'id': self.item['id'], 'slug': self.item['slug'], 'title': self.item['title'],
                'category': self.item['category'], 'created_at': '2026-10-25T07:55:00Z',
                'updated_at': '2026-10-25T08:00:00Z'}]
            return json.dumps({'data': data, 'total': len(data)})
        assert self.item and url == 'https://kemdiktisaintek.go.id/announcement/article/' + self.item['slug']
        props = {'data': {'id': self.item['id'], 'slug': self.item['slug'],
            'title': self.item['title'], 'category': self.item['category'],
            'datetime': '2026-10-25T08:00:00Z', 'updated_at': '2026-10-25T08:01:00Z'}}
        embedded = json.dumps({'props': {'pageProps': props}})
        links = ''.join('<a href="' + row['url'] + '">' + row.get('label', 'Dokumen') + '</a>'
                        for row in self.item.get('attachments', []))
        body = self.item.get('body_text',
            'Pendaftaran hibah penelitian perguruan tinggi dibuka. Dosen dan peneliti dapat mengajukan proposal sesuai panduan resmi.')
        return ('<html><body><div><h1>' + self.item['title'] + '</h1>' + links
            + '<time>25 Oktober 2026 | 15.00 WIB</time><article class="rich-content">'
            + '<p>' + body + '</p>'
            + '</article></div><script id="__NEXT_DATA__" type="application/json">' + embedded + '</script></body></html>')


class FakeGateway:
    def __init__(self, *, timeout=False):
        self.credentials = Credentials(TOKEN, TARGET, hashlib.sha256(TARGET.encode()).hexdigest(), 123456789)
        self.posts = []
        self.operations = []
        self.timeout = timeout

    def inspect(self, *, at):
        return Proof(self.credentials.identity(), at)

    def post_claimed(self, row, proof, *, at):
        assert row['status'] == 'SENDING'
        assert row['destination'] == proof.identity
        self.posts.append(row['event_key'])
        self.operations.append(row.get('operation', {}).get('kind', 'SUMMARY'))
        if self.timeout:
            return None, 'TIMEOUT'
        receipt = {'http_status': 200, 'ok': True, 'message_id': 70 + len(self.posts),
                   'destination_sha256': self.credentials.chat_sha256}
        if row.get('operation', {}).get('kind') == 'ATTACHMENT':
            receipt.update(method='sendDocument', file_id_sha256=hashlib.sha256(b'mock-file').hexdigest(),
                           file_name=row['operation']['payload']['filename'])
        else:
            receipt['method'] = 'sendMessage'
        return receipt, None


def new_state():
    coverage = {'requested_pages': 2, 'captured_pages': 1,
        'pages': [{'page': 1, 'url': 'https://kemdiktisaintek.go.id/api/announcement?page=1&size=10&search=',
                   'count': 0, 'total': 0, 'method': 'public_json_api', 'healthy': True}],
        'total_reported': 0, 'listing_count': 0, 'detail_count': 0, 'detail_budget': 10,
        'archive_complete': False, 'all_requested_pages_healthy': True, 'unfiltered': True}
    sink = Mock()
    capture = Capture('2026-10-25T07:50:00+00:00', [], coverage, [])
    initial = initialize(sink, capture)
    return migrate_v1(initial, at='2026-10-25T07:50:00+00:00')


def candidate():
    return {'id': str(uuid4()), 'slug': 'hibah-penelitian-perguruan-tinggi-2026',
            'title': 'Pendaftaran Hibah Penelitian Perguruan Tinggi Tahun 2026', 'category': 'Umum'}


def enabled_env(**extra):
    return {'KEMDIKTISAINTEK_PRODUCTION_ENABLED': 'true',
            'KEMDIKTISAINTEK_PRODUCTION_AUTO_SEND': 'true', **extra}


def test_realistic_new_relevant_announcement_delivers_once_across_restart():
    remote, gateway, item = MemoryRemote(new_state()), FakeGateway(), candidate()
    report = run_poll(enabled_env(), backend=remote, clock=lambda: NOW, source=FakeClient(item), gateway=gateway)
    assert report['api_health'] == 'HEALTHY'
    assert report['status_counts']['NEW_CANDIDATE'] == 1
    assert report['admissions_total'] == 1 and report['delivered_this_poll'] == 1
    key = gateway.posts[0]
    assert remote.state['outbox'][key]['status'] == 'DELIVERED'
    restarted = MemoryRemote(remote.state)
    second = run_poll(enabled_env(), backend=restarted, clock=lambda: '2026-10-25T08:31:00+00:00',
                      source=FakeClient(item), gateway=gateway)
    assert second['delivered_this_poll'] == 0
    assert gateway.posts == [key]


def test_formatter_changes_do_not_reopen_delivered_operations_or_modify_receipts(monkeypatch):
    remote, gateway, item = MemoryRemote(new_state()), FakeGateway(), candidate()
    item['attachments'] = [{'url': 'https://kemdiktisaintek.go.id/api/file/humas-production/announcements/2026/9/files/b63cc6de-f84c-4737-b7d9-9f47f6deb583/test.pdf',
                            'label': 'Panduan.pdf'}]
    run_poll(enabled_env(), backend=remote, clock=lambda: NOW, source=FakeClient(item), gateway=gateway)
    key = next(iter(remote.state['outbox']))
    before, posts = deepcopy(remote.state), list(gateway.posts)
    assert before['outbox'][key]['status'] == 'DELIVERED'
    assert len(before['outbox'][key]['operations']) == 2
    monkeypatch.setattr('kemdiktisaintek_prod_delivery.format_announcement',
                        Mock(side_effect=AssertionError('Delivered payloads must not be reformatted')))
    engine = Delivery(remote, gateway, environment=enabled_env(), clock=lambda: NOW)
    result = engine.send(key, gateway.inspect(at=NOW))
    assert result['post_attempted'] is False
    assert remote.state == before
    assert gateway.posts == posts


def test_switch_off_observes_without_preflight_reservation_or_post():
    remote, gateway = MemoryRemote(new_state()), FakeGateway()
    env = {'KEMDIKTISAINTEK_PRODUCTION_ENABLED': 'true',
           'KEMDIKTISAINTEK_PRODUCTION_AUTO_SEND': 'false'}
    result = run_poll(env, backend=remote, clock=lambda: NOW, source=FakeClient(candidate()), gateway=gateway)
    assert result['status_counts']['NEW_CANDIDATE'] == 1
    assert result['admissions_total'] == 1
    assert result['reserved_this_poll'] == 0 and result['delivered_this_poll'] == 0
    assert gateway.posts == []
    assert all(row['event_type'] != 'INITIAL' for row in remote.state['outbox'].values())


def test_observation_only_does_not_require_telegram_credentials():
    remote = MemoryRemote(new_state())
    result = run_poll({'KEMDIKTISAINTEK_PRODUCTION_ENABLED': 'true',
                       'KEMDIKTISAINTEK_PRODUCTION_AUTO_SEND': 'false'},
                      backend=remote, clock=lambda: NOW, source=FakeClient(candidate()))

    assert result['api_health'] == 'HEALTHY'
    assert result['delivery_enabled'] is False
    assert result['reserved_this_poll'] == result['delivered_this_poll'] == 0
    assert not any(row['event_type'] == 'INITIAL' for row in remote.state['outbox'].values())


def test_expired_admission_prevents_reservation_and_post():
    remote, gateway, item = MemoryRemote(new_state()), FakeGateway(), candidate()
    run_poll({'KEMDIKTISAINTEK_PRODUCTION_ENABLED': 'true',
              'KEMDIKTISAINTEK_PRODUCTION_AUTO_SEND': 'false'},
             backend=remote, clock=lambda: NOW, source=FakeClient(item))
    state, _ = remote.load()
    key = next(iter(state['admissions']))
    state['admissions'][key]['expires_at'] = '2026-10-25T08:30:05+00:00'
    remote = MemoryRemote(state)
    at = '2026-10-25T08:30:06+00:00'
    engine = Delivery(remote, gateway, environment=enabled_env(), clock=lambda: at)
    proof = gateway.inspect(at=at)

    with pytest.raises(StateError, match='NO_FRESH_EVENT_ADMISSION'):
        engine.reserve_initial(item['id'], proof)

    assert not remote.state['outbox']
    assert gateway.posts == []


def test_cas_conflict_on_durable_claim_prevents_post():
    remote, gateway = MemoryRemote(new_state(), conflict_on=4), FakeGateway()
    report = run_poll(enabled_env(), backend=remote, clock=lambda: NOW,
                      source=FakeClient(candidate()), gateway=gateway)
    assert gateway.posts == []
    assert report['delivery_error'] == 'MOCK_CAS_CONFLICT'
    assert next(iter(remote.state['outbox'].values()))['status'] == 'RESERVED'


def test_summary_then_pdf_are_independent_durable_operations():
    remote, gateway, item = MemoryRemote(new_state()), FakeGateway(), candidate()
    item['attachments'] = [{'url': 'https://kemdiktisaintek.go.id/api/file/humas-production/announcements/2026/9/files/b63cc6de-f84c-4737-b7d9-9f47f6deb583/test.pdf',
                            'label': 'Panduan.pdf'}]
    result = run_poll(enabled_env(), backend=remote, clock=lambda: NOW, source=FakeClient(item), gateway=gateway)
    assert result['delivery_error'] is None, result
    event = next(iter(remote.state['outbox'].values()))
    assert result['delivered_this_poll'] == 1
    assert gateway.operations == ['SUMMARY', 'ATTACHMENT']
    assert event['status'] == 'DELIVERED'
    assert [op['status'] for op in event['operations']] == ['DELIVERED', 'DELIVERED']


def test_multiple_official_pdfs_have_separate_receipts():
    remote, gateway, item = MemoryRemote(new_state()), FakeGateway(), candidate()
    base = 'https://kemdiktisaintek.go.id/api/file/humas-production/announcements/2026/9/files/b63cc6de-f84c-4737-b7d9-9f47f6deb583/'
    item['attachments'] = [{'url': base + name, 'label': name} for name in ('panduan.pdf', 'formulir.pdf')]
    run_poll(enabled_env(), backend=remote, clock=lambda: NOW, source=FakeClient(item), gateway=gateway)
    event = next(iter(remote.state['outbox'].values()))
    assert gateway.operations == ['SUMMARY', 'ATTACHMENT', 'ATTACHMENT']
    assert [op['operation_id'] for op in event['operations']] == ['summary', 'attachment-1', 'attachment-2']
    assert [op['status'] for op in event['operations']] == ['DELIVERED'] * 3
    assert len({op['receipt']['message_id'] for op in event['operations']}) == 3


def test_attachment_claim_cas_conflict_does_not_post_or_resend_summary():
    remote, gateway, item = MemoryRemote(new_state(), conflict_on=6), FakeGateway(), candidate()
    item['attachments'] = [{'url': 'https://kemdiktisaintek.go.id/api/file/humas-production/announcements/2026/9/files/b63cc6de-f84c-4737-b7d9-9f47f6deb583/test.pdf',
                            'label': 'Panduan.pdf'}]
    report = run_poll(enabled_env(), backend=remote, clock=lambda: NOW, source=FakeClient(item), gateway=gateway)
    event = next(iter(remote.state['outbox'].values()))
    assert report['delivery_error'] == 'MOCK_CAS_CONFLICT'
    assert gateway.operations == ['SUMMARY']
    assert event['status'] == 'PARTIAL'
    assert [op['status'] for op in event['operations']] == ['DELIVERED', 'RESERVED']


def test_receipt_checkpoint_conflict_becomes_ambiguous_after_restart_without_resend():
    remote, gateway, item = MemoryRemote(new_state(), conflict_on=5), FakeGateway(), candidate()
    report = run_poll(enabled_env(), backend=remote, clock=lambda: NOW,
                      source=FakeClient(item), gateway=gateway)
    event = next(iter(remote.state['outbox'].values()))
    assert report['ambiguous_this_poll'] == 1
    assert gateway.posts == [event['event_key']]
    assert event['status'] == 'SENDING'
    engine = Delivery(remote, gateway, environment=enabled_env(), clock=lambda: NOW)
    assert engine.recover() == 1
    assert remote.state['outbox'][event['event_key']]['status'] == 'AMBIGUOUS'
    assert gateway.posts == [event['event_key']]


def test_attachment_failure_keeps_delivered_summary_and_does_not_resend_it():
    remote, gateway, item = MemoryRemote(new_state()), FakeGateway(), candidate()
    item['attachments'] = [{'url': 'https://kemdiktisaintek.go.id/api/file/humas-production/announcements/2026/9/files/b63cc6de-f84c-4737-b7d9-9f47f6deb583/test.pdf',
                            'label': 'Panduan.pdf'}]
    original = gateway.post_claimed
    def fail_attachment(row, proof, *, at):
        if row.get('operation', {}).get('kind') == 'ATTACHMENT':
            gateway.posts.append(row['event_key'])
            gateway.operations.append('ATTACHMENT')
            return None, 'TELEGRAM_REJECTED'
        return original(row, proof, at=at)
    gateway.post_claimed = fail_attachment
    report = run_poll(enabled_env(), backend=remote, clock=lambda: NOW, source=FakeClient(item), gateway=gateway)
    assert report['delivery_error'] == 'PARTIAL_DELIVERY_RECONCILIATION_REQUIRED'
    event = next(iter(remote.state['outbox'].values()))
    assert event['status'] == 'PARTIAL'
    assert [op['status'] for op in event['operations']] == ['DELIVERED', 'FAILED']
    before = list(gateway.operations)
    engine = Delivery(remote, gateway, environment=enabled_env(), clock=lambda: NOW)
    result = engine.send(event['event_key'], gateway.inspect(at=NOW))
    assert result['post_attempted'] is False
    assert gateway.operations == before


def test_operation_schema_migration_preserves_test_receipt_and_observation_history():
    remote, gateway = MemoryRemote(new_state()), FakeGateway()
    engine = Delivery(remote, gateway, environment=enabled_env(), clock=lambda: NOW)
    proof = gateway.inspect(at=NOW)
    key = engine.reserve_test('KDTS-TEST-MIGRATION-01', proof, approval='SEND_ONE_EXPERIMENT_TEST')
    engine.send(key, proof)
    migrated, _ = remote.load()
    delivered = deepcopy(migrated['outbox'][key])
    legacy = deepcopy(migrated)
    legacy.pop('operation_schema_version')
    legacy.pop('operation_migration')
    for row in legacy['outbox'].values():
        row.pop('operations')
    legacy['generation'] += 1
    old_remote = MemoryRemote(legacy)
    before_baseline = deepcopy(old_remote.state['observation_state']['baseline'])
    before_fence = old_remote.state['observation_state']['activation_at']
    new_engine = Delivery(old_remote, gateway, environment=enabled_env(), clock=lambda: NOW)
    result, _ = new_engine._ensure_operations()
    row = result['outbox'][key]
    assert row['status'] == 'DELIVERED'
    assert row['receipt'] == delivered['receipt']
    assert row['operations'][0]['receipt'] == delivered['receipt']
    assert result['observation_state']['baseline'] == before_baseline
    assert result['observation_state']['activation_at'] == before_fence
    assert result['test_authorizations'] == migrated['test_authorizations']
    assert result['operation_migration']['source_sha256']


def test_timeout_is_ambiguous_and_never_retried():
    remote, gateway, item = MemoryRemote(new_state()), FakeGateway(timeout=True), candidate()
    result = run_poll(enabled_env(), backend=remote, clock=lambda: NOW, source=FakeClient(item), gateway=gateway)
    assert result['ambiguous_this_poll'] == 1
    row = next(row for row in remote.state['outbox'].values() if row['event_type'] == 'INITIAL')
    assert row['status'] == 'AMBIGUOUS'
    run_poll(enabled_env(), backend=MemoryRemote(remote.state), clock=lambda: '2026-10-25T08:31:00+00:00',
             source=FakeClient(item), gateway=gateway)
    assert len(gateway.posts) == 1


def test_production_credentials_are_explicit_and_never_fall_back_to_experiment():
    env = {'TELEGRAM_BOT_TOKEN': TOKEN, 'TELEGRAM_CHAT_ID': TARGET,
        'PRODUCTION_TELEGRAM_CHAT_ID_SHA256': hashlib.sha256(TARGET.encode()).hexdigest(),
        'YOUTUBE_EXPERIMENT_BOT_TOKEN': 'EXPERIMENT_TOKEN_ONLY',
        'YOUTUBE_EXPERIMENT_CHAT_ID': '-1000000000000',
        'YOUTUBE_EXPERIMENT_CHAT_ID_SHA256': '0' * 64}
    credentials = Credentials.from_environment(env)
    assert credentials.token == TOKEN and credentials.chat_id == TARGET
    with pytest.raises(StateError):
        Credentials.from_environment({'YOUTUBE_EXPERIMENT_BOT_TOKEN': 'EXPERIMENT_TOKEN_ONLY',
            'YOUTUBE_EXPERIMENT_CHAT_ID': TARGET,
            'YOUTUBE_EXPERIMENT_CHAT_ID_SHA256': hashlib.sha256(TARGET.encode()).hexdigest()})


def test_runtime_requires_production_source_gate_before_state_access():
    backend = Mock()
    env = {'GITHUB_ACTIONS': 'true', 'GITHUB_EVENT_NAME': 'workflow_dispatch',
        'GITHUB_REPOSITORY': REPOSITORY, 'GITHUB_REF': 'refs/heads/master',
        'GITHUB_SHA': 'a' * 40, 'KEMDIKTISAINTEK_PRODUCTION_REVIEWED_SHA': 'a' * 40,
        'KEMDIKTISAINTEK_PRODUCTION_APPROVED_SHA': 'a' * 40}
    with pytest.raises(StateError, match='PRODUCTION_SOURCE_SWITCH_OFF'):
        run('POLL', env, backend=backend)
    backend.load.assert_not_called()


@pytest.mark.parametrize('kwargs', [
    {'repository': 'other/repo'}, {'branch': 'youtube-state'},
    {'path': '.bot-state/youtube_delivery_state.json'},
])
def test_remote_backend_rejects_nonallowlisted_scope(kwargs):
    with pytest.raises(StateError, match='REMOTE_SCOPE_OR_TOKEN_INVALID'):
        RemoteState('not-a-real-token', **kwargs)


def _github_response(status, body=None):
    response = Mock(status_code=status)
    response.json.return_value = body
    return response


def _remote_content(state):
    raw = canonical(state)
    return {'path': STATE_PATH, 'encoding': 'base64',
            'content': base64.b64encode(raw).decode('ascii'), 'sha': blob_sha(raw)}


def test_remote_initialize_creates_only_allowlisted_absent_file():
    state = new_state()
    response = _github_response(201, {'content': {'sha': blob_sha(canonical(state))}})
    session = Mock()
    session.get.side_effect = [
        _github_response(200, {'ref': 'refs/heads/' + STATE_BRANCH}),
        _github_response(404),
    ]
    session.put.return_value = response
    remote = RemoteState(TOKEN, session=session)

    remote.create(state, approved=True)

    assert session.get.call_args_list[0].args[0].endswith('/git/ref/heads/' + STATE_BRANCH)
    assert session.get.call_args_list[1].args[0].endswith(
        '/contents/' + STATE_PATH + '?ref=' + STATE_BRANCH)
    assert session.put.call_count == 1
    assert session.put.call_args.args[0].endswith('/contents/' + STATE_PATH)
    assert session.put.call_args.kwargs['json']['branch'] == STATE_BRANCH
    assert 'sha' not in session.put.call_args.kwargs['json']


def test_remote_initialize_refuses_existing_state_without_put():
    state = new_state()
    session = Mock()
    session.get.side_effect = [
        _github_response(200, {'ref': 'refs/heads/' + STATE_BRANCH}),
        _github_response(200, _remote_content(state)),
    ]
    remote = RemoteState(TOKEN, session=session)

    with pytest.raises(StateError, match='REMOTE_INITIALIZATION_REFUSES_OVERWRITE'):
        remote.create(state, approved=True)

    session.put.assert_not_called()


def test_remote_initialize_fails_closed_on_wrong_ref_or_contents_error():
    for ref, content_status in [
        ({'ref': 'refs/heads/master'}, 404),
        ({'ref': 'refs/heads/' + STATE_BRANCH}, 403),
    ]:
        session = Mock()
        session.get.side_effect = [
            _github_response(200, ref), _github_response(content_status),
        ]
        remote = RemoteState(TOKEN, session=session)
        with pytest.raises(StateError):
            remote.create(new_state(), approved=True)
        session.put.assert_not_called()


def test_remote_checkpoint_sha_mismatch_prevents_put():
    state = new_state()
    session = Mock()
    session.get.return_value = _github_response(200, _remote_content(state))
    remote = RemoteState(TOKEN, session=session)

    with pytest.raises(StateConflict, match='REMOTE_CAS_CONFLICT'):
        remote.checkpoint(state, '0' * 40)

    session.put.assert_not_called()


def test_remote_checkpoint_api_cas_conflict_is_terminal():
    state = new_state()
    session = Mock()
    session.put.return_value = _github_response(409)
    remote = RemoteState(TOKEN, session=session)

    with pytest.raises(StateConflict, match='REMOTE_CAS_CONFLICT'):
        remote._put(state, blob_sha(canonical(state)))

    assert session.put.call_count == 1


def test_production_workflow_is_manual_and_all_delivery_gates_default_off():
    content = (ROOT / '.github' / 'workflows' / 'kemdiktisaintek-production.yml').read_text(encoding='utf-8')
    assert 'workflow_dispatch:' in content and '\n  schedule:' not in content
    assert "KEMDIKTISAINTEK_PRODUCTION_ENABLED || 'false'" in content
    assert "KEMDIKTISAINTEK_PRODUCTION_AUTO_SEND || 'false'" in content
    assert 'dikti-production-state' in content
    assert 'persist-credentials: false' in content
