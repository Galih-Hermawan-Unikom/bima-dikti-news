"""Detection is persisted only after durable retry records exist."""

from copy import deepcopy
from pathlib import Path

from notifier import Notifier
from reliability import RunResult, SourceError, SourceResult, atomic_json, read_json
from scraper import (BimaIdentityError, BimaScraper, announcement_identity,
                     match_announcement, new_announcements)
from youtube_monitor import YouTubeMonitor, youtube_identity


def item_key(item):
    if item.get("source") == "youtube":
        return "youtube:" + youtube_identity(item)
    return "bima:" + announcement_identity(item)


def event_key(item):
    suffix = item.get("status", "UNKNOWN") if item.get("is_status_update") else "initial"
    return item_key(item) + ":" + suffix


def stable_item(item):
    result = deepcopy(item)
    # Signed attachment URLs expire; refresh from the source on every retry.
    result["documents"] = [{"name": d.get("name", "") if isinstance(d, dict) else str(d)}
                           for d in item.get("documents", [])]
    return result


def load_delivery(path):
    state = read_json(path, {"version": 1, "batches": []})
    if not isinstance(state, dict) or state.get("version") != 1 or not isinstance(state.get("batches"), list):
        raise ValueError("Invalid delivery state; restore verified backup")
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


def run_once(config, *, dry_run=False, state_dir=".", scraper=None, youtube=None, notifier=None):
    result = RunResult()
    root = Path(state_dir)
    scraper = scraper or BimaScraper()
    scraper.data_file = str(root / "announcements_cache.json")
    youtube = youtube or YouTubeMonitor(channel_id=config.get("youtube_channel_id", ""),
                                        channel_url=config.get("youtube_channel_url", ""))
    youtube.cache_file = str(root / "youtube_cache.json")
    notifier = notifier or Notifier(config)
    sources = [("bima", scraper, scraper.check_new_announcements)]
    if config.get("youtube_monitor_enabled", True):
        sources.append(("youtube", youtube, youtube.check_new_videos))
    else:
        result.sources.append(SourceResult("youtube", "DISABLED"))
    candidates, successful, fresh = [], [], {}
    bima_identity_failed = False
    for name, monitor, check in sources:
        try:
            items = check()
            status = "DEGRADED" if getattr(monitor, "inspection_failed", False) else ("NEW_ITEMS" if items else "NO_NEW_ITEMS")
            result.sources.append(SourceResult(name, status, items,
                                 "Video inspection unresolved" if status == "DEGRADED" else ""))
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

    path = root / "delivery_state.json"
    try:
        state = load_delivery(path)
    except Exception as exc:
        result.errors.append("delivery state: " + type(exc).__name__)
        return result
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
    new = []
    for item in candidates:
        key = event_key(item)
        if key not in known:
            new.append(item)
            known.add(key)

    if dry_run or config.get("notification_method", "console") == "console":
        # Preview never acknowledges delivery, and never writes detection state.
        result.pending = len(new) + sum(len(b["items"]) for b in state["batches"] if not b["delivered"])
        print(f"[DRY-RUN/PREVIEW] {result.pending} pending items; no writes or Telegram calls")
        preview = new + [i for b in state["batches"] if not b["delivered"] for i in b["items"]]
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

    for batch in state["batches"]:
        if batch["delivered"]:
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
