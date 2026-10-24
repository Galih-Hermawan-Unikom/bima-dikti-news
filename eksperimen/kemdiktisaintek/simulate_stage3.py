"""Offline API -> parser -> monitor -> admission -> real CAS adapter/mock HTTP -> receipt."""
import json

from stage3_fixtures import setup
from kemdiktisaintek_delivery import observe
from kemdiktisaintek_monitor import capture_window
from kemdiktisaintek_state import StateError


def _reserve(c, p):
    return c['engine'].reserve_initial(c['source'].new_id, p, reference=c['reference'], reference_sha=c['reference_sha'])


def _send(c, key, p):
    return c['engine'].send(key, p, reference=c['reference'], reference_sha=c['reference_sha'])


def run_simulation():
    cases = []
    c = setup(); p = c['gateway'].inspect(at=c['clock']()); key = _reserve(c, p)
    def verify_claim():
        assert c['remote'].load()[0]['outbox'][key]['status'] == 'SENDING'
    c['telegram'].before_post = verify_claim
    delivered = _send(c, key, p)
    assert delivered['status'] == 'DELIVERED' and len(c['telegram'].posts) == 1
    cases.append({'case': 'A', 'scenario': 'new_relevant', 'status': 'DELIVERED', 'mock_posts': 1,
                  'receipt': delivered['receipt'], 'durable_claim_before_post': True})
    preview = c['telegram'].posts[0]['text']
    state, _ = c['remote'].load()
    observe(c['remote'], capture_window(c['source'], state['observation_state'], at='2026-10-24T10:12:00Z'),
            reference=c['reference'], reference_sha=c['reference_sha'])
    c['clock'].value = '2026-10-24T10:13:00Z'
    p = c['gateway'].inspect(at=c['clock']())
    assert _send(c, key, p)['post_attempted'] is False and len(c['telegram'].posts) == 1
    cases.append({'case': 'B', 'scenario': 'same_article_next_poll', 'status': 'DELIVERED', 'additional_posts': 0})
    for label, option, status in [('C', 'historical', 'HOLD'), ('D', 'irrelevant', 'REJECTED')]:
        c = setup(**{option: True}); p = c['gateway'].inspect(at=c['clock']())
        try:
            _reserve(c, p)
            raise AssertionError('Ineligible reservation unexpectedly allowed')
        except StateError:
            pass
        actual = c['remote'].load()[0]['observation_state']['observations'][c['source'].new_id]['detection_status']
        assert actual == status and not c['telegram'].posts
        cases.append({'case': label, 'scenario': option, 'status': actual, 'mock_posts': 0})
    c = setup(); c['source'].revised = True; state, _ = c['remote'].load()
    observe(c['remote'], capture_window(c['source'], state['observation_state'], at='2026-10-24T10:12:00Z'),
            reference=c['reference'], reference_sha=c['reference_sha'])
    revised = c['remote'].load()[0]['observation_state']
    assert revised['revisions'] and revised['observations'][c['source'].known_id]['detection_status'] == 'NEEDS_REVIEW'
    c['clock'].value = '2026-10-24T10:13:00Z'
    try:
        c['engine'].reserve_initial(c['source'].known_id, c['gateway'].inspect(at=c['clock']()),
            reference=c['reference'], reference_sha=c['reference_sha'])
        raise AssertionError('Revision unexpectedly admitted')
    except StateError:
        pass
    assert not c['telegram'].posts
    cases.append({'case': 'E', 'scenario': 'revision', 'status': 'NEEDS_REVIEW', 'mock_posts': 0})
    c = setup(); c['telegram'].outcome = 'timeout'; p = c['gateway'].inspect(at=c['clock']()); key = _reserve(c, p)
    assert _send(c, key, p)['status'] == 'AMBIGUOUS'
    assert _send(c, key, p)['post_attempted'] is False and len(c['telegram'].posts) == 1
    cases.append({'case': 'F', 'scenario': 'telegram_timeout', 'status': 'AMBIGUOUS', 'mock_posts': 1, 'retries': 0})
    c = setup(); p = c['gateway'].inspect(at=c['clock']()); key = _reserve(c, p)
    c['github'].fail_put = c['github'].put_count + 1
    try:
        _send(c, key, p)
        raise AssertionError('CAS failure unexpectedly ignored')
    except StateError:
        pass
    assert not c['telegram'].posts
    cases.append({'case': 'G', 'scenario': 'claim_cas_conflict', 'status': 'BLOCKED', 'mock_posts': 0})
    c = setup(); c['telegram'].target = '-1008888888888'
    try:
        c['gateway'].inspect(at=c['clock']())
        raise AssertionError('Wrong destination unexpectedly allowed')
    except RuntimeError:
        pass
    assert not c['telegram'].posts
    cases.append({'case': 'H', 'scenario': 'wrong_destination', 'status': 'BLOCKED', 'mock_posts': 0})
    return {'simulation': True, 'live_http_requests': 0, 'live_telegram_posts': 0,
            'fixture_origin': 'synthetic transport responses parsed by Stage 1 API/HTML parsers',
            'cases': cases, 'message_preview_html': preview}


if __name__ == '__main__':
    print(json.dumps(run_simulation(), ensure_ascii=True, indent=2))
