"""Offline failure-injection tests. All live sockets and HTTP are forbidden."""
from copy import deepcopy
import json
from pathlib import Path
import socket
from unittest.mock import Mock

import pytest
import requests
import yaml

from stage3_fixtures import setup, NOW, OBSERVED, AT, TARGET, FixtureTelegram, Clock, Response
from kemdiktisaintek_state import StateError, StateConflict, fingerprint, canonical
from kemdiktisaintek_delivery_state import (event_key, change, migrate_v1, migrate_delivery_policy,
    LEGACY_DELIVERY_POLICY, DELIVERY_POLICY, validate_delivery_state, validate_delivery_lineage)
from kemdiktisaintek_delivery import Delivery, observe, ReconciliationRequired
from kemdiktisaintek_delivery_policy import eligibility
from kemdiktisaintek_remote_state import RemoteState, MemoryRemote, blob_sha, EMPTY_TREE_SHA
from kemdiktisaintek_telegram import (Credentials, Proof, TelegramGateway, format_announcement,
                                    format_test, payload, length, switches)
from kemdiktisaintek_monitor import capture_window
from kemdiktisaintek_stage3_runtime import run

HERE = Path(__file__).parent


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Live HTTP/Telegram forbidden')
    monkeypatch.setattr(requests.sessions.Session, 'request', forbidden)
    monkeypatch.setattr(socket.socket, 'connect', forbidden)


@pytest.fixture
def context():
    return setup()


def proof(c):
    return c['gateway'].inspect(at=c['clock']())


def reserve(c, p=None):
    return c['engine'].reserve_initial(c['source'].new_id, p or proof(c))


def send(c, key, p=None):
    return c['engine'].send(key, p or proof(c))


def test_preflight_reuses_existing_identity_permission_and_production_separation(context):
    p=proof(context)
    assert p.identity==context['gateway'].credentials.identity()
    assert context['telegram'].gets==['getChat','getMe','getChatMember','getChat']
    assert context['telegram'].posts==[]


@pytest.mark.parametrize('problem',['bot','destination','production','permission','hash','token'])
def test_wrong_preflight_never_posts(context,problem):
    c=context
    if problem=='bot':c['telegram'].bot_id=74
    if problem=='destination':c['telegram'].target='-1008888888888'
    if problem=='production':c['telegram'].production_id=TARGET
    if problem=='permission':c['telegram'].can_post=False
    if problem=='hash':c['env']['YOUTUBE_EXPERIMENT_CHAT_ID_SHA256']='0'*64
    if problem=='token':c['env']['YOUTUBE_EXPERIMENT_BOT_TOKEN']=''
    with pytest.raises((StateError,RuntimeError)):
        credentials=Credentials.from_environment(c['env'])
        TelegramGateway(credentials,session=c['telegram']).inspect(at=NOW)
    assert c['telegram'].posts==[]


def test_mapping_never_reads_production_keys(context):
    class Restricted(dict):
        def get(self,key,default=None):
            assert key.startswith('YOUTUBE_EXPERIMENT_')
            return super().get(key,default)
    Credentials.from_environment(Restricted(context['env']))
    context['env']['TELEGRAM_BOT_TOKEN']='production-must-never-be-used'
    context['env'].pop('YOUTUBE_EXPERIMENT_BOT_TOKEN')
    with pytest.raises(StateError):Credentials.from_environment(context['env'])
    assert 'synthetic_fixture_only' not in repr(context['gateway'].credentials)


@pytest.mark.parametrize('field',['KEMDIKTISAINTEK_EXPERIMENT_ENABLED','KEMDIKTISAINTEK_EXPERIMENT_AUTO_SEND'])
@pytest.mark.parametrize('value',['false','','TRUE','1',True])
def test_kill_switch_exact_true_only(context,field,value):
    context['env'][field]=value
    with pytest.raises(StateError,match='SWITCH_OFF'):reserve(context)
    assert not context['remote'].load()[0]['outbox'] and context['telegram'].posts==[]


def test_real_pipeline_sends_once_after_two_remote_checkpoints(context):
    c=context;p=proof(c);key=reserve(c,p)
    assert c['remote'].load()[0]['outbox'][key]['status']=='RESERVED'
    def before_post():
        row=c['remote'].load()[0]['outbox'][key]
        assert row['status']=='SENDING' and row['attempt_id']
        assert c['github'].history[-2]['outbox'][key]['status']=='RESERVED'
    c['telegram'].before_post=before_post
    assert send(c,key,p)['status']=='DELIVERED'
    row=c['remote'].load()[0]['outbox'][key]
    assert row['receipt']['ok'] is True and row['message_id']==101 and row['delivered_at']
    assert send(c,key,p)=={'status':'DELIVERED','post_attempted':False}
    assert len(c['telegram'].posts)==1


@pytest.mark.parametrize('outcome,error',[('timeout','TIMEOUT'),('http_error','HTTP_ERROR'),
    ('ok_false','TELEGRAM_REJECTED'),('invalid_receipt','INVALID_OR_WRONG_DESTINATION_RECEIPT')])
