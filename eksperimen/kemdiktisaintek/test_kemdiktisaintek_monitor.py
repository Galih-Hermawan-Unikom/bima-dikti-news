"""Offline state-machine, CAS, source failure and production-isolation tests."""
from copy import deepcopy
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import socket
from unittest.mock import Mock
from urllib.parse import parse_qs, urlsplit, urlencode
from uuid import uuid5, NAMESPACE_URL
from html import escape

import pytest
import requests
import yaml

from kemdiktisaintek_parser import BASE, FetchError
from kemdiktisaintek_policy import classify, POLICY_VERSION
from kemdiktisaintek_state import (LocalStateStore, StateError, StateConflict, STATE_BRANCH, STATE_PATH,
    MockContentsTransport, ContentsCAS, fingerprint, timestamp, validate_state)
from kemdiktisaintek_monitor import (Capture, CaptureError, capture_window, initialize, apply_poll, poll)
from kemdiktisaintek_cli import main

HERE = Path(__file__).parent
AT = '2026-10-24T10:00:00+00:00'
LATER = '2026-10-24T10:10:00+00:00'


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Network and Telegram forbidden in Stage 2 tests')
    monkeypatch.setattr(requests.sessions.Session, 'request', forbidden)
    monkeypatch.setattr(socket.socket, 'connect', forbidden)


def item(name='known', published='2026-10-23T09:00:00+00:00', title=None):
    result = deepcopy(json.loads((HERE/'sample_output.json').read_text(encoding='utf-8'))['items'][2])
    identifier = str(uuid5(NAMESPACE_URL, name))
    result.update(id=identifier, identity='kemdiktisaintek:'+identifier, slug=name,
        url=BASE+'/announcement/article/'+name, title=title or 'Penawaran Hibah Penelitian untuk Dosen',
        body_text='Perguruan tinggi membuka pendaftaran pendanaan riset bagi dosen dan peneliti.',
        category='Umum', published_at=published, updated_at=published, publication_basis='detail_datetime',
        letter_number=None, deadline_evidence=[], attachments=[], external_links=[])
    return rehash(result)


def rehash(result):
    result['content_sha256'] = fingerprint({k:result.get(k) for k in (
        'title','category','body_text','attachments','external_links')})
    return result


def capture(items, at=AT, duplicates=None):
    count=len(items)
    assert count <= 10
    return Capture(at, deepcopy(items), {'requested_pages':2,'captured_pages':1,
        'pages':[{'page':1,'url':BASE+'/api/announcement?'+urlencode({'page':1,'size':10,'search':''}),
                  'count':count,'total':count,'method':'public_json_api','healthy':True}],
        'total_reported':count,'listing_count':count,'detail_count':count,
        'detail_budget':10,'archive_complete':False,'all_requested_pages_healthy':True,'unfiltered':True}, duplicates or [])


@pytest.fixture
def store(tmp_path):
    return LocalStateStore(tmp_path/STATE_BRANCH)


@pytest.fixture
def initialized(store):
    return initialize(store, capture([item()]))


def transition(initialized, new):
    return apply_poll(initialized, capture([item(),new], LATER))


def test_baseline_persists_coverage_and_fence(store):
    state=initialize(store,capture([item()]))
    loaded,sha=store.load()
    assert loaded==state and len(sha)==64
    assert state['baseline']['ids']==[item()['id']]
    assert state['baseline']['archive_complete'] is False and state['activation_at']==AT
    assert state['outbox']=={} and state['delivery_authorized'] is False
    assert state['observations'][item()['id']]['detection_status']=='KNOWN'


def test_reinitialization_never_overwrites(store,initialized):
    before=store.path.read_bytes()
    with pytest.raises(StateConflict,match='BASELINE_ALREADY_EXISTS'):
        initialize(store,capture([item('other')]))
    assert store.path.read_bytes()==before


def test_restart_preserves_first_seen(store,initialized):
    reopened=LocalStateStore(store.root)
    before,sha=reopened.load()
    after,report=transition(before,item('new','2026-10-24T10:05:00+00:00'))
    reopened.commit(after,sha)
    final,_=LocalStateStore(store.root).load()
    assert final['observations'][item()['id']]['first_seen_at']==AT
    assert final['observations'][item('new')['id']]['first_seen_at']==LATER


