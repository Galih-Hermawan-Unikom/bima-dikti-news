"""Detection is persisted only after durable retry records exist."""

from copy import deepcopy
from datetime import timedelta, timezone, datetime
import json
import os
from pathlib import Path

from notifier import Notifier
from reliability import RunResult, SourceError, SourceResult, atomic_json, read_json
from scraper import (BimaIdentityError, BimaScraper, announcement_identity,
                     match_announcement, new_announcements)
from youtube_history import DEFAULT_CHANNEL, youtube_identity, validate_baseline_marker


def _youtube_policy(config, channel):
    from youtube_eligibility import Policy
    from youtube_history import parse_timestamp
    def duration(key):
        value = config.get(key)
        return timedelta(seconds=value) if type(value) is int and value > 0 else None
    activation = parse_timestamp(config.get("youtube_activation_at"))
    return Policy(channel, config.get("youtube_policy_version", "tabs-only-v1"),
        activation_at=activation, window=duration("youtube_window_seconds"),
        ttl=duration("youtube_ttl_seconds"), skew=timedelta(seconds=config["youtube_skew_seconds"])
        if type(config.get("youtube_skew_seconds")) is int and config["youtube_skew_seconds"] >= 0 else None,
        capture_interval=duration("youtube_capture_interval_seconds"),
        approved=config.get("youtube_policy_approved", False),
        automatic_tabs_verification=config.get("youtube_automatic_tabs_verification_enabled", False))


def _youtube_sender(notifier):
    from youtube_delivery import SendConfirmation
    def send(item, ticket):
        sent = notifier.send_youtube_outbox_notification(item, ticket)
        receipts = getattr(notifier, "last_delivery_receipts", ())
        if sent is True and len(receipts) == 1:
            receipt = receipts[0]
            return SendConfirmation(receipt.get("http_status"), receipt.get("ok"), receipt.get("message_id"))
        return None
    return send


def _youtube_review_inputs(config):
    raw = config.get("youtube_review_evidence_json", "")
    key = config.get("youtube_review_hmac_key", "")
    hosts = config.get("youtube_review_source_hosts", [])
    if not raw:
        return {}, None
    if not raw or not key or len(key.encode("utf-8")) < 32 or not hosts:
        raise ValueError("Signed YouTube review authority is incomplete")
    if isinstance(hosts, str):
        hosts = [host.strip().lower() for host in hosts.split(",") if host.strip()]
    if not isinstance(hosts, (list, tuple)) or any(not isinstance(host, str) for host in hosts):
        raise ValueError("Invalid YouTube review source hosts")
    from youtube_review_evidence import SignedReviewVerifier
    document = json.loads(raw)
    envelopes = document.get("reviews") if isinstance(document, dict) else None
    if not isinstance(envelopes, list):
        raise ValueError("Invalid YouTube review evidence document")
    reviews = {}
    for envelope in envelopes:
        payload = envelope.get("payload") if isinstance(envelope, dict) else None
        video_id = payload.get("video_id") if isinstance(payload, dict) else None
        if not isinstance(video_id, str) or video_id in reviews:
            raise ValueError("Duplicate or invalid signed YouTube review")
        reviews[video_id] = envelope
    key_id = config.get("youtube_review_key_id", "operator")
    if not isinstance(key_id, str) or not key_id:
        raise ValueError("Invalid YouTube review key identifier")
    return reviews, SignedReviewVerifier({key_id: key.encode("utf-8")}, frozenset(hosts))


def item_key(item):
    if item.get("source") == "youtube":
        return "youtube:" + youtube_identity(item)
    return "bima:" + announcement_identity(item)


def event_key(item):
    return item_key(item) + ":initial"


def stable_item(item):
    result = deepcopy(item)
    # Signed attachment URLs expire; refresh from the source on every retry.
    result["documents"] = [{"name": d.get("name", "") if isinstance(d, dict) else str(d)}
                           for d in item.get("documents", [])]
    return result


def load_delivery(path):
    state = read_json(path, {"version": 1, "batches": []})
    return validate_delivery(state)


