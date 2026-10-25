"""Read-only, provenance-bound BIMA listing capture for Kemdiktisaintek dedup."""
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from kemdiktisaintek_state import StateError, fingerprint, timestamp

BIMA_URL = "https://bima.kemdiktisaintek.go.id/pengumuman"
REPOSITORY = "Galih-Hermawan-Unikom/bima-dikti-news"
WORKFLOW_SUFFIX = ".github/workflows/kemdiktisaintek-experiment.yml"
WIB = ZoneInfo("Asia/Jakarta")


def _producer(env):
    sha = env.get("GITHUB_SHA", "")
    branch = env.get("GITHUB_REF", "")
    if (env.get("GITHUB_ACTIONS") != "true" or env.get("GITHUB_REPOSITORY") != REPOSITORY
            or env.get("GITHUB_EVENT_NAME") != "workflow_dispatch" or len(sha) != 40
            or any(c not in "0123456789abcdef" for c in sha)
            or env.get("KEMDIKTISAINTEK_REVIEWED_CODE_SHA") != sha
            or not branch.startswith("refs/heads/feat/kemdiktisaintek-")):
        raise StateError("BIMA_REFERENCE_PRODUCER_UNVERIFIED")
    workflow_ref = env.get("GITHUB_WORKFLOW_REF", "")
    if WORKFLOW_SUFFIX not in workflow_ref:
        raise StateError("BIMA_REFERENCE_WORKFLOW_UNVERIFIED")
    run_id, attempt = env.get("GITHUB_RUN_ID", ""), env.get("GITHUB_RUN_ATTEMPT", "")
    if not run_id.isdigit() or not attempt.isdigit():
        raise StateError("BIMA_REFERENCE_RUN_UNVERIFIED")
    return {"repository": REPOSITORY, "workflow_ref": workflow_ref, "workflow_sha": sha,
            "run_id": run_id, "run_attempt": attempt, "event": "workflow_dispatch", "ref": branch}


def validate_reference(reference, expected_sha256, *, at, env, candidate=None):
    """Validate trusted runtime provenance and coverage before allowing NO_MATCH."""
    try:
        producer = _producer(env)
        if (not isinstance(reference, dict) or fingerprint(reference) != expected_sha256
                or reference.get("schema_version") != 2 or reference.get("source") != "bima"
                or reference.get("source_url") != BIMA_URL or reference.get("healthy") is not True
                or reference.get("archive_complete") is not False or reference.get("producer") != producer
                or not isinstance(reference.get("items"), list) or not reference["items"]
                or reference.get("captured_at") != reference.get("capture_finished_at")
                or reference.get("coverage_until") != reference.get("captured_at")
                or reference.get("pagination_state") not in {"FIRST_PAGE_HAS_NEXT", "FIRST_PAGE_COMPLETE"}
                or reference.get("newest_first") is not True):
            raise StateError("BIMA_REFERENCE_UNTRUSTED_OR_INCOMPLETE")
        now, started, captured = timestamp(at), timestamp(reference["capture_started_at"]), timestamp(reference["captured_at"])
        if not timedelta(0) <= now - captured <= timedelta(minutes=60) or started > captured or captured > now:
            raise StateError("BIMA_REFERENCE_STALE_OR_FUTURE")
        if candidate is not None:
            publication = timestamp(candidate.get("published_at"))
            coverage_from = timestamp(reference["coverage_from"])
            if publication < coverage_from:
                raise StateError("BIMA_REFERENCE_DOES_NOT_COVER_PUBLICATION")
    except StateError:
        raise
    except (KeyError, TypeError, ValueError, AttributeError):
        raise StateError("BIMA_REFERENCE_INVALID") from None
    return reference["items"]


