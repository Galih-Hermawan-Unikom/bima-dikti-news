"""Explicit, UUID-bound editorial recovery without changing automatic monitor history."""
from copy import deepcopy
from datetime import timedelta
import re
from uuid import uuid4

from kemdiktisaintek_prod_delivery_state import event_key as automatic_event_key, validate_delivery_state
from kemdiktisaintek_prod_parser import AnnouncementClient, parse_detail
from kemdiktisaintek_prod_state import StateError, fingerprint, timestamp
from kemdiktisaintek_prod_telegram import (document_filename, format_announcement, payload,
    safe_pdf_url)
from kemdiktisaintek_editorial_state import (empty_ledger, event_key, validate_ledger,
    validate_lineage)

REPOSITORY = 'Galih-Hermawan-Unikom/bima-dikti-news'
WORKFLOW_PATH = '.github/workflows/kemdiktisaintek-editorial-delivery.yml'
ALLOWED_ACTORS = {'Galih-Hermawan-Unikom'}
AUTHORIZATION_TTL = timedelta(minutes=60)
TARGETS = {
    '2407f6f4-1e06-452b-a053-864426e9a183': {
        'title': 'Tawaran Mengikuti Program Bridging Course Tahun 2026',
        'url': 'https://kemdiktisaintek.go.id/announcement/article/tawaran-mengikuti-program-bridging-course-tahun-2026',
        'published_date': '2026-10-27',
        'priority': 'TINGGI',
    },
    'b7abee5c-39d8-48fa-a2ac-93fe07ec7ed2': {
        'title': 'Kemdiktisaintek Perkuat Kesiapan Dosen dan Tendik untuk Studi Doktor di Luar Negeri',
        'url': 'https://kemdiktisaintek.go.id/announcement/article/kemdiktisaintek-perkuat-kesiapan-dosen-dan-tendik-untuk-studi-doktor-di-luar-negeri',
        'published_date': '2026-10-26',
        'priority': 'NORMAL',
    },
}
EDITORIAL_HEADER = '<b>📢 PENGUMUMAN BARU KEMDIKTISAINTEK</b>'
RECOVERY_HEADER = '<b>📢 INFORMASI RESMI KEMDIKTISAINTEK</b>'


class EditorialReconciliationRequired(StateError):
    """A POST may have happened but its durable receipt is not confirmed."""

    def __init__(self):
        super().__init__('EDITORIAL_RECEIPT_RECONCILIATION_REQUIRED')


def _now(clock):
    value = clock()
    timestamp(value)
    return value


def _review_gate(mode, uuid, confirmation, env):
    target = TARGETS.get(uuid)
    if not target:
        raise StateError('EDITORIAL_UUID_NOT_ALLOWLISTED')
    sha = env.get('GITHUB_SHA', '')
    reviewed = env.get('KEMDIKTISAINTEK_PRODUCTION_REVIEWED_SHA', '')
    approved = env.get('KEMDIKTISAINTEK_PRODUCTION_APPROVED_SHA', '')
    expected_ref = REPOSITORY + '/' + WORKFLOW_PATH + '@refs/heads/master'
    if (env.get('GITHUB_ACTIONS') != 'true' or env.get('GITHUB_EVENT_NAME') != 'workflow_dispatch'
            or env.get('GITHUB_REPOSITORY') != REPOSITORY or env.get('GITHUB_REF') != 'refs/heads/master'
            or env.get('GITHUB_WORKFLOW_REF') != expected_ref
            or not re.fullmatch('[a-f0-9]{40}', sha) or reviewed != sha or approved != sha):
        raise StateError('EDITORIAL_REVIEWED_SHA_GATE_BLOCKED')
    actor = env.get('GITHUB_ACTOR', '')
    if actor not in ALLOWED_ACTORS:
        raise StateError('EDITORIAL_ACTOR_NOT_AUTHORIZED')
    expected_confirmation = f'{mode} {uuid}'
    if mode in {'AUTHORIZE', 'SEND'} and confirmation != expected_confirmation:
        raise StateError('EDITORIAL_EXPLICIT_CONFIRMATION_REQUIRED')
    if mode in {'AUTHORIZE', 'SEND'} and env.get('KEMDIKTISAINTEK_PRODUCTION_ENABLED') != 'true':
        raise StateError('PRODUCTION_SOURCE_SWITCH_OFF')
    return target, actor, sha


