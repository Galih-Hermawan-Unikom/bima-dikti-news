"""Two-tab acquisition, lifecycle, signed review and runner integration; no network."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime,timedelta,timezone
import hashlib,hmac,json,runpy,sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
import youtube_channel_tabs as tabs
import youtube_eligibility as e
from youtube_tabs_monitor import ChannelTabsAdapter,history_fingerprint
from youtube_review_evidence import SignedReviewVerifier
from monitor_runner import run_once
from reliability import atomic_json

VID='video000001'
NOW=datetime(2026,10,22,2,tzinfo=timezone.utc)
KEY=b'synthetic-test-only-review-key!!!'
ROOT=Path(__file__).resolve().parents[1]


def page(tab,ids):
    return tabs.ChannelTabPage(tab,[{'source':'youtube','observation_format':tabs.FORMAT,'video_id':v,
        'title':'Synthetic video','channel_title':'Fixture channel','url':'https://www.youtube.com/watch?v='+v,
        'thumbnail':'','source_tabs':[tab],'source_metadata':{'relative_text':['Streaming 1 hour ago'],
        'badges':['LIVE'],'schedule_timezone':None}} for v in ids],False,empty_verified=not ids)


def sign(payload):
    raw=json.dumps(payload,sort_keys=True,separators=(',',':'),ensure_ascii=True).encode()
    return {'key_id':'fixture','payload':deepcopy(payload),'signature':hmac.new(KEY,raw,hashlib.sha256).hexdigest()}


@pytest.fixture
def harness(monkeypatch):
    clock=[NOW];present={'videos':[],'streams':[]};reader=Mock()
    monkeypatch.setattr(tabs,'datetime',SimpleNamespace(now=lambda tz:clock[0]))
    reader.fetch.side_effect=lambda t:page(t,present[t])
    state={'version':1,'batches':[],'youtube_rss_cutover':{'version':1,'cutover_at':'2026-10-21T01:32:44Z','baseline_ids':[]}}
    policy=e.Policy(tabs.CHANNEL_ID,'test-tabs-v1',activation_at=NOW-timedelta(hours=2),window=timedelta(hours=24),
        ttl=timedelta(minutes=30),skew=timedelta(minutes=5),capture_interval=timedelta(minutes=10),approved=True)
    verifier=SignedReviewVerifier({'fixture':KEY},frozenset({'www.youtube.com'}))
    adapter=ChannelTabsAdapter(tabs_reader=reader,policy=policy,review_verifier=verifier,clock=lambda:clock[0])
    return SimpleNamespace(clock=clock,present=present,reader=reader,state=state,cache=[],adapter=adapter,policy=policy,verifier=verifier)


def d(result):return next(v for v in result.decisions if v.video_id==VID)


def review(h,category='REGULAR',readiness='PUBLIC_RELEASED'):
    h.payload={'schema':'operator-youtube-tabs-review-v1','video_id':VID,'channel_id':tabs.CHANNEL_ID,
        'scope':h.policy.scope,'policy_version':h.policy.version,'state_fingerprint':history_fingerprint(h.cache,h.state),
        'category':category,'readiness':readiness,'source_url':'https://www.youtube.com/watch?v='+VID,
        'reviewer':'SYNTHETIC_ONLY','history_review_reference':'test-history','observed_at':h.clock[0].isoformat(),
        'expires_at':(h.clock[0]+timedelta(hours=1)).isoformat(),'ended_at':(h.clock[0]-timedelta(minutes=1)).isoformat() if readiness=='COMPLETED_REPLAY' else None,
        'first_seen_at':h.adapter._first_seen[VID].isoformat(),'approved':True}
    h.adapter.reviews[VID]=sign(h.payload)


def discover(h,category='REGULAR',readiness='PUBLIC_RELEASED'):
    h.adapter.evaluate(h.cache,h.state)  # Healthy empty startup inventory.
    h.clock[0]+=timedelta(minutes=12)
    h.present['streams' if category=='STREAM' else 'videos']=[VID]
    first=h.adapter.evaluate(h.cache,h.state)
    review(h,category,readiness)
    h.clock[0]+=timedelta(minutes=12)
    return first,h.adapter.evaluate(h.cache,h.state)


def test_arrival_after_activation_and_two_captures_is_preview_only(harness):
    original=deepcopy((harness.cache,harness.state))
    first,last=discover(harness)
    assert d(first).eligibility is e.Eligibility.HOLD_FOR_REVIEW
    assert d(last).eligibility is e.Eligibility.ELIGIBLE
    assert not d(last).enqueue and not d(last).delivery_authorized
    assert last.preview_items[0]['first_seen_at']==(NOW+timedelta(minutes=12)).isoformat()
    assert not {'date','published','published_at'} & last.preview_items[0].keys()
    assert original==(harness.cache,harness.state)
    assert [c.args[0] for c in harness.reader.fetch.call_args_list]==['videos','streams']*3


def test_startup_inventory_cannot_gain_admission_by_waiting_or_review(harness):
    h=harness;h.present['videos']=[VID];h.adapter.evaluate(h.cache,h.state);review(h)
    for _ in range(3):
        h.clock[0]+=timedelta(minutes=12)
        assert d(h.adapter.evaluate(h.cache,h.state)).eligibility is e.Eligibility.HOLD_FOR_REVIEW
    assert h.adapter._first_seen[VID]==NOW


def test_restart_and_epoch_change_cannot_auto_backfill(harness):
    h=harness;_,last=discover(h);assert d(last).eligibility is e.Eligibility.ELIGIBLE
    fresh=ChannelTabsAdapter(tabs_reader=h.reader,policy=h.policy,reviews=h.adapter.reviews,review_verifier=h.verifier,clock=lambda:h.clock[0])
    assert d(fresh.evaluate(h.cache,h.state)).eligibility is e.Eligibility.HOLD_FOR_REVIEW
    h.adapter.policy=replace(h.policy,version='new-policy')
    assert d(h.adapter.evaluate(h.cache,h.state)).eligibility is e.Eligibility.HOLD_FOR_REVIEW


@pytest.mark.parametrize('category,readiness,expected',[
    ('REGULAR','PUBLIC_RELEASED','ELIGIBLE'),('SHORTS','PUBLIC_RELEASED','REJECTED'),
    ('STREAM','UPCOMING','HOLD_FOR_REVIEW'),('STREAM','LIVE','HOLD_FOR_REVIEW'),('STREAM','COMPLETED_REPLAY','ELIGIBLE')])
def test_signed_category_and_readiness(harness,category,readiness,expected):
    assert d(discover(harness,category,readiness)[1]).eligibility.value==expected


def test_badges_and_relative_text_do_not_certify_readiness(harness):
    h=harness;discover(h,'STREAM','LIVE');h.adapter.reviews={};h.clock[0]+=timedelta(minutes=1)
    r=h.adapter.evaluate(h.cache,h.state)
    assert d(r).eligibility is e.Eligibility.HOLD_FOR_REVIEW
    assert r.evidence[0]['readiness']==r.evidence[0]['category']=='UNKNOWN'


@pytest.mark.parametrize('tab',['videos','streams'])
def test_failed_tab_never_falls_back_or_creates_gap_backfill(harness,tab):
    h=harness;discover(h)
    def fetch(t):
        if t==tab:raise RuntimeError('credential-bearing error')
        return page(t,h.present[t])
    h.reader.fetch.side_effect=fetch;h.clock[0]+=timedelta(minutes=1)
    r=h.adapter.evaluate(h.cache,h.state)
    assert r.health[tab]['health']=='FAILED' and not r.preview_items
    assert 'credential-bearing' not in json.dumps(r.report())
    h.reader.fetch.side_effect=lambda t:page(t,h.present[t]);h.clock[0]+=timedelta(minutes=1)
    assert d(h.adapter.evaluate(h.cache,h.state)).eligibility is e.Eligibility.HOLD_FOR_REVIEW
    assert VID in h.adapter._inventory


@pytest.mark.parametrize('field,value',[
    ('scope','production'),('video_id','another0001'),('channel_id','UC'+'y'*22),('policy_version','old'),
    ('state_fingerprint','0'*64),('approved',False),('approved','true'),('reviewer',''),('history_review_reference',''),
    ('source_url','https://www.youtube.com/shorts/'+VID),('source_url','https://evil.invalid/x'),
    ('observed_at','invalid'),('expires_at',NOW.isoformat()),('first_seen_at',NOW.isoformat()),
    ('category','UNKNOWN'),('readiness','UNKNOWN'),('schema','operator-youtube-review-v1')])
def test_invalid_even_signed_review_holds(harness,field,value):
    h=harness;discover(h);h.payload[field]=value;h.adapter.reviews[VID]=sign(h.payload)
    assert d(h.adapter.evaluate(h.cache,h.state)).eligibility is e.Eligibility.HOLD_FOR_REVIEW


@pytest.mark.parametrize('tamper',['signature','key_id','payload'])
def test_forged_review_holds(harness,tamper):
    h=harness;discover(h);record=h.adapter.reviews[VID]
    if tamper=='signature':record['signature']='0'*64
    elif tamper=='key_id':record['key_id']='unknown'
    else:record['payload']['approved']=False
    assert d(h.adapter.evaluate(h.cache,h.state)).eligibility is e.Eligibility.HOLD_FOR_REVIEW


def test_missing_history_does_not_fetch_or_seed_empty_baseline(harness):
    h=harness;h.state.pop('youtube_rss_cutover')
    assert h.adapter.evaluate(h.cache,h.state).errors
    h.reader.fetch.assert_not_called()


def batch(delivered):
    return {'event_keys':['youtube:video:'+VID+':initial'],'items':[{'source':'youtube','video_id':VID}],
        'chunks':['stored exact chunk'],'sent_chunks':[1],'sent_documents':[],'send_files':False,'delivered':delivered,'attempts':1}


@pytest.mark.parametrize('delivered',[True,False])
def test_journal_progress_is_not_reinterpreted(harness,delivered):
    h=harness;h.present['videos']=[VID];h.state['batches']=[batch(delivered)]
    r=h.adapter.evaluate(h.cache,h.state)
    assert d(r).eligibility is (e.Eligibility.ALREADY_DELIVERED if delivered else e.Eligibility.HOLD_FOR_REVIEW)
    assert not r.preview_items and h.state['batches'][0]['sent_chunks']==[1]


def test_runner_e2e_mock_never_notifies_or_enqueues_even_eligible(harness,tmp_path,config,monitors,announcement):
    h=harness;discover(h)
    h.clock[0]+=timedelta(minutes=12)
    for name,value in [('youtube_cache.json',h.cache),('delivery_state.json',h.state),('announcements_cache.json',[announcement])]:atomic_json(tmp_path/name,value)
    original={p.name:p.read_bytes() for p in tmp_path.iterdir()};spy=Mock()
    result=run_once(dict(config,youtube_monitor_enabled=True),dry_run=True,state_dir=tmp_path,scraper=monitors[0],notifier=spy,youtube_adapter=h.adapter)
    assert result.exit_code==0 and result.pending==0
    assert result.youtube_eligibility['preview_items'] and not spy.mock_calls
    assert original=={p.name:p.read_bytes() for p in tmp_path.iterdir()}


def test_non_dry_run_keeps_bima_path_and_does_not_evaluate_youtube(tmp_path,config,monitors,notifier):
    spy=Mock()
    result=run_once(dict(config,youtube_monitor_enabled=True,notification_method='console'),state_dir=tmp_path,
        scraper=monitors[0],notifier=spy,youtube_adapter=Mock())
    assert result.sources[0].source=='bima'
    assert result.sources[1].status=='DRY_RUN_REQUIRED'
    assert not result.youtube_eligibility and result.exit_code==0
    spy.send_notification.assert_not_called()


def test_delivered_second_runner_check_zero_notifier(harness,tmp_path,config,monitors,announcement):
    h=harness;h.present['videos']=[VID];h.state['batches']=[batch(True)]
    for name,value in [('youtube_cache.json',h.cache),('delivery_state.json',h.state),('announcements_cache.json',[announcement])]:atomic_json(tmp_path/name,value)
    before=(tmp_path/'delivery_state.json').read_bytes();spy=Mock()
    for _ in range(2):
        r=run_once(dict(config,youtube_monitor_enabled=True),dry_run=True,state_dir=tmp_path,scraper=monitors[0],notifier=spy,youtube_adapter=h.adapter)
        assert r.pending==0 and r.youtube_eligibility['decisions'][0]['eligibility']=='ALREADY_DELIVERED'
    assert not spy.mock_calls and (tmp_path/'delivery_state.json').read_bytes()==before


def test_valid_legacy_pending_payload_never_resumes_in_bima_only_mode(tmp_path,config,monitors,announcement,notifier,telegram):
    old=batch(False);old['items'][0].update(delivery_format='youtube-rss-v1',title='historical',published='2026-10-20T00:00:00Z',url='https://www.youtube.com/watch?v='+VID)
    atomic_json(tmp_path/'delivery_state.json',{'version':1,'batches':[old]});atomic_json(tmp_path/'announcements_cache.json',[announcement])
    result=run_once(config,state_dir=tmp_path,scraper=monitors[0],notifier=notifier)
    assert result.pending==1 and result.exit_code==1;telegram.assert_not_called()


def test_cli_and_main_guards_before_secrets(monkeypatch):
    import app_config,run_github
    loader=Mock(side_effect=AssertionError('no secrets'))
    monkeypatch.setattr(app_config,'load_config',loader);monkeypatch.setattr(run_github,'load_config',loader)
    with pytest.raises(SystemExit):run_github.main(['--youtube-eligibility'])
    monkeypatch.setattr(sys,'argv',['main.py','--youtube-eligibility']);monkeypatch.setattr(sys,'platform','linux')
    with pytest.raises(SystemExit):runpy.run_path(str(ROOT/'main.py'),run_name='__main__')
    loader.assert_not_called()


def test_old_hybrid_selector_has_no_fallback(config):
    spy=Mock()
    result=run_once(dict(config,youtube_eligibility_dry_run=False),dry_run=True,scraper=spy,notifier=spy)
    assert result.errors and not spy.mock_calls
