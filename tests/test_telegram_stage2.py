"""Synthetic media checks. Live Telegram captions/incoming still need acceptance."""
import os
import unittest

if os.environ.get("CODEX_CRM_ISOLATED_TESTS") != "1":
    raise unittest.SkipTest("Use tests/run_isolated.py")

from datetime import datetime, timedelta
from io import BytesIO
import json
from unittest.mock import AsyncMock, MagicMock, patch
from zipfile import ZipFile
from pathlib import Path

import httpx

from test_telegram_stage1 import TelegramFixture
from app.models import Message, TelegramFile
from app.routers import files
from app.services import telegram as tg, telegram_files as media, delivery_worker as worker
from app.services.file_cache import make_signed_url, store as cache_store

PDF = b"%PDF-1.7\nsynthetic fixture\n%%EOF"


class MediaCase(TelegramFixture):
    def incoming_media(self, *, kind="DOCUMENT", caption="Подпись", size=None):
        payload = self.payload()
        payload["messageContent"] = {"type": kind, "attachment": {
            "url": "https://app.edna.ru/edna.b1/adapter-telegram/6/synthetic.pdf",
            "name": "example.pdf", "size": len(PDF) if size is None else size,
        }, "caption": caption, "text": None, "items": None}
        return payload

    async def test_real_media_structures_deliver_caption_and_file_without_phone(self):
        fixtures = json.loads(Path(__file__).with_name("fixtures").joinpath("telegram_media_incoming.json").read_text(encoding="utf-8"))
        for payload in fixtures:
            content = b"\xff\xd8\xff\xd9" if payload["messageContent"]["type"] == "IMAGE" else PDF
            mime = "image/jpeg" if payload["messageContent"]["type"] == "IMAGE" else "application/pdf"
            self.assertLessEqual(payload["messageContent"]["attachment"]["size"], media.MAX_BYTES)
            # Structure is from real receipts; bytes are synthetic, not the user's documents.
            payload["messageContent"]["attachment"]["size"] = len(content)
            with patch.object(media, "download", AsyncMock(return_value=(content, mime))) as download:
                response = await self.client.post("/incoming/tg-token", json=payload)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(download.call_args.args[1], {"app.edna.ru"})
                await worker._process_one_task()
            sent = self.bitrix.call_args.args[3]["MESSAGES"][0]
            self.assertEqual(sent["message"]["text"], payload["messageContent"]["caption"])
            self.assertEqual(sent["message"]["files"][0]["name"], payload["messageContent"]["attachment"]["name"])
            self.assertTrue(sent["message"]["files"][0]["url"].startswith("https://app.invalid/file/tg-"))
            self.assertNotIn("phone", sent["user"])
            self.assertNotIn("disable_crm", sent["message"])
        self.assertEqual(len(self.rows(tg.RECEIVE)), 4)

    async def test_incoming_retries_download_before_ack_without_duplicate_after_save(self):
        payload = self.incoming_media()
        with patch.object(media, "download", AsyncMock(side_effect=tg.TelegramError("temporary", retryable=True))):
            self.assertEqual((await self.client.post("/incoming/tg-token", json=payload)).status_code, 503)
        self.assertEqual(self.rows(tg.RECEIVE), [])
        with patch.object(media, "download", AsyncMock(return_value=(PDF, "application/pdf"))) as download:
            self.assertEqual((await self.client.post("/incoming/tg-token", json=payload)).status_code, 200)
            self.assertEqual((await self.client.post("/incoming/tg-token", json=payload)).status_code, 200)
            self.assertEqual(download.await_count, 1)
        self.assertEqual(len(self.rows(tg.RECEIVE)), 1)

    async def test_large_or_mismatched_incoming_file_keeps_caption_and_warning(self):
        payload = self.incoming_media(size=media.MAX_BYTES + 1)
        with patch.object(media, "download", AsyncMock()) as download:
            self.assertEqual((await self.client.post("/incoming/tg-token", json=payload)).status_code, 200)
            download.assert_not_awaited()
        await worker._process_one_task()
        sent = self.bitrix.call_args.args[3]["MESSAGES"][0]["message"]
        self.assertIn("Подпись", sent["text"])
        self.assertIn("не удалось", sent["text"])
        self.assertNotIn("files", sent)

    async def test_incoming_media_expired_during_hold_delivers_warning(self):
        with patch.object(media, "download", AsyncMock(return_value=(PDF, "application/pdf"))):
            await self.client.post("/incoming/tg-token", json=self.incoming_media())
        with self.Session() as db:
            db.query(TelegramFile).one().expires_at = datetime.utcnow() - timedelta(seconds=1)
            db.commit()
        media.cleanup(self.Session)
        await worker._process_one_task()
        sent = self.bitrix.call_args.args[3]["MESSAGES"][0]["message"]
        self.assertIn("Подпись", sent["text"])
        self.assertIn("не удалось", sent["text"])
        self.assertNotIn("files", sent)

    async def file_event(self, *, text="", message_id="501", name="contract.pdf"):
        await self.receive()
        await worker._process_one_task()
        event = self.event(message_id=message_id, text=text)
        event["data"]["MESSAGES"][0]["message"]["files"] = [{
            "name": name, "mime": "application/pdf",
            "downloadLink": "https://portal.invalid/pub/im.file.php?FILE_ID=31&SIGN=private-sign",
        }]
        return event

    async def test_file_and_task_saved_atomically_then_sent_from_another_session(self):
        event = await self.file_event(text="Caption")
        with patch.object(media, "download", AsyncMock(return_value=(PDF, "application/pdf"))) as download:
            response = await self.client.post("/handler", json=event)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(download.call_args.args[1], {"portal.invalid"})
        tasks = self.rows(tg.SEND)
        self.assertEqual(tasks[0][1], "pending")
        self.assertNotIn("private-sign", str(tasks))
        with self.Session() as db:
            entry = db.query(TelegramFile).one()
            self.assertEqual(entry.content, PDF)
            self.assertEqual(db.query(Message).filter_by(direction="outgoing").one().content_type, "DOCUMENT")
        with patch.object(tg, "send_media", AsyncMock(return_value={})) as send:
            await worker._process_one_task()
            self.assertEqual(send.await_count, 1)
            self.assertEqual(send.call_args.args[3], "DOCUMENT")
            self.assertTrue(send.call_args.args[4].startswith("https://app.invalid/file/tg-"))
            self.assertEqual(send.call_args.kwargs, {"text": "Caption"})
        self.assertEqual(self.rows(tg.SEND)[0][1], "sent")

    async def test_duplicates_with_refreshed_sign_do_not_download_or_send_again(self):
        event = await self.file_event()
        with patch.object(media, "download", AsyncMock(return_value=(PDF, "application/pdf"))) as download:
            self.assertEqual((await self.client.post("/handler", json=event)).status_code, 200)
            event["data"]["MESSAGES"][0]["message"]["files"][0]["downloadLink"] = "https://portal.invalid/pub/im.file.php?FILE_ID=31&SIGN=changed"
            self.assertEqual((await self.client.post("/handler", json=event)).status_code, 200)
            self.assertEqual(download.await_count, 1)
        self.assertEqual(len(self.rows(tg.SEND)), 1)

    async def test_changed_file_with_same_im_id_is_a_conflict(self):
        event = await self.file_event()
        with patch.object(media, "download", AsyncMock(return_value=(PDF, "application/pdf"))):
            await self.client.post("/handler", json=event)
            event["data"]["MESSAGES"][0]["message"]["files"][0]["name"] = "different.pdf"
            self.assertEqual((await self.client.post("/handler", json=event)).status_code, 422)

    async def test_download_error_does_not_ack_or_keep_partial_batch(self):
        event = await self.file_event()
        event["data"]["MESSAGES"].append(self.event(message_id="502")["data"]["MESSAGES"][0])
        with patch.object(media, "download", AsyncMock(side_effect=tg.TelegramError("media_unavailable", retryable=True))):
            self.assertEqual((await self.client.post("/handler", json=event)).status_code, 503)
        self.assertEqual(self.rows(tg.SEND), [])
        with self.Session() as db:
            self.assertEqual(db.query(TelegramFile).count(), 0)
            self.assertEqual(db.query(Message).filter_by(direction="outgoing").count(), 0)

    async def test_multiple_files_are_rejected_visibly_without_sending_first(self):
        event = await self.file_event()
        event["data"]["MESSAGES"][0]["message"]["files"] *= 2
        with patch.object(media, "download", AsyncMock()) as download:
            self.assertEqual((await self.client.post("/handler", json=event)).status_code, 200)
            download.assert_not_awaited()
        self.assertEqual(self.rows(tg.SEND)[0][1], "failed")
        await worker._process_one_task()
        self.assertIn("по одному", self.bitrix.call_args.args[3]["MESSAGES"][0]["message"]["text"])

    async def test_foreign_source_and_unavailable_file_become_visible_failure(self):
        event = await self.file_event()
        event["data"]["MESSAGES"][0]["message"]["files"][0]["downloadLink"] = "https://attacker.invalid/file.pdf"
        self.assertEqual((await self.client.post("/handler", json=event)).status_code, 200)
        self.assertEqual(self.rows(tg.SEND)[0][1], "failed")
        self.assertEqual(self.rows(tg.NOTICE)[0][2]["reason"], "media")

    async def test_expired_file_never_calls_edna_and_posts_notice(self):
        event = await self.file_event()
        with patch.object(media, "download", AsyncMock(return_value=(PDF, "application/pdf"))):
            await self.client.post("/handler", json=event)
        with self.Session() as db:
            db.query(TelegramFile).one().expires_at = datetime.utcnow() - timedelta(seconds=1)
            db.commit()
        with patch.object(tg, "send_media", AsyncMock()) as send:
            await worker._process_one_task()
            send.assert_not_awaited()
        self.assertEqual(self.rows(tg.SEND)[0][1], "failed")
        self.assertEqual(len(self.rows(tg.NOTICE)), 1)

    async def test_ambiguous_send_is_not_repeated_after_recovery(self):
        event = await self.file_event()
        with patch.object(media, "download", AsyncMock(return_value=(PDF, "application/pdf"))):
            await self.client.post("/handler", json=event)
        with patch.object(tg, "send_media", AsyncMock(side_effect=tg.TelegramError("unknown", uncertain=True))) as send:
            await worker._process_one_task()
            await worker._process_one_task()  # Notice only.
            self.assertEqual(send.await_count, 1)
        self.assertEqual(self.rows(tg.SEND)[0][1], "uncertain")

    async def test_signed_route_supports_restart_expiry_and_legacy_cache(self):
        self.app.include_router(files.router)
        event = await self.file_event()
        with patch.object(media, "download", AsyncMock(return_value=(PDF, "application/pdf"))):
            await self.client.post("/handler", json=event)
        key = self.rows(tg.SEND)[0][2]["file_key"]
        url = make_signed_url(key)
        self.assertEqual((await self.client.get("/file/" + key)).status_code, 403)
        response = await self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, PDF)
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        with patch("app.services.file_cache.time.time", return_value=4102444800):
            self.assertEqual((await self.client.get(url)).status_code, 403)
        legacy = cache_store(b"legacy-image", "image/jpeg", "jpg")
        self.assertEqual((await self.client.get(make_signed_url(legacy))).content, b"legacy-image")


