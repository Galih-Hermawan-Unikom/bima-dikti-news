"""Hybrid acquisition -> classifier -> dry-run runner. All sources/notifiers mocked."""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
import xml.etree.ElementTree as ET

import pytest

import youtube_eligibility as e
import youtube_channel_tabs as tabs_module
from youtube_channel_tabs import ChannelTabPage, FORMAT
from youtube_hybrid import HybridYouTubeAdapter, ChannelBoundRSS, history_fingerprint
from youtube_monitor import DEFAULT_CHANNEL, YouTubeMonitor
from youtube_review_evidence import SignedReviewVerifier, ReviewVerificationError
from monitor_runner import run_once
from reliability import atomic_json

VID = "video000001"
NOW = datetime(2026, 10, 22, 2, tzinfo=timezone.utc)
KEY = b"synthetic-offline-test-key-only!!"
ROOT = Path(__file__).resolve().parents[1]


def rss_row(vid=VID, published=None):
    return {"source": "youtube", "kind": "video", "delivery_format": "youtube-rss-v1", "video_id": vid,
        "title": "Synthetic reviewed video", "published": published or (NOW-timedelta(hours=1)).isoformat(),
        "date": published or (NOW-timedelta(hours=1)).isoformat(), "documents": [],
        "url": "https://www.youtube.com/watch?v="+vid, "scraped_at": NOW.isoformat()}


def page(tab, ids):
    return ChannelTabPage(tab, [{"source": "youtube", "observation_format": FORMAT, "video_id": vid,
        "title": "Synthetic reviewed video", "channel_title": "Fixture channel",
        "url": "https://www.youtube.com/watch?v="+vid, "thumbnail": "", "source_tabs": [tab],
        "source_metadata": {"relative_text": ["Streaming 1 hour ago"], "badges": ["LIVE"],
                            "schedule_timezone": None}} for vid in ids], False, empty_verified=not ids)


def sign(payload, key=KEY, key_id="fixture-authority"):
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    return {"key_id": key_id, "payload": deepcopy(payload), "signature": hmac.new(key, raw, hashlib.sha256).hexdigest()}


@pytest.fixture
def hybrid(monkeypatch):
    cache = []
    state = {"version": 1, "batches": [], "youtube_rss_cutover": {"version": 1,
        "cutover_at": "2026-10-21T01:32:44+00:00", "baseline_ids": []}}
    clock = [NOW]
    monkeypatch.setattr(tabs_module, "datetime", SimpleNamespace(now=lambda tz: clock[0]))
    rss = SimpleNamespace(channel_id=DEFAULT_CHANNEL, fetch_videos=Mock(return_value=[rss_row()]))
    present = {"videos": [VID], "streams": []}
    reader = Mock()
    reader.fetch.side_effect = lambda tab: page(tab, present[tab])
    policy = e.Policy(DEFAULT_CHANNEL, "synthetic-policy-v1", activation_at=NOW-timedelta(hours=2),
        window=timedelta(hours=24), ttl=timedelta(minutes=30), skew=timedelta(minutes=5),
        capture_interval=timedelta(minutes=10), approved=True)
    payload = {"schema": "operator-youtube-review-v1", "video_id": VID, "channel_id": DEFAULT_CHANNEL,
        "scope": policy.scope, "policy_version": policy.version, "state_fingerprint": history_fingerprint(cache,state),
        "category": "REGULAR", "readiness": "PUBLIC_RELEASED", "source_url": "https://www.youtube.com/watch?v="+VID,
        "reviewer": "SYNTHETIC_TEST_REVIEWER", "history_review_reference": "synthetic-history-review",
        "observed_at": NOW.isoformat(), "expires_at": (NOW+timedelta(hours=1)).isoformat(), "ended_at": None,
        "first_seen_at": (NOW-timedelta(minutes=50)).isoformat(), "earliest_published": rss_row()["published"], "approved": True}
    verifier = SignedReviewVerifier({"fixture-authority":KEY},frozenset({"www.youtube.com"}))
    adapter = HybridYouTubeAdapter(rss=rss,tabs_reader=reader,policy=policy,
        reviews={VID:sign(payload)},review_verifier=verifier,clock=lambda:clock[0])
    return SimpleNamespace(adapter=adapter,rss=rss,reader=reader,present=present,cache=cache,state=state,
                           clock=clock,policy=policy,payload=payload,verifier=verifier)


