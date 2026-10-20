import os
import platform
import re
import sys
import unicodedata
from datetime import datetime
from urllib.parse import unquote

from playwright.sync_api import sync_playwright
from reliability import SourceError, atomic_json, read_json

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")


def normalize_text(value):
    return " ".join((value or "").split()).strip() if value else ""


def announcement_identity(item):
    """A letter number is not a unique announcement identifier."""
    surat = canonical_surat(item) if isinstance(item, dict) else ""
    title = _metadata_text(item.get("title", "")) if isinstance(item, dict) else ""
    if title:
        return f"surat:{surat.casefold()}|title:{title}"

    return ""


class BimaIdentityError(SourceError):
    pass


def _metadata_text(value):
    value = unicodedata.normalize("NFKC", value or "")
    value = value.translate(str.maketrans({"\u2013": "-", "\u2014": "-",
                                          "\u2018": "'", "\u2019": "'",
                                          "\u201c": '"', "\u201d": '"'}))
    return normalize_text(value).casefold()


def _publication_date(item):
    parts = normalize_text(item.get("date", "")).split()
    if len(parts) != 3 or not re.fullmatch(r"\d{1,2}", parts[0]) or not re.fullmatch(r"(?:19|20)\d{2}", parts[2]):
        return None
    months = ["january januari", "february februari", "march maret", "april",
              "may mei", "june juni", "july juli", "august agustus",
              "september", "october oktober", "november", "december desember"]
    month = next((i for i, names in enumerate(months, 1) if parts[1].lower() in names.split()), None)
    if month is not None:
        try:
            return datetime(int(parts[2]), month, int(parts[0]))
        except ValueError:
            pass
    return None


def _document_names(item):
    documents = item.get("documents", [])
    if not isinstance(documents, list) or any(not isinstance(doc, (str, dict)) for doc in documents):
        raise BimaIdentityError("BIMA identity ambiguous: invalid document metadata")
    return frozenset(name for doc in documents
                     if (name := _metadata_text(doc if isinstance(doc, str) else doc.get("name", ""))))


def _distinct_publications(item, old, documents, old_docs):
    """Require entry-local title/document evidence, not a letter-number alias."""
    title, old_title = _metadata_text(item.get("title")), _metadata_text(old.get("title"))
    words, old_words = set(re.findall(r"[^\W\d_]+", title)), set(re.findall(r"[^\W\d_]+", old_title))
    # One-word edits, generic attachments and title containment are insufficient.
    return (len(words - old_words) >= 2 and len(old_words - words) >= 2
            and any(title + ext in documents for ext in (".pdf", ".doc", ".docx"))
            and any(old_title + ext in old_docs for ext in (".pdf", ".doc", ".docx")))


def match_announcement(item, history):
    """Return a history index, or fail closed on conflicting evidence; no fuzzy aliases."""
    key = announcement_identity(item)
    if not key:
        raise BimaIdentityError("BIMA identity ambiguous: announcement title missing")
    title, surat = _metadata_text(item.get("title")), canonical_surat(item).casefold()
    date, documents = _publication_date(item), _document_names(item)
    matches = []
    for index, old in enumerate(history):
        old_key = announcement_identity(old)
        if not old_key:
            raise BimaIdentityError("BIMA identity ambiguous: historical announcement title missing")
        old_title, old_surat = _metadata_text(old.get("title")), canonical_surat(old).casefold()
        old_date, old_docs = _publication_date(old), _document_names(old)
        same_title = bool(title and title == old_title)
        same_surat = bool(surat and surat == old_surat)
        same_date = date is not None and date == old_date
        same_docs = bool(documents and documents == old_docs)
        disjoint_docs = bool(documents and old_docs and documents.isdisjoint(old_docs))
        if key == old_key:
            if not surat and (not same_date or (documents and old_docs and documents != old_docs)):
                raise BimaIdentityError("BIMA identity ambiguous: title alone is insufficient")
            if date and old_date and date != old_date and documents and old_docs and documents != old_docs:
                raise BimaIdentityError("BIMA identity ambiguous: conflicting dates and documents")
            matches.append(index)
        elif same_title:
            if same_date and not (documents and old_docs and documents != old_docs):
                matches.append(index)
            elif date and old_date and date.year != old_date.year and surat and old_surat and surat != old_surat:
                continue
            else:
                raise BimaIdentityError("BIMA identity ambiguous: title reused or letter correction unresolved")
        elif same_surat:
            if same_date and same_docs:
                matches.append(index)
            elif date and old_date and date != old_date and disjoint_docs:
                continue
            elif same_date and disjoint_docs and _distinct_publications(item, old, documents, old_docs):
                continue
            else:
                raise BimaIdentityError("BIMA identity ambiguous: letter reused without distinct dated documents")
        elif same_date and same_docs:
            raise BimaIdentityError("BIMA identity ambiguous: both title and letter changed")
    if len({announcement_identity(history[i]) for i in matches}) > 1:
        raise BimaIdentityError("BIMA identity ambiguous: multiple historical announcements match")
    return matches[0] if matches else None


