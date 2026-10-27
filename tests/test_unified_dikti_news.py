"""Offline scheduler rehearsal and source-local all-announcement regression."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from unittest.mock import Mock
from uuid import UUID

import pytest
import yaml

import dikti_news_scheduler as scheduler
from kemdiktisaintek_prod_automation import run_poll
from kemdiktisaintek_prod_delivery import Delivery, observe
from kemdiktisaintek_prod_delivery_policy import TIMING, eligibility
from kemdiktisaintek_prod_delivery_state import (OFFICIAL_POLICY, migrate_official_policy,
    validate_delivery_lineage, validate_delivery_state)
from kemdiktisaintek_prod_monitor import CaptureError, capture_window, apply_poll
from kemdiktisaintek_prod_runtime import _review_gate
from kemdiktisaintek_prod_state import StateError, fingerprint
from test_kemdiktisaintek_production import (
    NOW, FakeClient, FakeGateway, MemoryRemote, candidate, enabled_env, new_state)

ROOT = Path(__file__).resolve().parents[1]


def env(**extra):
    result = {'GITHUB_ACTIONS': 'true', 'GITHUB_EVENT_NAME': 'schedule',
        'GITHUB_REPOSITORY': scheduler.REPO, 'GITHUB_REF': 'refs/heads/master',
        'GITHUB_WORKFLOW_REF': f'{scheduler.REPO}/{scheduler.WORKFLOW}@refs/heads/master',
        'GITHUB_SHA': 'a' * 40, 'GITHUB_TOKEN': 'MOCK',
        'DIKTI_NEWS_PRODUCTION_SCHEDULER_ENABLED': 'true',
        'KEMDIKTISAINTEK_PRODUCTION_APPROVED_CODE_SHA': 'a' * 40,
        'SCHEDULE_CRON': scheduler.FULL_CRON}
    for prefix in scheduler.PREFIX.values():
        result[prefix + '_ENABLED'] = 'true'
        result[prefix + '_AUTO_SEND'] = 'false'
    return dict(result, **extra)


HEAD = lambda _: 'a' * 40


@pytest.mark.parametrize('cron,bima', [(scheduler.FULL_CRON, True), (scheduler.LIGHT_CRON, False)])
def test_nominal_event_controls_bima_even_if_runner_delayed(cron, bima):
    result = scheduler.plan(env(SCHEDULE_CRON=cron), read_head=HEAD)
    assert result['bima'] is bima
    assert result['youtube'] is result['kemdiktisaintek'] is True
    assert not any(result[s + '_auto_send'] for s in scheduler.PREFIX)


def test_switch_off_has_no_network_or_runtime_calls():
    read = Mock(side_effect=AssertionError('Network not allowed'))
    result = scheduler.plan(env(DIKTI_NEWS_PRODUCTION_SCHEDULER_ENABLED='false'), read_head=read)
    assert result['status'] == 'OFF'
    read.assert_not_called()


@pytest.mark.parametrize('change', [
    {'GITHUB_REF': 'refs/heads/feature'}, {'GITHUB_REPOSITORY': 'other/repo'},
    {'GITHUB_EVENT_NAME': 'pull_request'}, {'GITHUB_WORKFLOW_REF': 'other.yml'},
    {'KEMDIKTISAINTEK_PRODUCTION_APPROVED_CODE_SHA': 'b' * 40},
    {'SCHEDULE_CRON': '17 * * * *'}, {'BIMA_PRODUCTION_AUTO_SEND': 'TRUE'},
    {'YOUTUBE_PRODUCTION_ENABLED': 'yes'},
])
def test_invalid_scheduler_context_or_switch_fails_closed(change):
    with pytest.raises(scheduler.ScheduleError):
        scheduler.plan(env(**change), read_head=HEAD)


def test_queue_delay_master_change_rejected_before_runtime():
    runtime = Mock()
    with pytest.raises(scheduler.ScheduleError, match='MASTER_HEAD_CHANGED'):
        scheduler.run_source('bima', env(), read_head=lambda _: 'b' * 40, bima_youtube=runtime)
    runtime.assert_not_called()


@pytest.mark.parametrize('source', scheduler.PREFIX)
def test_observation_manual_cannot_enter_any_stateful_runtime(source):
    values = env(GITHUB_EVENT_NAME='workflow_dispatch', MANUAL_SOURCES='all', OBSERVATION_ONLY='true')
    for prefix in scheduler.PREFIX.values():
        values[prefix + '_AUTO_SEND'] = 'true'
    before, process = deepcopy(values), dict(os.environ)
    runtime = Mock(side_effect=AssertionError('Stateful runtime forbidden'))
    selection = scheduler.plan(values, read_head=HEAD)
    assert not any(selection[s] for s in scheduler.PREFIX)
    assert not any(selection[s+'_auto_send'] for s in scheduler.PREFIX)
    with pytest.raises(scheduler.ScheduleError, match='READ_ONLY_REHEARSAL'):
        scheduler.run_source(source, values, read_head=HEAD, bima_youtube=runtime, kemdikti=runtime)
    runtime.assert_not_called()
    assert values == before and dict(os.environ) == process


def test_delivery_mode_does_not_change_youtube_runtime_or_eligibility():
    runtime = Mock(return_value=0)
    scheduler.run_source('youtube', env(YOUTUBE_PRODUCTION_AUTO_SEND='true'),
                         read_head=HEAD, bima_youtube=runtime)
    runtime.assert_called_once_with('youtube', 'run')


def test_one_source_failure_does_not_change_sibling_selection():
    failing, success = Mock(side_effect=RuntimeError('BIMA unavailable')), Mock(return_value=0)
    with pytest.raises(RuntimeError):
        scheduler.run_source('bima', env(), read_head=HEAD, bima_youtube=failing)
    scheduler.run_source('youtube', env(), read_head=HEAD, bima_youtube=success)
    success.assert_called_once()


def test_adapter_preserves_historical_hash_guard(monkeypatch):
    import production_runtime
    runtime = Mock(return_value=0)
    monkeypatch.setattr(production_runtime, 'run', runtime)
    monkeypatch.setattr(production_runtime, 'hashes', Mock(side_effect=[{'cache':'before'}, {'cache':'after'}]))
    with pytest.raises(scheduler.ScheduleError, match='HISTORICAL_MASTER_STATE_CHANGED'):
        scheduler.run_source('youtube', env(), read_head=HEAD)
    runtime.assert_called_once_with('youtube','run')


def test_cli_sanitizes_runtime_failure(monkeypatch, capsys):
    import sys
    monkeypatch.setattr(sys, 'argv', ['scheduler','youtube'])
    monkeypatch.setattr(scheduler, 'run_source', Mock(side_effect=RuntimeError('SECRET_URL_MUST_NOT_LEAK')))
    assert scheduler.main() == 1
    assert 'SECRET_URL_MUST_NOT_LEAK' not in capsys.readouterr().out


def test_unified_workflow_schedule_locks_and_credentials():
    folder = ROOT / '.github/workflows'
    flows = {p.name: yaml.load(p.read_text(encoding='utf-8'), Loader=yaml.BaseLoader)
             for p in folder.glob('*.yml')}
    scheduled = [n for n, f in flows.items() if 'schedule' in f.get('on', {})]
    assert scheduled == ['dikti-news-production.yml']
    flow = flows[scheduled[0]]
    assert [r['cron'] for r in flow['on']['schedule']] == [scheduler.FULL_CRON, scheduler.LIGHT_CRON]
    # Two disjoint partitions: 8 full + 16 light = 24, never 32 overlapping runs.
    full = set(range(0, 24, 3))
    light = set(range(1, 24, 3)) | set(range(2, 24, 3))
    assert full.isdisjoint(light) and full | light == set(range(24))
    assert len(full) == 8 and len(light) == 16
    assert 'concurrency' not in flow  # Parent cannot own a lock awaited by its jobs.
    assert 'actions' not in flow['permissions']
    assert 'secrets.' not in json.dumps(flow['jobs']['plan'])
    assert flow['permissions'] == {'contents': 'read'}
    assert 'read-only' in json.dumps(flow['jobs']['plan'])
    for source in scheduler.PREFIX:
        job = flow['jobs'][source]
        assert job['needs'] == 'plan'  # No sibling failure propagation.
        assert job['concurrency'] == {'group': 'dikti-production-state',
            'cancel-in-progress': 'false', 'queue': 'max'}
        assert job['permissions'] == {'contents': 'write'}
        assert job['steps'][0]['with']['ref'] == '${{ needs.plan.outputs.sha }}'
        assert job['steps'][0]['with']['persist-credentials'] == 'false'
        assert not any('workflow run' in s.get('run', '') for s in job['steps'])
    kem = flow['jobs']['kemdiktisaintek']['steps'][-1]['env']
    assert "kemdiktisaintek_auto_send == 'true'" in kem['TELEGRAM_BOT_TOKEN']
    for name in ('main.yml', 'youtube-production.yml', 'kemdiktisaintek-production.yml',
                 'kemdiktisaintek-production-scheduler.yml', 'kemdiktisaintek-scheduler.yml'):
        assert set(flows[name]['on']) == {'workflow_dispatch'}
        assert 'DIKTI_NEWS_PRODUCTION_SCHEDULER_ENABLED' in json.dumps(flows[name])


def test_scheduled_kemdikti_gate_requires_exact_unified_workflow():
    values = env(KEMDIKTISAINTEK_PRODUCTION_REVIEWED_SHA='a' * 40,
                 KEMDIKTISAINTEK_PRODUCTION_APPROVED_SHA='a' * 40)
    _review_gate(values)
    with pytest.raises(StateError):
        _review_gate(dict(values, GITHUB_WORKFLOW_REF='other.yml'))
    with pytest.raises(StateError):
        _review_gate(values, initialize_mode=True)


def captured(item=None, at=NOW):
    return capture_window(FakeClient(item or candidate()), at=at)


def test_all_official_topics_eligible_with_diagnostic_relevance_only():
    item = candidate()
    item.update(title='Seleksi Tata Usaha Sekolah Tahun 2026', category='Administrasi',
                body_text='Informasi resmi seleksi tata usaha sekolah. Pendaftaran sesuai dokumen terlampir.')
    remote, gateway = MemoryRemote(new_state()), FakeGateway()
    result = run_poll(enabled_env(), backend=remote, source=FakeClient(item), gateway=gateway, clock=lambda: NOW)
    row = remote.state['observation_state']['observations'][item['id']]
    assert row['classification']['decision'] == 'NOT_RELEVANT'
    assert result['status_counts']['NEW_CANDIDATE'] == 1
    assert result['delivered_this_poll'] == 1 and len(gateway.posts) == 1


@pytest.mark.parametrize('age_hours,accepted', [(47, True), (71, True), (72, True), (73, False)])
def test_publication_window_independent_from_poll_gap(age_hours, accepted):
    at = (datetime(2026,10,25,8,tzinfo=timezone.utc) + timedelta(hours=age_hours)).isoformat()
    previous = new_state()['observation_state']
    previous['last_successful_poll_at'] = (datetime.fromisoformat(at)-timedelta(hours=1)).isoformat()
    _, report = apply_poll(previous, captured(at=at))
    event = report['events'][0]
    assert (event['status'] == 'NEW_CANDIDATE') is accepted
    if not accepted:
        assert 'HISTORICAL_OUTSIDE_NEW_WINDOW' in event['reasons']


@pytest.mark.parametrize('gap,accepted', [(1, True), (3, True), (8, True), (12, True), (13, False)])
def test_missed_crons_need_overlap_and_bounded_gap(gap, accepted):
    at = '2026-10-26T08:00:00+00:00'
    previous = new_state()['observation_state']
    previous['last_successful_poll_at'] = (datetime.fromisoformat(at)-timedelta(hours=gap)).isoformat()
    _, report = apply_poll(previous, captured(at=at))
    assert (report['events'][0]['status'] == 'NEW_CANDIDATE') is accepted
    assert ('UNHEALTHY_POLL_GAP' in report['capture_warnings']) is not accepted


def test_old_hold_is_never_promoted_after_gap_recovers():
    item = candidate()
    first = captured(item, '2026-10-26T08:00:00+00:00')
    monitor, report = apply_poll(new_state()['observation_state'], first)
    assert report['events'][0]['status'] == 'HOLD'
    monitor, report = apply_poll(monitor, captured(item, '2026-10-26T09:00:00+00:00'))
    assert report['events'][0]['status'] == 'HOLD'
    assert monitor['observations'][item['id']]['first_seen_at'] == first.observed_at


def test_migration_preserves_test_receipt_admissions_and_observations():
    remote, gateway = MemoryRemote(new_state()), FakeGateway()
    engine = Delivery(remote, gateway, environment=enabled_env(), clock=lambda: NOW)
    proof = gateway.inspect(at=NOW)
    key = engine.reserve_test('KDTS-TEST-PRESERVED-01', proof, approval='SEND_ONE_EXPERIMENT_TEST')
    engine.send(key, proof)
    old = deepcopy(remote.state)
    new = migrate_official_policy(old, at=NOW)
    validate_delivery_lineage(old, new)
    assert new['policy_version'] == OFFICIAL_POLICY
    assert new['official_policy_migration']['source_sha256'] == fingerprint(old)
    for field in ('observation_state','admissions','outbox','test_authorizations','migration','operation_migration'):
        assert new[field] == old[field]
    assert new['outbox'][key]['status'] == 'DELIVERED'
    tampered = deepcopy(new)
    tampered['outbox'][key]['payload']['text'] += 'changed'
    with pytest.raises(StateError):
        validate_delivery_lineage(old, tampered)
    with pytest.raises(StateError):
        migrate_official_policy(new, at=NOW)


class Dataset:
    def __init__(self, records):
        self.records, self.calls = records, []
        self.move_on_recheck = False

    def fetch(self, url):
        self.calls.append(url)
        if '/api/announcement?' in url:
            from urllib.parse import parse_qs, urlsplit
            query = parse_qs(urlsplit(url).query, keep_blank_values=True)
            assert query['search'] == [''] and set(query) == {'page','size','search'}
            page = int(query['page'][0])
            rows = self.records[(page-1)*10:page*10]
            if self.move_on_recheck and self.calls.count(url) > 1:
                rows = rows[::-1]
            return json.dumps({'data': rows, 'total': len(self.records)})
        item = next(r for r in self.records if url.endswith('/'+r['slug']))
        return FakeClient(item).fetch(url)


def records(count):
    return [{'id': str(UUID(int=i+1)), 'slug': f'pengumuman-{i}', 'title': f'Pengumuman resmi {i}',
             'category': 'Umum', 'created_at': (datetime(2026,10,25,8,tzinfo=timezone.utc)-timedelta(minutes=i)).isoformat()}
            for i in range(count)]


def test_adaptive_scan_extends_past_page_two_and_hydrates_every_new_uuid():
    rows = records(50)
    initial_capture = capture_window(Dataset(rows[25:]), at='2026-10-25T07:50:00+00:00', pages=3)
    from kemdiktisaintek_prod_monitor import initialize
    initial = initialize(Mock(), initial_capture)
    client = Dataset(rows)
    cap = capture_window(client, initial, at=NOW, detail_budget=100, adaptive=True)
    assert cap.coverage['captured_pages'] == 4
    assert cap.coverage['boundary_reached'] and cap.coverage['listing_order_valid']
    new_items = [i for i in cap.items if i['id'] not in initial['observations']]
    assert len(new_items) == 25 and all(i.get('body_text') for i in new_items)
    assert cap.coverage['detail_count'] == 28  # New IDs plus 3 rotating known details.


def test_scan_cap_and_unknown_order_hold_instead_of_assuming_completeness():
    client = Dataset(records(110))
    cap = capture_window(client, new_state()['observation_state'], at=NOW, detail_budget=100, adaptive=True)
    assert cap.coverage['captured_pages'] == 10 and cap.coverage['boundary_reached'] is False
    assert len(cap.items) == 100 and cap.coverage['detail_count'] == 100
    _, report = apply_poll(new_state()['observation_state'], cap)
    assert {e['status'] for e in report['events']} == {'HOLD'}
    assert 'CAPTURE_WINDOW_LIMIT_REACHED' in report['capture_warnings']
    rows = records(3)[::-1]
    cap = capture_window(Dataset(rows), new_state()['observation_state'], at=NOW, detail_budget=100, adaptive=True)
    _, report = apply_poll(new_state()['observation_state'], cap)
    assert 'LISTING_ORDER_UNVERIFIED' in report['capture_warnings']


def test_page_shift_recheck_and_partial_response_fail_capture():
    client = Dataset(records(3)); client.move_on_recheck = True
    with pytest.raises(CaptureError, match='PAGINATION_CHANGED'):
        capture_window(client, new_state()['observation_state'], at=NOW, detail_budget=100, adaptive=True)
    class Partial(Dataset):
        def fetch(self, url):
            raw = json.loads(super().fetch(url))
            raw['data'] = raw['data'][:-1]
            return json.dumps(raw)
    with pytest.raises(CaptureError, match='PARTIAL_API_PAGE'):
        capture_window(Partial(records(20)), new_state()['observation_state'], at=NOW, adaptive=True)


def test_two_new_announcements_once_and_revision_never_resent():
    remote, gateway = MemoryRemote(new_state()), FakeGateway()
    rows = records(2)
    first = run_poll(enabled_env(), backend=remote, source=Dataset(rows), gateway=gateway, clock=lambda: NOW)
    assert first['delivered_this_poll'] == 2 and len(set(gateway.posts)) == 2
    restart = MemoryRemote(remote.state)
    rows[0]['title'] += ' direvisi'
    second = run_poll(enabled_env(), backend=restart, source=Dataset(rows), gateway=gateway,
                      clock=lambda: '2026-10-25T09:00:00+00:00')
    assert second['delivered_this_poll'] == 0 and len(gateway.posts) == 2
    assert restart.state['observation_state']['revisions']


@pytest.mark.parametrize('value', ['false','TRUE','yes',''])
def test_auto_send_off_or_invalid_never_posts(value):
    remote, gateway = MemoryRemote(new_state()), FakeGateway()
    run_poll(enabled_env(KEMDIKTISAINTEK_PRODUCTION_AUTO_SEND=value), backend=remote,
             source=FakeClient(candidate()), gateway=gateway, clock=lambda: NOW)
    assert gateway.posts == []


def test_fresh_admission_and_evidence_do_not_expand_to_publication_window():
    remote = MemoryRemote(new_state())
    item = candidate()
    observe(remote, captured(item))
    assert TIMING.publication_window == timedelta(hours=72)
    assert TIMING.first_seen_ttl == timedelta(hours=24)
    assert TIMING.evidence_freshness == timedelta(minutes=10)
    with pytest.raises(StateError):
        eligibility(remote.state, item['id'], at='2026-10-25T08:41:00+00:00')


def test_real_bridging_fixture_new_vs_known_hold_and_historical():
    fixture = json.loads((ROOT/'tests/fixtures/kemdiktisaintek_bridging_2026.json').read_text(encoding='utf-8'))
    item, at = fixture['item'], fixture['observed_at_utc']
    assert item['id'] == '2407f6f4-1e06-452b-a053-864426e9a183'
    assert item['deadline_evidence'] == ['paling lambat 06 November 2026']
    # Reuse validated offline capture metadata while retaining the real parsed item.
    cap = captured(at=at)
    cap.items = [deepcopy(item)]
    state = new_state()['observation_state']
    state['last_successful_poll_at'] = (datetime.fromisoformat(at)-timedelta(hours=4)).isoformat()
    after, report = apply_poll(state, cap)
    assert report['events'][0]['status'] == 'NEW_CANDIDATE'
    row = after['observations'][item['id']]
    row.update(detection_status='HOLD', reasons=['UNHEALTHY_POLL_GAP'])
    later = (datetime.fromisoformat(at)+timedelta(hours=1)).isoformat()
    cap.observed_at = later
    _, report = apply_poll(after, cap)
    assert report['events'][0]['status'] == 'HOLD'
    historical = (datetime.fromisoformat(item['published_at'])+timedelta(hours=73)).astimezone(timezone.utc)
    cap.observed_at = historical.isoformat()
    state['last_successful_poll_at'] = (historical-timedelta(hours=1)).isoformat()
    _, report = apply_poll(state, cap)
    assert 'HISTORICAL_OUTSIDE_NEW_WINDOW' in report['events'][0]['reasons']


def test_two_concurrent_observers_cannot_both_checkpoint_same_blob():
    remote = MemoryRemote(new_state())
    old, sha = remote.load()
    first = migrate_official_policy(old, at=NOW)
    remote.checkpoint(first, sha)
    from kemdiktisaintek_prod_state import StateConflict
    with pytest.raises(StateConflict):
        remote.checkpoint(migrate_official_policy(old, at=NOW), sha)
    assert remote.state['outbox'] == {}


def test_lost_overlap_and_duplicate_uuid_never_admit_new_article():
    first_item = candidate()
    initial, _ = apply_poll(new_state()['observation_state'], captured(first_item))
    at = '2026-10-25T09:00:00+00:00'
    cap = captured(candidate(), at)
    cap.duplicate_ids = [cap.items[0]['id']]
    _, result = apply_poll(initial, cap)
    assert result['events'][0]['status'] == 'HOLD'
    assert set(result['capture_warnings']) == {'WINDOW_OVERLAP_LOST','PAGINATION_SHIFT_DUPLICATES'}


def test_snapshot_rehearsal_allowed_before_scheduler_activation():
    values = env(GITHUB_EVENT_NAME='workflow_dispatch', OBSERVATION_ONLY='true',
                 DIKTI_NEWS_PRODUCTION_SCHEDULER_ENABLED='false',
                 BIMA_PRODUCTION_ENABLED='false')
    result = scheduler.plan(values, read_head=HEAD)
    assert result['rehearsal_sources'] == 'bima,youtube,kemdiktisaintek'
    assert not any(result[source] for source in scheduler.PREFIX)
    with pytest.raises(scheduler.ScheduleError, match='APPROVED_MASTER_SHA_MISMATCH'):
        scheduler.plan(dict(values,KEMDIKTISAINTEK_PRODUCTION_APPROVED_CODE_SHA='b'*40),read_head=HEAD)


def test_snapshot_reader_only_gets_allowlisted_file_with_valid_blob(monkeypatch):
    import base64
    from kemdiktisaintek_prod_remote_state import blob_sha
    from kemdiktisaintek_prod_state import canonical
    state = new_state()
    raw = canonical(state)
    response = Mock(status=200)
    response.__enter__ = Mock(return_value=response)
    response.__exit__ = Mock(return_value=False)
    response.read.return_value = json.dumps({'path':'.bot-state/kemdiktisaintek_production.json',
        'encoding':'base64','sha':blob_sha(raw),'content':base64.b64encode(raw).decode()})
    request = Mock(return_value=response)
    monkeypatch.setattr(scheduler,'urlopen',request)
    loaded, sha = scheduler.read_snapshot('kemdiktisaintek',env())
    assert loaded == state and sha == blob_sha(raw)
    sent = request.call_args.args[0]
    assert sent.get_method() == 'GET'
    assert sent.full_url.endswith('/contents/.bot-state/kemdiktisaintek_production.json?ref=dikti-production-state')
    response.read.return_value = response.read.return_value.replace(blob_sha(raw),'0'*40)
    with pytest.raises(scheduler.ScheduleError,match='READ_ONLY_SNAPSHOT_FAILED'):
        scheduler.read_snapshot('kemdiktisaintek',env())
    with pytest.raises(scheduler.ScheduleError,match='SNAPSHOT_SCOPE'):
        scheduler.read_snapshot('../bima',env())


def test_readonly_bridging_rehearsal_keeps_candidate_for_real_poll_once():
    fixture = json.loads((ROOT/'tests/fixtures/kemdiktisaintek_bridging_2026.json').read_text(encoding='utf-8'))
    item, at = fixture['item'], fixture['observed_at_utc']
    state = new_state()
    state['observation_state']['last_successful_poll_at'] = (datetime.fromisoformat(at)-timedelta(hours=1)).isoformat()
    before = deepcopy(state)
    cap = captured(at=at); cap.items = [deepcopy(item)]
    for _ in range(2):
        report = scheduler.rehearse_snapshot('kemdiktisaintek',state,'a'*40,capture=cap)
        assert report['simulated_events'][0]['status'] == 'NEW_CANDIDATE'
        assert report['remote_writes'] == report['telegram_posts'] == 0
        assert state == before and state['admissions'] == state['outbox'] == {}
        assert state['policy_version'] != OFFICIAL_POLICY  # No production migration.
    remote, gateway = MemoryRemote(state), FakeGateway()
    observe(remote,cap)  # First authorized processing still admits original UUID.
    engine = Delivery(remote,gateway,environment=enabled_env(),clock=lambda: at)
    proof = gateway.inspect(at=at)
    key = engine.reserve_initial(item['id'],proof)
    assert engine.send(key,proof)['status'] == 'DELIVERED'
    count = len(gateway.posts)
    assert engine.send(key,proof)['post_attempted'] is False
    assert len(gateway.posts) == count


def test_stateful_observation_would_consume_bridging_but_old_hold_stays_hold():
    fixture = json.loads((ROOT/'tests/fixtures/kemdiktisaintek_bridging_2026.json').read_text(encoding='utf-8'))
    item, at = fixture['item'], fixture['observed_at_utc']
    state = new_state()
    state['observation_state']['last_successful_poll_at'] = (datetime.fromisoformat(at)-timedelta(hours=1)).isoformat()
    cap = captured(at=at); cap.items = [deepcopy(item)]
    remote = MemoryRemote(state)
    assert observe(remote,cap)['events'][0]['status'] == 'NEW_CANDIDATE'
    cap.observed_at = (datetime.fromisoformat(at)+timedelta(minutes=5)).isoformat()
    assert observe(remote,cap)['events'][0]['status'] == 'KNOWN'
    assert remote.state['outbox'] == {}  # Original cutover procedure could lose opportunity.
    held = deepcopy(remote.state)
    held['observation_state']['observations'][item['id']].update(
        detection_status='HOLD',initial_decision='HOLD',reasons=['UNHEALTHY_POLL_GAP'])
    before = deepcopy(held)
    cap.observed_at = (datetime.fromisoformat(at)+timedelta(minutes=6)).isoformat()
    report = scheduler.rehearse_snapshot('kemdiktisaintek',held,'a'*40,capture=cap)
    assert report['simulated_events'][0]['status'] == 'HOLD'
    assert held == before


def test_bima_snapshot_rehearsal_does_not_start_first_seen_ttl():
    import production_runtime as prod
    from test_production_runtime import bima_state, fake_notifier, announcement, NOW as BIMA_NOW
    state = bima_state(); before = deepcopy(state)
    scheduler.rehearse_snapshot('bima',state,'a'*40)
    assert state == before and state['first_seen'] == state['outbox'] == {}
    notifier = fake_notifier()
    report = prod.bima_cycle(state,[announcement()],[],notifier,lambda:None,enabled=True,at=BIMA_NOW)
    assert report['delivered_events'] == 1
    prod.bima_cycle(state,[announcement()],[],notifier,lambda:None,enabled=True,at=BIMA_NOW)
    assert notifier.send_notification.call_count == 1


def test_youtube_snapshot_rehearsal_preserves_capture_and_delivery_eligibility(tmp_path):
    import production_runtime as prod
    from test_production_runtime import yt_setup
    store, adapter, notifier, _ = yt_setup(tmp_path)
    before, file_before = deepcopy(store._state), store.path.read_bytes()
    scheduler.rehearse_snapshot('youtube',store._state,'a'*40)
    assert store._state == before and store.path.read_bytes() == file_before
    assert store._state['capture_seq'] == 0 and store._state['observations'] == {}
    def send(item,ticket):
        notifier.last_delivery_receipts = [{'http_status':200,'ok':True,'message_id':99}]
        return True
    notifier.send_youtube_production_notification.side_effect = send
    assert prod.youtube_cycle(store,adapter,[],{},notifier,enabled=True,startup=False)['delivered_events'] == 1
    prod.youtube_cycle(store,adapter,[],{},notifier,enabled=True,startup=False)
    assert notifier.send_youtube_production_notification.call_count == 1


def test_cli_rehearsal_writes_only_false_source_outputs(monkeypatch,tmp_path,capsys):
    import sys
    values = env(GITHUB_EVENT_NAME='workflow_dispatch',OBSERVATION_ONLY='true',
                 GITHUB_OUTPUT=str(tmp_path/'outputs'))
    monkeypatch.setattr(os,'environ',values)
    monkeypatch.setattr(sys,'argv',['scheduler','plan'])
    original = scheduler.plan
    monkeypatch.setattr(scheduler,'plan',lambda values:original(values,read_head=HEAD))
    rehearse = Mock(return_value=[{'source':'bima','remote_writes':0,'telegram_posts':0}])
    monkeypatch.setattr(scheduler,'read_only_rehearsal',rehearse)
    assert scheduler.main() == 0
    output = (tmp_path/'outputs').read_text()
    for source in scheduler.PREFIX:
        assert source+'=false\n' in output
    assert 'rehearsal=' not in output  # Nested report is log-only, never shell output.
    assert 'snapshot' not in output
