"""Fresh admission -> remote reservation -> remote claim -> one POST -> receipt.

No automatic retries, historical promotion, revision sending or production fallback.
"""
from copy import deepcopy
from datetime import timedelta
import re
from uuid import uuid4

from kemdiktisaintek_prod_state import StateError, fingerprint, timestamp
from kemdiktisaintek_prod_monitor import apply_poll, validate_capture
from kemdiktisaintek_prod_delivery_policy import TIMING, eligibility
from kemdiktisaintek_prod_delivery_state import (validate_delivery_state, event_key, change,
    migrate_delivery_policy, migrate_operation_schema, LEGACY_DELIVERY_POLICY, DELIVERY_POLICY,
    migrate_official_policy, OFFICIAL_POLICY)
from kemdiktisaintek_prod_telegram import (switches, format_announcement, format_test,
    document_filename, safe_pdf_url)


def observe(backend, capture):
    """Observation/admission checkpoint only. Does not inspect or POST Telegram."""
    old, sha = backend.load()
    validate_capture(capture)
    if old['policy_version'] == LEGACY_DELIVERY_POLICY:
        migrated = migrate_delivery_policy(old, at=capture.observed_at)
        sha = backend.checkpoint(migrated, sha)
        old, sha = backend.load()
    if old['policy_version'] == DELIVERY_POLICY:
        migrated = migrate_official_policy(old, at=capture.observed_at)
        sha = backend.checkpoint(migrated, sha)
        old, sha = backend.load()
    monitor, report = apply_poll(old['observation_state'], capture, max_poll_gap=TIMING.max_gap)
    state = change(old)
    state['observation_state'] = monitor
    for event in report['events']:
        identifier = event.get('uuid')
        if event['status'] != 'NEW_CANDIDATE' or identifier in old['migration']['legacy_ids']:
            continue
        key = event_key(identifier)
        if key in state['admissions'] or key in state['outbox']:
            continue
        if monitor['observations'][identifier]['detail_checked_at'] != capture.observed_at:
            continue
        try:
            item = eligibility(state, identifier, at=capture.observed_at,
                               require_admission=False)
        except StateError:
            continue
        state['admissions'][key] = {'event_key': key, 'uuid': identifier, 'event_type': 'INITIAL',
            'decision': 'ELIGIBLE', 'observed_at': capture.observed_at,
            'expires_at': (timestamp(capture.observed_at) + TIMING.evidence_freshness).isoformat(),
            'item_sha256': fingerprint(item), 'baseline_sha256': monitor['baseline']['sha256'],
            'admission_policy_version': OFFICIAL_POLICY}
    backend.checkpoint(state, sha)
    report.update(admissions=len(state['admissions']))
    return report


class ReconciliationRequired(StateError):
    def __init__(self, receipt=None):
        super().__init__('RECEIPT_CHECKPOINT_FAILED_RECONCILIATION_REQUIRED')
        self.receipt = receipt  # sanitized possible receipt, never a success claim