def test_missing_state_stops_before_network(store):
    client=Mock()
    with pytest.raises(StateError,match='STATE_MISSING'):
        poll(store,client,at=LATER)
    client.fetch.assert_not_called()


@pytest.mark.parametrize('raw',[b'',b'{',b'[]',b'{"schema_version":1}'])
def test_corrupt_state_fails_without_overwrite(store,raw):
    store.path.parent.mkdir(parents=True)
    store.path.write_bytes(raw)
    with pytest.raises(StateError):store.load()
    assert store.path.read_bytes()==raw


@pytest.mark.parametrize('pub', ['2026-10-23T10:00:00Z', AT, '2020-01-01T00:00:00Z',None,'2026-10-24'])
def test_historical_backdated_and_missing_date_hold(initialized,pub):
    new=item('new',pub)
    after,_=transition(initialized,new)
    assert after['observations'][new['id']]['detection_status']=='HOLD'


def test_new_relevant_candidate_and_then_known(initialized):
    new=item('new','2026-10-24T10:05:00Z')
    after,report=transition(initialized,new)
    row=after['observations'][new['id']]
    assert row['detection_status']=='NEW_CANDIDATE' and row['classification']['decision']=='RELEVANT'
    assert after['outbox']=={} and report['telegram_calls']==0
    again,_=apply_poll(after,capture([item(),new],'2026-10-24T10:20:00Z'))
    assert again['observations'][new['id']]['detection_status']=='KNOWN'
    assert again['observations'][new['id']]['initial_decision']=='NEW_CANDIDATE'


def test_relevant_is_not_send_eligibility(initialized):
    old=item('old','2026-01-01T00:00:00Z')
    after,_=transition(initialized,old)
    assert after['observations'][old['id']]['classification']['decision']=='RELEVANT'
    assert after['observations'][old['id']]['detection_status']=='HOLD'


def test_nonacademic_new_rejected(initialized):
    new=item('school','2026-10-24T10:05:00Z','Seleksi Kepala Tata Usaha SMA Unggul Garuda Baru')
    after,_=transition(initialized,new)
    assert after['observations'][new['id']]['detection_status']=='REJECTED'


def test_unknown_relevance_needs_review(initialized):
    new=item('unknown','2026-10-24T10:05:00Z','Pengumuman administratif')
    new['body_text']='Informasi tambahan untuk masyarakat.';rehash(new)
    after,_=transition(initialized,new)
    assert after['observations'][new['id']]['detection_status']=='NEEDS_REVIEW'


def test_new_candidate_does_not_require_bima_reference(initialized):
    new=item('new','2026-10-24T10:05:00Z')
    after,_=transition(initialized,new)
    assert after['observations'][new['id']]['detection_status']=='NEW_CANDIDATE'


@pytest.mark.parametrize('mode', ['body','attachments','dates','url','metadata','deadline'])
def test_revision_evidence(initialized,mode):
    changed=item()
    if mode=='body':changed['body_text']+=' Ketentuan penelitian direvisi.'
    if mode=='attachments':changed['attachments']=[{'url':BASE+'/new.pdf','kind':'pdf','label':'Revisi'}]
    if mode=='dates':changed['published_at']='2026-10-23T10:00:00Z'
    if mode=='url':changed.update(slug='changed',url=BASE+'/announcement/article/changed')
    if mode=='metadata':changed['title']+=' Revisi'
    if mode=='deadline':changed['deadline_evidence']=['Batas pendaftaran diperpanjang']
    rehash(changed)
    after,_=apply_poll(initialized,capture([changed],LATER))
    revision=after['revisions'][0]
    assert revision['event_type']=='REVISION_CANDIDATE' and revision['status']=='NEEDS_REVIEW'
    assert mode in revision['changes']
    assert after['outbox']=={}
    assert after['observations'][changed['id']]['detection_status']=='NEEDS_REVIEW'
    again,_=apply_poll(after,capture([changed],'2026-10-24T10:20:00Z'))
    assert len(again['revisions'])==1
    assert again['observations'][changed['id']]['detection_status']=='NEEDS_REVIEW'