def test_attempt_errors_are_terminal_ambiguous(context,outcome,error):
    c=context;c['telegram'].outcome=outcome;p=proof(c);key=reserve(c,p)
    result=send(c,key,p)
    assert result['status']=='AMBIGUOUS' and result['error_classification']==error
    assert send(c,key,p)['post_attempted'] is False and len(c['telegram'].posts)==1
    assert 'SYNTHETIC_SECRET' not in json.dumps(c['remote'].load()[0])


def test_receipt_checkpoint_failure_never_retries(context):
    c=context;p=proof(c);key=reserve(c,p)
    c['github'].fail_put=c['github'].put_count+2  # claim succeeds, receipt CAS fails
    with pytest.raises(ReconciliationRequired) as exc:send(c,key,p)
    assert exc.value.receipt['message_id']==101
    assert c['remote'].load()[0]['outbox'][key]['status']=='SENDING'
    c['github'].fail_put=None
    assert send(c,key,p)['status']=='AMBIGUOUS'
    assert len(c['telegram'].posts)==1


@pytest.mark.parametrize('phase',['reservation','claim'])
def test_cas_failure_before_post_blocks(context,phase):
    c=context;p=proof(c)
    if phase=='reservation':
        c['github'].fail_put=c['github'].put_count+1
        with pytest.raises(StateConflict):reserve(c,p)
    else:
        key=reserve(c,p);c['github'].fail_put=c['github'].put_count+1
        with pytest.raises(StateConflict):send(c,key,p)
    assert c['telegram'].posts==[]


def test_lost_claim_ack_never_posts_then_restart_ambiguous(context):
    c=context;p=proof(c);key=reserve(c,p)
    c['github'].unknown_ack=c['github'].put_count+1
    with pytest.raises(StateError,match='OUTCOME_UNKNOWN'):send(c,key,p)
    assert c['telegram'].posts==[] and c['remote'].load()[0]['outbox'][key]['status']=='SENDING'
    assert send(c,key,p)['status']=='AMBIGUOUS' and c['telegram'].posts==[]


def test_restart_after_reserved_revalidates_then_sends(context):
    c=context;p=proof(c);key=reserve(c,p)
    c['engine']=Delivery(c['remote'],c['gateway'],environment=c['env'],clock=c['clock'])
    assert c['engine'].recover()==0
    assert send(c,key,p)['status']=='DELIVERED' and len(c['telegram'].posts)==1


def test_restart_after_sending_does_not_retry(context):
    c=context;p=proof(c);key=reserve(c,p);state,sha=c['remote'].load();new=change(state)
    new['outbox'][key].update(status='SENDING',attempt_id='a'*32,sending_claimed_at=NOW)
    c['remote'].checkpoint(new,sha)
    assert send(c,key,p)['status']=='AMBIGUOUS' and c['telegram'].posts==[]


def test_concurrent_workflow_cas_claim_conflict(context):
    c=context;p=proof(c);key=reserve(c,p)
    stale,sha=c['remote'].load();one=change(stale);two=change(stale)
    one['outbox'][key].update(status='SENDING',attempt_id='a'*32,sending_claimed_at=NOW)
    two['outbox'][key].update(status='SENDING',attempt_id='b'*32,sending_claimed_at=NOW)
    c['remote'].checkpoint(one,sha)
    with pytest.raises(StateConflict):c['remote'].checkpoint(two,sha)
    assert c['telegram'].posts==[]


@pytest.mark.parametrize('case',['historical','irrelevant','revision','legacy'])
def test_ineligible_events_never_reserve_or_post(case):
    c=setup(historical=case=='historical',irrelevant=case=='irrelevant')
    identifier=c['source'].known_id if case in {'revision','legacy'} else c['source'].new_id
    if case=='revision':
        c['source'].revised=True;state,_=c['remote'].load()
        observe(c['remote'],capture_window(c['source'],state['observation_state'],at='2026-10-24T10:12:00Z'))
        c['clock'].value='2026-10-24T10:13:00Z'
    with pytest.raises(StateError):c['engine'].reserve_initial(identifier,proof(c))
    assert c['remote'].load()[0]['outbox']=={} and c['telegram'].posts==[]


def test_known_on_next_poll_not_promoted(context):
    c=context;state,_=c['remote'].load()
    observe(c['remote'],capture_window(c['source'],state['observation_state'],at='2026-10-24T10:12:00Z'))
    c['clock'].value='2026-10-24T10:13:00Z'
    assert c['remote'].load()[0]['observation_state']['observations'][c['source'].new_id]['detection_status']=='KNOWN'
    with pytest.raises(StateError,match='CURRENT_CANDIDATE'):reserve(c)
    assert c['telegram'].posts==[]


