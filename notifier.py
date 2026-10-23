import mimetypes
import os
import re
import sys
import tempfile
import unicodedata
from datetime import datetime, timedelta, timezone
from html import escape
from html.parser import HTMLParser
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

from app_config import load_config
from reliability import awaiting_youtube_verification

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

WIB = timezone(timedelta(hours=7))


def now_wib():
    return datetime.now(WIB)


def _upload_filename(name):
    # Metadata may contain either Windows or POSIX paths, regardless of this host.
    name = name if isinstance(name, str) else ""
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(char for char in name if not unicodedata.category(char).startswith("C"))
    name = re.sub(r'[<>:"/\\|?*]', "_", name).strip(" .")
    return name or "dokumen"


def _format_rss_publication(value):
    # Presentation only: leave invalid/naive values unchanged, without a guessed zone.
    if not isinstance(value, str):
        return str(value)
    try:
        published = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if published.tzinfo is None or published.utcoffset() is None:
            return value
        local = published.astimezone(ZoneInfo("Asia/Jakarta"))
    except (ValueError, OverflowError, ZoneInfoNotFoundError):
        return value
    months = ("Januari", "Februari", "Maret", "April", "Mei", "Juni",
              "Juli", "Agustus", "September", "Oktober", "November", "Desember")
    return f"{local.day} {months[local.month - 1]} {local.year}, {local:%H:%M} WIB"


def _message_length(text):
    # Conservative bound: count HTML markup/entities and UTF-16 surrogate pairs.
    return len(text.encode("utf-16-le")) // 2


class _SummaryHTMLSplitter(HTMLParser):
    """Split generated b/i markup without breaking tags or escaped characters."""

    def __init__(self, limit):
        super().__init__(convert_charrefs=True)
        self.limit = limit
        self.chunks = []
        self.current = []
        self.tags = []
        self.size = 0
        self.has_text = False

    def _remaining(self):
        return self.limit - self.size - sum(len(f"</{tag}>") for tag in self.tags)

    def _flush(self):
        if self.has_text:
            self.chunks.append("".join(self.current) + "".join(f"</{tag}>" for tag in reversed(self.tags)))
            self.current = [f"<{tag}>" for tag in self.tags]
            self.size = sum(map(len, self.current))
            self.has_text = False

    def handle_starttag(self, tag, attrs):
        if tag not in {"b", "i"} or attrs:
            raise ValueError("Unexpected summary HTML tag")
        if len(f"<{tag}></{tag}>") > self._remaining():
            self._flush()
        self.current.append(f"<{tag}>")
        self.size += len(f"<{tag}>")
        self.tags.append(tag)

    def handle_endtag(self, tag):
        if not self.tags or self.tags.pop() != tag:
            raise ValueError("Unbalanced summary HTML")
        self.current.append(f"</{tag}>")
        self.size += len(f"</{tag}>")

    def _append_text(self, text):
        encoded = escape(text)
        self.current.append(encoded)
        self.size += _message_length(encoded)
        self.has_text = True

    def handle_data(self, data):
        for line in data.splitlines(keepends=True):
            if _message_length(escape(line)) <= self._remaining():
                self._append_text(line)
                continue
            # Prefer intact lines, then intact words; split a single huge word last.
            fresh_capacity = self.limit - sum(len(f"<{t}></{t}>") for t in self.tags)
            if _message_length(escape(line)) <= fresh_capacity:
                self._flush()
                self._append_text(line)
                continue
            for word in re.findall(r"\S+\s*|\s+", line):
                if _message_length(escape(word)) <= self._remaining():
                    self._append_text(word)
                elif _message_length(escape(word)) <= fresh_capacity:
                    self._flush()
                    self._append_text(word)
                else:
                    for char in word:
                        if _message_length(escape(char)) > self._remaining():
                            self._flush()
                        self._append_text(char)