def test_detail_hydration_of_list_only_baseline_is_not_a_revision(initialized):
    previous=deepcopy(initialized)
    row=previous['observations'][item()['id']]
    listing=row['last_item']
    for key in ('body_text','letter_number','deadline_evidence','attachments','external_links','method'):
        listing.pop(key,None)
    listing.update(content_sha256=None,publication_basis='listing_display_timestamp',updated_at=None)
    row.update(content_sha256=None,detail_checked_at=None)
    validate_state(previous)

    hydrated=item()
    hydrated['letter_number']='2026/DST/DT.04.02/1'
    hydrated['attachments']=[{'url':BASE+'/notice.pdf','kind':'pdf','label':'Pengumuman'}]
    rehash(hydrated)
    after,report=apply_poll(previous,capture([hydrated],LATER))

    assert after['revisions']==[]
    assert report['revision_count']==0
    assert after['observations'][hydrated['id']]['detection_status']=='KNOWN'
    assert after['observations'][hydrated['id']]['last_item']['content_sha256']==hydrated['content_sha256']


def test_category_display_normalization_does_not_create_fingerprint_revision(initialized):
    previous=deepcopy(initialized)
    old_row=previous['observations'][item()['id']]
    old_row['last_item']['category']='Sains Dan-teknologi'
    rehash(old_row['last_item'])
    old_row['content_sha256']=old_row['last_item']['content_sha256']
    validate_state(previous)

    changed=deepcopy(old_row['last_item']); changed['category']='Sains-dan-teknologi'; rehash(changed)
    after,report=apply_poll(previous,capture([changed],LATER))

    assert after['revisions']==[]
    assert report['revision_count']==0
    assert after['observations'][changed['id']]['detection_status']=='KNOWN'


def test_substantive_category_change_remains_a_metadata_revision(initialized):
    changed=item(); changed['category']='Pendidikan Tinggi'; rehash(changed)
    after,_=apply_poll(initialized,capture([changed],LATER))
    assert len(after['revisions'])==1
    assert after['revisions'][0]['changes']['metadata']['category']=={
        'before':'Umum','after':'Pendidikan Tinggi'}


def test_absent_temporarily_not_deleted(initialized):
    after,_=apply_poll(initialized,capture([],LATER))
    assert item()['id'] in after['observations']
    assert after['observations'][item()['id']]['last_seen_at']==AT


def test_fallback_identity_and_return_require_reconciliation(initialized):
    fallback=item();fallback.update(id=None,identity_basis='url',identity='kemdiktisaintek:url:'+fallback['url'])
    after,report=apply_poll(initialized,capture([fallback],LATER))
    assert report['events'][0]['status']=='HOLD' and len(after['identity_holds'])==1
    assert after['observations'][item()['id']]['detection_status']=='NEEDS_REVIEW'
    again,_=apply_poll(after,capture([item()],'2026-10-24T10:20:00Z'))
    assert again['observations'][item()['id']]['detection_status']=='NEEDS_REVIEW'


def test_new_uuid_same_historical_url_is_hold(initialized):
    conflict=item('conflict');conflict['url']=item()['url']
    after,report=apply_poll(initialized,capture([conflict],LATER))
    assert report['events'][0]['status']=='HOLD'
    assert conflict['id'] not in after['observations']


def test_same_uuid_different_urls_in_capture_fails(initialized):
    conflict=item();conflict['url']=BASE+'/announcement/article/other'
    with pytest.raises(ValueError):
        apply_poll(initialized,capture([item(),conflict],LATER))


@pytest.mark.parametrize('context',['gap','overlap','duplicates'])
def test_unsafe_pagination_and_gaps_hold(initialized,context):
    new=item('new','2026-10-24T10:05:00Z')
    at='2026-10-24T11:00:00Z' if context=='gap' else LATER
    items=[new] if context=='overlap' else [item(),new]
    after,_=apply_poll(initialized,capture(items,at,[new['id']] if context=='duplicates' else []))
    assert after['observations'][new['id']]['detection_status']=='HOLD'