@pytest.mark.parametrize('observed,now,expected',[
    ('2026-10-24T10:31:00Z','2026-10-24T10:32:00Z','NEW_CANDIDATE'),
    ('2026-10-24T10:45:00Z','2026-10-24T10:46:00Z','NEW_CANDIDATE'),
    ('2026-10-24T10:51:00Z','2026-10-24T10:52:00Z','HOLD')])
def test_real_gap_tolerance_is_distinct_from_freshness(observed,now,expected):
    c=setup(observed=observed,now=now)
    assert c['remote'].load()[0]['observation_state']['observations'][c['source'].new_id]['detection_status']==expected
    if expected=='NEW_CANDIDATE':assert reserve(c)
    else:
        with pytest.raises(StateError):reserve(c)


def test_restart_with_stale_evidence_not_eligible(context):
    c=context;c['clock'].value='2026-10-24T10:25:00Z'
    with pytest.raises(StateError):reserve(c)
    assert c['telegram'].posts==[]


def test_eligibility_has_no_cross_source_reference_inputs(context):
    import inspect
    parameters=set(inspect.signature(eligibility).parameters)
    assert not {'reference','reference_sha','reference_env','bima_history'} & parameters


def test_formatter_escapes_html_and_uses_article_deadline_only(context):
    state,_=context['remote'].load();item=deepcopy(state['observation_state']['observations'][context['source'].new_id]['last_item'])
    item['title']='Riset <script> & "contoh"';item['body_text']='Isi <b>mentah</b>. Kegiatan 30 Oktober 2026.'
    message=format_announcement(item)['text']
    assert '&lt;script&gt;' in message and '&lt;b&gt;mentah&lt;/b&gt;' in message
    assert 'Tenggat' not in message and 'Nomor surat' not in message
    item['deadline_evidence']=['Paling lambat 28 Oktober 2026']
    assert 'Tenggat' in format_announcement(item)['text']
    assert 'PDF:' in message and 'Sumber:' in message and 'WIB' in message


def test_payload_utf16_bound_and_deterministic_formatter(context):
    state,_=context['remote'].load();item=state['observation_state']['observations'][context['source'].new_id]['last_item']
    item['title']='😀<&'*1000;item['body_text']='😀<&'*5000
    message=format_announcement(item)
    assert length(message['text'])<=3900 and message==format_announcement(item)
    with pytest.raises(StateError):payload('😀'*2000)
    with pytest.raises(StateError):payload('')


def test_migration_explicit_and_cannot_replay_old_initial_decision(context):
    state,_=context['remote'].load();v1=state['observation_state'];v2=migrate_v1(v1,at=NOW)
    assert v2['admissions']=={} and v2['outbox']=={}
    assert context['source'].new_id in v2['migration']['legacy_ids']
    with pytest.raises(StateError):eligibility(v2,context['source'].new_id,at=NOW)
    assert v1['schema_version']==1


@pytest.mark.parametrize('field,value',[('branch','youtube-state'),('branch','dikti-production-state'),
    ('branch','master'),('path','announcements_cache.json'),('path','../x'),('repo','different/repo')])
def test_remote_scope_fixed(field,value):
    with pytest.raises(StateError):RemoteState('synthetic',**{field:value})


def test_remote_no_create_without_operator_approval(context):
    with pytest.raises(StateError,match='NOT_AUTHORIZED'):context['remote'].create(context['remote'].load()[0])
    assert context['github'].put_count==1  # only fixture observation, no init write


def test_terminal_attempt_cannot_be_reset(context):
    c=context;key=reserve(c);send(c,key)
    state,sha=c['remote'].load();bad=change(state);bad['outbox'][key]['status']='RESERVED'
    bad['outbox'][key].update(attempt_id=None,sending_claimed_at=None)
    with pytest.raises(StateError):c['remote'].checkpoint(bad,sha)
    assert len(c['telegram'].posts)==1


def test_kill_switch_rechecked_after_claim(context):
    c=context;p=proof(c);key=reserve(c,p);original=c['remote'].checkpoint
    def checkpoint(state,sha):
        result=original(state,sha)
        if state['outbox'][key]['status']=='SENDING':c['env']['KEMDIKTISAINTEK_EXPERIMENT_AUTO_SEND']='false'
        return result
    c['remote'].checkpoint=checkpoint
    with pytest.raises(StateError,match='SWITCH_OFF'):send(c,key,p)
    assert c['telegram'].posts==[]


def test_test_send_needs_operator_gate_and_is_labelled_once(context):
    c=context;p=proof(c)
    with pytest.raises(StateError):c['engine'].reserve_test('fixture-test-001',p,approval='')
    key=c['engine'].reserve_test('fixture-test-001',p,approval='SEND_ONE_EXPERIMENT_TEST')
    assert c['engine'].send(key,p)['status']=='DELIVERED'
    assert c['telegram'].posts[0]['text'].startswith('<b>PENGUJIAN')
    assert c['engine'].send(key,p)['post_attempted'] is False
    with pytest.raises(StateError):c['engine'].reserve_test('fixture-test-002',p,approval='SEND_ONE_EXPERIMENT_TEST')
    assert len(c['telegram'].posts)==1


