"""Explicit schema v2 envelope; preserves the validated Stage 2 observation schema."""
from copy import deepcopy
import re

from kemdiktisaintek_prod_state import (validate_state as validate_v1, validate_lineage as lineage_v1,
    StateError, fingerprint, canonical, timestamp, uid, MAX_BYTES, SOURCE)

LEGACY_DELIVERY_POLICY = 'kemdiktisaintek-delivery-v1'
DELIVERY_POLICY = 'kemdiktisaintek-delivery-v2-independent-sources'
OFFICIAL_POLICY = 'kemdiktisaintek-delivery-v3-all-official'
STATUSES = {'RESERVED', 'SENDING', 'DELIVERED', 'AMBIGUOUS', 'PARTIAL', 'FAILED'}
OPERATION_STATUSES = {'RESERVED', 'SENDING', 'DELIVERED', 'AMBIGUOUS', 'FAILED'}


def event_key(identifier, event_type='INITIAL', revision_id=None):
    uid(identifier)
    if event_type == 'INITIAL':
        return f'kemdiktisaintek:{identifier}:initial'
    if event_type == 'REVISION' and re.fullmatch('[a-f0-9]{64}', revision_id or ''):
        return f'kemdiktisaintek:{identifier}:revision:{revision_id}'
    raise StateError('INVALID_EVENT_TYPE')


def migrate_v1(state, *, at):
    """Pure, explicit migration. All pre-migration UUIDs are non-admissible legacy IDs."""
    validate_v1(state)
    if timestamp(at) < timestamp(state['last_successful_poll_at']):
        raise StateError('MIGRATION_TIME_BEFORE_STATE')
    result = {'schema_version': 2, 'source': SOURCE, 'generation': 1,
        'policy_version': DELIVERY_POLICY, 'observation_state': deepcopy(state),
        'migration': {'from_schema': 1, 'at': at, 'source_sha256': fingerprint(state),
                      'legacy_ids': sorted(state['observations'])},
        'admissions': {}, 'outbox': {}, 'test_authorizations': {}}
    return validate_delivery_state(result)


def migrate_delivery_policy(state, *, at):
    """Explicit, history-preserving transition from cross-source to source-local eligibility."""
    validate_delivery_state(state)
    if state['policy_version'] != LEGACY_DELIVERY_POLICY:
        raise StateError('DELIVERY_POLICY_MIGRATION_NOT_APPLICABLE')
    if timestamp(at) < timestamp(state['observation_state']['last_successful_poll_at']):
        raise StateError('POLICY_MIGRATION_TIME_BEFORE_STATE')
    result = deepcopy(state)
    result['generation'] += 1
    result['policy_migration'] = {'from': LEGACY_DELIVERY_POLICY, 'to': DELIVERY_POLICY,
                                  'at': at, 'source_sha256': fingerprint(state)}
    result['policy_version'] = DELIVERY_POLICY
    return validate_delivery_state(result)


def migrate_operation_schema(state, *, at):
    """Explicit v2 outbox migration; preserves every event and receipt."""
    validate_delivery_state(state)
    if state.get('operation_schema_version') == 1:
        raise StateError('OPERATION_SCHEMA_ALREADY_MIGRATED')
    if timestamp(at) < timestamp(state['observation_state']['last_successful_poll_at']):
        raise StateError('OPERATION_MIGRATION_TIME_BEFORE_STATE')
    result = deepcopy(state)
    source_hash = fingerprint(state)
    for row in result['outbox'].values():
        status = row['status']
        error = row.get('error_classification')
        if status == 'SENDING':
            status, error = 'AMBIGUOUS', 'UNKNOWN_AFTER_OPERATION_MIGRATION'
            row.update(status=status, error_classification=error)
        summary = {'operation_id': 'summary', 'kind': 'SUMMARY', 'status': status,
            'payload': deepcopy(row['payload']), 'payload_hash': row['payload_hash'],
            'destination': deepcopy(row['destination']), 'attempt_id': row.get('attempt_id'),
            'sending_claimed_at': row.get('sending_claimed_at'), 'delivered_at': row.get('delivered_at'),
            'receipt': deepcopy(row.get('receipt')), 'error_classification': error}
        row['operations'] = [summary]
    result['generation'] += 1
    result['operation_schema_version'] = 1
    result['operation_migration'] = {'from': 0, 'to': 1, 'at': at, 'source_sha256': source_hash}
    return validate_delivery_state(result)


