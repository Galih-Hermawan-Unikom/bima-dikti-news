"""Deterministic offline transports. Never connect to GitHub or Telegram.

Article shape derives from Stage 1 samples; UUIDs, dates, API HTML and tokens
below are synthetic fixtures, not current announcements or credentials.
"""
import base64
from copy import deepcopy
from html import escape
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import uuid5, NAMESPACE_URL

import requests

from kemdiktisaintek_parser import BASE
from kemdiktisaintek_monitor import capture_window, initialize
from kemdiktisaintek_state import canonical, fingerprint, STATE_PATH
from kemdiktisaintek_delivery_state import migrate_v1
from kemdiktisaintek_remote_state import RemoteState, blob_sha
from kemdiktisaintek_telegram import Credentials, TelegramGateway
from kemdiktisaintek_delivery import observe, Delivery

AT = '2026-10-24T10:00:00Z'
OBSERVED = '2026-10-24T10:10:00Z'
NOW = '2026-10-24T10:11:00Z'
TARGET = '-1001234567890'


class Response:
    def __init__(self, status, result):
        self.status_code, self.result = status, result

    def json(self):
        return deepcopy(self.result)


class Clock:
    def __init__(self, value=NOW):
        self.value = value

    def __call__(self):
        return self.value


class FixtureSource:
    def __init__(self, *, historical=False, irrelevant=False, new_published='2026-10-24T10:05:00Z'):
        self.include_new = False
        self.revised = False
        self.historical, self.irrelevant = historical, irrelevant
        self.new_published = new_published
        self.known_id = str(uuid5(NAMESPACE_URL, 'stage3:known'))
        self.new_id = str(uuid5(NAMESPACE_URL, 'stage3:new'))

    def row(self, known):
        return {'id': self.known_id if known else self.new_id, 'slug': 'fixture-known' if known else 'fixture-new',
            'title': 'Seleksi Kepala Tata Usaha SMA Unggul Garuda' if self.irrelevant and not known
                     else 'Penawaran Hibah Penelitian untuk Dosen', 'category': 'Umum',
            'created_at': '2026-10-23T10:00:00Z' if known else '2026-10-24T10:05:00Z',
            'updated_at': '2026-10-23T10:00:00Z' if known or self.historical else self.new_published, 'tags': []}

    def fetch(self, url):
        if '/api/announcement?' in url:
            assert 'category=' not in url
            rows = [self.row(True)] + ([self.row(False)] if self.include_new else [])
            return json.dumps({'data': rows, 'total': len(rows)})
        known = url.endswith('/fixture-known')
        row = self.row(known)
        detail = {**row, 'datetime': row['updated_at'], 'source': '/fixture.pdf'}
        body = 'Perguruan tinggi membuka pendaftaran pendanaan riset bagi dosen dan peneliti.'
        if known and self.revised:
            body += ' Paling lambat 30 Oktober 2026. Dokumen persyaratan direvisi.'
        return '<div><h1>' + escape(row['title']) + '</h1><article class="rich-content">' + body + \
            '</article><a href="/fixture.pdf">PDF</a></div><script id="__NEXT_DATA__">' + \
            json.dumps({'props': {'pageProps': {'data': detail}}}) + '</script>'


class FixtureGitHub:
    def __init__(self, state):
        self.raw = canonical(state)
        self.sha = blob_sha(self.raw)
        self.put_count = 0
        self.fail_put = None
        self.unknown_ack = None
        self.history = []

    def get(self, url, **kwargs):
        assert kwargs.get('allow_redirects') is False
        assert 'contents/' + STATE_PATH + '?ref=kemdiktisaintek-experiment-state' in url
        return Response(200, {'encoding': 'base64', 'path': STATE_PATH, 'sha': self.sha,
                               'content': base64.b64encode(self.raw).decode()})

    def put(self, url, *, json, **kwargs):
        assert kwargs.get('allow_redirects') is False
        assert json['branch'] == 'kemdiktisaintek-experiment-state'
        self.put_count += 1
        if self.put_count == self.fail_put or json.get('sha') != self.sha:
            return Response(409, {})
        raw = base64.b64decode(json['content'])
        self.raw, self.sha = raw, blob_sha(raw)
        self.history.append(deepcopy(__import__('json').loads(raw)))
        if self.put_count == self.unknown_ack:
            raise requests.Timeout('synthetic lost GitHub ACK')
        return Response(200, {'content': {'sha': self.sha}})