def test_hold_after_gap_recovers_only_after_a_clean_poll(initialized):
    new=item('new','2026-10-24T10:05:00Z')
    after,_=apply_poll(initialized,capture([item(),new],'2026-10-24T11:00:00Z'))
    assert after['observations'][new['id']]['detection_status']=='HOLD'
    again,_=apply_poll(after,capture([item(),new],'2026-10-24T11:10:00Z'))
    assert again['observations'][new['id']]['detection_status']=='NEW_CANDIDATE'


def test_partial_capture_never_mutates_previous(initialized):
    before=deepcopy(initialized);cap=capture([item()],LATER);cap.healthy=False
    with pytest.raises(CaptureError):apply_poll(initialized,cap)
    assert initialized==before


@pytest.mark.parametrize('topic',['hibah penelitian','pengabdian kepada masyarakat','pendanaan riset',
    'beasiswa dosen','program doktor','reviewer penelitian','akreditasi perguruan tinggi',
    'jabatan akademik dosen','regulasi pendidikan tinggi'])
def test_high_priority_classifier(topic):
    row=item(title='Penawaran '+topic+' bagi dosen perguruan tinggi')
    result=classify(row)
    assert result['decision']=='RELEVANT' and result['priority']=='high'
    assert result['reasons'] and result['policy_version']==POLICY_VERSION


@pytest.mark.parametrize('topic',['seminar ilmiah','kompetisi mahasiswa','pelatihan','webinar','pengembangan kompetensi'])
def test_medium_priority_classifier(topic):
    row=item(title=topic+' bagi mahasiswa');row['body_text']='Program kompetensi bagi sivitas perguruan tinggi.'
    assert classify(row)['priority']=='medium'


def test_single_keyword_and_category_insufficient():
    assert classify({'title':'Reviewer','category':'Riset dan Pengembangan'})['decision']=='NEEDS_REVIEW'
    assert classify({'title':'Informasi','category':'Pendidikan Tinggi'})['decision']=='NEEDS_REVIEW'


def test_real_stage1_sample_classifications():
    rows=json.loads((HERE/'sample_output.json').read_text(encoding='utf-8'))['items']
    decisions=[classify(row)['decision'] for row in rows]
    assert decisions[:4]==['NOT_RELEVANT','NOT_RELEVANT','RELEVANT','RELEVANT']


def test_new_relevant_admission_does_not_require_cross_source_reference(initialized):
    new=item('new','2026-10-24T10:05:00Z')
    after,report=transition(initialized,new)
    assert after['observations'][new['id']]['detection_status']=='NEW_CANDIDATE'
    assert report['telegram_calls']==0
    assert 'deduplication' not in after['observations'][new['id']]


def test_old_bima_blocked_observation_is_not_promoted(initialized):
    new=item('new','2026-10-24T10:05:00Z')
    previous,_=transition(initialized,new)
    row=previous['observations'][new['id']]
    row.update(detection_status='HOLD',reasons=['BIMA_REFERENCE_UNAVAILABLE'],
               deduplication={'status':'NEEDS_REVIEW','reason':'BIMA_REFERENCE_MISSING'})
    later='2026-10-24T10:20:00Z'
    after,report=apply_poll(previous,capture([item(),new],later))
    held=after['observations'][new['id']]
    assert held['detection_status']=='HOLD'
    assert held['reasons']==['BIMA_REFERENCE_UNAVAILABLE']
    assert report['events'][-1]['status']=='HOLD'


def test_local_cas_conflict(store,initialized):
    current,sha=store.load();one,_=transition(current,item('one','2026-10-24T10:05:00Z'))
    store.commit(one,sha);before=store.path.read_bytes()
    two,_=transition(current,item('two','2026-10-24T10:05:00Z'))
    with pytest.raises(StateConflict):store.commit(two,sha)
    assert store.path.read_bytes()==before