def test_runtime_modes_without_approval_never_get_or_post(context):
    for mode in ('PREFLIGHT','SEND_TEST','INITIALIZE'):
        with pytest.raises(StateError):run(mode,{},backend=context['remote'],gateway=context['gateway'])
    assert context['telegram'].gets==[] and context['telegram'].posts==[]


def test_runtime_preflight_read_only_and_poll_no_post(context):
    c=context;env={**c['env'],'KEMDIKTISAINTEK_PREFLIGHT_APPROVAL':'READ_ONLY_PREFLIGHT'}
    report=run('PREFLIGHT',env,backend=c['remote'],gateway=c['gateway'],clock=c['clock'])
    assert report['post_attempted'] is False
    c['clock'].value='2026-10-24T10:12:00Z'
    run('POLL',{},backend=c['remote'],source=c['source'],clock=c['clock'])
    assert c['telegram'].posts==[]


def test_workflow_dispatch_only_default_off_no_production_credentials():
    text=(HERE.parents[1]/'.github/workflows/kemdiktisaintek-experiment.yml').read_text()
    workflow=yaml.safe_load(text);triggers=workflow.get('on',workflow.get(True))
    assert list(triggers)==['workflow_dispatch']
    assert triggers['workflow_dispatch']['inputs']['mode']['default']=='COLLECT'
    assert set(triggers['workflow_dispatch']['inputs']['mode']['options'])=={'PREFLIGHT','COLLECT','INITIALIZE','POLL','SEND_TEST'}
    assert "|| 'false'" in text and 'cancel-in-progress: false' in text
    assert 'secrets.TELEGRAM_BOT_TOKEN' not in text and 'secrets.TELEGRAM_CHAT_ID' not in text
    assert 'youtube-state' not in text and 'schedule:' not in text


def test_e2e_simulation_all_eight_cases_offline():
    from simulate_stage3 import run_simulation
    report=run_simulation()
    assert [r['case'] for r in report['cases']]==list('ABCDEFGH')
    assert [r['status'] for r in report['cases']]==['DELIVERED','DELIVERED','HOLD','REJECTED',
        'NEEDS_REVIEW','AMBIGUOUS','BLOCKED','BLOCKED']
    assert report['live_http_requests']==0 and report['live_telegram_posts']==0


def test_observation_admission_is_independent_of_bima(context):
    c=context;state,_=c['remote'].load();old_count=len(state['admissions'])
    cap=capture_window(c['source'],state['observation_state'],at='2026-10-24T10:12:00Z')
    report=observe(c['remote'],cap)
    assert len(c['remote'].load()[0]['admissions'])==old_count
    assert c['telegram'].posts==[]


def test_local_only_backend_cannot_send(context):
    c=context;c['remote'].durable=False
    with pytest.raises(StateError,match='REMOTE_CHECKPOINT_REQUIRED'):reserve(c)
    assert c['telegram'].posts==[]


def test_stale_or_changed_preflight_proof_blocks(context):
    c=context;p=proof(c);c['clock'].value='2026-10-24T10:17:00Z'
    with pytest.raises(StateError,match='PREFLIGHT'):reserve(c,p)
    c['clock'].value=NOW
    bad=Proof({**p.identity,'chat_sha256':'0'*64},NOW)
    with pytest.raises(StateError,match='PREFLIGHT'):reserve(c,bad)
    assert c['telegram'].posts==[]


def test_changed_payload_after_reservation_fails_state_validation(context):
    c=context;key=reserve(c);state,sha=c['remote'].load();bad=change(state)
    bad['outbox'][key]['payload']['text']='Altered'
    with pytest.raises(StateError):c['remote'].checkpoint(bad,sha)
    assert c['telegram'].posts==[]


def test_changed_destination_after_reservation_cannot_be_claimed(context):
    c=context;key=reserve(c);state,sha=c['remote'].load();bad=change(state)
    bad['outbox'][key]['destination']['chat_sha256']='0'*64
    with pytest.raises(StateError,match='IMMUTABLE'):c['remote'].checkpoint(bad,sha)
    assert c['telegram'].posts==[]


def test_failed_observation_cas_creates_no_durable_admission(context):
    c=context;state,_=c['remote'].load();before=deepcopy(state)
    c['github'].fail_put=c['github'].put_count+1
    with pytest.raises(StateConflict):
        observe(c['remote'],capture_window(c['source'],state['observation_state'],at='2026-10-24T10:12:00Z'))
    assert c['remote'].load()[0]==before and c['telegram'].posts==[]


def test_revision_key_distinct_and_revision_not_allowed(context):
    c=context;identifier=c['source'].new_id
    assert event_key(identifier)!=event_key(identifier,'REVISION','a'*64)
    with pytest.raises(StateError):event_key(identifier,'REVISION','invalid')
    assert c['telegram'].posts==[]


