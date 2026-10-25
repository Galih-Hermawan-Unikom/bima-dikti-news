"""Explicit schema v2 envelope; preserves the validated Stage 2 observation schema."""
from copy import deepcopy
import re

from kemdiktisaintek_prod_state import (validate_state as validate_v1, validate_lineage as lineage_v1,
    StateError, fingerprint, canonical, timestamp, uid, MAX_BYTES, SOURCE)

LEGACY_DELIVERY_POLICY = 'kemdiktisaintek-delivery-v1'
DELIVERY_POLICY = 'kemdiktisaintek-delivery-v2-independent-sources'
STATUSES = {'RESERVED', 'SENDING', 'DELIVERED', 'AMBIGUOUS'}


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


def validate_delivery_state(state):
    try:
        if (not isinstance(state, dict) or type(state.get('schema_version')) is not int or state['schema_version'] != 2
                or state.get('source') != SOURCE
                or state.get('policy_version') not in {LEGACY_DELIVERY_POLICY, DELIVERY_POLICY}
                or type(state.get('generation')) is not int or state['generation'] < 1
                or not all(isinstance(state.get(k), dict) for k in ('admissions', 'outbox', 'test_authorizations'))):
            raise StateError('INVALID_DELIVERY_SCHEMA')
        observations = validate_v1(state['observation_state'])
        policy_migration = state.get('policy_migration')
        if policy_migration is not None:
            timestamp(policy_migration['at'])
            if (state['policy_version'] != DELIVERY_POLICY
                    or policy_migration.get('from') != LEGACY_DELIVERY_POLICY
                    or policy_migration.get('to') != DELIVERY_POLICY
                    or not re.fullmatch('[a-f0-9]{64}', policy_migration.get('source_sha256', ''))):
                raise StateError('INVALID_DELIVERY_POLICY_MIGRATION')
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
                    or (admission.get('admission_policy_version') == DELIVERY_POLICY
                        and 'reference_sha256' in admission)
                    or (admission.get('admission_policy_version') is None
                        and not re.fullmatch('[a-f0-9]{64}', admission.get('reference_sha256', '')))
                    or (admission.get('admission_policy_version') not in {None, DELIVERY_POLICY})
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
            timestamp(row['reserved_at'])
            if initial and key not in state['admissions']:
                raise StateError('OUTBOX_WITHOUT_ADMISSION')
            if test and (row.get('source_uuid') is not None or key not in state['test_authorizations']
                         or not re.fullmatch('[A-Za-z0-9_-]{8,64}', row.get('test_id', ''))
                         or not row['payload']['text'].startswith('<b>PENGUJIAN')):
                raise StateError('INVALID_TEST_EVENT')
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
            elif any(row.get(k) is not None for k in ('receipt', 'message_id', 'delivered_at')):
                raise StateError('UNCONFIRMED_RECEIPT_FIELDS')
            errors = {'HTTP_ERROR', 'TELEGRAM_REJECTED', 'INVALID_OR_WRONG_DESTINATION_RECEIPT',
                      'TIMEOUT', 'NETWORK_OR_INVALID_RESPONSE', 'UNKNOWN_AFTER_RESTART'}
            if ((row['status'] == 'AMBIGUOUS' and row.get('error_classification') not in errors)
                    or (row['status'] != 'AMBIGUOUS' and row.get('error_classification') is not None)):
                raise StateError('INVALID_SAFE_ERROR_CLASSIFICATION')
        if len(canonical(state)) > MAX_BYTES:
            raise StateError('STATE_TOO_LARGE')
    except (KeyError, TypeError, ValueError, AttributeError):
        raise StateError('INVALID_DELIVERY_CONTENT') from None
    return state


def validate_delivery_lineage(old, new):
    validate_delivery_state(old); validate_delivery_state(new)
    policy_transition = old['policy_version'] == LEGACY_DELIVERY_POLICY and new['policy_version'] == DELIVERY_POLICY
    if (new['generation'] != old['generation'] + 1 or new['migration'] != old['migration']
            or not old['admissions'].keys() <= new['admissions'].keys()
            or not old['outbox'].keys() <= new['outbox'].keys()
            or not old['test_authorizations'].keys() <= new['test_authorizations'].keys()):
        raise StateError('INVALID_DELIVERY_LINEAGE')
    if old['policy_version'] != new['policy_version']:
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
    transitions = {'RESERVED': {'RESERVED', 'SENDING'}, 'SENDING': {'SENDING', 'DELIVERED', 'AMBIGUOUS'},
                   'DELIVERED': {'DELIVERED'}, 'AMBIGUOUS': {'AMBIGUOUS'}}
    for key in new['outbox'].keys() - old['outbox'].keys():
        if new['outbox'][key]['status'] != 'RESERVED':
            raise StateError('NEW_OUTBOX_MUST_START_RESERVED')
    for key, before in old['outbox'].items():
        after = new['outbox'][key]
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


def change(state):
    result = deepcopy(state)
    result['generation'] += 1
    return result