def migrate_official_policy(state, *, at):
    """Preserve all observations/admissions/receipts; only future IDs use new policy."""
    validate_delivery_state(state)
    if state['policy_version'] != DELIVERY_POLICY:
        raise StateError('OFFICIAL_POLICY_MIGRATION_NOT_APPLICABLE')
    if timestamp(at) < timestamp(state['observation_state']['last_successful_poll_at']):
        raise StateError('POLICY_MIGRATION_TIME_BEFORE_STATE')
    result = change(state)
    result['policy_version'] = OFFICIAL_POLICY
    result['official_policy_migration'] = {'from': DELIVERY_POLICY, 'to': OFFICIAL_POLICY,
        'at': at, 'source_sha256': fingerprint(state)}
    return validate_delivery_state(result)


def validate_delivery_state(state):
    try:
        if (not isinstance(state, dict) or type(state.get('schema_version')) is not int or state['schema_version'] != 2
                or state.get('source') != SOURCE
                or state.get('policy_version') not in {LEGACY_DELIVERY_POLICY, DELIVERY_POLICY, OFFICIAL_POLICY}
                or type(state.get('generation')) is not int or state['generation'] < 1
                or not all(isinstance(state.get(k), dict) for k in ('admissions', 'outbox', 'test_authorizations'))):
            raise StateError('INVALID_DELIVERY_SCHEMA')
        observations = validate_v1(state['observation_state'])
        operation_version = state.get('operation_schema_version')
        if operation_version not in (None, 1):
            raise StateError('INVALID_OPERATION_SCHEMA_VERSION')
        operation_migration = state.get('operation_migration')
        if operation_version == 1:
            timestamp(operation_migration['at'])
            if (operation_migration.get('from') != 0 or operation_migration.get('to') != 1
                    or not re.fullmatch('[a-f0-9]{64}', operation_migration.get('source_sha256', ''))):
                raise StateError('INVALID_OPERATION_MIGRATION')
        elif operation_migration is not None:
            raise StateError('UNEXPECTED_OPERATION_MIGRATION')
        policy_migration = state.get('policy_migration')
        if policy_migration is not None:
            timestamp(policy_migration['at'])
            if (state['policy_version'] not in {DELIVERY_POLICY, OFFICIAL_POLICY}
                    or policy_migration.get('from') != LEGACY_DELIVERY_POLICY
                    or policy_migration.get('to') != DELIVERY_POLICY
                    or not re.fullmatch('[a-f0-9]{64}', policy_migration.get('source_sha256', ''))):
                raise StateError('INVALID_DELIVERY_POLICY_MIGRATION')
        official = state.get('official_policy_migration')
        if state['policy_version'] == OFFICIAL_POLICY:
            if (not isinstance(official, dict) or official.get('from') != DELIVERY_POLICY
                    or official.get('to') != OFFICIAL_POLICY
                    or not re.fullmatch('[a-f0-9]{64}', official.get('source_sha256', ''))):
                raise StateError('INVALID_OFFICIAL_POLICY_MIGRATION')
            timestamp(official['at'])
        elif official is not None:
            raise StateError('UNEXPECTED_OFFICIAL_POLICY_MIGRATION')
        migration = state['migration']
        timestamp(migration['at'])
        if migration['from_schema'] != 1 or not re.fullmatch('[a-f0-9]{64}', migration['source_sha256']):
            raise StateError('INVALID_MIGRATION')
        if not set(migration['legacy_ids']) <= observations['observations'].keys():
            raise StateError('MIGRATED_HISTORY_MISSING')
        for key, admission in state['admissions'].items():
            if (key != event_key(admission['uuid']) or admission.get('event_key') != key
                    or admission.get('event_type') != 'INITIAL' or admission.get('decision') != 'ELIGIBLE'
                    or admission['uuid'] in migration['legacy_ids']
                    or admission['uuid'] not in observations['observations']
                    or not all(re.fullmatch('[a-f0-9]{64}', admission.get(field, '')) for field in
                               ('item_sha256', 'baseline_sha256'))
                    or (admission.get('admission_policy_version') in {DELIVERY_POLICY, OFFICIAL_POLICY}
                        and 'reference_sha256' in admission)
                    or (admission.get('admission_policy_version') is None
                        and not re.fullmatch('[a-f0-9]{64}', admission.get('reference_sha256', '')))
                    or (admission.get('admission_policy_version') not in {None, DELIVERY_POLICY, OFFICIAL_POLICY})
                    or timestamp(admission['expires_at']) <= timestamp(admission['observed_at'])):
                raise StateError('INVALID_ADMISSION')
        for key, row in state['outbox'].items():
            initial = row.get('event_type') == 'INITIAL'
            test = row.get('event_type') == 'TEST'
            expected = event_key(row['source_uuid']) if initial else 'kemdiktisaintek:test:' + row.get('test_id', '')
            if (not (initial or test) or key != expected or row.get('event_key') != key
                    or row.get('status') not in STATUSES or row.get('payload_hash') != fingerprint(row['payload'])
                    or row['payload'].get('parse_mode') != 'HTML' or not isinstance(row['payload'].get('text'), str)
                    or set(row['payload']) != {'text', 'parse_mode', 'disable_web_page_preview'}
                    or row['payload']['disable_web_page_preview'] is not True
                    or len(row['payload']['text'].encode('utf-16-le')) // 2 > 3900
                    or not re.fullmatch('[a-f0-9]{64}', row.get('destination', {}).get('chat_sha256', ''))
                    or type(row['destination'].get('bot_id')) is not int
                    or not re.fullmatch('[a-f0-9]{64}', row['destination'].get('credential_sha256', ''))):
                raise StateError('INVALID_OUTBOX')
            if operation_version is None and row['status'] in {'PARTIAL', 'FAILED'}:
                raise StateError('OPERATION_STATUS_REQUIRES_SCHEMA_MIGRATION')
            timestamp(row['reserved_at'])
            if initial and key not in state['admissions']:
                raise StateError('OUTBOX_WITHOUT_ADMISSION')
            if test and (row.get('source_uuid') is not None or key not in state['test_authorizations']
                         or not re.fullmatch('[A-Za-z0-9_-]{8,64}', row.get('test_id', ''))
                         or not row['payload']['text'].startswith('<b>PENGUJIAN')):
                raise StateError('INVALID_TEST_EVENT')
            if operation_version == 1:
                operations = row.get('operations')
                if not isinstance(operations, list) or not operations or operations[0].get('kind') != 'SUMMARY':
                    raise StateError('INVALID_DELIVERY_OPERATIONS')
                seen_ops = set()
                for op in operations:
                    if (not isinstance(op, dict) or set(op) != {'operation_id','kind','status','payload','payload_hash',
                            'destination','attempt_id','sending_claimed_at','delivered_at','receipt','error_classification'}
                            or not re.fullmatch('[a-z0-9_-]{1,80}', op.get('operation_id', ''))
                            or op['operation_id'] in seen_ops or op.get('kind') not in {'SUMMARY', 'ATTACHMENT'}
                            or op.get('status') not in OPERATION_STATUSES
                            or op.get('payload_hash') != fingerprint(op.get('payload'))
                            or op.get('destination') != row['destination']):
                        raise StateError('INVALID_DELIVERY_OPERATION')
                    if op is operations[0] and (op.get('payload') != row['payload']
                            or op.get('payload_hash') != row['payload_hash']):
                        raise StateError('SUMMARY_OPERATION_CONFLICT')
                    if op['kind'] == 'ATTACHMENT':
                        attachment = op['payload']
                        if (not isinstance(attachment, dict) or set(attachment) != {'url', 'filename', 'caption'}
                                or not re.fullmatch(r'https://kemdiktisaintek\.go\.id/api/file/humas-production/announcements/\d{4}/\d{1,2}/files/[0-9a-fA-F-]{36}/[^/?#]+\.pdf', attachment.get('url', ''), re.I)
                                or not isinstance(attachment.get('filename'), str)
                                or not attachment['filename'].lower().endswith('.pdf') or len(attachment['filename']) > 180
                                or re.search(r'[\x00-\x1f<>:"/\\|?*]', attachment['filename'])
                                or not isinstance(attachment.get('caption'), str)):
                            raise StateError('UNSAFE_ATTACHMENT_OPERATION')
                    elif op is not operations[0]:
                        raise StateError('SUMMARY_OPERATION_ORDER_INVALID')
                    seen_ops.add(op['operation_id'])
                    if op['status'] == 'RESERVED':
                        if (op.get('attempt_id') is not None or op.get('sending_claimed_at') is not None
                                or op.get('error_classification') is not None):
                            raise StateError('OPERATION_ALREADY_ATTEMPTED')
                    else:
                        if not re.fullmatch('[a-f0-9]{32}', op.get('attempt_id', '')):
                            raise StateError('INVALID_OPERATION_ATTEMPT')
                        if timestamp(op['sending_claimed_at']) < timestamp(row['reserved_at']):
                            raise StateError('INVALID_OPERATION_ATTEMPT_TIME')
                    if op['status'] == 'DELIVERED':
                        receipt = op.get('receipt') or {}
                        if (receipt.get('http_status') != 200 or receipt.get('ok') is not True
                                or type(receipt.get('message_id')) is not int or receipt['message_id'] <= 0
                                or receipt.get('destination_sha256') != row['destination']['chat_sha256']):
                            raise StateError('INVALID_OPERATION_RECEIPT')
                        expected_method = 'sendDocument' if op['kind'] == 'ATTACHMENT' else 'sendMessage'
                        if receipt.get('method') not in (None, expected_method):
                            raise StateError('INVALID_OPERATION_METHOD_RECEIPT')
                        if op['kind'] == 'ATTACHMENT' and not re.fullmatch('[a-f0-9]{64}', receipt.get('file_id_sha256', '')):
                            raise StateError('INVALID_DOCUMENT_FILE_RECEIPT')
                        if op['kind'] == 'ATTACHMENT' and receipt.get('file_name') != op['payload']['filename']:
                            raise StateError('INVALID_DOCUMENT_FILENAME_RECEIPT')
                        timestamp(op['delivered_at'])
                        if (timestamp(op['delivered_at']) < timestamp(op['sending_claimed_at'])
                                or op.get('error_classification') is not None):
                            raise StateError('INVALID_OPERATION_RECEIPT_TIME')
                    elif any(op.get(field) is not None for field in ('receipt', 'delivered_at')):
                        raise StateError('UNCONFIRMED_OPERATION_RECEIPT')
                    if op['status'] == 'AMBIGUOUS' and not op.get('error_classification'):
                        raise StateError('AMBIGUOUS_OPERATION_REQUIRES_REASON')
                    if op['status'] == 'SENDING' and op.get('error_classification') is not None:
                        raise StateError('SENDING_OPERATION_HAS_ERROR')
                    allowed_errors = {'HTTP_ERROR', 'TELEGRAM_REJECTED', 'INVALID_OR_WRONG_DESTINATION_RECEIPT',
                        'INVALID_DOCUMENT_RECEIPT', 'TIMEOUT', 'NETWORK_OR_INVALID_RESPONSE',
                        'UNKNOWN_AFTER_RESTART', 'UNKNOWN_AFTER_OPERATION_MIGRATION', 'PREPOST_PDF_DOWNLOAD_FAILED'}
                    if op.get('error_classification') is not None and op['error_classification'] not in allowed_errors:
                        raise StateError('UNSAFE_OPERATION_ERROR_CLASSIFICATION')
                statuses = [op['status'] for op in operations]
                aggregate = ('AMBIGUOUS' if 'AMBIGUOUS' in statuses else 'SENDING' if 'SENDING' in statuses
                    else 'DELIVERED' if all(s == 'DELIVERED' for s in statuses)
                    else 'PARTIAL' if 'DELIVERED' in statuses else 'FAILED' if 'FAILED' in statuses else 'RESERVED')
                if row['status'] != aggregate:
                    raise StateError('EVENT_OPERATION_STATUS_MISMATCH')
            elif row.get('operations') is not None:
                raise StateError('OPERATIONS_WITHOUT_SCHEMA_MIGRATION')
            if operation_version is None:
                if row['status'] == 'RESERVED':
                    if row.get('attempt_id') is not None or row.get('sending_claimed_at') is not None:
                        raise StateError('RESERVATION_ALREADY_ATTEMPTED')
                else:
                    if not re.fullmatch('[a-f0-9]{32}', row.get('attempt_id', '')):
                        raise StateError('INVALID_ATTEMPT')
                    if timestamp(row['sending_claimed_at']) < timestamp(row['reserved_at']):
                        raise StateError('INVALID_ATTEMPT_TIME')
                if row['status'] == 'DELIVERED':
                    receipt = row.get('receipt', {})
                    if (receipt.get('http_status') != 200 or receipt.get('ok') is not True
                            or type(receipt.get('message_id')) is not int or receipt['message_id'] <= 0
                            or row.get('message_id') != receipt['message_id']
                            or receipt.get('destination_sha256') != row['destination']['chat_sha256']
                            or timestamp(row['delivered_at']) < timestamp(row['sending_claimed_at'])):
                        raise StateError('UNCONFIRMED_DELIVERY')
                elif row['status'] in {'SENDING', 'AMBIGUOUS'} and any(row.get(k) is not None for k in ('receipt', 'message_id', 'delivered_at')):
                    raise StateError('UNCONFIRMED_RECEIPT_FIELDS')
            errors = {'HTTP_ERROR', 'TELEGRAM_REJECTED', 'INVALID_OR_WRONG_DESTINATION_RECEIPT',
                      'TIMEOUT', 'NETWORK_OR_INVALID_RESPONSE', 'UNKNOWN_AFTER_RESTART'}
            if (operation_version is None and
                    ((row['status'] == 'AMBIGUOUS' and row.get('error_classification') not in errors | {'UNKNOWN_AFTER_OPERATION_MIGRATION'})
                    or (row['status'] not in {'AMBIGUOUS', 'PARTIAL', 'FAILED'} and row.get('error_classification') is not None))):
                raise StateError('INVALID_SAFE_ERROR_CLASSIFICATION')
        if len(canonical(state)) > MAX_BYTES:
            raise StateError('STATE_TOO_LARGE')
    except (KeyError, TypeError, ValueError, AttributeError):
        raise StateError('INVALID_DELIVERY_CONTENT') from None
    return state


