"""Bounded capture and transactional observation-only monitor. No sender/outbox planning."""
from copy import deepcopy
from dataclasses import dataclass
from datetime import timedelta
import json
from urllib.parse import urlencode

from kemdiktisaintek_parser import BASE, parse_api_list, parse_detail, normalize_url, _dedupe
from kemdiktisaintek_policy import POLICY_VERSION, classify, cross_source_dedup, normalized
from kemdiktisaintek_state import (SOURCE, StateError, StateConflict, fingerprint,
                                  timestamp, uid, validate_state)

MAX_POLL_GAP = timedelta(minutes=30)
NEW_WINDOW = timedelta(hours=24)


class CaptureError(RuntimeError):
    pass


@dataclass
class Capture:
    observed_at: str
    items: list
    coverage: dict
    duplicate_ids: list
    healthy: bool = True


def capture_window(client, state=None, *, at, pages=2, detail_budget=10):
    """Reuse Stage 1 API/detail parsers. All requested pages/details must succeed.

    At most 3*10 listings + 10 details. No category or search filters. Known
    articles receive up to three rotating detail checks, even amid new arrivals.
    """
    timestamp(at)
    if type(pages) is not int or not 1 <= pages <= 3 or type(detail_budget) is not int or not 1 <= detail_budget <= 10:
        raise CaptureError('INVALID_CAPTURE_BUDGET')
    records, all_items, total, seen, duplicates = [], [], None, {}, []
    for number in range(1, pages + 1):
        url = BASE + '/api/announcement?' + urlencode({'page': number, 'size': 10, 'search': ''})
        body = client.fetch(url)
        parsed = parse_api_list(body, url)
        raw = json.loads(body)
        if total is not None and parsed['total'] != total:
            raise CaptureError('TOTAL_CHANGED_DURING_CAPTURE')
        total = parsed['total']
        expected = max(0, min(10, total - (number - 1) * 10))
        if len(raw['data']) != expected:
            raise CaptureError('PARTIAL_API_PAGE')
        for row in raw['data']:
            key = row.get('id') or row.get('slug')
            if key in seen:
                duplicates.append(key)
            seen[key] = True
        records.append({'page': number, 'url': url, 'count': len(raw['data']),
                        'total': total, 'method': 'public_json_api', 'healthy': True})
        all_items.extend(parsed['items'])
        if not parsed['next_url']:
            break
    items = _dedupe(all_items)
    observations = state['observations'] if state else {}
    unknown = [i for i, row in enumerate(items) if row['id'] not in observations]
    known = [i for i, row in enumerate(items) if row['id'] in observations]
    known.sort(key=lambda i: observations[items[i]['id']].get('detail_checked_at') or '')
    reserved_known = min(3, len(known), detail_budget)
    selected = unknown[:detail_budget - reserved_known] + known[:reserved_known]
    if len(selected) < detail_budget:
        selected.extend(i for i in known[reserved_known:] if i not in selected)
        selected = selected[:detail_budget]
    for index in selected:
        listing = items[index]
        items[index] = parse_detail(client.fetch(listing['url']), listing['url'], listing)
    coverage = {'requested_pages': pages, 'captured_pages': len(records), 'pages': records,
                'total_reported': total, 'listing_count': len(items), 'detail_count': len(selected),
                'detail_budget': detail_budget, 'archive_complete': False,
                'all_requested_pages_healthy': True, 'unfiltered': True}
    return Capture(at, items, coverage, sorted(set(duplicates)))