def stable(h):
    first = h.adapter.evaluate(h.cache,h.state)
    h.clock[0] += timedelta(minutes=12)
    return first,h.adapter.evaluate(h.cache,h.state)


def decision(result,vid=VID): return next(d for d in result.decisions if d.video_id==vid)


def test_regular_end_to_end_stability_and_no_delivery(hybrid):
    original = deepcopy((hybrid.cache,hybrid.state))
    first,last = stable(hybrid)
    assert decision(first).eligibility is e.Eligibility.HOLD_FOR_REVIEW
    assert decision(last).eligibility is e.Eligibility.ELIGIBLE
    assert len(last.preview_items)==1
    assert not decision(last).enqueue and not decision(last).delivery_authorized
    assert last.report()["evidence"][0]["review_verified"] is True
    assert (hybrid.cache,hybrid.state)==original
    assert [c.args[0] for c in hybrid.reader.fetch.call_args_list]==["videos","streams"]*2


@pytest.mark.parametrize("category,readiness,expected",[
    ("SHORTS","PUBLIC_RELEASED","REJECTED"), ("STREAM","UPCOMING","HOLD_FOR_REVIEW"),
    ("STREAM","LIVE","HOLD_FOR_REVIEW"), ("STREAM","COMPLETED_REPLAY","ELIGIBLE")])
def test_reviewed_categories_and_replay_only(hybrid,category,readiness,expected):
    hybrid.payload.update(category=category,readiness=readiness)
    if category=="STREAM": hybrid.present.update(videos=[],streams=[VID])
    if readiness=="COMPLETED_REPLAY": hybrid.payload['ended_at']=(NOW-timedelta(minutes=5)).isoformat()
    hybrid.adapter.reviews[VID]=sign(hybrid.payload)
    _,last=stable(hybrid)
    assert decision(last).eligibility.value==expected
    assert not decision(last).delivery_authorized


def test_badges_and_schedule_are_not_release_or_category_proof(hybrid):
    hybrid.adapter.reviews={}
    hybrid.present.update(videos=[],streams=[VID])
    _,last=stable(hybrid)
    assert decision(last).eligibility is e.Eligibility.HOLD_FOR_REVIEW
    assert last.evidence[0]["category"]=="UNKNOWN" and last.evidence[0]["readiness"]=="UNKNOWN"


@pytest.mark.parametrize("tab",["videos","streams"])
@pytest.mark.parametrize("empty",[True,False])
def test_one_failed_tab_never_becomes_rss_only_fallback(hybrid,tab,empty):
    if empty: hybrid.present.update(videos=[],streams=[])
    hybrid.adapter.evaluate(hybrid.cache,hybrid.state)
    hybrid.clock[0]+=timedelta(minutes=12)
    def fetch(name):
        if name==tab: raise RuntimeError("Do not expose credentials in errors")
        return page(name,hybrid.present[name])
    hybrid.reader.fetch.side_effect=fetch
    last=hybrid.adapter.evaluate(hybrid.cache,hybrid.state)
    assert last.health[tab]=="FAILED"
    assert decision(last).eligibility is e.Eligibility.HOLD_FOR_REVIEW
    assert "D29" in decision(last).rule_ids
    assert not last.preview_items


def test_rss_failure_keeps_tabs_readonly_and_holds(hybrid):
    hybrid.rss.fetch_videos.side_effect=RuntimeError("secret request URL")
    result=hybrid.adapter.evaluate(hybrid.cache,hybrid.state)
    assert result.errors==("RSS_SOURCE_FAILED",)
    assert "secret" not in json.dumps(result.report())
    assert decision(result).eligibility is e.Eligibility.HOLD_FOR_REVIEW
    assert not result.preview_items


@pytest.mark.parametrize("field,value",[
    ("scope","production"),("video_id","another0001"),("channel_id","UC"+"y"*22),
    ("policy_version","wrong"),("state_fingerprint","0"*64),("approved",False),("approved","true"),
    ("reviewer",""),("history_review_reference",""),("source_url","https://www.youtube.com/shorts/"+VID),
    ("source_url","https://evil.invalid/watch?v="+VID),("observed_at","invalid"),
    ("expires_at",NOW.isoformat()),("first_seen_at","2026-10-23T00:00:00Z"),
    ("earliest_published","invalid"),("category","UNKNOWN"),("readiness","UNKNOWN")])
def test_even_signed_review_must_satisfy_evidence_contract(hybrid,field,value):
    hybrid.payload[field]=value
    hybrid.adapter.reviews[VID]=sign(hybrid.payload)
    _,last=stable(hybrid)
    assert decision(last).eligibility is e.Eligibility.HOLD_FOR_REVIEW
    assert not last.preview_items


