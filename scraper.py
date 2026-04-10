import json
import os
import platform
import re
import sys
from datetime import datetime
from urllib.parse import unquote

from playwright.sync_api import sync_playwright

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")


def normalize_text(value):
    return " ".join((value or "").split()).strip() if value else ""


def announcement_identity(item):
    surat = normalize_surat(item.get("surat", "")) if isinstance(item, dict) else ""
    if surat:
        return f"surat:{surat}"

    title = normalize_text(item.get("title", "")) if isinstance(item, dict) else ""
    if title:
        return f"title:{title.lower()}"

    return ""


def normalize_surat(value):
    if not value:
        return ""

    cleaned = normalize_text(value).replace(" ", "")
    return re.sub(r"[^0-9A-Za-z./-]", "", cleaned)


def extract_surat(full_text):
    if not full_text:
        return ""

    patterns = [
        r"No\.\s*Surat:\s*([0-9A-Za-z./-]+?/\d{4})\b",
        r"No\.\s*Surat:\s*([0-9A-Za-z./-]+)",
    ]

    for pattern in patterns:
        match = re.search(pattern, full_text, flags=re.IGNORECASE)
        if match:
            return normalize_surat(match.group(1))

    return ""


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
        return page.evaluate(
            """() => {
                const cards = Array.from(
                    document.querySelectorAll('.card-body.p-4, div[class*="card-body"]')
                );

                return cards.map((card, cardIndex) => {
                    const titleEl = card.querySelector('h5.fw-bold, h5, h4, h3');
                    if (!titleEl) return null;

                    const title = (titleEl.textContent || '').trim();
                    if (!title || title.length < 10) return null;

                    const fullText = (card.textContent || '').trim();
                    const dateMatch = fullText.match(
                        /(\\d{1,2}\\s+(?:January|February|March|April|May|June|July|August|September|October|November|December|Januari|Februari|Maret|April|Mei|Juni|Juli|Agustus|September|Oktober|November|Desember)\\s+\\d{4})/i
                    );

                    const documents = Array.from(card.querySelectorAll('li span.text-primary'))
                        .map((el, docIndex) => ({
                            doc_index: docIndex,
                            name: (el.textContent || '').trim()
                        }))
                        .filter(doc => doc.name);

                    return {
                        card_index: cardIndex,
                        title,
                        full_text: fullText,
                        date: dateMatch ? dateMatch[1].trim() : '',
                        documents
                    };
                }).filter(Boolean);
            }"""
        )

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

    def _resolve_documents(self, page, card_index, raw_documents):
        documents = []
        card = page.locator(".card-body.p-4, div[class*='card-body']").nth(card_index)

        for raw_doc in raw_documents:
            doc_name = normalize_text(raw_doc.get("name", ""))
            doc_index = raw_doc.get("doc_index", 0)
            link = card.locator("li span.text-primary").nth(doc_index)
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
                page.goto(self.url, wait_until="networkidle", timeout=30000)
                page.wait_for_timeout(3000)

                items = self._collect_cards(page)

                for item in items:
                    title = normalize_text(item.get("title", ""))
                    if not title:
                        continue

                    announcements.append(
                        {
                            "title": title,
                            "surat": extract_surat(item.get("full_text", "")),
                            "date": normalize_text(item.get("date", "")),
                            "documents": self._resolve_documents(
                                page,
                                item.get("card_index", 0),
                                item.get("documents", []),
                            ),
                            "url": self.url,
                            "scraped_at": datetime.now().isoformat(),
                        }
                    )

            except Exception as e:
                print(f"  Error saat scraping: {e}")
            finally:
                browser.close()

        return announcements

    def load_cache(self):
        if os.path.exists(self.data_file):
            try:
                with open(self.data_file, "r", encoding="utf-8") as file:
                    raw_cache = json.load(file)
            except (json.JSONDecodeError, OSError):
                return []

            if not isinstance(raw_cache, list):
                return []

            return [
                item
                for item in (
                    self._sanitize_announcement_for_cache(announcement)
                    for announcement in raw_cache
                )
                if announcement_identity(item)
            ]
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

        with open(self.data_file, "w", encoding="utf-8") as file:
            json.dump(sanitized, file, ensure_ascii=False, indent=2)

    def check_new_announcements(self):
        print("  [Web] Mengambil data dari BIMA...")
        current = self.fetch_announcements()
        cached = self.load_cache()

        print(f"  [Info] Ditemukan {len(current)} pengumuman di halaman")
        print(f"  [Info] Cache berisi {len(cached)} pengumuman")

        if not current and cached:
            print("  [Warning] Tidak bisa mengambil data saat ini, menggunakan cache terakhir")
            return []

        cached_keys = {announcement_identity(item) for item in cached}
        new_announcements = [
            item
            for item in current
            if announcement_identity(item) and announcement_identity(item) not in cached_keys
        ]

        if new_announcements or not cached:
            all_announcements = current + cached
            seen = set()
            unique = []
            for item in all_announcements:
                key = announcement_identity(item)
                if not key:
                    continue
                if key not in seen:
                    seen.add(key)
                    unique.append(item)
            self.save_cache(unique)

        return new_announcements