def test_lost_successful_receipt_ack_does_not_duplicate(context):
    c=context;p=proof(c);key=reserve(c,p)
    c['github'].unknown_ack=c['github'].put_count+2
    with pytest.raises(ReconciliationRequired):send(c,key,p)
    assert c['remote'].load()[0]['outbox'][key]['status']=='DELIVERED'
    assert send(c,key,p)['post_attempted'] is False and len(c['telegram'].posts)==1


def test_failed_reservation_ack_does_not_post_and_can_safely_resume(context):
    c=context;p=proof(c);c['github'].unknown_ack=c['github'].put_count+1
    with pytest.raises(StateError):reserve(c,p)
    assert c['telegram'].posts==[]
    c['github'].unknown_ack=None
    key=reserve(c,p)
    assert send(c,key,p)['status']=='DELIVERED' and len(c['telegram'].posts)==1


def test_formatter_distinguishes_drive_and_external_links(context):
    state,_=context['remote'].load();item=state['observation_state']['observations'][context['source'].new_id]['last_item']
    item['attachments'].append({'url':'https://drive.google.com/drive/folders/fixture','kind':'external_document','label':'Drive'})
    item['external_links']=[{'url':'https://example.invalid/register?x=1&y=2'}]
    text=format_announcement(item)['text']
    assert 'Google Drive/Dokumen eksternal' in text and 'Tautan eksternal' in text and '&amp;' in text


def test_runtime_send_test_complete_offline_gate(context):
    from kemdiktisaintek_remote_state import REPOSITORY
    c=context;env={**c['env'],'KEMDIKTISAINTEK_SEND_TEST_APPROVAL':'SEND_ONE_EXPERIMENT_TEST',
        'GITHUB_SHA':'a'*40,'KEMDIKTISAINTEK_REVIEWED_CODE_SHA':'a'*40,'GITHUB_ACTIONS':'true',
        'GITHUB_EVENT_NAME':'workflow_dispatch','GITHUB_REPOSITORY':REPOSITORY,'KEMDIKTISAINTEK_TEST_ID':'fixture-live-test'}
    result=run('SEND_TEST',env,backend=c['remote'],gateway=c['gateway'],clock=c['clock'])
    assert result['status']=='DELIVERED' and len(c['telegram'].posts)==1
    assert c['telegram'].posts[0]['text'].startswith('<b>PENGUJIAN')


@pytest.mark.parametrize('field,value',[('GITHUB_ACTIONS','false'),('GITHUB_EVENT_NAME','push'),
    ('GITHUB_REPOSITORY','other/repo'),('GITHUB_SHA','x'),('KEMDIKTISAINTEK_REVIEWED_CODE_SHA','b'*40)])
def test_send_test_rejects_wrong_reviewed_runtime(context,field,value):
    c=context;env={**c['env'],'KEMDIKTISAINTEK_SEND_TEST_APPROVAL':'SEND_ONE_EXPERIMENT_TEST',
        'GITHUB_SHA':'a'*40,'KEMDIKTISAINTEK_REVIEWED_CODE_SHA':'a'*40,'GITHUB_ACTIONS':'true',
        'GITHUB_EVENT_NAME':'workflow_dispatch','GITHUB_REPOSITORY':'Galih-Hermawan-Unikom/bima-dikti-news'}
    env[field]=value
    with pytest.raises(StateError):run('SEND_TEST',env,backend=c['remote'],gateway=c['gateway'],clock=c['clock'])
    assert c['telegram'].posts==[] and c['telegram'].gets==[]


def test_remote_reads_reject_corrupt_blob_and_redirect(context):
    c=context;original=c['github'].get
    c['github'].get=Mock(return_value=Response(302,{}))
    with pytest.raises(StateError):c['remote'].load()
    body=original('https://api.github.com/repos/fake/contents/'+__import__('kemdiktisaintek_state').STATE_PATH+
                  '?ref=kemdiktisaintek-experiment-state',allow_redirects=False).json()
    body['sha']='0'*40;c['github'].get=Mock(return_value=Response(200,body))
    with pytest.raises(StateError):c['remote'].load()
    assert c['telegram'].posts==[]


def test_schema_requires_remote_reservation_before_claim(context):
    c=context;key=reserve(c);state,sha=c['remote'].load();row=deepcopy(state['outbox'][key])
    state['outbox'].pop(key)
    # A new SENDING row is invalid lineage even with a valid-shaped attempt.
    bad=change(state);row.update(status='SENDING',attempt_id='a'*32,sending_claimed_at=NOW);bad['outbox'][key]=row
    with pytest.raises(StateError,match='START_RESERVED'):validate_delivery_lineage(state,bad)