def _destination(identity):
    if (not isinstance(identity, dict) or type(identity.get('bot_id')) is not int
            or not re.fullmatch('[a-f0-9]{64}', identity.get('chat_sha256', ''))):
        raise StateError('EDITORIAL_DESTINATION_PREFLIGHT_INVALID')
    return {'bot_id': identity['bot_id'], 'chat_sha256': identity['chat_sha256']}


def _official_item(identifier, client):
    target = TARGETS.get(identifier)
    if target is None:
        raise StateError('EDITORIAL_UUID_NOT_ALLOWLISTED')
    capture = client.collect(max_pages=3, detail_limit=0, search='', category=None, method='api')
    matches = [item for item in capture['items'] if item.get('id') == identifier]
    if len(matches) != 1:
        raise StateError('EDITORIAL_UUID_NOT_FOUND_UNIQUELY_IN_RECENT_API_WINDOW')
    listing = matches[0]
    if listing.get('title') != target['title'] or listing.get('url') != target['url']:
        raise StateError('EDITORIAL_API_IDENTITY_MISMATCH')
    detail = parse_detail(client.fetch(target['url']), target['url'], listing)
    if (detail.get('id') != identifier or detail.get('url') != target['url']
            or detail.get('title') != target['title'] or detail.get('publication_basis') != 'detail_datetime'
            or not isinstance(detail.get('published_at'), str)
            or not detail['published_at'].startswith(target['published_date'])
            or not detail.get('body_text') or not re.fullmatch('[a-f0-9]{64}', detail.get('content_sha256', ''))):
        raise StateError('EDITORIAL_DETAIL_IDENTITY_OR_EVIDENCE_INVALID')
    if identifier.startswith('2407') and not any('06 November 2026' in value for value in detail.get('deadline_evidence', [])):
        raise StateError('EDITORIAL_BRIDGING_DEADLINE_EVIDENCE_MISSING')
    return detail


def _message(item):
    message = format_announcement(item)
    text = message['text']
    if EDITORIAL_HEADER not in text:
        raise StateError('EDITORIAL_FORMAT_HEADER_CHANGED')
    # The recovery notice is dated and official, not represented as newly published today.
    return payload(text.replace(EDITORIAL_HEADER, RECOVERY_HEADER, 1))


def _operations(item, destination):
    rows = [{'operation_id': 'summary', 'kind': 'SUMMARY', 'payload': _message(item),
             'destination': deepcopy(destination), 'status': 'AUTHORIZED', 'attempt_id': None,
             'sending_claimed_at': None, 'delivered_at': None, 'receipt': None,
             'error_classification': None}]
    for index, doc in enumerate(item.get('attachments', []), 1):
        if not isinstance(doc, dict) or doc.get('kind') != 'pdf' or not safe_pdf_url(doc.get('url')):
            continue
        filename = document_filename(doc['url'], doc.get('label') or '')
        rows.append({'operation_id': f'attachment-{index}', 'kind': 'ATTACHMENT',
            'payload': {'url': doc['url'], 'filename': filename, 'caption': ''},
            'destination': deepcopy(destination), 'status': 'AUTHORIZED', 'attempt_id': None,
            'sending_claimed_at': None, 'delivered_at': None, 'receipt': None,
            'error_classification': None})
    for row in rows:
        row['payload_hash'] = fingerprint(row['payload'])
    return rows


def _payload_hash(item, operations):
    return fingerprint({'content_sha256': item['content_sha256'],
        'operations': [{'operation_id': op['operation_id'], 'kind': op['kind'],
                        'payload_hash': op['payload_hash'], 'destination': op['destination']}
                       for op in operations]})


def _source_record(state, identifier, *, require_hold=True, block_duplicate=True):
    validate_delivery_state(state)
    observation = state['observation_state']['observations'].get(identifier)
    target = TARGETS[identifier]
    if (not isinstance(observation, dict) or observation.get('uuid') != identifier
            or (require_hold and (observation.get('detection_status') != 'HOLD'
                or observation.get('initial_decision') != 'HOLD'))
            or observation.get('url') != target['url'] or observation.get('title') != target['title']):
        raise StateError('EDITORIAL_SOURCE_NOT_HELD_TARGET')
    baseline_ids = state['observation_state']['baseline']['ids']
    if identifier in baseline_ids:
        raise StateError('EDITORIAL_BASELINE_UUID_BLOCKED')
    key = automatic_event_key(identifier)
    if block_duplicate and (key in state['admissions'] or key in state['outbox'] or any(
            row.get('source_uuid') == identifier for row in state['outbox'].values())):
        raise StateError('EDITORIAL_AUTOMATIC_DELIVERY_ALREADY_ADMITTED_OR_RESERVED')
    return observation