def test_mock_blob_cas_and_isolation(initialized):
    transport=MockContentsTransport();backend=ContentsCAS(transport)
    sha=backend.create(initialized);assert len(sha)==40
    old,_=backend.load();changed,_=transition(old,item('new','2026-10-24T10:05:00Z'))
    backend.commit(changed,sha)
    with pytest.raises(StateConflict):backend.commit(changed,sha)
    assert all(branch==STATE_BRANCH and path==STATE_PATH for _,branch,path in transport.calls)


@pytest.mark.parametrize('branch',['master','main','dikti-production-state','youtube-state','other'])
def test_disallow_other_remote_branches(branch):
    with pytest.raises(StateError):ContentsCAS(MockContentsTransport(),branch=branch)


@pytest.mark.parametrize('path',['announcements_cache.json','youtube_cache.json','delivery_state.json','../x',STATE_PATH+'.bak'])
def test_disallow_other_remote_paths(path):
    with pytest.raises(StateError):ContentsCAS(MockContentsTransport(),path=path)


def test_real_transport_impossible():
    with pytest.raises(StateError):ContentsCAS(requests.Session())


def test_local_root_must_be_explicitly_allowlisted(tmp_path):
    with pytest.raises(StateError):LocalStateStore(tmp_path/'youtube-state')


@pytest.mark.parametrize('field,value',[('source','bima'),('schema_version',2),('outbox',{'event':{}}),
    ('delivery_authorized',True),('initialized',False),('policy_version','other')])
def test_invalid_or_sendable_state_rejected(initialized,field,value):
    bad=deepcopy(initialized);bad[field]=value
    with pytest.raises(StateError):validate_state(bad)


class FakeClient:
    def __init__(self, *, fail_page=None, partial=False, duplicate=False):
        self.urls=[];self.fail_page=fail_page;self.partial=partial;self.duplicate=duplicate
        self.pages=[json.loads((HERE/'fixtures'/n).read_text()) for n in ['api-list.json','api-page2.json']]
        if duplicate:self.pages[1]['data'][0]=deepcopy(self.pages[0]['data'][-1])
        if partial:self.pages[1]['data'].pop()
        self.rows={r['slug']:r for p in self.pages for r in p['data']}

    def fetch(self,url):
        self.urls.append(url)
        if '/api/announcement?' in url:
            page=int(parse_qs(urlsplit(url).query)['page'][0])
            if page==self.fail_page:raise FetchError('FIXTURE_HTTP_FAILURE')
            return json.dumps(self.pages[page-1])
        row=self.rows[urlsplit(url).path.split('/')[-1]]
        detail={**row,'datetime':row['updated_at'],'source':None}
        return '<div><h1>'+escape(row['title'])+'</h1><article class="rich-content">Penawaran program akademik untuk dosen.</article></div><script id="__NEXT_DATA__">'+json.dumps({'props':{'pageProps':{'data':detail}}})+'</script>'


def test_capture_all_pages_unfiltered_and_bounded():
    client=FakeClient();cap=capture_window(client,at=AT,pages=2,detail_budget=4)
    assert len(cap.items)==20 and cap.coverage['detail_count']==4 and len(client.urls)==6
    assert all('category=' not in url for url in client.urls)
    assert cap.coverage['archive_complete'] is False


def test_capture_pagination_shift_duplicate_detected():
    cap=capture_window(FakeClient(duplicate=True),at=AT,pages=2,detail_budget=1)
    assert len(cap.items)==19 and len(cap.duplicate_ids)==1


@pytest.mark.parametrize('case',['http','partial'])
def test_poll_failed_capture_leaves_checkpoint_identical(store,initialized,case):
    before=store.path.read_bytes();client=FakeClient(fail_page=2 if case=='http' else None,partial=case=='partial')
    with pytest.raises((FetchError,CaptureError)):poll(store,client,at=LATER)
    assert store.path.read_bytes()==before


def test_checkpoint_failure_no_success_report(store,initialized,monkeypatch):
    before=store.path.read_bytes()
    monkeypatch.setattr(store,'commit',Mock(side_effect=StateConflict('CONFLICT')))
    with pytest.raises(StateConflict):poll(store,FakeClient(),at=LATER)
    assert store.path.read_bytes()==before


