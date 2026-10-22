"""Tabs policy supersedes the retired hybrid/publication-based rules."""
import ast
from copy import deepcopy
from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
import youtube_eligibility as e

VID='video000001'
NOW=datetime(2026,10,22,2,tzinfo=timezone.utc)
CHANNEL='UC'+'x'*22


@pytest.fixture
def valid():
    proof=lambda name,minutes=2:e.Proof(VID,CHANNEL,name,NOW-timedelta(minutes=minutes),'offline',True)
    return e.EligibilityInput(VID,
        e.HistoricalIdentity(frozenset({'history0001'}),frozenset(),frozenset(),NOW-timedelta(days=1),'offline','a'*64,True,True),
        e.TimeEvidence(NOW,NOW-timedelta(minutes=35),True,False),
        e.Policy(CHANNEL,'test-v1',activation_at=NOW-timedelta(hours=2),window=timedelta(hours=24),ttl=timedelta(minutes=30),
                 skew=timedelta(minutes=5),capture_interval=timedelta(minutes=10),approved=True),
        e.VideoEvidence(proof('video'),'Synthetic video','https://www.youtube.com/watch?v='+VID),
        (e.TabCapture(proof('c1',15),frozenset({'videos'}),True,True),e.TabCapture(proof('c2'),frozenset({'videos'}),True,True)),
        category=e.CategoryEvidence(e.Category.REGULAR,e.CategoryMethod.MANUAL,proof('category')),
        release=e.ReleaseEvidence(e.Readiness.PUBLIC_RELEASED,proof('release')),
        admission=e.AdmissionEvidence(proof('admission'),'test-v1','history-review','a'*64,NOW+timedelta(minutes=10),reviewer='test-only',approved=True))


def change(obj,path,value):
    head,*tail=path.split('.')
    if isinstance(obj,tuple):
        items=list(obj);items[int(head)]=change(items[int(head)],'.'.join(tail),value);return tuple(items)
    if tail:return replace(obj,**{head:change(getattr(obj,head),'.'.join(tail),value)})
    return replace(obj,**{head:value})


@pytest.mark.parametrize('path,value',[
    ('policy.approved',False),('policy.activation_at',None),('policy.window',None),('policy.ttl',None),
    ('policy.skew',None),('policy.capture_interval',None),('policy.version',''),('policy.scope',''),
    ('policy.window',timedelta(0)),('policy.ttl',timedelta(seconds=-1)),('policy.skew',timedelta(seconds=-1)),
    ('policy.capture_interval',timedelta(0)),('time.clock_verified',False),('time.initial_inventory',True),
    ('history.integrity_verified',False),('history.authority_verified',False),('history.fingerprint','bad'),
    ('history.schema_version',2),('history.scope','production'),('video',None),('video.title',''),
    ('video.url','https://www.youtube.com/shorts/'+VID),('video.proof.verified',False),
    ('video.proof.video_id','another0001'),('video.proof.channel_id','UC'+'y'*22),('video.proof.scope','wrong'),
    ('captures',()),('captures.0.videos_healthy',False),('captures.1.streams_healthy',False),
    ('captures.1.partial',True),('captures.1.title_conflict',True),('captures.1.tabs',frozenset({'shorts'})),
    ('captures.1.proof.reference','c1'),('captures.1.proof.observed_at',NOW-timedelta(minutes=16)),
    ('captures.1.proof.verified',False),('category.category',e.Category.UNKNOWN),
    ('category.method',e.CategoryMethod.TAB_SURROGATE),('category.proof',None),('release.proof',None),
    ('release.readiness',e.Readiness.UNKNOWN),('admission.proof',None),('admission.approved',False),
    ('admission.reviewer',''),('admission.history_review_reference',''),('admission.policy_version','old'),
    ('admission.state_fingerprint','b'*64),('admission.expires_at',NOW),
    ('reservation',e.Reservation.OWNED_BY_OTHER),('journal.scope','production')])
def test_fail_closed(valid,path,value):
    d=e.classify(change(valid,path,value))
    assert d.eligibility is e.Eligibility.HOLD_FOR_REVIEW and not d.enqueue and not d.delivery_authorized


@pytest.mark.parametrize('path',['time.now','time.first_seen_at','history.baseline_at','policy.activation_at',
    'video.proof.observed_at','captures.0.proof.observed_at','category.proof.observed_at',
    'release.proof.observed_at','admission.proof.observed_at'])
def test_naive_times_hold(valid,path):
    assert e.classify(change(valid,path,NOW.replace(tzinfo=None))).eligibility is e.Eligibility.HOLD_FOR_REVIEW


@pytest.mark.parametrize('path,value',[('policy.approved','true'),('time.initial_inventory','false'),
    ('captures',[]),('history.baseline_ids',set()),('category.category','REGULAR'),('reservation','UNRESERVED')])
def test_untyped_inputs(valid,path,value):
    d=e.classify(change(valid,path,value));assert d.knowledge is None and d.eligibility is e.Eligibility.HOLD_FOR_REVIEW


@pytest.mark.parametrize('seconds',[-1,0])
def test_strict_activation_fence(valid,seconds):
    item=change(valid,'time.first_seen_at',valid.policy.activation_at+timedelta(seconds=seconds))
    assert e.classify(item).reason_codes==('PRE_ACTIVATION_OBSERVATION_NO_BACKFILL',)