def _entry_status(operations):
    statuses = [row['status'] for row in operations]
    if 'AMBIGUOUS' in statuses:
        return 'AMBIGUOUS'
    if 'SENDING' in statuses:
        return 'SENDING'
    if statuses and all(value == 'DELIVERED' for value in statuses):
        return 'DELIVERED'
    if 'DELIVERED' in statuses:
        return 'PARTIAL'
    if 'FAILED' in statuses:
        return 'FAILED'
    return 'RESERVED'


def _block_unattempted(operations, after, reason):
    for operation in operations[after + 1:]:
        if operation['status'] == 'RESERVED':
            operation.update(status='BLOCKED', error_classification='BLOCKED_BY_' + reason)


class EditorialDelivery:
    def __init__(self, production_backend, ledger_backend, client, gateway, *, env, clock):
        self.production, self.ledger = production_backend, ledger_backend
        self.client, self.gateway, self.env, self.clock = client, gateway, env, clock

    def _fresh(self, identifier, *, require_hold=True, block_duplicate=True):
        item = _official_item(identifier, self.client)
        source_state, source_sha = self.production.load()
        observation = _source_record(source_state, identifier, require_hold=require_hold,
                                      block_duplicate=block_duplicate)
        if observation.get('content_sha256') != item['content_sha256']:
            raise StateError('EDITORIAL_OBSERVED_CONTENT_CHANGED_REVIEW_REQUIRED')
        return item, source_state, source_sha

    def preview(self, identifier, *, confirmation=''):
        _, actor, sha = _review_gate('PREVIEW', identifier, confirmation, self.env)
        item, source_state, _ = self._fresh(identifier, require_hold=False, block_duplicate=False)
        ledger, ledger_sha = self.ledger.load()
        key = event_key(identifier)
        prior = ledger['entries'].get(key)
        message = _message(item)
        documents = [op['payload']['filename'] for op in _operations(
            item, {'bot_id': 1, 'chat_sha256': '0' * 64}) if op['kind'] == 'ATTACHMENT']
        return {'mode': 'PREVIEW', 'source': 'kemdiktisaintek', 'uuid': identifier,
            'title': item['title'], 'published_at': item['published_at'], 'canonical_url': item['url'],
            'category': item.get('category'), 'deadline_evidence': item.get('deadline_evidence', []),
            'editorial_priority': TARGETS[identifier]['priority'],
            'content_sha256': item['content_sha256'], 'payload_hash': fingerprint(message),
            'message': message['text'], 'documents': documents, 'destination_alias': '@dikti_news',
            'delivery_origin': 'EDITORIAL_RECOVERY', 'production_status':
                source_state['observation_state']['observations'][identifier]['detection_status'],
            'already_delivered': bool(prior and prior['status'] == 'DELIVERED'),
            'editorial_status': prior['status'] if prior else 'NOT_AUTHORIZED',
            'editorial_blob_sha': ledger_sha, 'reviewed_master_sha': sha, 'operator': actor,
            'telegram_post_attempted': False, 'remote_write': False}

    def authorize(self, identifier, confirmation):
        _, actor, sha = _review_gate('AUTHORIZE', identifier, confirmation, self.env)
        item, _, _ = self._fresh(identifier)
        proof = self.gateway.inspect(at=_now(self.clock))
        destination = _destination(proof.identity)
        operations = _operations(item, destination)
        payload_sha = _payload_hash(item, operations)
        key = event_key(identifier)
        old, expected = self.ledger.load()
        existing = old['entries'].get(key)
        at = _now(self.clock)
        if existing:
            if existing['status'] == 'DELIVERED':
                return {'status': 'DELIVERED', 'event_key': key, 'receipts': _receipts(existing),
                        'post_attempted': False}
            current_auth = existing['authorizations'][existing['active_authorization']]
            if timestamp(current_auth['expires_at']) > timestamp(at):
                return {'status': 'ALREADY_AUTHORIZED', 'event_key': key,
                        'expires_at': current_auth['expires_at'], 'post_attempted': False}
            if existing['status'] != 'AUTHORIZED':
                raise StateError('EDITORIAL_AUTHORIZATION_EXPIRED_AFTER_RESERVATION_RECONCILE')
            ledger = deepcopy(old)
            row = ledger['entries'][key]
            row['authorizations'].append(self._authorization(actor, at, sha, destination, item, payload_sha))
            row['active_authorization'] = len(row['authorizations']) - 1
            row['updated_at'] = at
        else:
            auth = self._authorization(actor, at, sha, destination, item, payload_sha)
            row = {'event_key': key, 'uuid': identifier, 'canonical_url': item['url'],
                'title': item['title'], 'published_at': item['published_at'],
                'content_sha256': item['content_sha256'], 'payload_hash': payload_sha,
                'operations': operations, 'authorizations': [auth], 'active_authorization': 0,
                'status': 'AUTHORIZED', 'created_at': at, 'reserved_at': None,
                'updated_at': at, 'delivery_origin': 'EDITORIAL_RECOVERY'}
            ledger = deepcopy(old)
            ledger['entries'][key] = row
        ledger['generation'] += 1
        validate_lineage(old, ledger)
        blob = self.ledger.checkpoint(ledger, expected)
        return {'status': 'AUTHORIZED', 'event_key': key, 'expires_at': row['authorizations'][row['active_authorization']]['expires_at'],
                'destination_alias': '@dikti_news', 'operations': len(operations),
                'ledger_blob_sha': blob, 'post_attempted': False}

    @staticmethod
    def _authorization(actor, at, sha, destination, item, payload_sha):
        expires = (timestamp(at) + AUTHORIZATION_TTL).isoformat()
        return {'authorized_by': actor, 'authorized_at': at, 'expires_at': expires,
            'reviewed_master_sha': sha, 'destination': deepcopy(destination),
            'content_sha256': item['content_sha256'], 'payload_hash': payload_sha}

    def send(self, identifier, confirmation):
        _, actor, sha = _review_gate('SEND', identifier, confirmation, self.env)
        key = event_key(identifier)
        ledger, ledger_sha = self.ledger.load()
        row = ledger['entries'].get(key)
        if row is None:
            raise StateError('EDITORIAL_AUTHORIZATION_NOT_FOUND')
        if row['status'] == 'DELIVERED':
            return {'status': 'DELIVERED', 'event_key': key, 'receipts': _receipts(row),
                    'post_attempted': False, 'ledger_blob_sha': ledger_sha}
        if row['status'] == 'SENDING':
            return {'status': 'SENDING', 'event_key': key, 'receipts': _receipts(row),
                    'post_attempted': False,
                    'reconciliation_required': True}
        if row['status'] in {'AMBIGUOUS', 'FAILED'}:
            return {'status': row['status'], 'event_key': key, 'receipts': _receipts(row),
                    'post_attempted': False, 'reconciliation_required': row['status'] == 'AMBIGUOUS'}
        item, _, _ = self._fresh(identifier)
        auth = row['authorizations'][row['active_authorization']]
        at = _now(self.clock)
        if (actor != auth['authorized_by'] or sha != auth['reviewed_master_sha']
                or item['url'] != row['canonical_url'] or item['content_sha256'] != auth['content_sha256']
                or timestamp(at) < timestamp(auth['authorized_at'])
                or timestamp(at) >= timestamp(auth['expires_at'])):
            raise StateError('EDITORIAL_AUTHORIZATION_BINDING_OR_TTL_INVALID')
        expected_operations = _operations(item, auth['destination'])
        if _payload_hash(item, expected_operations) != auth['payload_hash'] or row['payload_hash'] != auth['payload_hash']:
            raise StateError('EDITORIAL_AUTHORIZED_PAYLOAD_CHANGED')
        current_ops = row['operations']
        if len(expected_operations) != len(current_ops) or any(
                a['operation_id'] != b['operation_id'] or a['payload_hash'] != b['payload_hash']
                or a['destination'] != b['destination'] for a, b in zip(expected_operations, current_ops)):
            raise StateError('EDITORIAL_OPERATION_SET_CHANGED')
        if row['status'] == 'AUTHORIZED':
            reserved = deepcopy(ledger)
            entry = reserved['entries'][key]
            entry['reserved_at'] = at
            entry['status'] = 'RESERVED'
            entry['updated_at'] = at
            for operation in entry['operations']:
                operation['status'] = 'RESERVED'
            reserved['generation'] += 1
            ledger_sha = self.ledger.checkpoint(reserved, ledger_sha)
            ledger, _ = self.ledger.load()
            row = ledger['entries'][key]
        attempted = False
        last_status = row['status']
        for index, operation in enumerate(row['operations']):
            if operation['status'] == 'DELIVERED':
                continue
            if operation['status'] in {'FAILED', 'AMBIGUOUS', 'SENDING'}:
                break
            if operation['status'] != 'RESERVED':
                raise StateError('EDITORIAL_OPERATION_NOT_RESERVED')
            self._fresh(identifier)  # Recheck main production outbox immediately before every operation.
            proof = self.gateway.inspect(at=_now(self.clock))
            if _destination(proof.identity) != auth['destination']:
                raise StateError('EDITORIAL_DESTINATION_CHANGED')
            current, current_sha = self.ledger.load()
            current_row = current['entries'].get(key)
            current_op = current_row['operations'][index] if current_row else None
            if current_sha != ledger_sha or not current_op or current_op['status'] != 'RESERVED':
                raise StateError('EDITORIAL_CAS_OR_RESERVATION_CHANGED')
            claimed = deepcopy(current)
            attempt_id = uuid4().hex
            op = claimed['entries'][key]['operations'][index]
            op.update(status='SENDING', attempt_id=attempt_id, sending_claimed_at=_now(self.clock))
            claimed['entries'][key]['status'] = 'SENDING'
            claimed['entries'][key]['updated_at'] = op['sending_claimed_at']
            claimed['generation'] += 1
            ledger_sha = self.ledger.checkpoint(claimed, current_sha)  # Durable claim ACK before POST.
            confirmed, confirmed_sha = self.ledger.load()
            confirmed_op = confirmed['entries'][key]['operations'][index]
            if (confirmed_sha != ledger_sha or confirmed_op['status'] != 'SENDING'
                    or confirmed_op['attempt_id'] != attempt_id):
                raise StateError('EDITORIAL_DURABLE_CLAIM_NOT_CONFIRMED_NO_POST')
            self._fresh(identifier)
            request_op = {**confirmed_op, 'payload': deepcopy(confirmed_op['payload'])}
            request_row = {'event_key': key, 'status': 'SENDING', 'payload': request_op['payload'],
                'payload_hash': request_op['payload_hash'], 'destination': proof.identity,
                'operation': request_op}
            receipt, error = self.gateway.post_claimed(request_row, proof, at=_now(self.clock))
            attempted = True
            finalized = deepcopy(confirmed)
            final_entry = finalized['entries'][key]
            final_op = final_entry['operations'][index]
            status = ('DELIVERED' if receipt else 'FAILED' if error == 'TELEGRAM_REJECTED'
                or (error and error.startswith('PREPOST_')) else 'AMBIGUOUS')
            final_op.update(status=status, receipt=receipt,
                delivered_at=_now(self.clock) if receipt else None, error_classification=error)
            if status in {'AMBIGUOUS', 'FAILED'}:
                _block_unattempted(final_entry['operations'], index, status + '_PREDECESSOR')
            final_entry['status'] = _entry_status(final_entry['operations'])
            final_entry['updated_at'] = _now(self.clock)
            finalized['generation'] += 1
            try:
                ledger_sha = self.ledger.checkpoint(finalized, confirmed_sha)
            except Exception:
                raise EditorialReconciliationRequired() from None
            ledger, _ = self.ledger.load()
            row = ledger['entries'][key]
            last_status = row['status']
            if not receipt:
                break
        row = self.ledger.load()[0]['entries'][key]
        return {'status': row['status'], 'event_key': key, 'post_attempted': attempted,
            'receipts': _receipts(row), 'ledger_blob_sha': self.ledger.load()[1],
            'reconciliation_required': row['status'] == 'AMBIGUOUS'}


def _receipts(row):
    return [{'operation_id': op['operation_id'], 'kind': op['kind'],
             'status': op['status'], 'message_id': (op.get('receipt') or {}).get('message_id')}
            for op in row.get('operations', [])]