def validate_delivery(state):
    """Validate supplied history without reading or writing a state file."""
    if not isinstance(state, dict) or state.get("version") != 1 or not isinstance(state.get("batches"), list):
        raise ValueError("Invalid delivery state; restore verified backup")
    validate_baseline_marker(state.get("youtube_rss_cutover"))
    for batch in state["batches"]:
        if not isinstance(batch, dict) or not all(k in batch for k in
                ("event_keys", "items", "chunks", "sent_chunks", "sent_documents", "delivered", "send_files", "attempts")):
            raise ValueError("Invalid delivery batch")
        if (not isinstance(batch["delivered"], bool) or not isinstance(batch["send_files"], bool)
                or not isinstance(batch["items"], list) or not isinstance(batch["chunks"], list)
                or not isinstance(batch["event_keys"], list) or not isinstance(batch["sent_chunks"], list)
                or not isinstance(batch["sent_documents"], list)
                or not isinstance(batch["attempts"], int)
                or any(not isinstance(k, str) for k in batch["event_keys"])
                or any(not isinstance(k, str) for k in batch["sent_documents"])
                or any(not isinstance(c, str) for c in batch["chunks"])
                or any(not isinstance(i, dict) for i in batch["items"])
                or any(not isinstance(i, int) or not 1 <= i <= len(batch["chunks"]) for i in batch["sent_chunks"])):
            raise ValueError("Invalid delivery progress")
    return state