def test_explicit_remote_migration_keeps_all_old_history_quarantined(context):
    from stage3_fixtures import FixtureGitHub
    c=context;v1=c['remote'].load()[0]['observation_state'];transport=FixtureGitHub(v1)
    remote=RemoteState('fixture-github',session=transport);sha=transport.sha
    with pytest.raises(StateError):remote.load()
    with pytest.raises(StateError):remote.migrate(expected=sha,at=NOW)
    remote.migrate(expected=sha,at=NOW,approved=True)
    migrated,_=remote.load()
    assert migrated['observation_state']==v1 and migrated['admissions']=={} and migrated['outbox']=={}
    assert set(migrated['migration']['legacy_ids'])==set(v1['observations'])


def test_explicit_source_independence_migration_preserves_baseline_history_and_test_receipt(context):
    c=context
    test_key=c['engine'].reserve_test('migration-receipt-01',proof(c),approval='SEND_ONE_EXPERIMENT_TEST')
    assert c['engine'].send(test_key,proof(c))['status']=='DELIVERED'
    prior,_=c['remote'].load()
    legacy=deepcopy(prior)
    legacy['policy_version']=LEGACY_DELIVERY_POLICY
    legacy.pop('policy_migration',None)
    legacy_remote=MemoryRemote(legacy)
    before,_=legacy_remote.load()
    capture=capture_window(c['source'],before['observation_state'],at='2026-10-24T10:12:00Z')
    observe(legacy_remote,capture)
    after,_=legacy_remote.load()
    assert after['policy_version']==DELIVERY_POLICY
    assert after['policy_migration']['from']==LEGACY_DELIVERY_POLICY
    assert after['policy_migration']['to']==DELIVERY_POLICY
    assert after['policy_migration']['source_sha256']==fingerprint(before)
    assert after['observation_state']['baseline']==before['observation_state']['baseline']
    assert after['observation_state']['activation_at']==before['observation_state']['activation_at']
    assert {k:v['first_seen_at'] for k,v in after['observation_state']['observations'].items()} == \
           {k:v['first_seen_at'] for k,v in before['observation_state']['observations'].items()}
    assert after['observation_state']['revisions']==before['observation_state']['revisions']
    assert after['admissions']==before['admissions']
    assert after['outbox'][test_key]==before['outbox'][test_key]
    assert after['outbox'][test_key]['status']=='DELIVERED'
    assert after['test_authorizations']==before['test_authorizations']


class InitializationTransport:
    def __init__(self):
        self.raw=None;self.sha=None;self.puts=0;self.parents=[]
        self.head_sha='a'*40;self.tree_sha=EMPTY_TREE_SHA;self.tree_status=200
        self.tree_entries=[];self.root_status=200;self.root_payload=[]
        self.state_status=404;self.ref_reads=0;self.change_head_on_ref_call=None
        self.gets=[]
    def get(self,url,**kwargs):
        assert kwargs['allow_redirects'] is False
        self.gets.append(url)
        if '/git/ref/heads/' in url:
            self.ref_reads+=1
            if self.change_head_on_ref_call==self.ref_reads:self.head_sha='c'*40
            from kemdiktisaintek_state import STATE_BRANCH
            return Response(200,{'ref':'refs/heads/'+STATE_BRANCH,'object':{'sha':self.head_sha}})
        if '/git/commits/' in url:
            reviewed=url.rsplit('/',1)[-1]
            return Response(200,{'sha':reviewed,'tree':{'sha':self.tree_sha},'parents':self.parents})
        if '/git/trees/' in url:
            return Response(self.tree_status,{'sha':self.tree_sha,'tree':self.tree_entries,'truncated':False})
        if url.split('?',1)[0].endswith('/contents'):
            return Response(self.root_status,self.root_payload)
        if '/contents/.bot-state/kemdiktisaintek_experiment.json?' in url:
            if self.state_status==404:return Response(404,{})
            import base64
            from kemdiktisaintek_state import STATE_PATH
            return Response(self.state_status,{'encoding':'base64','path':STATE_PATH,
                'sha':self.sha,'content':base64.b64encode(self.raw).decode()})
        return Response(404,{})
    def put(self,url,*,json,**kwargs):
        import base64
        self.puts+=1
        if self.raw is not None:return Response(409,{})
        assert 'sha' not in json
        self.raw=base64.b64decode(json['content']);self.sha=blob_sha(self.raw)
        self.state_status=200
        return Response(201,{'content':{'sha':self.sha}})


def test_remote_initialization_requires_reviewed_empty_branch_and_does_not_create_branch(context):
    transport=InitializationTransport();remote=RemoteState('fixture-github',session=transport)
    state=migrate_v1(context['remote'].load()[0]['observation_state'],at=NOW)
    with pytest.raises(StateError):remote.create(state,approved=True,empty_branch_sha='c'*40)
    assert transport.puts==0
    transport.tree_entries=[{'path':'old-state'}]
    with pytest.raises(StateError):remote.create(state,approved=True,empty_branch_sha='a'*40)
    assert transport.puts==0
    transport.tree_entries=[];remote.create(state,approved=True,empty_branch_sha='a'*40)
    assert remote.load()[0]==state and transport.puts==1
    with pytest.raises(StateError):remote.create(state,approved=True,empty_branch_sha='a'*40)
    assert transport.puts==1