class FixtureTelegram:
    def __init__(self):
        self.posts = []
        self.gets = []
        self.bot_id = 73
        self.target = TARGET
        self.production_id = '-1009999999999'
        self.can_post = True
        self.outcome = 'success'
        self.before_post = None

    def get(self, url, *, params=None, **kwargs):
        method = url.rsplit('/', 1)[-1]
        self.gets.append(method)
        if method == 'getMe':
            result = {'id': self.bot_id, 'is_bot': True}
        elif method == 'getChatMember':
            result = {'user': {'id': self.bot_id}, 'status': 'administrator', 'can_post_messages': self.can_post}
        elif params['chat_id'] == '@dikti_news':
            result = {'id': int(self.production_id), 'type': 'channel', 'username': 'dikti_news'}
        else:
            result = {'id': int(self.target), 'type': 'channel', 'title': 'Synthetic experiment'}
        return Response(200, {'ok': True, 'result': result})

    def post(self, url, *, json, **kwargs):
        assert url.endswith('/sendMessage') and kwargs['allow_redirects'] is False
        if self.before_post:
            self.before_post()
        self.posts.append(deepcopy(json))
        if self.outcome == 'timeout':
            raise requests.Timeout('SYNTHETIC_SECRET_MUST_NOT_LEAK')
        if self.outcome == 'http_error':
            return Response(500, {'ok': False})
        if self.outcome == 'ok_false':
            return Response(200, {'ok': False})
        if self.outcome == 'invalid_receipt':
            return Response(200, {'ok': True, 'result': {'message_id': True}})
        return Response(200, {'ok': True, 'result': {'message_id': 101, 'chat': {'id': int(self.target)}}})


def reference(at=OBSERVED):
    return {'schema_version': 1, 'source': 'bima', 'source_url': 'https://bima.kemdiktisaintek.go.id/pengumuman',
            'scope': 'unfiltered_recent', 'healthy': True, 'complete_window': True,
            'captured_at': at, 'coverage_from': '2026-10-22T00:00:00Z', 'coverage_until': at,
            'items': [{'title': 'Program lain yang berbeda sepenuhnya', 'surat': '999/FIXTURE/2026',
                       'date': '23 Oktober 2026', 'documents': []}]}


def setup(*, historical=False, irrelevant=False, observed=OBSERVED, now=NOW, new_published='2026-10-24T10:05:00Z'):
    source = FixtureSource(historical=historical, irrelevant=irrelevant, new_published=new_published)
    class Sink:
        def create(self, state):
            self.state = state
    sink = Sink()
    baseline = initialize(sink, capture_window(source, at=AT))
    github = FixtureGitHub(migrate_v1(baseline, at=AT))
    remote = RemoteState('synthetic-github-credential', session=github)
    source.include_new = True
    ref = reference(observed)
    observe(remote, capture_window(source, baseline, at=observed), reference=ref, reference_sha=fingerprint(ref))
    env = {'YOUTUBE_EXPERIMENT_BOT_TOKEN': '73:synthetic_fixture_only', 'YOUTUBE_EXPERIMENT_CHAT_ID': TARGET,
           'YOUTUBE_EXPERIMENT_CHAT_ID_SHA256': __import__('hashlib').sha256(TARGET.encode()).hexdigest(),
           'KEMDIKTISAINTEK_EXPERIMENT_ENABLED': 'true', 'KEMDIKTISAINTEK_EXPERIMENT_AUTO_SEND': 'true'}
    telegram = FixtureTelegram()
    gateway = TelegramGateway(Credentials.from_environment(env), session=telegram)
    clock = Clock(now)
    engine = Delivery(remote, gateway, environment=env, clock=clock)
    return {'remote': remote, 'github': github, 'source': source, 'reference': ref, 'reference_sha': fingerprint(ref),
            'env': env, 'telegram': telegram, 'gateway': gateway, 'clock': clock, 'engine': engine}
