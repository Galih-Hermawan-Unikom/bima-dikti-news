"""Deterministic offline demonstration, derived from Stage 1 fixtures.

Synthetic UUIDs/timestamps are deliberate. Local state lives in a TemporaryDirectory.
No HTTP, credentials, bot modules or sender are used.
"""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from urllib.parse import urlencode
from uuid import uuid5, NAMESPACE_URL

from kemdiktisaintek_parser import BASE
from kemdiktisaintek_monitor import Capture, initialize, apply_poll
from kemdiktisaintek_policy import classify
from kemdiktisaintek_state import LocalStateStore, STATE_BRANCH, fingerprint

HERE = Path(__file__).parent


def _article(name, published, *, nonacademic=False):
    row = deepcopy(json.loads((HERE/'sample_output.json').read_text(encoding='utf-8'))['items'][2])
    identifier = str(uuid5(NAMESPACE_URL, 'stage2-simulation:'+name))
    row.update(id=identifier, identity='kemdiktisaintek:'+identifier, slug=name,
        url=BASE+'/announcement/article/'+name, published_at=published, updated_at=published,
        title='Seleksi Kepala Tata Usaha SMA Unggul Garuda Baru' if nonacademic else 'Penawaran Hibah Penelitian bagi Dosen',
        body_text='Perguruan tinggi membuka pendaftaran hibah penelitian bagi dosen.',
        deadline_evidence=[], attachments=[], external_links=[], letter_number=None)
    row['content_sha256'] = fingerprint({k:row[k] for k in ('title','category','body_text','attachments','external_links')})
    return row


def _capture(items, at):
    count = len(items)
    coverage = {'requested_pages':2,'captured_pages':1,'pages':[{
        'page':1,'url':BASE+'/api/announcement?'+urlencode({'page':1,'size':10,'search':''}),
        'count':count,'total':count,'method':'public_json_api','healthy':True}],
        'total_reported':count,'listing_count':count,'detail_count':count,
        'detail_budget':10,'archive_complete':False,'all_requested_pages_healthy':True,'unfiltered':True}
    return Capture(at,items,coverage,[])


def run_simulation():
    known = _article('baseline-known','2026-10-23T09:00:00Z')
    historical = _article('historical-outside-baseline','2026-10-20T09:00:00Z')
    new = _article('new-relevant','2026-10-24T10:05:00Z')
    irrelevant = _article('new-nonacademic','2026-10-24T10:05:00Z',nonacademic=True)
    with tempfile.TemporaryDirectory(prefix='kemdiktisaintek-stage2-simulation-') as directory:
        store = LocalStateStore(Path(directory)/STATE_BRANCH)
        initialize(store,_capture([known],'2026-10-24T10:00:00Z'))
        state,sha=store.load()
        state,first=apply_poll(state,_capture([known,historical,new,irrelevant],'2026-10-24T10:10:00Z'),bima_history=[])
        store.commit(state,sha)
        first_cases=[{'case':name,'status':state['observations'][row['id']]['detection_status'],
                      'reasons':state['observations'][row['id']]['reasons']}
                     for name,row in [('historical',historical),('new_relevant',new),('new_not_relevant',irrelevant)]]
        revision=deepcopy(known)
        revision['body_text']+=' Batas pendaftaran diperpanjang hingga 30 Oktober 2026.'
        revision['deadline_evidence']=['hingga 30 Oktober 2026']
        revision['attachments']=[{'url':BASE+'/simulation-revision.pdf','label':'Revisi','kind':'pdf','verification':'link_only'}]
        revision['content_sha256']=fingerprint({k:revision[k] for k in ('title','category','body_text','attachments','external_links')})
        state,sha=store.load()
        state,second=apply_poll(state,_capture([revision,historical,new,irrelevant],'2026-10-24T10:20:00Z'),bima_history=[])
        store.commit(state,sha)
        cases=first_cases+[
            {'case':'old_revised','status':state['observations'][known['id']]['detection_status'],
             'event_type':state['revisions'][0]['event_type'],'changed_fields':list(state['revisions'][0]['changes'])},
            {'case':'already_processed','status':state['observations'][new['id']]['detection_status']}]
        assert [c['status'] for c in cases]==['HOLD','NEW_CANDIDATE','REJECTED','NEEDS_REVIEW','KNOWN']
        assert state['outbox']=={} and state['delivery_authorized'] is False
    samples=json.loads((HERE/'sample_output.json').read_text(encoding='utf-8'))['items']
    return {'simulation':True,'delivery_authorized':False,'telegram_calls':0,
        'bima_reference':'explicit empty simulation snapshot; not production history',
        'cases':cases,'sample_classifications':[{'id':row['id'],'title':row['title'],
        'published_at':row['published_at'],**classify(row)} for row in samples]}


if __name__=='__main__':
    print(json.dumps(run_simulation(),ensure_ascii=True,indent=2))