def validate_capture(capture):
    if not isinstance(capture, Capture) or capture.healthy is not True:
        raise CaptureError('UNHEALTHY_OR_PARTIAL_CAPTURE')
    timestamp(capture.observed_at)
    coverage = capture.coverage
    records = coverage.get('pages', [])
    if (coverage.get('all_requested_pages_healthy') is not True or coverage.get('unfiltered') is not True
            or coverage.get('archive_complete') is not False
            or type(coverage.get('requested_pages')) is not int or not 1 <= coverage['requested_pages'] <= 3
            or coverage.get('captured_pages') != len(records) or not records
            or len(records) > coverage['requested_pages'] or coverage.get('listing_count') != len(capture.items)):
        raise CaptureError('INVALID_CAPTURE_COVERAGE')
    total = coverage['total_reported']
    if type(total) is not int or total < 0 or len(records) != min(coverage['requested_pages'], max(1, (total + 9) // 10)):
        raise CaptureError('INCOMPLETE_CAPTURE')
    for number, row in enumerate(records, 1):
        expected_url = BASE + '/api/announcement?' + urlencode({'page': number, 'size': 10, 'search': ''})
        if (row.get('healthy') is not True or row.get('page') != number or row.get('total') != total
                or row.get('url') != expected_url or row.get('method') != 'public_json_api'
                or row.get('count') != max(0, min(10, total - (number - 1) * 10))):
            raise CaptureError('UNHEALTHY_OR_FILTERED_PAGE')
    expected_rows = sum(row['count'] for row in records)
    if len(capture.items) > expected_rows or (len(capture.items) < expected_rows and not capture.duplicate_ids):
        raise CaptureError('CAPTURE_ITEMS_MISSING')
    if (type(coverage.get('detail_budget')) is not int or not 1 <= coverage['detail_budget'] <= 10
            or coverage.get('detail_count') != sum('body_text' in row for row in capture.items)
            or coverage['detail_count'] > coverage['detail_budget']):
        raise CaptureError('INVALID_DETAIL_COVERAGE')
    # Same UUID/URL but inconsistent metadata is a failed capture, not zero new.
    _dedupe(capture.items)
    if len({row['url'] for row in capture.items}) != len(capture.items):
        raise CaptureError('CAPTURE_DUPLICATES_NOT_RECONCILED')
    for row in capture.items:
        if normalize_url(row['url'], article=True) != row['url'] or row.get('source') != SOURCE:
            raise CaptureError('INVALID_CAPTURE_ITEM')


def _snapshot(item, previous=None):
    if 'body_text' in item:
        return deepcopy(item)
    result = deepcopy(previous or {})
    # API's displayed publication timestamp is not a true modification date.
    for key in ('id', 'identity', 'identity_basis', 'slug', 'title', 'category', 'url', 'created_at'):
        result[key] = deepcopy(item.get(key))
    if not previous:
        result.update(published_at=item.get('published_at'), publication_basis=item.get('publication_basis'),
                      updated_at=None, content_sha256=None)
    elif result.get('content_sha256'):
        result['content_sha256'] = fingerprint({k: result.get(k) for k in (
            'title', 'category', 'body_text', 'attachments', 'external_links')})
    return result


def _changes(old, new):
    groups = {'metadata': ('title', 'category', 'letter_number'), 'body': ('body_text',),
              'attachments': ('attachments',), 'dates': ('published_at', 'updated_at'),
              'url': ('url', 'slug'), 'deadline': ('deadline_evidence',), 'links': ('external_links',)}
    changes = {}
    for group, keys in groups.items():
        fields = {}
        for key in keys:
            before, after = old.get(key), new.get(key)
            if key == 'category' and normalized(before) == normalized(after):
                continue
            if before != after:
                fields[key] = {'before': before, 'after': after}
        if fields:
            changes[group] = fields
    if old.get('content_sha256') and new.get('content_sha256') != old.get('content_sha256') and not changes:
        changes['fingerprint'] = {'before': old['content_sha256'], 'after': new.get('content_sha256')}
    return changes


def _hold_identity(state, item, at, reason):
    key = fingerprint({'url': item['url'], 'id': item.get('id')})
    old = state['identity_holds'].get(key)
    state['identity_holds'][key] = {'uuid': item.get('id'), 'url': item['url'], 'title': item['title'],
        'first_seen_at': old['first_seen_at'] if old else at, 'last_seen_at': at,
        'detection_status': 'HOLD', 'delivery_status': 'BLOCKED_STAGE2', 'reason': reason}
    for row in state['observations'].values():
        if item['url'] in row['url_history'] or item.get('id') == row['uuid']:
            row['detection_status'] = 'NEEDS_REVIEW'
            row['reasons'] = ['IDENTITY_RECONCILIATION_PENDING']


def _observation(item, at, *, initial=False):
    return {'uuid': item['id'], 'url': item['url'], 'title': item['title'],
        'published_at': item.get('published_at'), 'first_seen_at': at, 'last_seen_at': at,
        'content_sha256': item.get('content_sha256'), 'classification': classify(item),
        'detection_status': 'KNOWN' if initial else 'HOLD', 'delivery_status': 'BLOCKED_STAGE2',
        'initial_inventory': initial, 'reasons': ['BASELINE_INVENTORY'] if initial else [],
        'detail_checked_at': at if 'body_text' in item else None, 'last_item': deepcopy(item),
        'initial_decision': 'KNOWN' if initial else None, 'deduplication': None,
        'url_history': [item['url']]}


def initialize(store, capture):
    validate_capture(capture)
    at = capture.observed_at
    state = {'schema_version': 1, 'source': SOURCE, 'initialized': True, 'activation_at': at,
        'policy_version': POLICY_VERSION, 'generation': 1, 'last_successful_poll_at': at,
        'delivery_authorized': False, 'observations': {}, 'identity_holds': {}, 'outbox': {}, 'revisions': [],
        'baseline': {'observed_at': at, 'ids': [], 'coverage': deepcopy(capture.coverage),
                     'archive_complete': False}}
    for item in capture.items:
        if not item.get('id') or item.get('identity_basis') != 'uuid':
            _hold_identity(state, item, at, 'BASELINE_UUID_UNAVAILABLE')
            continue
        uid(item['id'])
        state['baseline']['ids'].append(item['id'])
        state['observations'][item['id']] = _observation(_snapshot(item), at, initial=True)
    state['baseline']['ids'].sort()
    state['baseline']['sha256'] = fingerprint(state['baseline'])
    validate_state(state)
    store.create(state)  # existing state is never overwritten
    return state


def _admission(item, state, at, classification, dedup, context, first_seen):
    reasons = []
    try:
        publication = timestamp(item.get('published_at'))
    except StateError:
        publication = None
    if item.get('publication_basis') != 'detail_datetime' or publication is None:
        reasons.append('UNTRUSTED_PUBLICATION_DATE')
    if publication:
        if publication <= timestamp(state['activation_at']):
            reasons.append('AT_OR_BEFORE_ACTIVATION_FENCE')
        if publication > timestamp(at):
            reasons.append('FUTURE_PUBLICATION')
        if timestamp(at) - publication > NEW_WINDOW:
            reasons.append('HISTORICAL_OUTSIDE_NEW_WINDOW')
    if timestamp(at) - timestamp(first_seen) > NEW_WINDOW:
        reasons.append('FIRST_SEEN_TTL_EXPIRED')
    if not item.get('body_text') or not item.get('content_sha256'):
        reasons.append('DETAIL_EVIDENCE_UNAVAILABLE')
    reasons.extend(context)
    if reasons:
        return 'HOLD', reasons
    if dedup['status'] == 'DUPLICATE':
        return 'REJECTED', ['CONFIRMED_CROSS_SOURCE_DUPLICATE']
    if dedup['status'] == 'NEEDS_REVIEW':
        if dedup.get('reason') == 'BIMA_REFERENCE_MISSING':
            return 'HOLD', ['BIMA_REFERENCE_UNAVAILABLE']
        return 'NEEDS_REVIEW', ['CROSS_SOURCE_EVIDENCE_REQUIRES_REVIEW']
    if classification['decision'] == 'NOT_RELEVANT':
        return 'REJECTED', ['NOT_RELEVANT_TO_AUDIENCE']
    if classification['decision'] != 'RELEVANT':
        return 'NEEDS_REVIEW', ['RELEVANCE_UNCERTAIN']
    return 'NEW_CANDIDATE', ['POLICY_COMPLETE_OBSERVATION_ONLY']


def apply_poll(previous, capture, *, bima_history=None, max_poll_gap=MAX_POLL_GAP):
    """Pure transition. Caller must checkpoint before publishing a successful report."""
    validate_state(previous)
    validate_capture(capture)
    if not isinstance(max_poll_gap, timedelta) or not timedelta(0) < max_poll_gap <= timedelta(minutes=90):
        raise CaptureError('INVALID_POLL_GAP_POLICY')
    at = capture.observed_at
    if timestamp(at) <= timestamp(previous['last_successful_poll_at']):
        raise CaptureError('NON_MONOTONIC_POLL')
    state = deepcopy(previous)
    context = []
    if timestamp(at) - timestamp(previous['last_successful_poll_at']) > max_poll_gap:
        context.append('UNHEALTHY_POLL_GAP')
    visible = {i.get('id') for i in capture.items if i.get('id')}
    if previous['observations'] and not visible.intersection(previous['observations']):
        context.append('WINDOW_OVERLAP_LOST')
    if capture.duplicate_ids:
        context.append('PAGINATION_SHIFT_DUPLICATES')
    known_urls = {url: key for key, row in previous['observations'].items() for url in row['url_history']}
    events = []
    for item in capture.items:
        identifier = item.get('id')
        if not identifier or item.get('identity_basis') != 'uuid':
            _hold_identity(state, item, at, 'UUID_FALLBACK_RECONCILIATION_REQUIRED')
            events.append({'uuid': identifier, 'url': item['url'], 'status': 'HOLD', 'reason': 'UUID_FALLBACK'})
            continue
        uid(identifier)
        collision = known_urls.get(item['url'])
        if collision and collision != identifier:
            _hold_identity(state, item, at, 'UUID_URL_CONFLICT')
            if collision in state['observations']:
                state['observations'][collision]['detection_status'] = 'NEEDS_REVIEW'
                state['observations'][collision]['reasons'] = ['UUID_URL_CONFLICT']
            events.append({'uuid': identifier, 'status': 'HOLD', 'reason': 'UUID_URL_CONFLICT'})
            continue
        old = state['observations'].get(identifier)
        identity_pending = any(h['url'] == item['url'] or h.get('uuid') == identifier
                               for h in state['identity_holds'].values())
        latest = _snapshot(item, old['last_item'] if old else None)
        classification = classify(latest)
        dedup = cross_source_dedup(latest, bima_history)
        row = deepcopy(old) if old else _observation(latest, at)
        changes = _changes(old['last_item'], latest) if old else {}
        # Hydrating a baseline/list-only record is first evidence, not a revision.
        if old and not old['content_sha256']:
            changes = {k: v for k, v in changes.items() if k in {'metadata', 'url', 'dates'}}
            changes.pop('dates', None)
        if old and changes:
            revision = {'uuid': identifier, 'event_type': 'REVISION_CANDIDATE', 'status': 'NEEDS_REVIEW',
                'observed_at': at, 'changes': changes, 'before': fingerprint(old['last_item']),
                'after': fingerprint(latest), 'important': bool(set(changes) & {'attachments', 'deadline', 'dates', 'url'})}
            revision['revision_id'] = fingerprint({k: revision[k] for k in ('uuid', 'before', 'after')})
            if revision['revision_id'] not in {r['revision_id'] for r in state['revisions']}:
                state['revisions'].append(revision)
            status, reasons = 'NEEDS_REVIEW', ['REVISION_CANDIDATE']
        elif identity_pending:
            status, reasons = 'NEEDS_REVIEW', ['IDENTITY_RECONCILIATION_PENDING']
        elif (old and old['detection_status'] == 'NEEDS_REVIEW'
              and old.get('reasons') != ['BIMA_REFERENCE_COVERAGE_INSUFFICIENT']):
            status, reasons = old['detection_status'], old['reasons']
        elif old and (old['initial_inventory'] or old['detection_status'] in {'NEW_CANDIDATE', 'KNOWN', 'REJECTED'}):
            status, reasons = 'KNOWN', ['PREVIOUSLY_OBSERVED']
        else:
            status, reasons = _admission(latest, state, at, classification, dedup,
                                         context, row['first_seen_at'])
        row.update(url=latest['url'], title=latest['title'], published_at=latest.get('published_at'),
            last_seen_at=at, content_sha256=latest.get('content_sha256'), classification=classification,
            detection_status=status, reasons=reasons, deduplication=dedup, last_item=latest)
        if 'body_text' in item:
            row['detail_checked_at'] = at
        if latest['url'] not in row['url_history']:
            row['url_history'].append(latest['url'])
        if row['initial_decision'] is None:
            row['initial_decision'] = status
        state['observations'][identifier] = row
        events.append({'uuid': identifier, 'status': status, 'reasons': reasons,
                       'classification': classification['decision'], 'dedup_status': dedup['status']})
    state.update(last_successful_poll_at=at, generation=previous['generation'] + 1,
                 last_capture_coverage=deepcopy(capture.coverage),
                 bima_reference_sha256=fingerprint(bima_history) if bima_history is not None else None)
    validate_state(state)
    return state, {'source': SOURCE, 'delivery_authorized': False, 'telegram_calls': 0,
                   'events': events, 'capture_warnings': context, 'revision_count': len(state['revisions'])}


def poll(store, client, *, at, bima_history=None, pages=2, detail_budget=10):
    previous, expected = store.load()  # Missing/corrupt state fails before network.
    capture = capture_window(client, previous, at=at, pages=pages, detail_budget=detail_budget)
    state, report = apply_poll(previous, capture, bima_history=bima_history)
    store.commit(state, expected)  # No success report escapes a failed checkpoint.
    return report