def new_announcements(current, history):
    new = []
    for item in current:
        if match_announcement(item, history + new) is None:
            new.append(item)
    return new


def normalize_surat(value):
    if not value:
        return ""

    cleaned = normalize_text(value).replace(" ", "")
    return re.sub(r"[^0-9A-Za-z./-]", "", cleaned)


def extract_surat(full_text):
    if not full_text:
        return ""

    patterns = [r"No\.?\s*Surat\s*:\s*([0-9A-Za-z./-]+?/(?:19|20)\d{2})(?![0-9A-Za-z./-])"]

    for pattern in patterns:
        match = re.search(pattern, full_text, flags=re.IGNORECASE)
        if match:
            return normalize_surat(match.group(1))

    return ""


def canonical_surat(item):
    """Recognize only the known year+publication-day corruption."""
    value = normalize_surat(item.get("surat", ""))
    date = _publication_date(item)
    if date:
        day, year = item["date"].split()[0], str(date.year)
        for suffix in {day, day.zfill(2)}:
            if value.endswith("/" + year + suffix):
                return value[:-len(suffix)]
    return value


def extract_filename_from_url(url):
    if not url:
        return ""

    match = re.search(r"/([^/?]+)(?:\?|$)", url)
    if not match:
        return ""

    return normalize_text(unquote(match.group(1)))


def looks_like_matching_document(doc_name, url):
    if not doc_name or not url:
        return False

    return extract_filename_from_url(url).lower() in normalize_text(doc_name).lower()


def get_browser_path():
    system = platform.system()
    base = os.path.expanduser("~")

    if system == "Windows":
        paths = [
            os.path.join(
                base,
                "AppData",
                "Local",
                "ms-playwright",
                "chromium-1194",
                "chrome-win",
                "chrome.exe",
            ),
            os.path.join(
                base,
                "AppData",
                "Local",
                "ms-playwright",
                "chromium-1200",
                "chrome-win",
                "chrome.exe",
            ),
        ]
    elif system == "Linux":
        paths = [
            os.path.join(
                base,
                ".cache",
                "ms-playwright",
                "chromium-1194",
                "chrome-linux",
                "chrome",
            ),
            os.path.join(
                base,
                ".cache",
                "ms-playwright",
                "chromium-1200",
                "chrome-linux",
                "chrome",
            ),
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
            "/snap/bin/chromium",
        ]
    else:
        paths = []

    for path in paths:
        if os.path.exists(path):
            return path

    return None


