"""Single-source capture, observation, and optional durable production delivery."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from kemdiktisaintek_prod_delivery import Delivery, ReconciliationRequired, observe
from kemdiktisaintek_prod_delivery_policy import TIMING
from kemdiktisaintek_prod_parser import AnnouncementClient
from kemdiktisaintek_prod_monitor import capture_window
from kemdiktisaintek_prod_state import StateError, timestamp
from kemdiktisaintek_prod_telegram import Credentials, TelegramGateway

WIB = ZoneInfo("Asia/Jakarta")


def _counts(events):
    keys = ("NEW_CANDIDATE", "KNOWN", "HOLD", "REJECTED", "NEEDS_REVIEW")
    counts = {key: sum(e.get("status") == key for e in events) for key in keys}
    counts["DUPLICATE"] = 0  # No cross-source matching: sources are independent.
    return counts


def run_poll(env, *, backend, clock=lambda: datetime.now(timezone.utc).isoformat(),
             source=None, gateway=None):
    """Capture Kemdiktisaintek only; admission and delivery are source-local."""
    prior, _ = backend.load()
    prior_poll = timestamp(prior['observation_state']['last_successful_poll_at'])
    delivery_probe = Delivery(backend, gateway or object(), environment=env, clock=clock)
    recovered = delivery_probe.recover()
    prior, _ = backend.load()
    now_at = timestamp(clock()).isoformat()
    client = source or AnnouncementClient()
    try:
        try:
            capture = capture_window(client, prior['observation_state'], at=now_at, pages=2, detail_budget=10)
        except Exception as exc:
            observed = timestamp(now_at)
            return {"source": "kemdiktisaintek", "observed_at_utc": observed.isoformat(),
                "observed_at_wib": observed.astimezone(WIB).isoformat(), "pages": 0,
                "articles_observed": 0, "details_checked": 0, "status_counts": _counts([]),
                "admissions_total": len(prior['admissions']), "reserved_this_poll": 0,
                "delivered_this_poll": 0, "ambiguous_this_poll": 0, "api_health": "FAILED",
                "poll_gap_seconds": int((observed-prior_poll).total_seconds()),
                "capture_error": type(exc).__name__,
                "recovered_sending_attempts": recovered, "delivery_enabled": False,
                "delivery_authorized": False}
    finally:
        if source is None:
            client.session.close()
    report = observe(backend, capture)
    events = report['events']
    delivery_counts = {"reserved": 0, "delivered": 0, "ambiguous": 0}
    delivery_error = None
    switches_on = (env.get('KEMDIKTISAINTEK_PRODUCTION_ENABLED') == 'true'
                   and env.get('KEMDIKTISAINTEK_PRODUCTION_AUTO_SEND') == 'true')
    if switches_on:
        candidates = [e['uuid'] for e in events if e.get('status') == 'NEW_CANDIDATE' and e.get('uuid')]
        if candidates:
            telegram = gateway or TelegramGateway(Credentials.from_environment(env))
            engine = Delivery(backend, telegram, environment=env, clock=clock)
            try:
                for identifier in candidates:
                    proof = telegram.inspect(at=clock())
                    key = engine.reserve_initial(identifier, proof)
                    state, _ = backend.load()
                    if state['outbox'][key]['status'] == 'RESERVED':
                        delivery_counts['reserved'] += 1
                    result = engine.send(key, proof)
                    if result['status'] == 'DELIVERED' and result.get('post_attempted'):
                        delivery_counts['delivered'] += 1
                    elif result['status'] == 'AMBIGUOUS':
                        delivery_counts['ambiguous'] += 1
                        break
            except ReconciliationRequired:
                delivery_counts['ambiguous'] += 1
                delivery_error = 'RECEIPT_RECONCILIATION_REQUIRED'
            except Exception as exc:
                # Error details may contain request URLs or credentials.
                safe = getattr(exc, 'code', None)
                if not isinstance(safe, str):
                    safe = str(exc) if isinstance(exc, StateError) else type(exc).__name__
                delivery_error = safe
            finally:
                if gateway is None:
                    telegram.session.close()
    gap = timestamp(now_at) - prior_poll
    local = timestamp(now_at).astimezone(WIB)
    captured = {item.get('id'): item for item in capture.items}
    attention = []
    for event in events:
        if event.get('status') not in {'HOLD', 'NEEDS_REVIEW'}:
            continue
        item = captured.get(event.get('uuid'), {})
        attention.append({"uuid": event.get('uuid'), "status": event.get('status'),
            "title": item.get('title'), "url": item.get('url'),
            "reasons": event.get('reasons', []), "classification": event.get('classification')})
    summary = {"source": "kemdiktisaintek", "observed_at_utc": timestamp(now_at).isoformat(),
        "observed_at_wib": local.isoformat(), "pages": capture.coverage['captured_pages'],
        "articles_observed": capture.coverage['listing_count'], "details_checked": capture.coverage['detail_count'],
        "status_counts": _counts(events), "admissions_total": report['admissions'],
        "reserved_this_poll": delivery_counts['reserved'], "delivered_this_poll": delivery_counts['delivered'],
        "ambiguous_this_poll": delivery_counts['ambiguous'], "api_health": "HEALTHY",
        "poll_gap_seconds": int(gap.total_seconds()), "planned_interval_seconds": int(TIMING.planned_interval.total_seconds()),
        "lateness_tolerance_seconds": int(TIMING.lateness_tolerance.total_seconds()),
        "recovered_sending_attempts": recovered,
        "capture_warnings": report.get('capture_warnings', []), "reference_error": report.get('reference_error'),
        "hold_needs_review": attention,
        "delivery_enabled": switches_on, "delivery_authorized": bool(delivery_counts['delivered']),
        "delivery_error": delivery_error}
    return summary