@pytest.mark.parametrize("tamper",["signature","key_id","payload"])
def test_forged_review_never_approves(hybrid,tamper):
    record=hybrid.adapter.reviews[VID]
    if tamper=="signature": record['signature']='0'*64
    elif tamper=="key_id": record['key_id']='unknown'
    else: record['payload']['reviewer']='forged'
    _,last=stable(hybrid)
    assert decision(last).eligibility is e.Eligibility.HOLD_FOR_REVIEW
    assert "REVIEW_EVIDENCE_UNVERIFIED" in decision(last).reason_codes


def test_history_missing_does_not_fetch_or_initialize_baseline(hybrid):
    hybrid.state.pop('youtube_rss_cutover')
    result=hybrid.adapter.evaluate(hybrid.cache,hybrid.state)
    assert result.errors and not result.decisions
    hybrid.rss.fetch_videos.assert_not_called(); hybrid.reader.fetch.assert_not_called()


def test_wrong_channel_does_not_fetch(hybrid):
    hybrid.rss.channel_id='UC'+'y'*22
    assert hybrid.adapter.evaluate(hybrid.cache,hybrid.state).errors
    hybrid.rss.fetch_videos.assert_not_called()


def test_metadata_drift_does_not_reset_earliest_or_backfill(hybrid):
    hybrid.adapter.reviews={}
    hybrid.rss.fetch_videos.return_value=[rss_row(published='2026-10-20T00:00:00Z')]
    hybrid.adapter.evaluate(hybrid.cache,hybrid.state)
    hybrid.rss.fetch_videos.return_value=[rss_row()]
    hybrid.clock[0]+=timedelta(minutes=12)
    d=decision(hybrid.adapter.evaluate(hybrid.cache,hybrid.state))
    assert d.eligibility is e.Eligibility.REJECTED and 'D20' in d.rule_ids
    assert hybrid.adapter._earliest[VID]=='2026-10-20T00:00:00Z'


@pytest.mark.parametrize("mutation,reason",[
    ("stale","STALE_OR_FUTURE_EVIDENCE"),("activation","PRE_ACTIVATION_BACKLOG"),
    ("age","AGE_WINDOW_EXCEEDED"),("expired_review","ADMISSION_EXPIRED")])
def test_freshness_activation_and_expiry(hybrid,mutation,reason):
    if mutation=='activation': hybrid.adapter.policy=replace(hybrid.policy,activation_at=NOW)
    if mutation=='age': hybrid.adapter.policy=replace(hybrid.policy,window=timedelta(minutes=30))
    if mutation=='expired_review':
        hybrid.payload['expires_at']=(NOW+timedelta(minutes=5)).isoformat()
        hybrid.adapter.reviews[VID]=sign(hybrid.payload)
    hybrid.adapter.evaluate(hybrid.cache,hybrid.state)
    hybrid.clock[0]+=timedelta(minutes=31 if mutation=='stale' else 12)
    assert reason in decision(hybrid.adapter.evaluate(hybrid.cache,hybrid.state)).reason_codes


def test_rss_only_does_not_inherit_approval(hybrid):
    hybrid.present.update(videos=[],streams=[])
    assert decision(stable(hybrid)[1]).eligibility is e.Eligibility.HOLD_FOR_REVIEW


def test_source_absence_is_not_deletion(hybrid):
    hybrid.adapter.evaluate(hybrid.cache,hybrid.state)
    hybrid.present['videos']=[]
    hybrid.clock[0]+=timedelta(minutes=12)
    assert 'NOT_VISIBLE_IN_CAPTURE_NOT_DELETED' in decision(hybrid.adapter.evaluate(hybrid.cache,hybrid.state)).reason_codes


def test_baseline_and_duplicate_delivery_survive_source_failure(hybrid):
    hybrid.state['youtube_rss_cutover']['baseline_ids']=[VID]
    d=decision(hybrid.adapter.evaluate(hybrid.cache,hybrid.state))
    assert d.knowledge is e.Knowledge.KNOWN and d.eligibility is e.Eligibility.REJECTED
    hybrid.state['batches']=[batch(VID,delivered=True)]
    hybrid.rss.fetch_videos.side_effect=RuntimeError()
    d=decision(hybrid.adapter.evaluate(hybrid.cache,hybrid.state))
    assert d.eligibility is e.Eligibility.ALREADY_DELIVERED