class Notifier:
    def __init__(self, config=None, config_path="config.json"):
        self.config = load_config(config_path=config_path) if config is None else config
        self.last_delivery_receipts = []

    def send_notification(self, items, progress=None, checkpoint=None):
        self.last_delivery_receipts = []
        if any(awaiting_youtube_verification(i)
               for group in (items or [], (progress or {}).get("items", [])) for i in group):
            print("  Notification blocked: YouTube payload awaiting verification")
            return False
        if not items:
            return True

        method = self.config.get("notification_method", "console")

        if method == "telegram":
            return self._send_telegram(items, progress, checkpoint)
        elif method == "console":
            self._send_console(items)
            return False
        elif method == "both":
            self._send_console(items)
            return self._send_telegram(items, progress, checkpoint)
        return False

    def send_youtube_outbox_notification(self, item, ticket):
        """Send one reserved tabs item; generic YouTube delivery remains blocked."""
        self.last_delivery_receipts = []
        try:
            from youtube_delivery import DeliveryTicket
            import hashlib
            import json
            payload = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if (type(ticket) is not DeliveryTicket or ticket.attempt != 1
                    or not re.fullmatch(r"[a-f0-9]{32}", ticket.attempt_token)
                    or ticket.video_id != item.get("video_id")
                    or ticket.event_key != f"youtube:video:{ticket.video_id}:initial"
                    or hashlib.sha256(payload.encode("utf-8")).hexdigest() != ticket.payload_sha256
                    or item.get("source") != "youtube"
                    or item.get("delivery_format") != "youtube-tabs-preview-v1"
                    or item.get("url") != f"https://www.youtube.com/watch?v={ticket.video_id}"
                    or item.get("documents") or self.config.get("notification_method") != "telegram"
                    or self.config.get("youtube_delivery_enabled") is not True
                    or self.config.get("youtube_delivery_send_enabled") is not True
                    or not self.validate_youtube_experiment_destination()
                    or len(self._build_summary_chunks([item])) != 1):
                return False
            bot_token = self.config.get("youtube_experiment_bot_token", "")
            chat_id = self.config.get("youtube_experiment_chat_id", "")
            if not bot_token or not chat_id:
                return False
            return self._send_summary_message(
                bot_token, chat_id, [item], progress={}, checkpoint=lambda: None,
                reserved_youtube=True,
            )
        except (AttributeError, TypeError, ValueError):
            return False

    def validate_youtube_experiment_destination(self):
        """Require a separate, allowlisted experiment ID and reject production ID."""
        import hashlib
        import hmac
        experiment_id = self.config.get("youtube_experiment_chat_id", "")
        allowlisted_hash = self.config.get("youtube_experiment_chat_id_sha256", "")
        if (not isinstance(experiment_id, str) or not experiment_id
                or not isinstance(allowlisted_hash, str)
                or not re.fullmatch(r"[a-f0-9]{64}", allowlisted_hash)):
            return False
        actual_hash = hashlib.sha256(experiment_id.encode("utf-8")).hexdigest()
        production_id = self.config.get("telegram_chat_id", "")
        production_conflict = (os.environ.get("GITHUB_ACTIONS", "").lower() == "true"
            and production_id and hmac.compare_digest(experiment_id, str(production_id)))
        return hmac.compare_digest(actual_hash, allowlisted_hash) and not production_conflict

    def preflight_youtube_experiment_destination(self):
        """Read-only Telegram identity/rights check; never sends a message."""
        if not self.validate_youtube_experiment_destination():
            return False
        token = self.config.get("youtube_experiment_bot_token", "")
        chat_id = self.config.get("youtube_experiment_chat_id", "")
        if not token:
            return False
        base = f"https://api.telegram.org/bot{token}"
        try:
            chat_response = requests.get(base + "/getChat", params={"chat_id": chat_id}, timeout=15)
            chat_result = chat_response.json()
            chat = chat_result.get("result") if isinstance(chat_result, dict) else None
            if (chat_response.status_code != 200 or chat_result.get("ok") is not True
                    or not isinstance(chat, dict) or str(chat.get("id")) != chat_id
                    or chat.get("type") != "channel"):
                return False
            me_response = requests.get(base + "/getMe", timeout=15)
            me_result = me_response.json()
            me = me_result.get("result") if isinstance(me_result, dict) else None
            if (me_response.status_code != 200 or me_result.get("ok") is not True
                    or not isinstance(me, dict) or type(me.get("id")) is not int):
                return False
            member_response = requests.get(base + "/getChatMember", params={
                "chat_id": chat_id, "user_id": me["id"]}, timeout=15)
            member_result = member_response.json()
            member = member_result.get("result") if isinstance(member_result, dict) else None
            if member_response.status_code != 200 or member_result.get("ok") is not True or not isinstance(member, dict):
                return False
            status = member.get("status")
            return status == "creator" or (status == "administrator" and member.get("can_post_messages") is True)
        except (requests.RequestException, ValueError, TypeError, AttributeError):
            return False

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
            channel_title = ann.get("channel_title", "Kemdiktisaintek")
            message = f"<b>{index}. {escape(ann['title'])}</b>\n"
            message += f"   📺 {escape(channel_title)}\n"
            if ann.get("published"):
                message += f"   📆 Publikasi (RSS): {escape(_format_rss_publication(ann['published']))}\n"

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
            title = "▶️ VIDEO BARU — YOUTUBE KEMDIKTISAINTEK"
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
        footer = "━━━━━━━━━━━━━━━━━━━━\n<i>Bot Dikti News</i>"

        message = header + "".join(self._format_html_item(index, ann)
                                  for index, ann in enumerate(items, 1)) + footer
        if _message_length(message) <= 4000:
            return [message]
        splitter = _SummaryHTMLSplitter(limit=4000)
        splitter.feed(message)
        splitter.close()
        if splitter.tags:
            raise ValueError("Unclosed summary HTML")
        splitter._flush()
        return splitter.chunks

    def _send_console(self, items):
        bima_count = sum(1 for item in items if self._item_source(item) == "bima")
        youtube_count = sum(1 for item in items if self._item_source(item) == "youtube")

        print("\n" + "=" * 60)
        if bima_count and youtube_count:
            print("UPDATE BARU BIMA + YOUTUBE KEMDIKTISAINTEK")
        elif youtube_count:
            print("VIDEO BARU — YOUTUBE KEMDIKTISAINTEK")
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
                print(f"  {i}. {ann['title']}")
                if ann.get("channel_title"):
                    print(f"     📺 {ann['channel_title']}")
                
                if ann.get("published"):
                    print(f"     📆 Publikasi (RSS): {_format_rss_publication(ann['published'])}")
                    
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
        temp_path = None
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
            fd, temp_path = tempfile.mkstemp(suffix="-" + safe_name)
            os.close(fd)

            with open(temp_path, "wb") as file:
                total = 0
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        total += len(chunk)
                        if total > max_size_mb * 1024 * 1024:
                            raise ValueError("Attachment exceeds size limit")
                        file.write(chunk)

            return temp_path
        except Exception as e:
            if temp_path and os.path.exists(temp_path):
                os.unlink(temp_path)
            print(f"  Error download file: {type(e).__name__}")
            return None

    def _send_summary_message(self, bot_token, chat_id, items, progress=None, checkpoint=None,
                              reserved_youtube=False):
        if not reserved_youtube and any(awaiting_youtube_verification(i)
               for group in (items or [], (progress or {}).get("items", [])) for i in group):
            print("  Telegram blocked: YouTube payload awaiting verification")
            return False
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        success = True
        progress = progress if progress is not None else {}
        if "chunks" not in progress:
            progress["chunks"] = self._build_summary_chunks(items)
        progress.setdefault("sent_chunks", [])
        checkpoint = checkpoint or (lambda: None)

        for index, text in enumerate(progress["chunks"], 1):
            if index in progress["sent_chunks"]:
                continue
            if not text or _message_length(text) > 4000:
                print(f"  Ringkasan Telegram tidak valid ({index}): panjang payload di luar batas")
                return False
            payload = {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            }

            try:
                response = requests.post(url, json=payload, timeout=15)
                result = response.json()
                if response.status_code == 200 and result.get("ok") is True:
                    message = result.get("result")
                    message_id = message.get("message_id") if isinstance(message, dict) else None
                    if type(message_id) is int and message_id > 0:
                        self.last_delivery_receipts.append({"method": "sendMessage", "http_status": 200,
                            "ok": True, "message_id": message_id})
                    progress["sent_chunks"].append(index)
                    checkpoint()
                    print(f"  Ringkasan Telegram terkirim ({index})")
                    continue

                print(f"  Gagal kirim ringkasan Telegram: HTTP {response.status_code} or ok=false")
                success = False
                break
            except Exception as e:
                print(f"  Error kirim ringkasan Telegram: {type(e).__name__}")
                success = False
                break

        return success

    def _send_document(self, bot_token, chat_id, file_path, file_name):
        url = f"https://api.telegram.org/bot{bot_token}/sendDocument"
        caption = f"📎 {file_name}"
        upload_name = _upload_filename(file_name)
        content_type = mimetypes.guess_type(upload_name)[0] or "application/octet-stream"

        with open(file_path, "rb") as file:
            response = requests.post(
                url,
                data={"chat_id": chat_id, "caption": caption},
                files={"document": (upload_name, file, content_type)},
                timeout=120,
            )

        result = response.json()
        if response.status_code == 200 and result.get("ok") is True:
            print(f"  Dokumen terkirim: {file_name}")
            return True

        print(f"  Gagal kirim dokumen {file_name}: HTTP {response.status_code} or ok=false")
        return False

    def _send_telegram_documents(self, bot_token, chat_id, announcements, progress, checkpoint):
        progress.setdefault("sent_documents", [])
        success = True
        for item_index, ann in enumerate(announcements):
            for doc_index, doc in enumerate(ann.get("documents", [])):
                document_key = f"{item_index}:{doc_index}"
                if document_key in progress["sent_documents"]:
                    continue
                file_name = self._document_name(doc) or "dokumen"
                if not self._document_url(doc):
                    success = False
                    continue
                temp_path = self._download_file(self._document_url(doc), file_name)
                if not temp_path:
                    success = False
                    continue

                try:
                    if self._send_document(bot_token, chat_id, temp_path, file_name):
                        progress["sent_documents"].append(document_key)
                        checkpoint()
                    else:
                        success = False
                except Exception as exc:
                    print(f"  Error kirim dokumen: {type(exc).__name__}")
                    success = False
                finally:
                    try:
                        os.unlink(temp_path)
                    except OSError:
                        pass
        return success

    def _send_telegram(self, items, progress=None, checkpoint=None):
        bot_token = self.config.get("telegram_bot_token", "")
        chat_id = self.config.get("telegram_chat_id", "")
        send_files = self.config.get("telegram_send_files", False)

        if not bot_token or not chat_id:
            print("  Telegram bot_token atau chat_id belum dikonfigurasi")
            return False

        progress = progress if progress is not None else {}
        checkpoint = checkpoint or (lambda: None)
        if not self._send_summary_message(bot_token, chat_id, items, progress, checkpoint):
            return False

        if send_files:
            print("  Mode Telegram: ringkasan + lampiran")
            return self._send_telegram_documents(
                bot_token,
                chat_id,
                [item for item in items if self._item_source(item) == "bima"],
                progress,
                checkpoint,
            )
        else:
            print("  Mode Telegram: ringkasan saja")
        return True
