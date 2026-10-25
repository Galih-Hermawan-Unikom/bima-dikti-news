"""Fresh, pinned BIMA evidence and distinct timing budgets for experiment delivery."""
from dataclasses import dataclass
from datetime import timedelta

from kemdiktisaintek_state import StateError, fingerprint, timestamp
from kemdiktisaintek_policy import cross_source_dedup, classify


@dataclass(frozen=True)
class Timing:
    planned_interval: timedelta = timedelta(minutes=30)
    lateness_tolerance: timedelta = timedelta(minutes=20)
    evidence_freshness: timedelta = timedelta(minutes=10)
    publication_window: timedelta = timedelta(hours=24)
    first_seen_ttl: timedelta = timedelta(hours=24)
    reference_freshness: timedelta = timedelta(minutes=60)

    @property
    def max_gap(self):
        return self.planned_interval + self.lateness_tolerance


TIMING = Timing()


def validate_reference(reference, expected_sha256, *, at, env=None, candidate=None):
    """Operator pins a reviewed snapshot externally; a self-declared timestamp isn't authentication."""
    if isinstance(reference, dict) and reference.get('schema_version') == 2:
        if env is None:
            raise StateError('BIMA_REFERENCE_PRODUCER_CONTEXT_REQUIRED')
        from kemdiktisaintek_bima_reference import validate_reference as validate_live
        return validate_live(reference, expected_sha256, at=at, env=env, candidate=candidate)
    try:
        if (not isinstance(reference, dict) or fingerprint(reference) != expected_sha256
                or reference.get('schema_version') != 1 or reference.get('source') != 'bima'
                or reference.get('source_url') != 'https://bima.kemdiktisaintek.go.id/pengumuman'
                or reference.get('scope') != 'unfiltered_recent' or reference.get('healthy') is not True
                or reference.get('complete_window') is not True or not isinstance(reference.get('items'), list)
                or not reference['items'] or any(not isinstance(r, dict) or not r.get('title') for r in reference['items'])):
            raise StateError('BIMA_REFERENCE_UNTRUSTED_OR_EMPTY')
        now, captured = timestamp(at), timestamp(reference['captured_at'])
        if (not timedelta(0) <= now - captured <= TIMING.reference_freshness
                or timestamp(reference['coverage_from']) > now - TIMING.publication_window
                or timestamp(reference['coverage_until']) != captured):
            raise StateError('BIMA_REFERENCE_STALE_OR_INCOMPLETE')
    except (KeyError, TypeError, ValueError):
        raise StateError('BIMA_REFERENCE_INVALID') from None
    return reference['items']


def eligibility(state, identifier, reference, reference_sha, *, at, require_admission=True, reference_env=None):
    monitor = state['observation_state']
    row = monitor['observations'].get(identifier)
    if row is None or row['initial_inventory'] or identifier in state['migration']['legacy_ids']:
        raise StateError('INITIAL_OR_LEGACY_INVENTORY_NOT_ADMISSIBLE')
    if row['detection_status'] != 'NEW_CANDIDATE' or row['reasons'] != ['POLICY_COMPLETE_OBSERVATION_ONLY']:
        raise StateError('DETECTION_NOT_CURRENT_CANDIDATE')
    item = row['last_item']
    now, seen, publication = timestamp(at), timestamp(row['first_seen_at']), timestamp(item.get('published_at'))
    evidence = timestamp(row.get('detail_checked_at'))
    if (item.get('identity_basis') != 'uuid' or item.get('id') != identifier or not item.get('body_text')
            or item.get('publication_basis') != 'detail_datetime' or not item.get('content_sha256')
            or not timestamp(monitor['activation_at']) < publication <= now
            or not timedelta(0) <= now - publication <= TIMING.publication_window
            or not timedelta(0) <= now - seen <= TIMING.first_seen_ttl
            or not timedelta(0) <= now - evidence <= TIMING.evidence_freshness
            or not timedelta(0) <= now - timestamp(monitor['last_successful_poll_at']) <= TIMING.evidence_freshness
            or row['classification']['decision'] != 'RELEVANT' or classify(item)['decision'] != 'RELEVANT'
            or any(h.get('uuid') == identifier or h['url'] in row['url_history'] for h in monitor['identity_holds'].values())):
        raise StateError('ARTICLE_EVIDENCE_INELIGIBLE')
    coverage = monitor.get('last_capture_coverage', {})
    if coverage.get('all_requested_pages_healthy') is not True or coverage.get('unfiltered') is not True:
        raise StateError('CAPTURE_NOT_TRUSTED')
    items = validate_reference(reference, reference_sha, at=at, env=reference_env, candidate=item)
    if cross_source_dedup(item, items)['status'] != 'NO_MATCH':
        raise StateError('CROSS_SOURCE_DEDUP_NOT_CLEAR')
    if require_admission:
        from kemdiktisaintek_delivery_state import event_key
        admission = state['admissions'].get(event_key(identifier))
        if (not admission or fingerprint(item) != admission['item_sha256']
                or admission['baseline_sha256'] != monitor['baseline']['sha256']
                or admission['reference_sha256'] != reference_sha
                or not timestamp(admission['observed_at']) <= now < timestamp(admission['expires_at'])):
            raise StateError('NO_FRESH_EVENT_ADMISSION')
    return item