class Delivery:
    def __init__(self, backend, gateway, *, environment, clock):
        self.backend, self.gateway = backend, gateway
        # Keep a live mapping/callback, so a kill switch can be checked again before POST.
        self.environment, self.clock = environment, clock

    def _gate(self, proof):
        env = self.environment() if callable(self.environment) else self.environment
        switches(env)
        if getattr(self.backend, 'durable', False) is not True:
            raise StateError('REMOTE_CHECKPOINT_REQUIRED')
        proof.validate(self.gateway.credentials, at=self.clock())
        state, sha = self.backend.load()
        validate_delivery_state(state)
        return state, sha

    def _ensure_operations(self):
        state, sha = self.backend.load()
        if state.get('operation_schema_version') == 1:
            return state, sha
        migrated = migrate_operation_schema(state, at=self.clock())
        self.backend.checkpoint(migrated, sha)
        return self.backend.load()

    @staticmethod
    def _aggregate(row):
        statuses = [op['status'] for op in row['operations']]
        row['status'] = ('AMBIGUOUS' if 'AMBIGUOUS' in statuses else 'SENDING' if 'SENDING' in statuses
            else 'DELIVERED' if all(s == 'DELIVERED' for s in statuses)
            else 'PARTIAL' if 'DELIVERED' in statuses else 'FAILED' if 'FAILED' in statuses else 'RESERVED')
        summary = row['operations'][0]
        for field in ('attempt_id', 'sending_claimed_at', 'delivered_at', 'receipt', 'error_classification'):
            row[field] = summary.get(field)
        if row['status'] in {'AMBIGUOUS', 'PARTIAL', 'FAILED'} and not row.get('error_classification'):
            row['error_classification'] = next((op.get('error_classification') for op in row['operations']
                if op.get('error_classification')), None)
        row['message_id'] = (summary.get('receipt') or {}).get('message_id')
        return row

    def recover(self):
        """After restart, a claimed attempt is ambiguous even if it may never have POSTed."""
        old, sha = self._ensure_operations()
        keys = [key for key, row in old['outbox'].items()
                if any(op['status'] == 'SENDING' for op in row['operations'])]
        if not keys:
            return 0
        state = change(old)
        for key in keys:
            row = state['outbox'][key]
            for op in row['operations']:
                if op['status'] == 'SENDING':
                    op.update(status='AMBIGUOUS', error_classification='UNKNOWN_AFTER_RESTART')
            self._aggregate(row)
        self.backend.checkpoint(state, sha)
        return len(keys)

    def reserve_initial(self, identifier, proof):
        old, sha = self._gate(proof)
        old, sha = self._ensure_operations()
        self._gate(proof)
        key = event_key(identifier)
        if key in old['outbox']:
            return key
        item = eligibility(old, identifier, at=self.clock())
        message = format_announcement(item)
        state = change(old)
        row = self._reservation(key, identifier, 'INITIAL', message, proof)
        row['operations'] = [self._operation('summary', 'SUMMARY', message, proof)]
        for index, doc in enumerate(item.get('attachments', []), 1):
            if doc.get('kind') != 'pdf' or not safe_pdf_url(doc.get('url')):
                continue
            label = doc.get('label') or ''
            metadata = {'url': doc['url'], 'filename': document_filename(doc['url'], label), 'caption': ''}
            row['operations'].append(self._operation(f'attachment-{index}', 'ATTACHMENT', metadata, proof))
        state['outbox'][key] = row
        self.backend.checkpoint(state, sha)
        return key

    def reserve_test(self, test_id, proof, *, approval):
        old, sha = self._gate(proof)
        old, sha = self._ensure_operations()
        self._gate(proof)
        if approval != 'SEND_ONE_EXPERIMENT_TEST':
            raise StateError('EXPLICIT_SEND_TEST_APPROVAL_REQUIRED')
        message = format_test(test_id)
        key = 'kemdiktisaintek:test:' + test_id
        if key in old['outbox']:
            return key
        # Prevent changing the nonce to obtain an accidental second live test.
        if any(row['event_type'] == 'TEST' for row in old['outbox'].values()):
            raise StateError('FIRST_TEST_ALREADY_RESERVED_OPERATOR_REVIEW_REQUIRED')
        at = self.clock()
        state = change(old)
        state['test_authorizations'][key] = {'approved_at': at,
            'expires_at': (timestamp(at)+timedelta(minutes=5)).isoformat(),
            'scope': 'ONE_EXPERIMENT_MESSAGE', 'destination': proof.identity}
        row = {**self._reservation(key, None, 'TEST', message, proof), 'test_id': test_id}
        row['operations'] = [self._operation('summary', 'SUMMARY', message, proof)]
        state['outbox'][key] = row
        self.backend.checkpoint(state, sha)
        return key

    def _reservation(self, key, identifier, kind, message, proof):
        return {'event_key': key, 'source_uuid': identifier, 'event_type': kind, 'status': 'RESERVED',
            'payload': message, 'payload_hash': fingerprint(message), 'destination': deepcopy(proof.identity),
            'reserved_at': self.clock(), 'attempt_id': None, 'sending_claimed_at': None,
            'delivered_at': None, 'message_id': None, 'receipt': None, 'error_classification': None}

    def _operation(self, operation_id, kind, operation_payload, proof):
        return {'operation_id': operation_id, 'kind': kind, 'status': 'RESERVED',
            'payload': deepcopy(operation_payload), 'payload_hash': fingerprint(operation_payload),
            'destination': deepcopy(proof.identity), 'attempt_id': None, 'sending_claimed_at': None,
            'delivered_at': None, 'receipt': None, 'error_classification': None}

    def _eligible_reserved(self, state, row):
        if row['event_type'] == 'INITIAL':
            item = eligibility(state, row['source_uuid'], at=self.clock())
            if fingerprint(format_announcement(item)) != row['payload_hash']:
                raise StateError('RESERVED_PAYLOAD_CHANGED')
        elif row['event_type'] == 'TEST':
            auth = state['test_authorizations'][row['event_key']]
            if (auth.get('scope') != 'ONE_EXPERIMENT_MESSAGE' or auth.get('destination') != row['destination']
                    or not timestamp(auth['approved_at']) <= timestamp(self.clock()) < timestamp(auth['expires_at'])
                    or row['payload'] != format_test(row['test_id'])):
                raise StateError('SEND_TEST_AUTHORIZATION_EXPIRED_OR_INVALID')
        else:
            raise StateError('REVISION_AUTOSEND_DISABLED')

    def send(self, key, proof):
        old, sha = self._gate(proof)
        old, sha = self._ensure_operations()
        self._gate(proof)
        row = old['outbox'].get(key)
        if row is None:
            raise StateError('NO_DURABLE_RESERVATION')
        if row['status'] in {'DELIVERED', 'AMBIGUOUS', 'FAILED'}:
            return {'status': row['status'], 'post_attempted': False}
        if row['status'] == 'SENDING':
            self.recover()
            return {'status': 'AMBIGUOUS', 'post_attempted': False}
        if row['destination'] != proof.identity:
            raise StateError('RESERVED_DESTINATION_MISMATCH')
        if row['status'] == 'PARTIAL':
            # Continue only a still-reserved operation; completed/uncertain parts are immutable.
            pass
        self._eligible_reserved(old, row)
        attempted = False
        last_receipt = None
        for index, operation in enumerate(row['operations']):
            if operation['status'] == 'DELIVERED':
                continue
            if operation['status'] != 'RESERVED':
                break
            state = change(old)
            claimed_row = state['outbox'][key]
            claimed = claimed_row['operations'][index]
            claimed.update(status='SENDING', attempt_id=uuid4().hex, sending_claimed_at=self.clock())
            self._aggregate(claimed_row)
            self.backend.checkpoint(state, sha)
            current, current_sha = self._gate(proof)
            current_row = current['outbox'][key]
            current_op = current_row['operations'][index]
            if current_op['status'] != 'SENDING' or current_op['attempt_id'] != claimed['attempt_id']:
                raise StateError('CLAIM_LOST_NO_POST')
            self._eligible_reserved(current, current_row)
            # One operation payload is presented to the gateway with its durable claim and destination.
            request_row = {**current_op, 'event_key': key, 'status': 'SENDING',
                           'destination': current_op['destination'], 'operation': current_op}
            receipt, error = self.gateway.post_claimed(request_row, proof, at=self.clock())
            attempted = True
            final = change(current)
            final_row = final['outbox'][key]
            final_op = final_row['operations'][index]
            op_status = ('DELIVERED' if receipt else 'FAILED' if error in {'TELEGRAM_REJECTED'}
                         or (error and error.startswith('PREPOST_')) else 'AMBIGUOUS')
            final_op.update(status=op_status, receipt=receipt,
                delivered_at=self.clock() if receipt else None, error_classification=error)
            self._aggregate(final_row)
            # Legacy summary aliases remain available for immutable TEST history and audit tools.
            if index == 0:
                final_row.update(payload=deepcopy(final_op['payload']), payload_hash=final_op['payload_hash'])
            try:
                self.backend.checkpoint(final, current_sha)
            except Exception:
                raise ReconciliationRequired(receipt) from None
            last_receipt = receipt
            old, sha = self.backend.load()
            if not receipt:
                break
        final_row = old['outbox'][key]
        return {'status': final_row['status'], 'post_attempted': attempted, 'receipt': last_receipt,
                'error_classification': final_row.get('error_classification')}
