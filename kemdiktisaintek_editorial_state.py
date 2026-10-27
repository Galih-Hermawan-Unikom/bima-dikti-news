"""CAS-protected, append-only ledger for one-time Kemdiktisaintek recovery."""
from copy import deepcopy
import base64
import hashlib
import json
import re

import requests

from kemdiktisaintek_prod_state import StateConflict, StateError, canonical, fingerprint, timestamp
from kemdiktisaintek_prod_state import MAX_BYTES
from kemdiktisaintek_prod_parser import normalize_url

REPOSITORY = 'Galih-Hermawan-Unikom/bima-dikti-news'
BRANCH = 'dikti-production-state'
PATH = '.bot-state/kemdiktisaintek_editorial.json'
SOURCE = 'kemdiktisaintek-editorial'
SCHEMA_VERSION = 1
ENTRY_STATUSES = {'AUTHORIZED', 'RESERVED', 'SENDING', 'DELIVERED', 'PARTIAL', 'AMBIGUOUS', 'FAILED'}
OPERATION_STATUSES = {'AUTHORIZED', 'RESERVED', 'SENDING', 'DELIVERED', 'AMBIGUOUS', 'FAILED', 'BLOCKED'}


def blob_sha(raw):
    return hashlib.sha1(b'blob ' + str(len(raw)).encode() + b'\0' + raw).hexdigest()


def event_key(identifier):
    try:
        import uuid
        normalized = str(uuid.UUID(identifier))
    except (ValueError, TypeError, AttributeError):
        raise StateError('EDITORIAL_UUID_INVALID') from None
    if normalized != identifier:
        raise StateError('EDITORIAL_UUID_NOT_CANONICAL')
    return f'kemdiktisaintek:editorial:{identifier}:initial'


def empty_ledger():
    return {'schema_version': SCHEMA_VERSION, 'source': SOURCE, 'generation': 0, 'entries': {}}


def _aggregate(operations):
    statuses = [op['status'] for op in operations]
    if 'AMBIGUOUS' in statuses:
        return 'AMBIGUOUS'
    if 'SENDING' in statuses:
        return 'SENDING'
    if statuses and all(status == 'DELIVERED' for status in statuses):
        return 'DELIVERED'
    if 'DELIVERED' in statuses:
        return 'PARTIAL'
    if 'FAILED' in statuses:
        return 'FAILED'
    return 'RESERVED'


def _validate_receipt(receipt, operation):
    if receipt is None:
        return
    if (not isinstance(receipt, dict) or receipt.get('http_status') != 200
            or receipt.get('ok') is not True or type(receipt.get('message_id')) is not int
            or receipt['message_id'] <= 0 or not re.fullmatch('[a-f0-9]{64}', receipt.get('destination_sha256', ''))):
        raise StateError('EDITORIAL_RECEIPT_INVALID')
    expected_method = 'sendDocument' if operation['kind'] == 'ATTACHMENT' else 'sendMessage'
    if receipt.get('method') != expected_method:
        raise StateError('EDITORIAL_RECEIPT_METHOD_INVALID')
    if operation['status'] == 'DELIVERED' and receipt['destination_sha256'] != operation['destination']['chat_sha256']:
        raise StateError('EDITORIAL_RECEIPT_DESTINATION_MISMATCH')
    if expected_method == 'sendDocument':
        if (not re.fullmatch('[a-f0-9]{64}', receipt.get('file_id_sha256', ''))
                or not isinstance(receipt.get('file_name'), str) or not receipt['file_name']):
            raise StateError('EDITORIAL_DOCUMENT_RECEIPT_INVALID')


