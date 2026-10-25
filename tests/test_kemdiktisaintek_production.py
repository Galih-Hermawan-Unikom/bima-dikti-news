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
        return ('<html><body><div><h1>' + self.item['title'] + '</h1>'
            '<time>25 Oktober 2026 | 15.00 WIB</time><article class="rich-content">'
            '<p>Pendaftaran hibah penelitian perguruan tinggi dibuka. Dosen dan peneliti dapat mengajukan proposal sesuai panduan resmi.</p>'
            '</article></div><script id="__NEXT_DATA__" type="application/json">' + embedded + '</script></body></html>')


class FakeGateway:
    def __init__(self, *, timeout=False):
        self.credentials = Credentials(TOKEN, TARGET, hashlib.sha256(TARGET.encode()).hexdigest(), 123456789)
        self.posts = []
        self.timeout = timeout

    def inspect(self, *, at):
        return Proof(self.credentials.identity(), at)

    def post_claimed(self, row, proof, *, at):
        assert row['status'] == 'SENDING'
        assert row['destination'] == proof.identity
        self.posts.append(row['event_key'])
        if self.timeout:
            return None, 'TIMEOUT'
        return {'http_status': 200, 'ok': True, 'message_id': 71,
                'destination_sha256': self.credentials.chat_sha256}, None


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


def test_cas_conflict_on_durable_claim_prevents_post():
    remote, gateway = MemoryRemote(new_state(), conflict_on=3), FakeGateway()
    report = run_poll(enabled_env(), backend=remote, clock=lambda: NOW,
                      source=FakeClient(candidate()), gateway=gateway)
    assert gateway.posts == []
    assert report['delivery_error'] == 'MOCK_CAS_CONFLICT'
    assert next(iter(remote.state['outbox'].values()))['status'] == 'RESERVED'


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
