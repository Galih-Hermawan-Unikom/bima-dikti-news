"""Fresh admission -> remote reservation -> remote claim -> one POST -> receipt.

No automatic retries, historical promotion, revision sending or production fallback.
"""
from copy import deepcopy
from datetime import timedelta
import re
from uuid import uuid4

from kemdiktisaintek_state import StateError, fingerprint, timestamp
from kemdiktisaintek_monitor import apply_poll, validate_capture
from kemdiktisaintek_delivery_policy import TIMING, validate_reference, eligibility
from kemdiktisaintek_delivery_state import (validate_delivery_state, event_key, change)
from kemdiktisaintek_telegram import switches, format_announcement, format_test


def observe(backend, capture, *, reference=None, reference_sha=None):
    """Observation/admission checkpoint only. Does not inspect or POST Telegram."""
    old, sha = backend.load()
    validate_capture(capture)
    try:
        history = validate_reference(reference, reference_sha, at=capture.observed_at)
        reference_error = None
    except StateError as exc:
        history, reference_error = None, str(exc)
    monitor, report = apply_poll(old['observation_state'], capture, bima_history=history,
                                 max_poll_gap=TIMING.max_gap)
    state = change(old)
    state['observation_state'] = monitor
    for event in report['events']:
        identifier = event.get('uuid')
        if event['status'] != 'NEW_CANDIDATE' or reference_error or identifier in old['migration']['legacy_ids']:
            continue
        key = event_key(identifier)
        if key in state['admissions'] or key in state['outbox']:
            continue
        if monitor['observations'][identifier]['detail_checked_at'] != capture.observed_at:
            continue
        try:
            item = eligibility(state, identifier, reference, reference_sha, at=capture.observed_at,
                               require_admission=False)
        except StateError:
            continue
        state['admissions'][key] = {'event_key': key, 'uuid': identifier, 'event_type': 'INITIAL',
            'decision': 'ELIGIBLE', 'observed_at': capture.observed_at,
            'expires_at': (timestamp(capture.observed_at) + TIMING.evidence_freshness).isoformat(),
            'item_sha256': fingerprint(item), 'baseline_sha256': monitor['baseline']['sha256'],
            'reference_sha256': reference_sha}
    backend.checkpoint(state, sha)
    report.update(reference_error=reference_error, admissions=len(state['admissions']))
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

    def recover(self):
        """After restart, a claimed attempt is ambiguous even if it may never have POSTed."""
        old, sha = self.backend.load()
        keys = [key for key, row in old['outbox'].items() if row['status'] == 'SENDING']
        if not keys:
            return 0
        state = change(old)
        for key in keys:
            state['outbox'][key].update(status='AMBIGUOUS', error_classification='UNKNOWN_AFTER_RESTART')
        self.backend.checkpoint(state, sha)
        return len(keys)

    def reserve_initial(self, identifier, proof, *, reference, reference_sha):
        old, sha = self._gate(proof)
        key = event_key(identifier)
        if key in old['outbox']:
            return key
        item = eligibility(old, identifier, reference, reference_sha, at=self.clock())
        message = format_announcement(item)
        state = change(old)
        state['outbox'][key] = self._reservation(key, identifier, 'INITIAL', message, proof)
        self.backend.checkpoint(state, sha)
        return key

    def reserve_test(self, test_id, proof, *, approval):
        old, sha = self._gate(proof)
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
        state['outbox'][key] = {**self._reservation(key, None, 'TEST', message, proof), 'test_id': test_id}
        self.backend.checkpoint(state, sha)
        return key

    def _reservation(self, key, identifier, kind, message, proof):
        return {'event_key': key, 'source_uuid': identifier, 'event_type': kind, 'status': 'RESERVED',
            'payload': message, 'payload_hash': fingerprint(message), 'destination': deepcopy(proof.identity),
            'reserved_at': self.clock(), 'attempt_id': None, 'sending_claimed_at': None,
            'delivered_at': None, 'message_id': None, 'receipt': None, 'error_classification': None}

    def _eligible_reserved(self, state, row, *, reference, reference_sha):
        if row['event_type'] == 'INITIAL':
            item = eligibility(state, row['source_uuid'], reference, reference_sha, at=self.clock())
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

    def send(self, key, proof, *, reference=None, reference_sha=None):
        old, sha = self._gate(proof)
        row = old['outbox'].get(key)
        if row is None:
            raise StateError('NO_DURABLE_RESERVATION')
        if row['status'] in {'DELIVERED', 'AMBIGUOUS'}:
            return {'status': row['status'], 'post_attempted': False}
        if row['status'] == 'SENDING':
            self.recover()
            return {'status': 'AMBIGUOUS', 'post_attempted': False}
        if row['destination'] != proof.identity:
            raise StateError('RESERVED_DESTINATION_MISMATCH')
        self._eligible_reserved(old, row, reference=reference, reference_sha=reference_sha)
        state = change(old)
        claimed = state['outbox'][key]
        claimed.update(status='SENDING', attempt_id=uuid4().hex, sending_claimed_at=self.clock())
        self.backend.checkpoint(state, sha)  # durable ACK required before any Telegram POST
        current, current_sha = self._gate(proof)  # switch/preflight/state recheck after CAS
        current_row = current['outbox'][key]
        if current_row['status'] != 'SENDING' or current_row['attempt_id'] != claimed['attempt_id']:
            raise StateError('CLAIM_LOST_NO_POST')
        self._eligible_reserved(current, current_row, reference=reference, reference_sha=reference_sha)
        receipt, error = self.gateway.post_claimed(current_row, proof, at=self.clock())
        final = change(current)
        final_row = final['outbox'][key]
        final_row.update(status='DELIVERED' if receipt else 'AMBIGUOUS', receipt=receipt,
            message_id=receipt['message_id'] if receipt else None,
            delivered_at=self.clock() if receipt else None, error_classification=error)
        try:
            self.backend.checkpoint(final, current_sha)
        except Exception:
            raise ReconciliationRequired(receipt) from None
        return {'status': final_row['status'], 'post_attempted': True, 'receipt': receipt,
                'error_classification': error}