class BimaScraper:
    def __init__(self):
        self.url = "https://bima.kemdiktisaintek.go.id/pengumuman"
        self.data_file = "announcements_cache.json"
        self.browser_path = get_browser_path()

    def _sanitize_document_for_cache(self, document):
        if isinstance(document, str):
            return {"name": normalize_text(document)}

        return {
            "name": normalize_text(document.get("name", "")),
        }

    def _sanitize_announcement_for_cache(self, announcement):
        documents = announcement.get("documents", []) if isinstance(announcement, dict) else []

        return {
            "title": normalize_text(announcement.get("title", "")),
            "surat": normalize_surat(announcement.get("surat", "")),
            "date": normalize_text(announcement.get("date", "")),
            "documents": [
                doc
                for doc in (
                    self._sanitize_document_for_cache(document) for document in documents
                )
                if doc.get("name")
            ],
            "url": normalize_text(announcement.get("url", self.url)) or self.url,
            "scraped_at": announcement.get("scraped_at", ""),
        }

    def _collect_cards(self, page):
        raw = page.evaluate(
            """() => {
                const modern = [...document.querySelectorAll('article.ap-card')];
                const legacy = [...document.querySelectorAll('.card-body.p-4, div[class*="card-body"]')];
                const layout = modern.length ? 'ap-card' : 'legacy';
                return {mixed: !!(modern.length && legacy.length), cards: (modern.length ? modern : legacy).map((card, index) => {
                    const texts = [];
                    const walker = document.createTreeWalker(card, NodeFilter.SHOW_TEXT);
                    while (walker.nextNode()) texts.push(walker.currentNode.textContent);
                    const metadata = card.cloneNode(true);
                    metadata.querySelectorAll('h3, h4, h5, ul, ol').forEach(el => el.remove());
                    const metadataTexts = [];
                    const metadataWalker = document.createTreeWalker(metadata, NodeFilter.SHOW_TEXT);
                    while (metadataWalker.nextNode()) metadataTexts.push(metadataWalker.currentNode.textContent);
                    const values = selector => [...card.querySelectorAll(selector)].map(el => el.textContent.trim());
                    return {
                        card_index: index, layout, full_text: texts.join(' '),
                        metadata_text: metadataTexts.join(' '),
                        titles: values(layout === 'ap-card' ? '.ap-card__title' : 'h5, h4, h3'),
                        numbers: values('.ap-card__number'),
                        times: [...card.querySelectorAll('time.ap-date')].map(el => ({iso: el.getAttribute('datetime'), label: el.getAttribute('title')})),
                        documents: [...card.querySelectorAll(layout === 'ap-card' ? '.ap-file' : 'li span.text-primary')].map((el, i) => ({
                            doc_index: i,
                            names: layout === 'ap-card' ? [...el.querySelectorAll('.ap-file__name')].map(n => n.textContent.trim()) : [el.textContent.trim()]
                        }))
                    };
                })};
            }"""
        )
        if raw["mixed"]:
            raise SourceError("BIMA ambiguous mixed card layouts")
        if not raw["cards"]:
            raise SourceError("BIMA returned zero cards; unrecognized or empty page")
        months = "Januari Februari Maret April Mei Juni Juli Agustus September Oktober November Desember".split()
        cards = []
        for item in raw["cards"]:
            error = f"BIMA invalid or ambiguous metadata in card {item['card_index'] + 1}"
            if len(item["titles"]) != 1 or len(normalize_text(item["titles"][0])) < 10:
                raise SourceError(error + ": title")
            item["title"] = normalize_text(item.pop("titles")[0])
            if item["layout"] == "ap-card":
                if len(item["numbers"]) != 1 or len(item["times"]) != 1:
                    raise SourceError(error + ": number/date")
                surat = normalize_text(item["numbers"][0])
                time = item["times"][0]
                iso = time["iso"] or ""
                try:
                    if not re.fullmatch(r"(?:19|20)\d{2}-\d{2}-\d{2}", iso):
                        raise ValueError()
                    date = datetime.strptime(iso, "%Y-%m-%d")
                except ValueError:
                    raise SourceError(error + ": ISO date") from None
                if time["label"] and _publication_date({"date": time["label"]}) != date:
                    raise SourceError(error + ": conflicting date label")
                item["date"] = f"{date.day:02d} {months[date.month - 1]} {date.year}"
            else:
                numbers = re.findall(r"No\.?\s*Surat\s*:\s*([0-9A-Za-z./-]+)", item["metadata_text"], re.I)
                dates = re.findall(r"\b\d{1,2}\s+[A-Za-z]+\s+(?:19|20)\d{2}\b", item["metadata_text"])
                if len(numbers) != 1 or len(dates) != 1:
                    raise SourceError(error + ": number/date")
                surat, item["date"] = numbers[0], normalize_text(dates[0])
                if _publication_date(item) is None:
                    raise SourceError(error + ": date")
            if not re.fullmatch(r"[0-9A-Za-z./-]+/(?:19|20)\d{2}", surat):
                raise SourceError(error + ": letter number")
            item["surat"] = surat
            if not item["documents"]:
                raise SourceError(error + ": documents missing")
            for doc in item["documents"]:
                if len(doc["names"]) != 1 or not normalize_text(doc["names"][0]):
                    raise SourceError(error + ": document name")
                doc["name"] = normalize_text(doc.pop("names")[0])
            cards.append(item)
        return cards

    def _capture_document_url(self, page, link, doc_name):
        captured_urls = []

        def handle_request(request):
            url = request.url
            lowered = url.lower()
            if any(
                token in lowered
                for token in ("storage.googleapis", "apibima", ".pdf", ".doc", ".docx")
            ):
                captured_urls.append(url)

        page.context.on("request", handle_request)

        try:
            link.scroll_into_view_if_needed()
            link.click(timeout=3000)
            page.wait_for_timeout(2500)

            candidates = list(dict.fromkeys(captured_urls))
            for url in reversed(candidates):
                if looks_like_matching_document(doc_name, url):
                    return url

            for url in reversed(candidates):
                if "storage.googleapis" in url or "apibima" in url:
                    return url

            return ""
        except Exception:
            return ""
        finally:
            try:
                close = page.locator(".btn-close, button.close")
                if close.count() > 0:
                    close.first.click(timeout=1000)
                    page.wait_for_timeout(500)
            except Exception:
                pass

            try:
                page.context.remove_listener("request", handle_request)
            except Exception:
                pass

    def _resolve_documents(self, page, card_index, raw_documents, layout="legacy"):
        documents = []
        modern = layout == "ap-card"
        card = page.locator("article.ap-card" if modern else ".card-body.p-4, div[class*='card-body']").nth(card_index)

        for raw_doc in raw_documents:
            doc_name = normalize_text(raw_doc.get("name", ""))
            doc_index = raw_doc.get("doc_index", 0)
            link = card.locator(".ap-file" if modern else "li span.text-primary").nth(doc_index)
            download_url = self._capture_document_url(page, link, doc_name)

            documents.append(
                {
                    "name": doc_name or f"Dokumen {doc_index + 1}",
                    "download_url": download_url,
                    "url_type": (
                        "gcs"
                        if "storage.googleapis" in download_url
                        else "api" if "apibima" in download_url else ""
                    ),
                    "gcs_url": download_url if "storage.googleapis" in download_url else "",
                    "api_url": download_url if "apibima" in download_url else "",
                }
            )

        return documents

    def fetch_announcements(self):
        announcements = []

        with sync_playwright() as playwright:
            if self.browser_path:
                browser = playwright.chromium.launch(
                    headless=True, executable_path=self.browser_path
                )
            else:
                browser = playwright.chromium.launch(headless=True)

            context = browser.new_context(
                accept_downloads=True,
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1920, "height": 1080},
            )
            page = context.new_page()

            try:
                response = page.goto(self.url, wait_until="networkidle", timeout=30000)
                if response is not None and response.status >= 400:
                    raise SourceError(f"BIMA HTTP {response.status}")
                page.wait_for_timeout(3000)

                items = self._collect_cards(page)

                for item in items:
                    title = normalize_text(item.get("title", ""))
                    if not title:
                        continue

                    announcements.append(
                        {
                            "title": title,
                            "surat": item["surat"],
                            "date": normalize_text(item.get("date", "")),
                            "documents": self._resolve_documents(
                                page,
                                item.get("card_index", 0),
                                item.get("documents", []),
                                item.get("layout", "legacy"),
                            ),
                            "url": self.url,
                            "scraped_at": datetime.now().isoformat(),
                        }
                    )

            except SourceError:
                raise
            except Exception as e:
                raise SourceError(f"BIMA scraping failed: {type(e).__name__}") from None
            finally:
                browser.close()

        if not announcements:
            raise SourceError("BIMA returned zero cards; source health cannot be confirmed")
        return announcements

    def load_cache(self):
        if os.path.exists(self.data_file):
            raw_cache = read_json(self.data_file, [])

            if not isinstance(raw_cache, list):
                raise ValueError("BIMA cache must be a list")

            if any(not isinstance(item, dict) or not announcement_identity(item) for item in raw_cache):
                raise ValueError("BIMA cache contains invalid records")
            return raw_cache
        return []

    def save_cache(self, announcements):
        sanitized = [
            item
            for item in (
                self._sanitize_announcement_for_cache(announcement)
                for announcement in announcements
            )
            if announcement_identity(item)
        ]

        atomic_json(self.data_file, sanitized)

    def check_new_announcements(self):
        self.proposed_cache = None
        print("  [Web] Mengambil data dari BIMA...")
        current = self.fetch_announcements()
        cached = self.load_cache()

        print(f"  [Info] Ditemukan {len(current)} pengumuman di halaman")
        print(f"  [Info] Cache berisi {len(cached)} pengumuman")

        if not current:
            raise SourceError("BIMA returned zero cards")
        self.current_items = current

        detected = new_announcements(current, cached)

        if detected or not cached:
            self.proposed_cache = cached + detected

        if self.proposed_cache is None:
            self.proposed_cache = cached

        return detected