def validate_ledger(state):
    if (not isinstance(state, dict) or set(state) != {'schema_version', 'source', 'generation', 'entries'}
            or state.get('schema_version') != SCHEMA_VERSION or state.get('source') != SOURCE
            or type(state.get('generation')) is not int or state['generation'] < 0
            or not isinstance(state.get('entries'), dict) or len(state['entries']) > 1000):
        raise StateError('EDITORIAL_LEDGER_SCHEMA_INVALID')
    seen = set()
    for key, row in state['entries'].items():
        expected = {'event_key', 'uuid', 'canonical_url', 'title', 'published_at', 'content_sha256',
            'payload_hash', 'operations', 'authorizations', 'active_authorization', 'status',
            'created_at', 'reserved_at', 'updated_at', 'delivery_origin'}
        if not isinstance(row, dict) or set(row) != expected:
            raise StateError('EDITORIAL_ENTRY_SCHEMA_INVALID')
        identifier = row.get('uuid')
        if (key != event_key(identifier) or row.get('event_key') != key or identifier in seen
                or row.get('delivery_origin') != 'EDITORIAL_RECOVERY'):
            raise StateError('EDITORIAL_IDENTITY_INVALID')
        seen.add(identifier)
        if normalize_url(row.get('canonical_url'), article=True) != row['canonical_url']:
            raise StateError('EDITORIAL_URL_INVALID')
        if not isinstance(row.get('title'), str) or not row['title'].strip():
            raise StateError('EDITORIAL_TITLE_INVALID')
        if not isinstance(row.get('published_at'), str) or not row['published_at']:
            raise StateError('EDITORIAL_PUBLICATION_INVALID')
        if (not re.fullmatch('[a-f0-9]{64}', row.get('content_sha256', ''))
                or not re.fullmatch('[a-f0-9]{64}', row.get('payload_hash', ''))):
            raise StateError('EDITORIAL_HASH_INVALID')
        if row.get('status') not in ENTRY_STATUSES:
            raise StateError('EDITORIAL_STATUS_INVALID')
        for field in ('created_at', 'updated_at'):
            timestamp(row.get(field))
        if row.get('reserved_at') is not None:
            timestamp(row['reserved_at'])
        auths = row.get('authorizations')
        active = row.get('active_authorization')
        if (not isinstance(auths, list) or not auths or type(active) is not int
                or not 0 <= active < len(auths)):
            raise StateError('EDITORIAL_AUTHORIZATION_MISSING')
        for auth in auths:
            if (not isinstance(auth, dict) or set(auth) != {'authorized_by', 'authorized_at', 'expires_at',
                    'reviewed_master_sha', 'destination', 'content_sha256', 'payload_hash'}
                    or not isinstance(auth.get('authorized_by'), str) or not auth['authorized_by']
                    or not re.fullmatch('[a-f0-9]{40}', auth.get('reviewed_master_sha', ''))
                    or auth.get('content_sha256') != row['content_sha256']
                    or auth.get('payload_hash') != row['payload_hash']):
                raise StateError('EDITORIAL_AUTHORIZATION_INVALID')
            start, expiry = timestamp(auth['authorized_at']), timestamp(auth['expires_at'])
            if expiry <= start:
                raise StateError('EDITORIAL_AUTHORIZATION_WINDOW_INVALID')
            dest = auth.get('destination')
            if (not isinstance(dest, dict) or set(dest) != {'bot_id', 'chat_sha256'}
                    or type(dest.get('bot_id')) is not int or dest['bot_id'] <= 0
                    or not re.fullmatch('[a-f0-9]{64}', dest.get('chat_sha256', ''))):
                raise StateError('EDITORIAL_DESTINATION_INVALID')
        operations = row.get('operations')
        if not isinstance(operations, list) or not operations or operations[0].get('operation_id') != 'summary':
            raise StateError('EDITORIAL_OPERATIONS_INVALID')
        if len({op.get('operation_id') for op in operations if isinstance(op, dict)}) != len(operations):
            raise StateError('EDITORIAL_OPERATION_DUPLICATE')
        for index, op in enumerate(operations):
            if (not isinstance(op, dict) or set(op) != {'operation_id', 'kind', 'payload', 'payload_hash',
                    'destination', 'status', 'attempt_id', 'sending_claimed_at', 'delivered_at',
                    'receipt', 'error_classification'}
                    or op.get('kind') != ('SUMMARY' if index == 0 else 'ATTACHMENT')
                    or op.get('status') not in OPERATION_STATUSES
                    or not isinstance(op.get('payload'), dict)
                    or op.get('payload_hash') != fingerprint(op['payload'])):
                raise StateError('EDITORIAL_OPERATION_SCHEMA_INVALID')
            dest = op.get('destination')
            if (not isinstance(dest, dict) or set(dest) != {'bot_id', 'chat_sha256'}
                    or type(dest.get('bot_id')) is not int or dest['bot_id'] <= 0
                    or not re.fullmatch('[a-f0-9]{64}', dest.get('chat_sha256', ''))
                    or dest != auths[active]['destination']):
                raise StateError('EDITORIAL_OPERATION_DESTINATION_INVALID')
            for field in ('sending_claimed_at', 'delivered_at'):
                if op.get(field) is not None:
                    timestamp(op[field])
            if op.get('attempt_id') is not None and not re.fullmatch('[a-f0-9]{32}', op['attempt_id']):
                raise StateError('EDITORIAL_ATTEMPT_ID_INVALID')
            if (op['status'] == 'SENDING' and (not op.get('attempt_id') or not op.get('sending_claimed_at'))
                    or op['status'] == 'DELIVERED' and (not op.get('receipt') or not op.get('delivered_at'))
                    or op['status'] != 'DELIVERED' and op.get('receipt') is not None):
                raise StateError('EDITORIAL_OPERATION_RECEIPT_STATUS_INVALID')
            _validate_receipt(op.get('receipt'), op)
            if op['status'] == 'BLOCKED' and (op.get('attempt_id') or op.get('receipt')
                    or not op.get('error_classification', '').startswith('BLOCKED_BY_')):
                raise StateError('EDITORIAL_BLOCKED_OPERATION_INVALID')
            if op['status'] == 'BLOCKED' and not any(
                    prior['status'] in {'FAILED', 'AMBIGUOUS'} for prior in operations[:index]):
                raise StateError('EDITORIAL_BLOCKED_WITHOUT_FAILED_PREDECESSOR')
        if row['status'] == 'AUTHORIZED':
            if any(op['status'] != 'AUTHORIZED' for op in operations) or row['reserved_at'] is not None:
                raise StateError('EDITORIAL_AUTHORIZED_STATUS_CONFLICT')
        elif row['status'] != _aggregate(operations) or row['reserved_at'] is None:
            raise StateError('EDITORIAL_AGGREGATE_STATUS_INVALID')
        if row['status'] in {'DELIVERED', 'AMBIGUOUS', 'FAILED'} and any(
                op['status'] == 'RESERVED' for op in operations):
            raise StateError('EDITORIAL_TERMINAL_STATUS_HAS_RESERVED_OPERATION')
    if len(canonical(state)) > MAX_BYTES:
        raise StateError('EDITORIAL_LEDGER_TOO_LARGE')
    return state


