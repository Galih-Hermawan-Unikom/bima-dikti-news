"""Single-run capture, BIMA comparison, observation, and optional durable delivery."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from kemdiktisaintek_delivery import Delivery, ReconciliationRequired, observe
from kemdiktisaintek_delivery_policy import TIMING
from kemdiktisaintek_parser import AnnouncementClient
from kemdiktisaintek_monitor import capture_window
from kemdiktisaintek_state import StateError, timestamp
from kemdiktisaintek_telegram import Credentials, TelegramGateway
from kemdiktisaintek_bima_reference import capture_live

WIB = ZoneInfo("Asia/Jakarta")


def _counts(events):
    keys = ("NEW_CANDIDATE", "KNOWN", "HOLD", "REJECTED", "NEEDS_REVIEW")
    counts = {key: sum(e.get("status") == key for e in events) for key in keys}
    counts["DUPLICATE"] = sum(e.get("dedup_status") == "DUPLICATE" for e in events)
    return counts


def run_poll(env, *, backend, clock=lambda: datetime.now(timezone.utc).isoformat(),
             source=None, bima_collector=None, gateway=None):
    """Observe and, when both exact switches are on, immediately deliver fresh admissions."""
    prior, _ = backend.load()
    prior_poll = timestamp(prior['observation_state']['last_successful_poll_at'])
    delivery_probe = Delivery(backend, gateway or object(), environment=env, clock=clock)
    recovered = delivery_probe.recover()
    prior, _ = backend.load()
    reference = None
    reference_sha = None
    bima_status = "UNAVAILABLE"
    try:
        reference, reference_sha = capture_live(env=env, clock=clock, collector=bima_collector)
        bima_status = reference['pagination_state']
    except StateError as exc:
        # Never turn missing, stale, empty, or structurally unknown BIMA data into NO_MATCH.
        bima_status = str(exc)
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
                "bima_reference_status": bima_status, "capture_error": type(exc).__name__,
                "recovered_sending_attempts": recovered, "delivery_enabled": False,
                "delivery_authorized": False}
    finally:
        if source is None:
            client.session.close()
    report = observe(backend, capture, reference=reference, reference_sha=reference_sha,
                     reference_env=env)
    events = report['events']
    delivery_counts = {"reserved": 0, "delivered": 0, "ambiguous": 0}
    switches_on = (env.get('KEMDIKTISAINTEK_EXPERIMENT_ENABLED') == 'true'
                   and env.get('KEMDIKTISAINTEK_EXPERIMENT_AUTO_SEND') == 'true')
    if switches_on and reference is not None:
        candidates = [e['uuid'] for e in events if e.get('status') == 'NEW_CANDIDATE' and e.get('uuid')]
        if candidates:
            telegram = gateway or TelegramGateway(Credentials.from_environment(env))
            engine = Delivery(backend, telegram, environment=env, clock=clock)
            try:
                for identifier in candidates:
                    proof = telegram.inspect(at=clock())
                    key = engine.reserve_initial(identifier, proof, reference=reference,
                        reference_sha=reference_sha, reference_env=env)
                    state, _ = backend.load()
                    if state['outbox'][key]['status'] == 'RESERVED':
                        delivery_counts['reserved'] += 1
                    result = engine.send(key, proof, reference=reference,
                        reference_sha=reference_sha, reference_env=env)
                    if result['status'] == 'DELIVERED' and result.get('post_attempted'):
                        delivery_counts['delivered'] += 1
                    elif result['status'] == 'AMBIGUOUS':
                        delivery_counts['ambiguous'] += 1
                        break
            except ReconciliationRequired:
                delivery_counts['ambiguous'] += 1
                bima_status += "; RECEIPT_RECONCILIATION_REQUIRED"
            except Exception as exc:
                # Error details may contain request URLs or credentials.
                safe = getattr(exc, 'code', None)
                if not isinstance(safe, str):
                    safe = str(exc) if isinstance(exc, StateError) else type(exc).__name__
                bima_status += "; DELIVERY_BLOCKED:" + safe
            finally:
                if gateway is None:
                    telegram.session.close()
    gap = timestamp(now_at) - prior_poll
    local = timestamp(now_at).astimezone(WIB)
    summary = {"source": "kemdiktisaintek", "observed_at_utc": timestamp(now_at).isoformat(),
        "observed_at_wib": local.isoformat(), "pages": capture.coverage['captured_pages'],
        "articles_observed": capture.coverage['listing_count'], "details_checked": capture.coverage['detail_count'],
        "status_counts": _counts(events), "admissions_total": report['admissions'],
        "reserved_this_poll": delivery_counts['reserved'], "delivered_this_poll": delivery_counts['delivered'],
        "ambiguous_this_poll": delivery_counts['ambiguous'], "api_health": "HEALTHY",
        "poll_gap_seconds": int(gap.total_seconds()), "planned_interval_seconds": int(TIMING.planned_interval.total_seconds()),
        "lateness_tolerance_seconds": int(TIMING.lateness_tolerance.total_seconds()),
        "bima_reference_status": bima_status, "recovered_sending_attempts": recovered,
        "capture_warnings": report.get('capture_warnings', []), "reference_error": report.get('reference_error'),
        "delivery_enabled": switches_on, "delivery_authorized": bool(delivery_counts['delivered'])}
    return summary