def batch(vid,delivered=False):
    return {'event_keys':[f'youtube:video:{vid}:initial'],'items':[rss_row(vid)],'chunks':['historical chunk'],
            'sent_chunks':[1],'sent_documents':[],'send_files':False,'delivered':delivered,'attempts':1}


def test_pending_progress_is_never_synthetic_pinned_or_delivered(hybrid):
    hybrid.state['batches']=[batch(VID)]
    d=decision(stable(hybrid)[1])
    assert d.delivery_state is e.DeliveryState.PARTIAL and d.eligibility is e.Eligibility.HOLD_FOR_REVIEW
    assert not hybrid.state['batches'][0]['delivered']


def test_runner_preview_notifier_mock_and_second_check_no_state_change(hybrid,tmp_path,config,monitors,monkeypatch,announcement):
    for name,value in [('delivery_state.json',hybrid.state),('youtube_cache.json',[]),('announcements_cache.json',[announcement])]:
        atomic_json(tmp_path/name,value)
    legacy=Mock(side_effect=AssertionError('legacy fallback forbidden'))
    monkeypatch.setattr(monitors[1],'check_new_videos',legacy)
    spy=Mock()
    before={p.name:p.read_bytes() for p in tmp_path.iterdir()}
    def run(): return run_once(config,dry_run=True,state_dir=tmp_path,scraper=monitors[0],youtube=monitors[1],notifier=spy,youtube_adapter=hybrid.adapter)
    assert run().awaiting_verification==1
    hybrid.clock[0]+=timedelta(minutes=12)
    result=run()
    assert result.pending==1 and result.exit_code==0
    spy._send_console.assert_called_once()
    assert spy._send_console.call_args.args[0][0]['video_id']==VID
    spy.send_notification.assert_not_called(); spy._build_summary_chunks.assert_not_called(); legacy.assert_not_called()
    assert before=={p.name:p.read_bytes() for p in tmp_path.iterdir()}
    assert result.youtube_eligibility['decisions'][0]['delivery_authorized'] is False


def test_hybrid_non_dry_run_stops_before_any_monitor_or_notifier(config):
    spy=Mock()
    result=run_once(config,youtube_adapter=spy,notifier=spy,scraper=spy,youtube=spy)
    assert result.exit_code==1 and 'dry_run=True' in result.errors[0]
    assert not spy.mock_calls


def test_hybrid_runner_pending_batch_is_not_previewed_or_changed(hybrid,tmp_path,config,monitors,monkeypatch,announcement):
    hybrid.state['batches']=[batch(VID)]
    atomic_json(tmp_path/'delivery_state.json',hybrid.state); atomic_json(tmp_path/'youtube_cache.json',[])
    atomic_json(tmp_path/'announcements_cache.json',[announcement])
    spy=Mock(); before=(tmp_path/'delivery_state.json').read_bytes()
    result=run_once(config,dry_run=True,state_dir=tmp_path,scraper=monitors[0],youtube=monitors[1],notifier=spy,youtube_adapter=hybrid.adapter)
    spy._send_console.assert_not_called(); spy.send_notification.assert_not_called()
    assert result.pending==1 and (tmp_path/'delivery_state.json').read_bytes()==before


def test_six_historical_candidates_hold_with_real_identity_state(hybrid,fixtures):
    facts=json.loads((fixtures/'youtube_eligibility_audit_candidates.json').read_text(encoding='utf-8'))
    cache=json.loads((ROOT/'youtube_cache.json').read_text(encoding='utf-8'))
    state=json.loads((ROOT/'delivery_state.json').read_text(encoding='utf-8'))
    hybrid.rss.fetch_videos.return_value=[dict(rss_row(c['video_id'],c['published_rss']),title=c['title'],scraped_at=facts['rss_observed_at']) for c in facts['candidates']]
    hybrid.present.update(videos=[c['video_id'] for c in facts['candidates'] if 'videos' in c['tabs']],streams=[c['video_id'] for c in facts['candidates'] if 'streams' in c['tabs']])
    hybrid.adapter.policy=e.Policy(DEFAULT_CHANNEL,'inactive')
    hybrid.adapter.reviews={}
    result=hybrid.adapter.evaluate(cache,state)
    selected=[d for d in result.decisions if d.video_id in {c['video_id'] for c in facts['candidates']}]
    assert len(selected)==6 and all(d.eligibility is e.Eligibility.HOLD_FOR_REVIEW for d in selected)
    assert len(cache)==129 and len(state['youtube_rss_cutover']['baseline_ids'])==133
    assert not result.preview_items