def validate_lineage(old, new):
    validate_ledger(old)
    validate_ledger(new)
    if new['generation'] != old['generation'] + 1:
        raise StateError('EDITORIAL_GENERATION_NOT_MONOTONIC')
    if not set(old['entries']).issubset(new['entries']):
        raise StateError('EDITORIAL_HISTORY_REMOVED')
    transitions = {'AUTHORIZED': {'AUTHORIZED', 'RESERVED'}, 'RESERVED': {'RESERVED', 'SENDING'},
        'SENDING': {'DELIVERED', 'PARTIAL', 'AMBIGUOUS', 'FAILED'}, 'PARTIAL': {'PARTIAL', 'SENDING', 'AMBIGUOUS'},
        'DELIVERED': {'DELIVERED'}, 'AMBIGUOUS': {'AMBIGUOUS'}, 'FAILED': {'FAILED'}}
    op_transitions = {'AUTHORIZED': {'AUTHORIZED', 'RESERVED'}, 'RESERVED': {'RESERVED', 'SENDING', 'BLOCKED'},
        'SENDING': {'DELIVERED', 'AMBIGUOUS', 'FAILED'}, 'DELIVERED': {'DELIVERED'},
        'AMBIGUOUS': {'AMBIGUOUS'}, 'FAILED': {'FAILED'}, 'BLOCKED': {'BLOCKED'}}
    for key, previous in old['entries'].items():
        current = new['entries'][key]
        for field in ('event_key', 'uuid', 'canonical_url', 'title', 'published_at', 'content_sha256',
                      'payload_hash', 'operations', 'created_at', 'delivery_origin'):
            if field == 'operations':
                continue
            if current[field] != previous[field]:
                raise StateError('EDITORIAL_IMMUTABLE_BINDING_CHANGED')
        old_auths, new_auths = previous['authorizations'], current['authorizations']
        if new_auths[:len(old_auths)] != old_auths:
            raise StateError('EDITORIAL_AUTHORIZATION_HISTORY_CHANGED')
        if len(new_auths) not in {len(old_auths), len(old_auths) + 1}:
            raise StateError('EDITORIAL_AUTHORIZATION_HISTORY_INVALID')
        if len(new_auths) == len(old_auths) + 1:
            active_old = old_auths[previous['active_authorization']]
            if (previous['status'] != 'AUTHORIZED' or any(op['status'] != 'AUTHORIZED' for op in previous['operations'])
                    or timestamp(active_old['expires_at']) > timestamp(new_auths[-1]['authorized_at'])):
                raise StateError('EDITORIAL_REAUTHORIZATION_NOT_ALLOWED')
        if current['active_authorization'] < previous['active_authorization']:
            raise StateError('EDITORIAL_ACTIVE_AUTHORIZATION_ROLLBACK')
        if current['status'] not in transitions[previous['status']]:
            raise StateError('EDITORIAL_STATUS_ROLLBACK')
        old_ops, new_ops = previous['operations'], current['operations']
        if len(old_ops) != len(new_ops):
            raise StateError('EDITORIAL_OPERATION_SET_CHANGED')
        for a, b in zip(old_ops, new_ops):
            for field in ('operation_id', 'kind', 'payload', 'payload_hash', 'destination'):
                if a[field] != b[field]:
                    raise StateError('EDITORIAL_OPERATION_BINDING_CHANGED')
            if b['status'] not in op_transitions[a['status']]:
                raise StateError('EDITORIAL_OPERATION_STATUS_ROLLBACK')
            if a.get('receipt') is not None and b.get('receipt') != a['receipt']:
                raise StateError('EDITORIAL_RECEIPT_IMMUTABLE')
            if a.get('attempt_id') is not None and b.get('attempt_id') != a['attempt_id']:
                raise StateError('EDITORIAL_ATTEMPT_IMMUTABLE')
    return new


