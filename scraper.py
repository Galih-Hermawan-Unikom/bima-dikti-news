import json
import os
import sys
import platform
from datetime import datetime
from playwright.sync_api import sync_playwright

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")


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

    for p in paths:
        if os.path.exists(p):
            return p

    return None


class BimaScraper:
    def __init__(self):
        self.url = "https://bima.kemdiktisaintek.go.id/pengumuman"
        self.data_file = "announcements_cache.json"
        self.browser_path = get_browser_path()

    def fetch_announcements(self):
        announcements = []

        with sync_playwright() as p:
            if self.browser_path:
                browser = p.chromium.launch(
                    headless=True, executable_path=self.browser_path
                )
            else:
                browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                viewport={"width": 1920, "height": 1080},
            )
            page = context.new_page()

            try:
                page.goto(self.url, wait_until="networkidle", timeout=30000)
                page.wait_for_timeout(3000)

                items = page.evaluate("""() => {
                    const results = [];
                    const cards = document.querySelectorAll('.card-body.p-4, div[class*="card-body"]');

                    for (const card of cards) {
                        const titleEl = card.querySelector('h5.fw-bold, h5, h4, h3');
                        if (!titleEl) continue;

                        const title = titleEl.textContent.trim();
                        if (!title || title.length < 10) continue;

                        const fullText = card.textContent.trim();

                        const suratMatch = fullText.match(/No\\.\\s*Surat:\\s*([\\d\\/\\w.-]+)/);
                        const dateMatch = fullText.match(/(\\d{1,2}\\s+(?:January|February|March|April|May|June|July|August|September|October|November|December|Januari|Februari|Maret|April|Mei|Juni|Juli|Agustus|September|Oktober|November|Desember)\\s+\\d{4})/);

                        const docLinks = [];
                        const spans = card.querySelectorAll('span.text-primary');
                        for (const span of spans) {
                            const docText = span.textContent.trim();
                            if (docText) {
                                docLinks.push(docText);
                            }
                        }

                        results.push({
                            title: title,
                            surat: suratMatch ? suratMatch[1].trim() : '',
                            date: dateMatch ? dateMatch[1].trim() : '',
                            documents: docLinks,
                            url: window.location.href
                        });
                    }

                    return results;
                }""")

                for item in items:
                    if item["title"]:
                        announcements.append(
                            {
                                "title": item["title"],
                                "surat": item.get("surat", ""),
                                "date": item.get("date", ""),
                                "documents": item.get("documents", []),
                                "url": item.get("url", self.url),
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
            with open(self.data_file, "r", encoding="utf-8") as f:
                return json.load(f)
        return []

    def save_cache(self, announcements):
        with open(self.data_file, "w", encoding="utf-8") as f:
            json.dump(announcements, f, ensure_ascii=False, indent=2)

    def check_new_announcements(self):
        print("  [Web] Mengambil data dari BIMA...")
        current = self.fetch_announcements()
        cached = self.load_cache()

        print(f"  [Info] Ditemukan {len(current)} pengumuman di halaman")
        print(f"  [Info] Cache berisi {len(cached)} pengumuman")

        if not current and cached:
            print(
                "  [Warning] Tidak bisa mengambil data saat ini, menggunakan cache terakhir"
            )
            return []

        cached_titles = {item["title"] for item in cached}
        new_announcements = [
            item for item in current if item["title"] not in cached_titles
        ]

        if new_announcements or not cached:
            all_announcements = current + cached
            seen = set()
            unique = []
            for item in all_announcements:
                if item["title"] not in seen:
                    seen.add(item["title"])
                    unique.append(item)
            self.save_cache(unique)

        return new_announcements