@pytest.mark.parametrize('bad_owner',[True,False])
def test_rss_channel_binding_on_feed_and_entries(monkeypatch,bad_owner):
    ns='{http://www.youtube.com/xml/schemas/2015}'
    root=ET.fromstring('<feed xmlns="http://www.w3.org/2005/Atom"><entry/></feed>')
    ET.SubElement(root,ns+'channelId').text='wrong' if bad_owner else DEFAULT_CHANNEL
    entry=root.find('{http://www.w3.org/2005/Atom}entry')
    ET.SubElement(entry,ns+'channelId').text=DEFAULT_CHANNEL if bad_owner else 'wrong'
    monkeypatch.setattr(YouTubeMonitor,'_read_feed',lambda s:root)
    with pytest.raises(Exception,match='CHANNEL_BINDING_INVALID'): ChannelBoundRSS()._read_feed()


def test_cli_requires_dry_run_before_loading_configuration(monkeypatch):
    import run_github
    loader=Mock(side_effect=AssertionError('do not load config'))
    monkeypatch.setattr(run_github,'load_config',loader)
    with pytest.raises(SystemExit): run_github.main(['--youtube-eligibility'])
    loader.assert_not_called()


def test_runner_delivered_fixture_recheck_has_zero_notifier_calls(hybrid,tmp_path,config,monitors,announcement):
    # Test-only journal acknowledgement; no real application journal is written.
    hybrid.state['batches']=[batch(VID,delivered=True)]
    for name,value in [('delivery_state.json',hybrid.state),('youtube_cache.json',[]),('announcements_cache.json',[announcement])]:
        atomic_json(tmp_path/name,value)
    before={p.name:p.read_bytes() for p in tmp_path.iterdir()}
    spy=Mock()
    for _ in range(2):
        result=run_once(config,dry_run=True,state_dir=tmp_path,scraper=monitors[0],youtube=monitors[1],notifier=spy,youtube_adapter=hybrid.adapter)
        assert result.pending==0
        assert result.youtube_eligibility['decisions'][0]['eligibility']==e.Eligibility.ALREADY_DELIVERED
    assert not spy.mock_calls
    assert before=={p.name:p.read_bytes() for p in tmp_path.iterdir()}


def test_adapter_failure_has_no_legacy_fallback(hybrid,tmp_path,config,monitors,monkeypatch,announcement):
    atomic_json(tmp_path/'announcements_cache.json',[announcement])
    broken=Mock(); broken.policy=hybrid.policy; broken.evaluate.side_effect=RuntimeError('credential-bearing URL')
    legacy=Mock(side_effect=AssertionError('no legacy fallback'))
    monkeypatch.setattr(monitors[1],'check_new_videos',legacy)
    spy=Mock()
    result=run_once(config,dry_run=True,state_dir=tmp_path,scraper=monitors[0],youtube=monitors[1],notifier=spy,youtube_adapter=broken)
    assert result.exit_code==1 and 'credential-bearing' not in repr(result)
    legacy.assert_not_called(); spy.send_notification.assert_not_called()


def test_cli_hybrid_dry_run_does_not_load_secrets(monkeypatch):
    import run_github
    from reliability import RunResult
    loader=Mock(return_value={}); runner=Mock(return_value=RunResult())
    monkeypatch.setattr(run_github,'load_config',loader); monkeypatch.setattr(run_github,'run_once',runner)
    assert run_github.main(['--dry-run','--youtube-eligibility'])==0
    loader.assert_called_once_with(load_env=False,include_secrets=False)
    assert runner.call_args.args[0]['youtube_eligibility_dry_run'] is True


@pytest.mark.parametrize('value',[None,'true',1])
def test_malformed_hybrid_flag_stops_early(config,value):
    config['youtube_eligibility_dry_run']=value
    spy=Mock()
    assert run_once(config,dry_run=True,youtube=spy,scraper=spy,notifier=spy).exit_code==1
    assert not spy.mock_calls


