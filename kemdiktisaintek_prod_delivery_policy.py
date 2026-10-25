"""Source-local Kemdiktisaintek evidence and timing budgets for delivery."""
from dataclasses import dataclass
from datetime import timedelta

from kemdiktisaintek_prod_state import StateError, fingerprint, timestamp
from kemdiktisaintek_prod_policy import classify


@dataclass(frozen=True)
class Timing:
    planned_interval: timedelta = timedelta(minutes=30)
    lateness_tolerance: timedelta = timedelta(minutes=20)
    evidence_freshness: timedelta = timedelta(minutes=10)
    publication_window: timedelta = timedelta(hours=24)
    first_seen_ttl: timedelta = timedelta(hours=24)

    @property
    def max_gap(self):
        return self.planned_interval + self.lateness_tolerance


TIMING = Timing()


def eligibility(state, identifier, *, at, require_admission=True):
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
    if require_admission:
        from kemdiktisaintek_prod_delivery_state import event_key
        admission = state['admissions'].get(event_key(identifier))
        if (not admission or fingerprint(item) != admission['item_sha256']
                or admission['baseline_sha256'] != monitor['baseline']['sha256']
                or admission.get('admission_policy_version') != 'kemdiktisaintek-delivery-v2-independent-sources'
                or not timestamp(admission['observed_at']) <= now < timestamp(admission['expires_at'])):
            raise StateError('NO_FRESH_EVENT_ADMISSION')
    return item