def test_cli_missing_or_existing_state_does_not_fetch(store,initialized,monkeypatch):
    client=Mock();monkeypatch.setattr('kemdiktisaintek_cli.AnnouncementClient',client)
    assert main(['initialize','--state-dir',str(store.root)])==1
    store.path.unlink()
    assert main(['poll','--state-dir',str(store.root)])==1
    client.assert_not_called()


@pytest.mark.parametrize('change',['first_seen','baseline','delete','outbox'])
def test_checkpoint_lineage_is_immutable(store,initialized,change):
    old,sha=store.load();new,_=transition(old,item('new','2026-10-24T10:05:00Z'))
    store.commit(new,sha);current,sha=store.load()
    next_state,_=apply_poll(current,capture([item()],'2026-10-24T10:20:00Z'))
    if change=='first_seen':next_state['observations'][item()['id']]['first_seen_at']=LATER
    if change=='baseline':
        next_state['baseline']['coverage']['detail_budget']=9
        next_state['baseline']['sha256']=fingerprint({k:v for k,v in next_state['baseline'].items() if k!='sha256'})
    if change=='delete':next_state['observations'].pop(item('new')['id'])
    if change=='outbox':next_state['outbox']={'test':{'status':'reserved'}}
    before=store.path.read_bytes()
    with pytest.raises(StateError):store.commit(next_state,sha)
    assert store.path.read_bytes()==before


def test_missing_capture_items_detected(initialized):
    cap=capture([item()],LATER);cap.items=[];cap.coverage['listing_count']=0
    with pytest.raises(CaptureError,match='CAPTURE_ITEMS_MISSING'):apply_poll(initialized,cap)


def test_listing_timestamp_cannot_admit_even_with_content(initialized):
    new=item('new','2026-10-24T10:05:00Z');new['publication_basis']='listing_display_timestamp'
    after,_=transition(initialized,new)
    assert 'UNTRUSTED_PUBLICATION_DATE' in after['observations'][new['id']]['reasons']


def test_future_article_held(initialized):
    new=item('new','2026-10-24T11:00:00Z');after,_=transition(initialized,new)
    assert 'FUTURE_PUBLICATION' in after['observations'][new['id']]['reasons']


def test_detail_failure_aborts_entire_poll(store,initialized):
    client=FakeClient();original=client.fetch
    def fail(url):
        if '/article/' in url:raise FetchError('DETAIL_TIMEOUT')
        return original(url)
    client.fetch=fail;before=store.path.read_bytes()
    with pytest.raises(FetchError):poll(store,client,at=LATER)
    assert store.path.read_bytes()==before


def test_rotating_known_article_refresh():
    client=FakeClient();first=capture_window(client,at=AT,pages=2,detail_budget=4)
    backend=ContentsCAS(MockContentsTransport());state=initialize(backend,first)
    refreshed=capture_window(client,state,at=LATER,pages=2,detail_budget=4)
    original_ids={i['id'] for i in first.items if 'body_text' in i}
    refreshed_ids={i['id'] for i in refreshed.items if 'body_text' in i}
    assert refreshed_ids and not original_ids.intersection(refreshed_ids)


def test_api_total_changed_aborts():
    client=FakeClient();client.pages[1]['total']+=1
    with pytest.raises(CaptureError,match='TOTAL_CHANGED'):capture_window(client,at=AT)


def test_local_atomic_write_failure(store,initialized,monkeypatch):
    old,sha=store.load();new,_=transition(old,item('new','2026-10-24T10:05:00Z'))
    before=store.path.read_bytes()
    monkeypatch.setattr('kemdiktisaintek_state.os.replace',Mock(side_effect=OSError('disk full')))
    with pytest.raises(StateError,match='CHECKPOINT_FAILED'):store.commit(new,sha)
    assert store.path.read_bytes()==before