def test_empty_tree_after_deleted_state_cannot_reinitialize(context):
    transport=InitializationTransport();transport.parents=[{'sha':'d'*40}]
    remote=RemoteState('fixture-github',session=transport)
    state=migrate_v1(context['remote'].load()[0]['observation_state'],at=NOW)
    with pytest.raises(StateError,match='NOT_REVIEWED_EMPTY_TREE'):
        remote.create(state,approved=True,empty_branch_sha='a'*40)
    assert transport.puts==0


def _initial_state(context):
    return migrate_v1(context['remote'].load()[0]['observation_state'],at=NOW)


def test_remote_initialize_accepts_real_empty_orphan_when_tree_api_is_404(context):
    transport=InitializationTransport();transport.tree_status=404
    remote=RemoteState('fixture-github',session=transport)
    assert remote.create(_initial_state(context),approved=True,empty_branch_sha='a'*40)
    assert transport.puts==1
    assert any(url.endswith('/contents?ref=kemdiktisaintek-experiment-state') for url in transport.gets)
    assert any(url.endswith('/contents/.bot-state/kemdiktisaintek_experiment.json?ref=kemdiktisaintek-experiment-state')
               for url in transport.gets)


@pytest.mark.parametrize('scenario',[
    'tree_404_root_nonempty','tree_404_tree_sha_mismatch','root_parent','head_changed',
    'state_exists','root_http_error','state_http_error','root_invalid','tree_truncated',
])
def test_remote_initialize_rejects_unproven_empty_branch_without_put(context,scenario):
    transport=InitializationTransport();remote=RemoteState('fixture-github',session=transport)
    if scenario=='tree_404_root_nonempty':
        transport.tree_status=404;transport.root_payload=[{'name':'existing'}]
    elif scenario=='tree_404_tree_sha_mismatch':
        transport.tree_status=404;transport.tree_sha='b'*40
    elif scenario=='root_parent':
        transport.parents=[{'sha':'d'*40}]
    elif scenario=='head_changed':
        transport.change_head_on_ref_call=2
    elif scenario=='state_exists':
        transport.state_status=200
    elif scenario=='root_http_error':
        transport.root_status=503
    elif scenario=='state_http_error':
        transport.state_status=503
    elif scenario=='root_invalid':
        transport.root_payload={'unexpected':'object'}
    elif scenario=='tree_truncated':
        transport.tree_entries=[]
        original=transport.get
        def truncated(url,**kwargs):
            result=original(url,**kwargs)
            if '/git/trees/' in url:result.result['truncated']=True
            return result
        transport.get=truncated
    with pytest.raises(StateError):
        remote.create(_initial_state(context),approved=True,empty_branch_sha='a'*40)
    assert transport.puts==0


def test_remote_initialize_rejects_wrong_branch_before_any_request(context):
    transport=InitializationTransport()
    with pytest.raises(StateError,match='REMOTE_SCOPE_OR_TOKEN_INVALID'):
        RemoteState('fixture-github',session=transport,branch='master')
    assert transport.gets==[] and transport.puts==0


def test_remote_initialize_rejects_invalid_initial_state_before_any_request(context):
    transport=InitializationTransport();remote=RemoteState('fixture-github',session=transport)
    invalid=_initial_state(context);invalid['generation']=2
    with pytest.raises(StateError):remote.create(invalid,approved=True,empty_branch_sha='a'*40)
    assert transport.gets==[] and transport.puts==0


@pytest.mark.parametrize('fail',['getChat','getMe','getChatMember'])
def test_preflight_http_failure_no_post(context,fail):
    c=context;original=c['telegram'].get
    def get(url,**kwargs):
        return Response(403,{'ok':False}) if url.endswith('/'+fail) else original(url,**kwargs)
    c['telegram'].get=get
    with pytest.raises(RuntimeError):proof(c)
    assert c['telegram'].posts==[]


def test_restart_after_delivered_does_not_repost(context):
    c=context;key=reserve(c);send(c,key)
    c['engine']=Delivery(c['remote'],c['gateway'],environment=c['env'],clock=c['clock'])
    assert c['engine'].recover()==0 and send(c,key)['post_attempted'] is False
    assert len(c['telegram'].posts)==1


def test_expired_reserved_test_does_not_post(context):
    c=context;key=c['engine'].reserve_test('fixture-test-001',proof(c),approval='SEND_ONE_EXPERIMENT_TEST')
    c['clock'].value='2026-10-24T10:17:00Z'
    with pytest.raises(StateError):c['engine'].send(key,proof(c))
    assert c['telegram'].posts==[]