class EditorialRemoteState:
    """Strict Contents API CAS adapter for one file on the production state branch."""
    durable = True

    def __init__(self, token, *, session=None, repository=REPOSITORY, branch=BRANCH, path=PATH):
        if repository != REPOSITORY or branch != BRANCH or path != PATH or not isinstance(token, str) or not token:
            raise StateError('EDITORIAL_REMOTE_SCOPE_INVALID')
        self.session = session or requests.Session()
        if session is None:
            self.session.trust_env = False
        self.headers = {'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json',
            'X-GitHub-Api-Version': '2022-11-28'}
        self.base = 'https://api.github.com/repos/' + REPOSITORY + '/'
        self.contents = self.base + 'contents/' + PATH

    def _get(self):
        try:
            return self.session.get(self.contents + '?ref=' + BRANCH, headers=self.headers,
                                    timeout=30, allow_redirects=False)
        except Exception:
            raise StateError('EDITORIAL_REMOTE_GET_FAILED') from None

    def load(self):
        response = self._get()
        if response.status_code == 404:
            return empty_ledger(), None
        if response.status_code != 200:
            raise StateError('EDITORIAL_LEDGER_UNAVAILABLE')
        try:
            body = response.json()
            raw = base64.b64decode(''.join(body['content'].split()), validate=True)
            sha = body['sha']
            if (body.get('path') != PATH or body.get('encoding') != 'base64' or not raw
                    or len(raw) > MAX_BYTES or sha != blob_sha(raw)):
                raise ValueError()
            ledger = json.loads(raw.decode('utf-8'))
            validate_ledger(ledger)
            return ledger, sha
        except (KeyError, TypeError, ValueError, UnicodeDecodeError):
            raise StateError('EDITORIAL_LEDGER_INVALID') from None

    def checkpoint(self, ledger, expected):
        current, sha = self.load()
        if sha != expected:
            raise StateConflict('EDITORIAL_REMOTE_CAS_CONFLICT')
        validate_lineage(current, ledger)
        raw = canonical(ledger)
        body = {'branch': BRANCH, 'message': 'chore: checkpoint Kemdiktisaintek editorial ledger',
                'content': base64.b64encode(raw).decode('ascii')}
        if expected is not None:
            body['sha'] = expected
        try:
            response = self.session.put(self.contents, json=body, headers=self.headers,
                                        timeout=45, allow_redirects=False)
        except Exception:
            raise StateError('EDITORIAL_REMOTE_CHECKPOINT_OUTCOME_UNKNOWN') from None
        if response.status_code in {409, 422}:
            raise StateConflict('EDITORIAL_REMOTE_CAS_CONFLICT')
        try:
            if response.status_code not in {200, 201} or response.json()['content']['sha'] != blob_sha(raw):
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise StateError('EDITORIAL_REMOTE_CHECKPOINT_RECEIPT_INVALID') from None
        return blob_sha(raw)