def validate_delivery_lineage(old, new):
    validate_delivery_state(old); validate_delivery_state(new)
    policy_transition = old['policy_version'] == LEGACY_DELIVERY_POLICY and new['policy_version'] == DELIVERY_POLICY
    official_transition = old['policy_version'] == DELIVERY_POLICY and new['policy_version'] == OFFICIAL_POLICY
    operation_migration = old.get('operation_schema_version') is None and new.get('operation_schema_version') == 1
    if (new['generation'] != old['generation'] + 1 or new['migration'] != old['migration']
            or not old['admissions'].keys() <= new['admissions'].keys()
            or not old['outbox'].keys() <= new['outbox'].keys()
            or not old['test_authorizations'].keys() <= new['test_authorizations'].keys()):
        raise StateError('INVALID_DELIVERY_LINEAGE')
    if operation_migration:
        marker = new.get('operation_migration') or {}
        if marker.get('source_sha256') != fingerprint(old) or marker.get('from') != 0 or marker.get('to') != 1:
            raise StateError('INVALID_OPERATION_MIGRATION_LINEAGE')
    elif (old.get('operation_schema_version') != new.get('operation_schema_version')
          or old.get('operation_migration') != new.get('operation_migration')):
        raise StateError('OPERATION_SCHEMA_MIGRATION_IMMUTABLE')
    if official_transition:
        expected = migrate_official_policy(old, at=new['official_policy_migration']['at'])
        if new != expected:
            raise StateError('OFFICIAL_POLICY_MIGRATION_MUST_PRESERVE_HISTORY')
    elif old.get('official_policy_migration') != new.get('official_policy_migration'):
        raise StateError('OFFICIAL_POLICY_MIGRATION_IMMUTABLE')
    if old['policy_version'] != new['policy_version'] and not official_transition:
        marker = new.get('policy_migration') or {}
        if (not policy_transition or marker.get('from') != old['policy_version']
                or marker.get('to') != new['policy_version'] or marker.get('source_sha256') != fingerprint(old)):
            raise StateError('INVALID_DELIVERY_POLICY_LINEAGE')
    elif old.get('policy_migration') != new.get('policy_migration'):
        raise StateError('DELIVERY_POLICY_MIGRATION_IMMUTABLE')
    if old['observation_state'] != new['observation_state']:
        lineage_v1(old['observation_state'], new['observation_state'])
    for key, value in old['admissions'].items():
        if new['admissions'][key] != value:
            raise StateError('ADMISSION_IMMUTABLE')
    for key, value in old['test_authorizations'].items():
        if new['test_authorizations'][key] != value:
            raise StateError('TEST_AUTHORIZATION_IMMUTABLE')
    transitions = {'RESERVED': {'RESERVED', 'SENDING', 'AMBIGUOUS'}, 'SENDING': {'SENDING', 'DELIVERED', 'AMBIGUOUS', 'FAILED', 'PARTIAL'},
                   'DELIVERED': {'DELIVERED'}, 'AMBIGUOUS': {'AMBIGUOUS'}, 'PARTIAL': {'PARTIAL', 'SENDING', 'DELIVERED', 'AMBIGUOUS', 'FAILED'},
                   'FAILED': {'FAILED'}}
    for key in new['outbox'].keys() - old['outbox'].keys():
        if new['outbox'][key]['status'] != 'RESERVED':
            raise StateError('NEW_OUTBOX_MUST_START_RESERVED')
    for key, before in old['outbox'].items():
        after = new['outbox'][key]
        if operation_migration:
            allowed = deepcopy(before)
            if before['status'] == 'SENDING':
                allowed.update(status='AMBIGUOUS', error_classification='UNKNOWN_AFTER_OPERATION_MIGRATION')
            if any(after.get(field) != value for field, value in allowed.items()):
                raise StateError('OPERATION_MIGRATION_CHANGED_EVENT_HISTORY')
            if set(after) != set(allowed) | {'operations'}:
                raise StateError('OPERATION_MIGRATION_CHANGED_EVENT_SCHEMA')
            continue
        if after['status'] not in transitions[before['status']]:
            raise StateError('OUTBOX_TRANSITION_FORBIDDEN')
        for field in ('event_key', 'source_uuid', 'event_type', 'payload', 'payload_hash', 'destination', 'reserved_at'):
            if before[field] != after[field]:
                raise StateError('RESERVATION_IMMUTABLE')
        if before.get('test_id') != after.get('test_id'):
            raise StateError('TEST_ID_IMMUTABLE')
        if before['status'] != 'RESERVED' and any(before.get(k) != after.get(k) for k in ('attempt_id', 'sending_claimed_at')):
            raise StateError('ATTEMPT_IMMUTABLE')
        if before['status'] in {'DELIVERED', 'AMBIGUOUS'} and before != after:
            raise StateError('TERMINAL_EVENT_IMMUTABLE')
        if new.get('operation_schema_version') == 1:
            old_ops = before.get('operations', [])
            new_ops = after.get('operations', [])
            if len(new_ops) != len(old_ops) or [x['operation_id'] for x in new_ops] != [x['operation_id'] for x in old_ops]:
                raise StateError('OPERATION_SET_IMMUTABLE')
            op_transitions = {'RESERVED': {'RESERVED', 'SENDING'}, 'SENDING': {'SENDING','DELIVERED','AMBIGUOUS','FAILED'},
                'DELIVERED': {'DELIVERED'}, 'AMBIGUOUS': {'AMBIGUOUS'}, 'FAILED': {'FAILED'}}
            for prev, nxt in zip(old_ops, new_ops):
                if nxt['status'] not in op_transitions[prev['status']]:
                    raise StateError('OPERATION_TRANSITION_FORBIDDEN')
                for field in ('operation_id','kind','payload','payload_hash','destination'):
                    if prev[field] != nxt[field]:
                        raise StateError('OPERATION_RESERVATION_IMMUTABLE')
                if prev['status'] != 'RESERVED' and any(prev.get(field) != nxt.get(field)
                        for field in ('attempt_id', 'sending_claimed_at')):
                    raise StateError('OPERATION_ATTEMPT_IMMUTABLE')
                if prev['status'] in {'DELIVERED', 'AMBIGUOUS', 'FAILED'} and prev != nxt:
                    raise StateError('TERMINAL_OPERATION_IMMUTABLE')


def change(state):
    result = deepcopy(state)
    result['generation'] += 1
    return result
