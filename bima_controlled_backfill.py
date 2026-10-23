"""One operator-authorized historical BIMA identity; no general backfill policy."""
from copy import deepcopy
import hashlib
import os
from pathlib import Path
import shutil
import tempfile
from urllib.parse import urlsplit

from scraper import announcement_identity, canonical_surat, match_announcement
from youtube_experiment_preflight import PreflightError


TITLE = "Pemberitahuan Jadwal Pelaporan dan Monitoring dan Evaluasi Internal Program Pengabdian kepada Masyarakat Tahun Anggaran 2026"
LETTER = "1454/DST/C3/DT.06.01/2026"
DATE = "22 Oktober 2026"
SOURCE = "https://bima.kemdiktisaintek.go.id/pengumuman"
PDF_NAME = TITLE + ".pdf"
PDF_BYTES = 285439
PDF_SHA256 = "99b60bf8549ac938df565aa378e3e8e259ac6201132aa86ee9eb6169241f4792"


def _blocked(code):
    raise PreflightError("BIMA_SINGLE_BACKFILL_" + code)


def perform(state, current, cache, journal, notifier, checkpoint, *, at):
    from production_runtime import _deliver_bima_event, validate_bima
    validate_bima(state)
    if not state["initialized"]:
        _blocked("UNINITIALIZED_STATE")
    matches = [item for item in current if item.get("title") == TITLE
        and canonical_surat(item) == LETTER and item.get("date") == DATE and item.get("url") == SOURCE]
    if len(matches) != 1:
        _blocked("EXACT_SOURCE_IDENTITY_NOT_UNIQUE")
    item = deepcopy(matches[0])
    key = "bima:" + announcement_identity(item) + ":initial"
    for batch in journal["batches"]:
        items = [entry for entry in batch["items"] if entry.get("source") != "youtube"]
        if match_announcement(item, items) is not None:
            if batch.get("delivered") is True:
                return {"source": "bima", "backfill": "SKIPPED", "reason": "HISTORICAL_DELIVERY_EXISTS",
                        "confirmed_messages": 0}
            _blocked("HISTORICAL_PENDING_REQUIRES_RECONCILIATION")
    for row in state["outbox"].values():
        if match_announcement(item, row["items"]) is not None:
            if row["status"] == "delivered":
                return {"source": "bima", "backfill": "SKIPPED", "reason": "PRODUCTION_DELIVERY_EXISTS",
                        "message_ids": [r["message_id"] for r in row["receipts"]], "confirmed_messages": 0}
            _blocked("EXISTING_OUTBOX_REQUIRES_RECONCILIATION")
    if match_announcement(item, state["inventory"]) is None:
        _blocked("TARGET_NOT_IN_STARTUP_INVENTORY")
    documents = item.get("documents", [])
    if len(documents) != 1 or documents[0].get("name") != PDF_NAME:
        _blocked("EXPECTED_MAIN_PDF_NOT_UNIQUE")
    url = notifier._document_url(documents[0])
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in {
            "storage.googleapis.com", "bima.kemdiktisaintek.go.id", "apibima.kemdiktisaintek.go.id"}:
        _blocked("UNTRUSTED_DOCUMENT_SOURCE")
    if len(notifier._build_summary_chunks([item])) != 1:
        _blocked("SUMMARY_NOT_SINGLE_MESSAGE")
    original_download = notifier._download_file
    source_path = original_download(url, PDF_NAME)
    if not source_path:
        _blocked("PDF_DOWNLOAD_FAILED")
    try:
        data = Path(source_path).read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if not data.startswith(b"%PDF-") or len(data) != PDF_BYTES or digest != PDF_SHA256:
            _blocked("PDF_CONTENT_CHANGED_REVIEW_REQUIRED")
        audit = {"authorization": "operator-single-backfill-1454-20261022",
            "title": TITLE, "letter": LETTER, "publication_date": DATE,
            "source": SOURCE, "pdf_name": PDF_NAME, "pdf_sha256": digest,
            "pdf_bytes": len(data), "verified_at": at.isoformat(),
            "historical_cache_known": match_announcement(item, cache) is not None,
            "historical_journal_match": False, "production_outbox_match": False,
            "startup_inventory_match": True, "github_run_id": os.environ.get("GITHUB_RUN_ID", "")}
        used = False
        def validated_download(requested_url, requested_name, max_size_mb=25):
            nonlocal used
            if used or requested_url != url or requested_name != PDF_NAME:
                _blocked("UNEXPECTED_DOCUMENT_REQUEST")
            used = True
            descriptor, upload_path = tempfile.mkstemp(suffix=".pdf")
            os.close(descriptor)
            shutil.copyfile(source_path, upload_path)
            return upload_path
        notifier._download_file = validated_download
        messages, delivered = _deliver_bima_event(state, item, notifier, checkpoint, at=at, audit=audit)
        row = state["outbox"][key]
        if not delivered or row["status"] != "delivered" or messages != 2:
            _blocked("OUTCOME_REQUIRES_RECONCILIATION")
        return {"source": "bima", "backfill": "SENT", "confirmed_messages": messages,
                "message_ids": [r["message_id"] for r in row["receipts"]],
                "pdf": "DELIVERED", "pdf_bytes": len(data), "pdf_sha256": digest}
    finally:
        notifier._download_file = original_download
        Path(source_path).unlink(missing_ok=True)