def test_new_admission_needs_detail_from_current_capture():
    c=setup(new_published='2026-10-24T10:15:00Z')
    state,_=c['remote'].load()
    assert not state['admissions']
    cap=capture_window(c['source'],state['observation_state'],at='2026-10-24T10:16:00Z',detail_budget=1)
    observe(c['remote'],cap)
    state,_=c['remote'].load()
    assert state['observation_state']['observations'][c['source'].new_id]['detection_status']=='NEW_CANDIDATE'
    assert not state['admissions']
    c['clock'].value='2026-10-24T10:17:00Z'
    with pytest.raises(StateError,match='NO_FRESH_EVENT_ADMISSION'):reserve(c)
    assert c['telegram'].posts==[]


def test_ambiguous_runtime_result_is_nonzero_exit(monkeypatch):
    import kemdiktisaintek_stage3_runtime as runtime
    monkeypatch.setattr(runtime,'run',lambda *args,**kwargs:{'status':'AMBIGUOUS','post_attempted':True})
    assert runtime.main(['SEND_TEST'])==1


def test_workflow_preflight_is_read_only_and_all_checkouts_pin_reviewed_sha():
    workflow=yaml.safe_load((HERE.parents[1]/'.github/workflows/kemdiktisaintek-experiment.yml').read_text())
    jobs=workflow['jobs']
    assert jobs['preflight']['permissions']=={'contents':'read'}
    assert jobs['review']['permissions']=={'contents':'read'}
    assert jobs['controlled']['permissions']=={'contents':'write'}
    assert jobs['preflight']['if']=="inputs.mode == 'PREFLIGHT'"
    assert "'PREFLIGHT'" not in jobs['controlled']['if']
    for name in ('collect','preflight','controlled'):
        assert jobs[name]['needs']=='review'
        checkout=jobs[name]['steps'][0]
        assert checkout['uses']=='actions/checkout@v4'
        assert checkout['with']=={'ref':'${{ inputs.reviewed_code_sha }}','persist-credentials':False}
    preflight_step=jobs['preflight']['steps'][-1]
    assert preflight_step['run'].endswith(' PREFLIGHT')
    assert 'KEMDIKTISAINTEK_SEND_TEST_APPROVAL' not in preflight_step['env']
    approval_env=jobs['review']['steps'][0]['env']
    assert approval_env['APPROVED_CODE_SHA']=="${{ vars.KEMDIKTISAINTEK_EXPERIMENT_APPROVED_CODE_SHA || '' }}"
    assert 'inputs.' not in approval_env['APPROVED_CODE_SHA']
    assert 'environment' not in jobs['review']  # repository approval, not a job environment override


@pytest.mark.parametrize('reviewed,event,repo,operator_approved,dispatch_sha,expected',[
    ('a'*40,'workflow_dispatch','Galih-Hermawan-Unikom/bima-dikti-news','a'*40,'a'*40,0),
    ('','workflow_dispatch','Galih-Hermawan-Unikom/bima-dikti-news','a'*40,'a'*40,1),
    ('b'*40,'workflow_dispatch','Galih-Hermawan-Unikom/bima-dikti-news','a'*40,'a'*40,1),
    ('not-a-sha','workflow_dispatch','Galih-Hermawan-Unikom/bima-dikti-news','a'*40,'a'*40,1),
    ('a'*40,'push','Galih-Hermawan-Unikom/bima-dikti-news','a'*40,'a'*40,1),
    ('a'*40,'workflow_dispatch','other/repo','a'*40,'a'*40,1),
    ('a'*40,'workflow_dispatch','Galih-Hermawan-Unikom/bima-dikti-news','','a'*40,1),
    ('a'*40,'workflow_dispatch','Galih-Hermawan-Unikom/bima-dikti-news','invalid','a'*40,1),
    ('a'*40,'workflow_dispatch','Galih-Hermawan-Unikom/bima-dikti-news','b'*40,'a'*40,1),
    ('b'*40,'workflow_dispatch','Galih-Hermawan-Unikom/bima-dikti-news','a'*40,'b'*40,1),
    ('b'*40,'workflow_dispatch','Galih-Hermawan-Unikom/bima-dikti-news','b'*40,'b'*40,0)])
def test_workflow_review_gate_before_checkout(reviewed,event,repo,operator_approved,dispatch_sha,expected):
    import subprocess,sys,os
    workflow=yaml.safe_load((HERE.parents[1]/'.github/workflows/kemdiktisaintek-experiment.yml').read_text())
    run_script=workflow['jobs']['review']['steps'][0]['run']
    assert run_script.startswith("python - <<'PY'\n")
    script='\n'.join(run_script.splitlines()[1:-1])
    result=subprocess.run([sys.executable,'-c',script],capture_output=True,text=True,
        env={**os.environ,'REVIEWED_SHA':reviewed,'GITHUB_SHA':dispatch_sha,'APPROVED_CODE_SHA':operator_approved,
             'GITHUB_EVENT_NAME':event,'GITHUB_REPOSITORY':repo})
    assert result.returncode==expected
    assert 'REVIEWED_SHA_GATE='+('PASS' if expected==0 else 'BLOCKED') in result.stdout