def run_once(config, *, dry_run=False, state_dir=".", scraper=None, notifier=None,
             youtube_adapter=None, youtube_send_limit=None, youtube_experiment_only=False):
    result = RunResult()
    enabled = config.get("youtube_monitor_enabled", True)
    if type(enabled) is not bool or "youtube_eligibility_dry_run" in config:
        result.errors.append("Use tabs-only YouTube with a boolean youtube_monitor_enabled; hybrid selection was removed")
        return result
    observation_enabled = config.get("youtube_observation_enabled", False)
    delivery_enabled = config.get("youtube_delivery_enabled", False)
    send_enabled = config.get("youtube_delivery_send_enabled", False)
    state_initialize = config.get("youtube_state_initialize", False)
    if any(type(v) is not bool for v in (observation_enabled, delivery_enabled, send_enabled, state_initialize)):
        result.errors.append("YouTube persistent feature flags must be booleans")
        observation_enabled = delivery_enabled = send_enabled = False
        state_initialize = False
    if delivery_enabled and not observation_enabled:
        result.errors.append("YouTube outbox planning requires persistent observation")
        delivery_enabled = send_enabled = False
    if send_enabled and not delivery_enabled:
        result.errors.append("YouTube sending requires outbox planning")
        send_enabled = False
    if state_initialize and (delivery_enabled or send_enabled):
        result.errors.append("YouTube state initialization requires delivery and sending disabled")
        delivery_enabled = send_enabled = False
    evaluate_youtube = enabled and (dry_run or observation_enabled)
    root = Path(state_dir)
    scraper = scraper or BimaScraper()
    scraper.data_file = str(root / "announcements_cache.json")
    notifier = notifier or Notifier(config)
    if youtube_send_limit is not None and (type(youtube_send_limit) is not int or youtube_send_limit < 1):
        result.errors.append("Invalid YouTube send limit")
        return result
    if youtube_experiment_only and (dry_run or youtube_send_limit != 1 or not observation_enabled):
        result.errors.append("Experiment-only mode requires observation and a one-item limit")
        return result
    path = root / "delivery_state.json"
    try:
        state = load_delivery(path)
    except Exception as exc:
        result.errors.append("delivery state: " + type(exc).__name__)
        return result
    sources = [("bima", scraper, scraper.check_new_announcements)]
    if evaluate_youtube:
        from youtube_tabs_monitor import ChannelTabsAdapter
        from youtube_delivery import YouTubeStateStore
        try:
            channel = config.get("youtube_channel_id") or DEFAULT_CHANNEL
            reviews, review_verifier = _youtube_review_inputs(config)
            adapter = youtube_adapter or ChannelTabsAdapter(channel_id=channel,
                policy=_youtube_policy(config, channel), reviews=reviews,
                review_verifier=review_verifier)
            if youtube_adapter is not None and reviews:
                adapter.reviews = reviews
                adapter.review_verifier = review_verifier
            if adapter.policy.channel_id != channel:
                raise ValueError("Channel-tabs does not match runner configuration")
            store = getattr(adapter, "observation_store", None)
            if observation_enabled and store is None:
                db_path = root / "youtube_delivery_state.json"
                if os.environ.get("GITHUB_ACTIONS", "").lower() == "true":
                    from youtube_state_backend import GitHubStateBranch
                    backend = GitHubStateBranch(
                        os.environ.get("GITHUB_REPOSITORY", ""),
                        config.get("youtube_state_branch", "youtube-state"),
                        ".bot-state/youtube_delivery_state.json",
                        config.get("youtube_state_token", ""), initialize=state_initialize,
                    )
                    backend.load(db_path)
                    store = YouTubeStateStore(db_path, activation_at=adapter.policy.activation_at,
                        policy_version=adapter.policy.version)
                    store.set_commit_hook(backend.persist)
                    store.flush()
                else:
                    store = YouTubeStateStore(db_path, activation_at=adapter.policy.activation_at,
                        policy_version=adapter.policy.version)
                adapter.observation_store = store
            elif observation_enabled and os.environ.get("GITHUB_ACTIONS", "").lower() == "true":
                if getattr(store, "_commit_hook", None) is None:
                    raise ValueError("GitHub Actions requires durable YouTube state backend")
            if delivery_enabled:
                if store is None:
                    raise ValueError("Persistent observation store unavailable")
                store.recover_expired_sends(now=adapter.clock())
            evaluation = adapter.evaluate(read_json(root / "youtube_cache.json", None), state)
            result.youtube_eligibility = evaluation.report()
            outbox = []
            destination_allowed = (not send_enabled or
                getattr(notifier, "validate_youtube_experiment_destination", lambda: False)() is True)
            if delivery_enabled and not dry_run and not evaluation.errors:
                decisions = {d.video_id: d for d in evaluation.decisions}
                scheduled = 0
                for item in evaluation.preview_items:
                    decision = decisions.get(item.get("video_id"))
                    if decision is None or decision.eligibility.value != "ELIGIBLE":
                        continue
                    if youtube_send_limit is not None and scheduled >= youtube_send_limit:
                        outbox.append({"event_key": decision.event_key, "status": "held_send_limit"})
                        continue
                    scheduled += 1
                    if len(notifier._build_summary_chunks([item])) != 1:
                        outbox.append({"event_key": decision.event_key, "status": "held_single_message_limit"})
                        continue
                    reservation = store.reserve(decision, item, now=adapter.clock())
                    entry = {"event_key": decision.event_key, **reservation}
                    if send_enabled and not dry_run and not destination_allowed:
                        entry["status"] = "blocked_experiment_destination"
                        result.errors.append("YouTube send blocked by experiment destination allowlist")
                    elif send_enabled and not dry_run and reservation["status"] == "reserved":
                        entry["status"] = store.send_once(decision.event_key, _youtube_sender(notifier),
                            enabled=True, now=adapter.clock())
                    if send_enabled and not dry_run and entry.get("status") in {"ambiguous", "sending"}:
                        result.errors.append("YouTube send outcome requires operator reconciliation")
                    if entry.get("status") == "delivered":
                        receipt = store.outbox_record(decision.event_key)
                        entry["message_id"] = receipt.get("message_id") if receipt else None
                    outbox.append(entry)
            result.youtube_eligibility["outbox"] = outbox
            failed = bool(evaluation.errors or any(v.get("health") == "FAILED" for v in evaluation.health.values()))
            status = "FAILED" if failed else "ELIGIBILITY_PREVIEW"
            result.sources.append(SourceResult("youtube", status, list(evaluation.preview_items),
                                                "; ".join(evaluation.errors)))
            result.awaiting_verification = sum(d.eligibility.value == "HOLD_FOR_REVIEW"
                                               for d in evaluation.decisions)
        except Exception as exc:
            result.sources.append(SourceResult("youtube", "FAILED",
                error=f"CHANNEL_TABS_EVALUATION_FAILED:{type(exc).__name__}"))
    if youtube_experiment_only:
        result.pending = 0
        return result
    candidates, successful, fresh = [], [], {}
    bima_identity_failed = False
    for name, monitor, check in sources:
        try:
            items = check()
            status = "NEW_ITEMS" if items else "NO_NEW_ITEMS"
            result.sources.append(SourceResult(name, status, items,
                                              ""))
            candidates.extend(items)
            successful.append(monitor)
            fresh.update({item_key(i): i for i in monitor.current_items})
        except Exception as exc:
            bima_identity_failed |= name == "bima" and isinstance(exc, BimaIdentityError)
            # Exception strings may contain signed URLs or request credentials.
            detail = str(exc) if isinstance(exc, SourceError) else type(exc).__name__
            response = getattr(exc, "response", None)
            if response is not None:
                detail += f" HTTP {response.status_code}"
            result.sources.append(SourceResult(name, "FAILED", error=detail))

    if not evaluate_youtube:
        status = "DISABLED" if not enabled else "DRY_RUN_REQUIRED"
        detail = "" if not enabled else "Channel-tabs evaluation is read-only and requires dry_run=True"
        result.sources.append(SourceResult("youtube", status, error=detail))

    # Old journals retain their exact chunks/keys. Match their BIMA items, not a
    # global letter alias, even if detection-cache persistence previously failed.
    if not bima_identity_failed:
        try:
            historical = [i for b in state["batches"] for i in b["items"] if i.get("source") != "youtube"]
            selected = new_announcements([i for i in candidates if i.get("source") != "youtube"], historical)
            candidates = [i for i in candidates if i.get("source") == "youtube" or any(i is s for s in selected)]
            current_bima = [i for key, i in fresh.items() if key.startswith("bima:")]
            for item in historical:
                match = match_announcement(item, current_bima)
                if match is not None:
                    fresh[item_key(item)] = current_bima[match]
        except BimaIdentityError as exc:
            bima_identity_failed = True
            source = next(s for s in result.sources if s.source == "bima")
            source.status, source.items, source.error = "FAILED", [], str(exc)
    if bima_identity_failed:
        candidates = [i for i in candidates if i.get("source") == "youtube"]
        successful = [m for m in successful if m is not scraper]
    known = {key for batch in state["batches"] for key in batch["event_keys"]}
    # Tabs evaluations are report-only. Historical payload formats cannot enter
    # this path through a scraper or turn into an implicit delivery fallback.
    candidates = [i for i in candidates if i.get("source") != "youtube"]
    new = []
    for item in candidates:
        key = event_key(item)
        if key not in known:
            new.append(item)
            known.add(key)

    blocked = {index for index, batch in enumerate(state["batches"]) if not batch["delivered"]
               and any(i.get("source") == "youtube" for i in batch["items"])}
    if blocked:
        result.errors.append("Pending YouTube batch blocked; operator reconciliation required")
    print(f"[VERIFICATION] awaiting: {result.awaiting_verification}; eligible new: {len(new)}; blocked batches: {len(blocked)}")

    if dry_run or config.get("notification_method", "console") == "console":
        # Preview never acknowledges delivery, and never writes detection state.
        result.pending = len(new) + sum(len(b["items"]) for b in state["batches"] if not b["delivered"])
        print(f"[DRY-RUN/PREVIEW] {result.pending} pending items; no writes or Telegram calls")
        preview = new + [i for index, b in enumerate(state["batches"])
                         if not b["delivered"] and index not in blocked for i in b["items"]]
        if preview:
            notifier._send_console(preview)
        return result

    if new:
        state["batches"].append({
            "event_keys": [event_key(i) for i in new], "items": [stable_item(i) for i in new],
            "chunks": notifier._build_summary_chunks(new), "sent_chunks": [], "sent_documents": [],
            "send_files": bool(config.get("telegram_send_files", False)), "delivered": False,
            "attempts": 0,
        })
    result.pending = sum(len(b["items"]) for b in state["batches"] if not b["delivered"])
    try:
        # Ordering is deliberate: a crash must leave retry data before detection advances.
        atomic_json(path, state)
        for monitor in successful:
            monitor.save_cache(monitor.proposed_cache)
    except Exception as exc:
        result.errors.append("persist detection/retry state: " + type(exc).__name__)
        return result

    for index, batch in enumerate(state["batches"]):
        if batch["delivered"]:
            continue
        if index in blocked:
            continue
        if bima_identity_failed and any(i.get("source") != "youtube" for i in batch["items"]):
            # Do not send an ambiguous BIMA batch, including a mixed-source batch.
            continue
        items = deepcopy(batch["items"])
        for item in items:
            current = fresh.get(item_key(item), {})
            docs = {notifier._document_name(d): d for d in current.get("documents", [])}
            item["documents"] = [docs.get(d["name"], d) for d in item.get("documents", [])]
        original_send_files = notifier.config.get("telegram_send_files", False)
        try:
            notifier.config["telegram_send_files"] = batch["send_files"]
            batch["attempts"] += 1
            atomic_json(path, state)
            success = notifier.send_notification(items, progress=batch,
                                                  checkpoint=lambda: atomic_json(path, state))
            if success:
                batch["delivered"] = True
                batch.pop("last_error", None)
            else:
                batch["last_error"] = "Telegram not fully confirmed"
                result.errors.append(batch["last_error"])
            atomic_json(path, state)
        except Exception as exc:
            batch["delivered"] = False
            result.errors.append("delivery/checkpoint: " + type(exc).__name__)
            # Do not continue sending when a checkpoint could not be persisted.
            break
        finally:
            notifier.config["telegram_send_files"] = original_send_files
    result.pending = sum(len(b["items"]) for b in state["batches"] if not b["delivered"])
    return result
