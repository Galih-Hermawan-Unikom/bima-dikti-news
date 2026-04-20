import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from html import escape

import requests

from app_config import load_config

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

WIB = timezone(timedelta(hours=7))


def now_wib():
    return datetime.now(WIB)


class Notifier:
    def __init__(self, config=None, config_path="config.json"):
        self.config = config or load_config(config_path=config_path)

    def send_notification(self, items):
        if not items:
            return

        method = self.config.get("notification_method", "console")

        if method == "telegram":
            self._send_telegram(items)
        elif method == "console":
            self._send_console(items)
        elif method == "both":
            self._send_console(items)
            self._send_telegram(items)

    def _item_source(self, item):
        source = (item or {}).get("source", "bima")
        return source if source in {"bima", "youtube"} else "bima"

    def _document_name(self, doc):
        if isinstance(doc, dict):
            return doc.get("name", "")
        return str(doc)

    def _document_url(self, doc):
        if not isinstance(doc, dict):
            return ""

        return (
            doc.get("download_url")
            or doc.get("gcs_url")
            or doc.get("api_url")
            or ""
        )

    def _downloadable_documents(self, ann):
        return [
            doc
            for doc in ann.get("documents", [])
            if isinstance(doc, dict) and self._document_url(doc)
        ]

    def _format_html_item(self, index, ann):
        source = self._item_source(ann)
        if source == "youtube":
            status = ann.get("status", "VOD")
            if status == "LIVE":
                icon = "🔴 [LIVE SEDANG TAYANG]"
            elif status == "UPCOMING":
                icon = "⏰ [RENCANA TAYANG]"
            else:
                icon = "▶️ [VIDEO BARU]"
            
            if ann.get("is_status_update"):
                icon = f"🔄 {icon}"

            channel_title = ann.get("channel_title", "Kemdiktisaintek")
            message = f"<b>{index}. {icon} {escape(ann['title'])}</b>\n"
            message += f"   📺 {escape(channel_title)}\n"
            
            if status == "UPCOMING" and ann.get("scheduled_start"):
                try:
                    start_dt = datetime.fromisoformat(ann["scheduled_start"].replace("Z", "+00:00")).astimezone(WIB)
                    message += f"   📅 Jadwal: {escape(start_dt.strftime('%d/%m/%Y %H:%M WIB'))}\n"
                except:
                    pass
            elif ann.get("published"):
                message += f"   📆 Rilis: {escape(ann['published'])}\n"
                
            if ann.get("description"):
                message += f"   📝 {escape(ann['description'][:300])}...\n"
            message += f"   🔗 {escape(ann.get('url', 'https://www.youtube.com/@kemdiktisaintek'))}\n\n"
            return message

        message = f"<b>{index}. {escape(ann['title'])}</b>\n"
        if ann.get("surat"):
            message += f"   📜 {escape(ann['surat'])}\n"
        if ann.get("date"):
            message += f"   📆 {escape(ann['date'])}\n"

        documents = ann.get("documents", [])
        if documents:
            message += f"   📦 Dokumen: {len(documents)} file\n"
            for doc in documents:
                message += f"   📎 {escape(self._document_name(doc))}\n"

        message += f"   🔗 {escape(ann.get('url', 'https://bima.kemdiktisaintek.go.id/pengumuman'))}\n\n"
        return message

    def _build_summary_chunks(self, items):
        bima_count = sum(1 for item in items if self._item_source(item) == "bima")
        youtube_count = sum(1 for item in items if self._item_source(item) == "youtube")

        if bima_count and youtube_count:
            title = "📢 UPDATE BARU BIMA + YOUTUBE KEMDIKTISAINTEK"
        elif youtube_count:
            title = "▶️ VIDEO BARU YOUTUBE KEMDIKTISAINTEK"
        else:
            title = "📢 PENGUMUMAN BARU BIMA KEMDIKTISAINTEK"

        header = f"<b>{title}</b>\n\n"
        header += f"📅 {escape(now_wib().strftime('%d/%m/%Y %H:%M WIB'))}\n"
        header += f"🔢 Jumlah: {len(items)} item baru\n"
        if bima_count:
            header += f"🏛️ BIMA: {bima_count}\n"
        if youtube_count:
            header += f"📺 YouTube: {youtube_count}\n"
        header += "\n"
        footer = "━━━━━━━━━━━━━━━━━━━━\n<i>Bot Notifikasi BIMA</i>"

        chunks = []
        current = header

        for index, ann in enumerate(items, 1):
            item = self._format_html_item(index, ann)
            if len(current) + len(item) + len(footer) > 4000:
                chunks.append(current)
                current = item
            else:
                current += item

        if current:
            chunks.append(current + footer)

        return chunks

    def _send_console(self, items):
        bima_count = sum(1 for item in items if self._item_source(item) == "bima")
        youtube_count = sum(1 for item in items if self._item_source(item) == "youtube")

        print("\n" + "=" * 60)
        if bima_count and youtube_count:
            print("UPDATE BARU BIMA + YOUTUBE KEMDIKTISAINTEK")
        elif youtube_count:
            print("VIDEO BARU YOUTUBE KEMDIKTISAINTEK")
        else:
            print("PENGUMUMAN BARU BIMA KEMDIKTISAINTEK")
        print("=" * 60)
        print(f"  {now_wib().strftime('%d/%m/%Y %H:%M WIB')}")
        print(f"  Jumlah: {len(items)} item baru")
        if bima_count:
            print(f"  BIMA: {bima_count}")
        if youtube_count:
            print(f"  YouTube: {youtube_count}")
        print()

        for i, ann in enumerate(items, 1):
            source = self._item_source(ann)
            if source == "youtube":
                status = ann.get("status", "VOD")
                if status == "LIVE":
                    prefix = "🔴 [LIVE] "
                elif status == "UPCOMING":
                    prefix = "⏰ [UPCOMING] "
                else:
                    prefix = "▶️ [VOD] "
                    
                if ann.get("is_status_update"):
                    prefix = f"🔄 {prefix}"
                    
                print(f"  {i}. {prefix}{ann['title']}")
                if ann.get("channel_title"):
                    print(f"     📺 {ann['channel_title']}")
                
                if status == "UPCOMING" and ann.get("scheduled_start"):
                    try:
                        start_dt = datetime.fromisoformat(ann["scheduled_start"].replace("Z", "+00:00")).astimezone(WIB)
                        print(f"     📅 Jadwal: {start_dt.strftime('%d/%m/%Y %H:%M WIB')}")
                    except:
                        pass
                elif ann.get("published"):
                    print(f"     📆 Rilis: {ann['published']}")
                    
                if ann.get("description"):
                    print(f"     📝 {ann['description'][:300]}...")
                print(f"     🔗 {ann.get('url', 'https://www.youtube.com/@kemdiktisaintek')}")
                print()
                continue

            if ann.get("surat"):
                print(f"     📜 {ann['surat']}")
            if ann.get("date"):
                print(f"     📆 {ann['date']}")
            documents = ann.get("documents", [])
            if documents:
                print(f"     📦 Dokumen: {len(documents)} file")
                for doc in documents:
                    label = self._document_name(doc)
                    url = self._document_url(doc)
                    if url:
                        print(f"     📎 {label}")
                        print(f"        {url}")
                    else:
                        print(f"     📎 {label}")
            print()

        print("=" * 60)

    def _download_file(self, url, file_name, max_size_mb=25):
        try:
            headers = {
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko)"
                )
            }
            response = requests.get(url, headers=headers, timeout=60, stream=True)

            if response.status_code != 200:
                print(f"  Gagal download file: status {response.status_code}")
                return None

            content_length = response.headers.get("content-length")
            if content_length and int(content_length) > max_size_mb * 1024 * 1024:
                print(f"  File terlalu besar: {int(content_length) / 1024 / 1024:.1f} MB")
                return None

            safe_name = "".join(
                char for char in file_name if char.isalnum() or char in ".-_ "
            ).strip() or "dokumen"
            temp_path = os.path.join(tempfile.gettempdir(), safe_name)

            with open(temp_path, "wb") as file:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        file.write(chunk)

            return temp_path
        except Exception as e:
            print(f"  Error download file: {e}")
            return None

    def _send_summary_message(self, bot_token, chat_id, items):
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        success = True

        for index, text in enumerate(self._build_summary_chunks(items), 1):
            payload = {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            }

            try:
                response = requests.post(url, json=payload, timeout=15)
                if response.status_code == 200 and response.json().get("ok"):
                    print(f"  Ringkasan Telegram terkirim ({index})")
                    continue

                print(f"  Gagal kirim ringkasan Telegram: {response.text}")
                success = False
            except Exception as e:
                print(f"  Error kirim ringkasan Telegram: {e}")
                success = False

        return success

    def _send_document(self, bot_token, chat_id, file_path, file_name):
        url = f"https://api.telegram.org/bot{bot_token}/sendDocument"
        caption = f"📎 {file_name}"

        with open(file_path, "rb") as file:
            response = requests.post(
                url,
                data={"chat_id": chat_id, "caption": caption},
                files={"document": file},
                timeout=120,
            )

        result = response.json()
        if result.get("ok"):
            print(f"  Dokumen terkirim: {file_name}")
            return True

        print(f"  Gagal kirim dokumen {file_name}: {result.get('description')}")
        return False

    def _send_telegram_documents(self, bot_token, chat_id, announcements):
        for ann in announcements:
            for doc in self._downloadable_documents(ann):
                file_name = self._document_name(doc) or "dokumen"
                temp_path = self._download_file(self._document_url(doc), file_name)
                if not temp_path:
                    continue

                try:
                    self._send_document(bot_token, chat_id, temp_path, file_name)
                finally:
                    try:
                        os.unlink(temp_path)
                    except OSError:
                        pass

    def _send_telegram(self, items):
        bot_token = self.config.get("telegram_bot_token", "")
        chat_id = self.config.get("telegram_chat_id", "")
        send_files = self.config.get("telegram_send_files", False)

        if not bot_token or not chat_id:
            print("  Telegram bot_token atau chat_id belum dikonfigurasi")
            return

        self._send_summary_message(bot_token, chat_id, items)

        if send_files:
            print("  Mode Telegram: ringkasan + lampiran")
            self._send_telegram_documents(
                bot_token,
                chat_id,
                [item for item in items if self._item_source(item) == "bima"],
            )
        else:
            print("  Mode Telegram: ringkasan saja")