def test_stage2_never_imports_sender_or_configuration():
    import ast
    for filename in ('kemdiktisaintek_monitor.py','kemdiktisaintek_policy.py','kemdiktisaintek_state.py','kemdiktisaintek_cli.py'):
        tree=ast.parse((HERE/filename).read_text())
        imports=[n.module for n in ast.walk(tree) if isinstance(n,ast.ImportFrom)]
        assert not set(imports)&{'notifier','monitor_runner','production_runtime','app_config','youtube_state_backend'}


def test_workflow_is_manual_read_only_and_inert():
    path=HERE/'kemdiktisaintek-collect.workflow.yml'
    body=path.read_text();workflow=yaml.safe_load(body)
    triggers=workflow.get('on',workflow.get(True))  # PyYAML YAML 1.1 boolean 'on'
    assert set(triggers)=={'workflow_dispatch'}
    assert workflow['permissions']=={'contents':'read'}
    assert 'secrets.' not in body and 'cron:' not in body and '--state-dir' not in body
    assert 'telegram' not in body.lower() and 'kemdiktisaintek_cli.py collect' in body
    checkout=workflow['jobs']['collect']['steps'][0]['with']
    assert checkout['persist-credentials'] is False and checkout['ref']=='${{ github.sha }}'
    assert '.github' not in path.parts


def test_offline_simulations_are_reproducible():
    from simulate_stage2 import run_simulation
    report=run_simulation()
    assert [r['status'] for r in report['cases']]==['HOLD','NEW_CANDIDATE','REJECTED','NEEDS_REVIEW','KNOWN']
    assert report['telegram_calls']==0 and report['delivery_authorized'] is False


def test_corrupt_content_hash_is_rejected(initialized):
    bad=deepcopy(initialized);bad['observations'][item()['id']]['last_item']['body_text']='Corrupted content'
    with pytest.raises(StateError,match='FINGERPRINT_MISMATCH'):validate_state(bad)


def test_lock_conflict_cannot_overwrite(store,initialized):
    state,sha=store.load();next_state,_=transition(state,item('new','2026-10-24T10:05:00Z'))
    store.path.with_suffix('.lock').write_text('previous operator process')
    with pytest.raises(StateConflict,match='STATE_LOCKED'):store.commit(next_state,sha)
    assert store.load()[0]==initialized


def test_remote_mock_409_stops_checkpoint(initialized,monkeypatch):
    transport=MockContentsTransport();backend=ContentsCAS(transport);sha=backend.create(initialized)
    next_state,_=transition(initialized,item('new','2026-10-24T10:05:00Z'))
    monkeypatch.setattr(transport,'put',Mock(return_value={'status':409}))
    with pytest.raises(StateConflict):backend.commit(next_state,sha)
    assert backend.load()[0]==initialized


def test_without_hydration_new_article_is_hold(initialized):
    new=item('new','2026-10-24T10:05:00Z');new.pop('body_text');new.pop('content_sha256')
    cap=capture([item(),new],LATER);cap.coverage['detail_count']=1
    after,_=apply_poll(initialized,cap)
    assert after['observations'][new['id']]['detection_status']=='HOLD'


def test_lost_state_cannot_be_reinitialized(store,initialized,monkeypatch):
    assert store.marker.is_file()
    store.path.unlink()
    with pytest.raises(StateError,match='RESTORE_REQUIRED'):initialize(store,capture([item()]))
    client=Mock();monkeypatch.setattr('kemdiktisaintek_cli.AnnouncementClient',client)
    assert main(['initialize','--state-dir',str(store.root)])==1
    client.assert_not_called()


def test_missing_initialization_marker_fails_closed(store,initialized):
    store.marker.unlink()
    with pytest.raises(StateError,match='MARKER_MISSING'):store.load()


def test_marker_mismatch_fails_closed(store,initialized):
    store.marker.write_text('{}')
    with pytest.raises(StateError,match='MARKER_CONFLICT'):store.load()


def test_mock_missing_initialized_state_cannot_recreate(initialized):
    transport=MockContentsTransport();backend=ContentsCAS(transport);backend.create(initialized)
    transport.raw=None;transport.sha=None
    with pytest.raises(StateError):backend.load()
    with pytest.raises(StateConflict):backend.create(initialized)