def test_startup_inventory_never_auto_approved(valid):
    assert e.classify(change(valid,'time.initial_inventory',True)).reason_codes==('STARTUP_INVENTORY_NO_AUTOMATIC_BACKFILL',)


@pytest.mark.parametrize('value,reason',[(NOW+timedelta(minutes=6),'FUTURE_OBSERVATION_OR_CLOCK_ERROR'),(NOW-timedelta(hours=3),'OBSERVATION_WINDOW_EXPIRED')])
def test_observation_age_is_not_content_age(valid,value,reason):
    item=replace(valid,policy=replace(valid.policy,activation_at=NOW-timedelta(hours=4),window=timedelta(hours=1)),time=replace(valid.time,first_seen_at=value))
    assert reason in e.classify(item).reason_codes


@pytest.mark.parametrize('category,readiness,expected',[
    (e.Category.REGULAR,e.Readiness.PUBLIC_RELEASED,e.Eligibility.ELIGIBLE),
    (e.Category.SHORTS,e.Readiness.PUBLIC_RELEASED,e.Eligibility.REJECTED),
    (e.Category.STREAM,e.Readiness.UPCOMING,e.Eligibility.HOLD_FOR_REVIEW),
    (e.Category.STREAM,e.Readiness.LIVE,e.Eligibility.HOLD_FOR_REVIEW),
    (e.Category.STREAM,e.Readiness.COMPLETED_REPLAY,e.Eligibility.ELIGIBLE)])
def test_category_and_readiness(valid,category,readiness,expected):
    item=replace(valid,category=replace(valid.category,category=category),release=replace(valid.release,readiness=readiness,ended_at=NOW-timedelta(minutes=5)))
    d=e.classify(item);assert d.eligibility is expected and not d.delivery_authorized and not d.enqueue


@pytest.mark.parametrize('end',[None,NOW+timedelta(seconds=1),NOW.replace(tzinfo=None)])
def test_replay_end_must_be_valid(valid,end):
    item=replace(valid,category=replace(valid.category,category=e.Category.STREAM),release=replace(valid.release,readiness=e.Readiness.COMPLETED_REPLAY,ended_at=end))
    assert e.classify(item).eligibility is e.Eligibility.HOLD_FOR_REVIEW


@pytest.mark.parametrize('source',['baseline_ids','cache_ids'])
def test_historical_suppression(valid,source):
    d=e.classify(replace(valid,history=replace(valid.history,**{source:frozenset({VID})})))
    assert d.knowledge is e.Knowledge.KNOWN and d.eligibility is e.Eligibility.REJECTED and d.delivery_state is e.DeliveryState.NONE


def test_delivered_is_terminal(valid):
    item=replace(valid,video=None,captures=(),policy=replace(valid.policy,approved=False),journal=e.JournalEvidence(e.DeliveryState.DELIVERED,'youtube:video:'+VID+':initial',True))
    assert e.classify(item).eligibility is e.Eligibility.ALREADY_DELIVERED


@pytest.mark.parametrize('state',[e.DeliveryState.PENDING,e.DeliveryState.PARTIAL,e.DeliveryState.UNVERIFIED])
def test_no_automatic_recovery(valid,state):
    d=e.classify(replace(valid,journal=e.JournalEvidence(state,'youtube:video:'+VID+':initial')))
    assert d.delivery_state is state and d.eligibility is e.Eligibility.HOLD_FOR_REVIEW


def test_ambiguous_outcome_holds(valid):
    assert e.classify(replace(valid,journal=e.JournalEvidence(ambiguous_outcome=True))).reason_codes==('RECOVERY_OUTCOME_AMBIGUOUS',)


def test_absence_is_not_deletion(valid):
    assert e.classify(change(valid,'captures.1.tabs',frozenset())).reason_codes==('NOT_VISIBLE_IN_CAPTURE_NOT_DELETED',)


def test_metadata_and_purity(valid):
    before=deepcopy(valid);first=e.classify(valid);changed=e.classify(replace(valid,video=replace(valid.video,title='Changed title')))
    assert before==valid and first.event_key==changed.event_key and first.eligibility==changed.eligibility
    for cls in (e.EligibilityInput,e.TimeEvidence,e.VideoEvidence):
        assert not {'rss','published','published_at','earliest_published'} & {f.name for f in fields(cls)}


def test_active_graph_has_no_rss_fallback():
    root=Path(__file__).resolve().parents[1]
    for name in ('monitor_runner.py','youtube_eligibility.py','youtube_tabs_monitor.py','youtube_channel_tabs.py',
                 'youtube_shadow_live.py','youtube_shadow.py','youtube_videos_preflight.py'):
        tree=ast.parse((root/name).read_text(encoding='utf-8'))
        imports={n.module for n in ast.walk(tree) if isinstance(n,ast.ImportFrom)}
        imports|={a.name for n in ast.walk(tree) if isinstance(n,ast.Import) for a in n.names}
        assert not {'youtube_monitor','youtube_hybrid'} & imports
    assert not (root/'youtube_hybrid.py').exists()