class MediaValidationTests(unittest.IsolatedAsyncioTestCase):
    def test_successful_download_uses_verified_tls_host_with_pinned_public_ip(self):
        class FakeSocket:
            def __init__(self):
                self.sent = []
            def sendall(self, data):
                self.sent.append(data)
            def makefile(self, mode):
                self.stream = BytesIO(b"HTTP/1.1 200 OK\r\nContent-Type: application/pdf\r\nContent-Length: "
                                      + str(len(PDF)).encode() + b"\r\nConnection: close\r\n\r\n" + PDF)
                return self.stream
            def settimeout(self, value):
                if self.stream.closed:
                    raise OSError("closed stream")
            def close(self):
                pass
        socket = FakeSocket()
        context = MagicMock()
        context.wrap_socket.return_value = socket
        with patch.object(media.socket, "getaddrinfo", return_value=[(None, None, None, None, ("8.8.8.8", 443))]), \
             patch.object(media.socket, "create_connection", return_value=socket) as connect, \
             patch.object(media.ssl, "create_default_context", return_value=context):
            self.assertEqual(media._download("https://portal.invalid/file.pdf?SIGN=synthetic", {"portal.invalid"}), (PDF, "application/pdf"))
        self.assertEqual(connect.call_args.args[0], ("8.8.8.8", 443))
        self.assertEqual(context.wrap_socket.call_args.kwargs["server_hostname"], "portal.invalid")
        self.assertIn(b"Host: portal.invalid", b"".join(socket.sent))

    def test_source_rejects_non_https_credentials_foreign_hosts_and_ports(self):
        for url in ("http://portal.invalid/f", "https://portal.invalid:444/f", "https://a@portal.invalid/f",
                    "https://other.invalid/f", "https://portal.invalid/f#fragment", "https://portal.invalid/\nf", None):
            with self.subTest(url=url), self.assertRaises(tg.TelegramError):
                media.source(url, {"portal.invalid"})

    async def test_private_dns_is_rejected_before_connection(self):
        for address in ("127.0.0.1", "10.1.2.3", "169.254.169.254", "::1", "::ffff:127.0.0.1"):
            with patch.object(media.socket, "getaddrinfo", return_value=[(None, None, None, None, (address, 443))]), \
                 patch.object(media, "_PinnedHTTPS") as connection:
                with self.assertRaises(tg.TelegramError):
                    await media.download("https://portal.invalid/f", {"portal.invalid"})
                connection.assert_not_called()

    def test_download_pins_checked_ip_and_rejects_redirect_to_private_origin(self):
        response = MagicMock(status=302)
        response.getheader.return_value = "https://127.0.0.1/private"
        connection = MagicMock()
        connection.getresponse.return_value = response
        with patch.object(media.socket, "getaddrinfo", return_value=[(None, None, None, None, ("8.8.8.8", 443))]), \
             patch.object(media, "_PinnedHTTPS", return_value=connection) as factory:
            with self.assertRaises(tg.TelegramError):
                media._download("https://portal.invalid/f", {"portal.invalid"})
        self.assertEqual(factory.call_args.args[:2], ("portal.invalid", "8.8.8.8"))
        connection.close.assert_called_once()

    def test_stream_rejects_oversize_without_trusting_headers(self):
        response = MagicMock(status=200)
        response.isclosed.return_value = False
        response.getheader.side_effect = lambda name, default=None: {"Content-Type": "application/pdf"}.get(name, default)
        response.read1.side_effect = [b"12345", b"6"]
        connection = MagicMock()
        connection.getresponse.return_value = response
        with patch.object(media, "MAX_BYTES", 5), \
             patch.object(media.socket, "getaddrinfo", return_value=[(None, None, None, None, ("8.8.8.8", 443))]), \
             patch.object(media, "_PinnedHTTPS", return_value=connection):
            with self.assertRaises(tg.TelegramError) as error:
                media._download("https://portal.invalid/f", {"portal.invalid"})
        self.assertEqual(error.exception.code, "media_too_large")

    def test_mime_and_extension_do_not_turn_html_into_an_image_or_pdf(self):
        for name, mime in (("file.png", "image/png"), ("file.pdf", "application/pdf"), ("file.txt", "text/plain")):
            with self.subTest(name=name), self.assertRaises(tg.TelegramError):
                media.inspect_file(b"<html>not a file</html>", mime, name)
        self.assertEqual(media.inspect_file(PDF, "application/octet-stream", "file.pdf")[0], "DOCUMENT")
        with self.assertRaises(tg.TelegramError):
            media.inspect_file(PDF, "image/png", "file.png")

    def test_office_container_and_filename_are_checked(self):
        document = BytesIO()
        with ZipFile(document, "w") as archive:
            archive.writestr("[Content_Types].xml", "synthetic")
            archive.writestr("word/document.xml", "synthetic")
        self.assertEqual(media.inspect_file(document.getvalue(), "application/zip", "contract.docx")[2], "docx")
        for name in ("../contract.pdf", "a\\b.pdf", "a\r.pdf"):
            with self.assertRaises(tg.TelegramError):
                media.inspect_file(PDF, "application/pdf", name)

    async def test_transport_uses_image_document_and_same_caption_field_as_max(self):
        for kind in ("IMAGE", "DOCUMENT"):
            requests = []
            def respond(request):
                requests.append(json.loads(request.content))
                return httpx.Response(200, json={"outMessageId": "saved", "telegramId": "123"})
            client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
            with patch.object(tg.httpx, "AsyncClient", return_value=client):
                await tg.send_media("synthetic", "sender", "123", kind, "https://app.invalid/file/f.pdf", "f.pdf", "saved", text="Caption")
            self.assertEqual(requests[0]["content"], {"type": kind, "url": "https://app.invalid/file/f.pdf", "name": "f.pdf", "text": "Caption"})