def create_reference(items, *, capture_started_at, capture_finished_at, pagination_state,
                     newest_first, producer):
    """Build a canonical reference. Timestamps come from the runner, never scraped records."""
    started, finished = timestamp(capture_started_at), timestamp(capture_finished_at)
    if not isinstance(items, list) or not items or newest_first is not True:
        raise StateError("BIMA_REFERENCE_EMPTY_OR_UNSORTED")
    if pagination_state not in {"FIRST_PAGE_HAS_NEXT", "FIRST_PAGE_COMPLETE"}:
        raise StateError("BIMA_REFERENCE_COVERAGE_UNKNOWN")
    normalized = []
    dates = []
    for row in items:
        if not isinstance(row, dict) or not isinstance(row.get("title"), str) or not row["title"].strip():
            raise StateError("BIMA_REFERENCE_ROW_INVALID")
        date = row.get("date")
        parsed = _date(date)
        if parsed is None:
            raise StateError("BIMA_REFERENCE_DATE_INVALID")
        dates.append(parsed)
        docs = row.get("documents")
        if not isinstance(docs, list):
            raise StateError("BIMA_REFERENCE_DOCUMENTS_INVALID")
        normalized.append({"title": row["title"].strip(), "surat": str(row.get("surat") or "").strip(),
            "date": date, "documents": [{"name": str(d.get("name") or "").strip()}
                for d in docs if isinstance(d, dict) and str(d.get("name") or "").strip()],
            "url": BIMA_URL})
    if any(dates[i] < dates[i + 1] for i in range(len(dates) - 1)):
        raise StateError("BIMA_REFERENCE_NOT_NEWEST_FIRST")
    oldest = dates[-1].date()
    # On a paginated newest-first listing, records from the oldest visible day may
    # continue on the next page, so that day is excluded from proven coverage.
    if pagination_state == "FIRST_PAGE_HAS_NEXT":
        oldest += timedelta(days=1)
    coverage_from = datetime.combine(oldest, time.min, WIB).astimezone(timezone.utc).isoformat()
    finished_iso = finished.isoformat()
    return {"schema_version": 2, "source": "bima", "source_url": BIMA_URL,
        "scope": "first_page_sorted_date_window", "healthy": True, "archive_complete": False,
        "pagination_state": pagination_state, "newest_first": True,
        "capture_started_at": started.isoformat(), "capture_finished_at": finished_iso,
        "captured_at": finished_iso, "coverage_from": coverage_from, "coverage_until": finished_iso,
        "producer": producer, "items": normalized}


def _date(value):
    from scraper import _publication_date
    parsed = _publication_date({"date": value})
    return parsed


class _ReferenceScraper:
    """Adapter around the production DOM collector, skipping document-link clicks."""
    def __init__(self):
        from scraper import BimaScraper

        class ReadOnlyBimaScraper(BimaScraper):
            def __init__(self):
                super().__init__()
                self.pagination_state = "UNKNOWN"
                self.newest_first = False

            def _collect_cards(self, page):
                meta = page.evaluate("""() => {
                  const p = document.querySelector('.pagination, nav[aria-label*=pagination i]');
                  const current = p && p.querySelector('.active, [aria-current=page]');
                  const currentPage = current ? parseInt(current.textContent.trim(), 10) || 1 : null;
                  const nums = p ? [...p.querySelectorAll('a')].map(a => parseInt(a.textContent.trim(), 10)).filter(Number.isFinite) : [];
                  const next = p && p.querySelector('a[rel=next], a[aria-label*=next i], a.next, .page-item:last-child a');
                  let hasNext = null;
                  if (currentPage && nums.length) hasNext = currentPage < Math.max(...nums);
                  else if (next) hasNext = !next.closest('.disabled') && next.getAttribute('aria-disabled') !== 'true';
                  return {known: !!p, current: currentPage, hasNext};
                }""")
                if meta.get("known") and meta.get("current") == 1 and meta.get("hasNext") is not None:
                    self.pagination_state = "FIRST_PAGE_HAS_NEXT" if meta.get("hasNext") else "FIRST_PAGE_COMPLETE"
                cards = super()._collect_cards(page)
                dates = [__import__('scraper')._publication_date({"date": c.get("date", "")}) for c in cards]
                self.newest_first = bool(dates) and all(dates[i] >= dates[i + 1] for i in range(len(dates) - 1))
                return cards

            def _resolve_documents(self, page, card_index, raw_documents, layout="legacy"):
                # The reference needs document names only; never click or fetch BIMA attachments.
                return [{"name": str(d.get("name") or "").strip(), "download_url": "", "url_type": "",
                         "gcs_url": "", "api_url": ""} for d in raw_documents]

        self.scraper = ReadOnlyBimaScraper()

    def fetch(self):
        rows = self.scraper.fetch_announcements()
        return rows, self.scraper.pagination_state, self.scraper.newest_first


def capture_live(*, env, clock=lambda: datetime.now(timezone.utc).isoformat(), collector=None):
    """Read BIMA once; return data plus an independently generated integrity digest."""
    producer = _producer(env)
    started = timestamp(clock()).isoformat()
    collector = collector or _ReferenceScraper()
    try:
        rows, pagination_state, newest_first = collector.fetch()
    except Exception as exc:
        raise StateError("BIMA_REFERENCE_FETCH_FAILED:" + type(exc).__name__) from None
    finished = timestamp(clock()).isoformat()
    try:
        reference = create_reference(rows, capture_started_at=started, capture_finished_at=finished,
            pagination_state=pagination_state, newest_first=newest_first, producer=producer)
    except StateError:
        raise
    except Exception as exc:
        raise StateError("BIMA_REFERENCE_BUILD_FAILED:" + type(exc).__name__) from None
    return reference, fingerprint(reference)