def test_xml_and_html_acquisition_to_classifier_without_http(hybrid,fixtures,monkeypatch):
    from youtube_channel_tabs import YouTubeChannelTabsReader, URLS
    import youtube_monitor as rss_module
    monkeypatch.setattr(rss_module,'datetime',SimpleNamespace(now=lambda tz:NOW,fromisoformat=datetime.fromisoformat))
    rss=ChannelBoundRSS()
    xml=(fixtures/'youtube.xml').read_text(encoding='utf-8').replace('fixture1234',VID)
    xml=xml.replace('<title>Kemdiktisaintek</title>', '<yt:channelId>'+DEFAULT_CHANNEL+'</yt:channelId><title>Kemdiktisaintek</title>')
    xml=xml.replace('<entry>','<entry><yt:channelId>'+DEFAULT_CHANNEL+'</yt:channelId>')
    response=Mock(status_code=200,content=xml.encode())
    rss.session.get=Mock(return_value=response)
    reader=YouTubeChannelTabsReader()
    def get(url,**kwargs):
        tab=next(t for t in ('videos','streams') if URLS[t]==url)
        data=json.loads((fixtures/('youtube_'+tab+'_initial.json')).read_text(encoding='utf-8'))
        return Mock(status_code=200,text='<html><script>var ytInitialData = '+json.dumps(data)+';</script></html>')
    reader.session.get=Mock(side_effect=get)
    adapter=HybridYouTubeAdapter(rss=rss,tabs_reader=reader,clock=lambda:NOW)
    result=adapter.evaluate(hybrid.cache,hybrid.state)
    assert result.health['rss']==result.health['videos']==result.health['streams']=='HEALTHY'
    assert decision(result).eligibility is e.Eligibility.HOLD_FOR_REVIEW
    rss.session.get.assert_called_once_with('https://www.youtube.com/feeds/videos.xml?channel_id='+DEFAULT_CHANNEL,timeout=15,allow_redirects=False)
    assert {c.args[0] for c in reader.session.get.call_args_list}==set(URLS.values())
    reader.close(); rss.session.close()


def test_current_cycle_health_is_exposed_even_with_no_ids(hybrid):
    hybrid.rss.fetch_videos.return_value=[]
    hybrid.present.update(videos=[],streams=[])
    hybrid.reader.fetch.side_effect=RuntimeError('both unavailable')
    result=hybrid.adapter.evaluate(hybrid.cache,hybrid.state)
    assert not result.decisions and not result.preview_items
    assert result.health['videos']==result.health['streams']=='FAILED'


def test_default_authorities_and_policy_are_inactive(hybrid):
    adapter=HybridYouTubeAdapter(rss=hybrid.rss,tabs_reader=hybrid.reader,clock=lambda:NOW)
    assert not adapter.policy.approved and adapter.review_verifier is None and not adapter.reviews
    result=adapter.evaluate(hybrid.cache,hybrid.state)
    assert decision(result).eligibility is e.Eligibility.HOLD_FOR_REVIEW
    assert not result.evidence[0]['review_verified']


def test_unknown_review_authority_is_a_configuration_error():
    with pytest.raises(ValueError): SignedReviewVerifier({'short':b'short'},frozenset({'example.invalid'}))


def test_runner_channel_mismatch_cannot_silently_observe_another_channel(hybrid,tmp_path,config,monitors,monkeypatch,announcement):
    atomic_json(tmp_path/'announcements_cache.json',[announcement])
    monitors[1].channel_id='UC'+'z'*22
    spy=Mock()
    result=run_once(config,dry_run=True,state_dir=tmp_path,scraper=monitors[0],youtube=monitors[1],notifier=spy,youtube_adapter=hybrid.adapter)
    assert result.exit_code==1
    hybrid.rss.fetch_videos.assert_not_called(); hybrid.reader.fetch.assert_not_called()
    spy.send_notification.assert_not_called()


def test_conflicting_rss_and_tab_titles_require_review(hybrid):
    hybrid.rss.fetch_videos.return_value[0]['title']='Conflicting metadata title'
    assert 'SOURCE_EVIDENCE_CONFLICT' in decision(stable(hybrid)[1]).reason_codes


def test_main_entrypoint_cannot_bypass_hybrid_dry_run_guard(monkeypatch):
    import runpy
    import sys
    import app_config
    import run_github
    loader=Mock(side_effect=AssertionError('must not load secrets or start scheduler'))
    monkeypatch.setattr(app_config,'load_config',loader)
    monkeypatch.setattr(run_github,'load_config',loader)
    monkeypatch.setattr(sys,'argv',['main.py','--youtube-eligibility'])
    monkeypatch.setattr(sys,'platform','linux')
    with pytest.raises(SystemExit) as error: runpy.run_path(str(ROOT/'main.py'),run_name='__main__')
    assert error.value.code==2
    loader.assert_not_called()
