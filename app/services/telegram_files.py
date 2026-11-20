"""Telegram-only downloads and durable media; legacy MAX cache is unchanged."""
import asyncio
import codecs
from datetime import datetime, timedelta
import http.client
import ipaddress
import logging
import re
import socket
import ssl
import struct
import time
from urllib.parse import urljoin, urlsplit
import uuid
from io import BytesIO
from zipfile import ZipFile, BadZipFile

from app.models import TelegramFile
from app.services import telegram as tg
from app.services.file_cache import make_signed_url

MAX_BYTES = 64 * 1024 * 1024
RETENTION = timedelta(days=7)
DOWNLOAD_SECONDS = 120
logger = logging.getLogger(__name__)


def normalize_mime(mime):
    value = mime.split(";", 1)[0].strip().lower() if isinstance(mime, str) else ""
    return {
        "image/jpg": "image/jpeg", "image/pjpeg": "image/jpeg", "image/x-png": "image/png",
        "": "application/octet-stream", "binary/octet-stream": "application/octet-stream",
        "application/binary": "application/octet-stream", "application/x-binary": "application/octet-stream",
    }.get(value, value)


def image_format(content):
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", "png"
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", "jpg"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif", "gif"
    if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return "image/webp", "webp"
    return None


def log_metadata(direction, channel_id, message_id, content, mime, name):
    # No URL, filename, parameters or body bytes in diagnostics.
    declared = mime.split(";", 1)[0].strip().lower() if isinstance(mime, str) else ""
    if not re.fullmatch(r"[a-z0-9.+_-]{1,40}/[a-z0-9.+_-]{1,40}", declared):
        declared = "missing-or-invalid"
    ext = name.rsplit(".", 1)[-1].lower() if isinstance(name, str) and "." in name else ""
    if not re.fullmatch(r"[a-z0-9]{1,10}", ext):
        ext = "other"
    detected = image_format(content)
    logger.info("Telegram %s file metadata: channel=%s message_id=%s declared_mime=%s detected_image=%s extension=%s bytes=%s",
                direction, channel_id, message_id, declared, detected[0] if detected else "not-recognized",
                ext, len(content))


def source(url, allowed_hosts):
    if not isinstance(url, str) or len(url) > 8192:
        raise tg.TelegramError("media_source_not_allowed")
    try:
        endpoint = urlsplit(url)
        valid = (endpoint.scheme == "https" and endpoint.hostname and endpoint.hostname in allowed_hosts
                 and endpoint.port in (None, 443) and not endpoint.username
                 and not endpoint.password and not endpoint.fragment
                 and not any(ord(c) < 33 or ord(c) == 127 for c in url))
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise tg.TelegramError("media_source_not_allowed")
    return endpoint


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host, address, timeout):
        super().__init__(host, timeout=timeout, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        # DNS is resolved once and validated before the connection. TLS still
        # verifies the original hostname, including SNI; no environment proxies.
        raw = socket.create_connection((self.address, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def _download(url, allowed_hosts):
    deadline = time.monotonic() + DOWNLOAD_SECONDS
    for _ in range(4):
        endpoint = source(url, allowed_hosts)
        addresses = {row[4][0] for row in socket.getaddrinfo(endpoint.hostname, 443, type=socket.SOCK_STREAM)}
        if not addresses or any(not ipaddress.ip_address(ip).is_global for ip in addresses):
            raise tg.TelegramError("media_private_address")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise tg.TelegramError("media_download_timeout", retryable=True)
        connection = _PinnedHTTPS(endpoint.hostname, sorted(addresses)[0], remaining)
        try:
            path = endpoint.path or "/"
            if endpoint.query:
                path += "?" + endpoint.query
            connection.request("GET", path, headers={"Accept-Encoding": "identity"})
            active_socket = connection.sock
            response = connection.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                url = urljoin(url, response.getheader("Location") or "")
                continue  # Every redirect repeats the host and IP checks.
            if response.status != 200:
                raise tg.TelegramError("media_download_rejected", retryable=response.status == 429 or response.status >= 500)
            declared = response.getheader("Content-Length")
            if declared and (not declared.isdecimal() or int(declared) > MAX_BYTES):
                raise tg.TelegramError("media_too_large")
            if response.getheader("Content-Encoding", "identity").lower() != "identity":
                raise tg.TelegramError("media_encoding_not_supported")
            chunks, size = [], 0
            while True:
                if response.isclosed():
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise tg.TelegramError("media_download_timeout", retryable=True)
                active_socket.settimeout(remaining)
                chunk = response.read1(min(65536, MAX_BYTES + 1 - size))
                if not chunk:
                    break
                size += len(chunk)
                if size > MAX_BYTES:
                    raise tg.TelegramError("media_too_large")
                chunks.append(chunk)
            if not size or (declared and size != int(declared)):
                raise tg.TelegramError("media_incomplete")
            return b"".join(chunks), response.getheader("Content-Type", "application/octet-stream").split(";")[0].lower()
        finally:
            connection.close()
    raise tg.TelegramError("media_too_many_redirects")


async def download(url, allowed_hosts):
    source(url, allowed_hosts)
    try:
        return await asyncio.to_thread(_download, url, allowed_hosts)
    except tg.TelegramError:
        raise
    except (OSError, http.client.HTTPException, ValueError):
        # Exception text can contain signed source URLs. Never propagate it.
        raise tg.TelegramError("media_download_error", retryable=True) from None


def inspect_file(content, mime, name):
    if not isinstance(content, bytes) or not content or len(content) > MAX_BYTES:
        raise tg.TelegramError("media_empty_or_too_large")
    if not isinstance(name, str) or not name or len(name) > 255 or re.search(r'[\x00-\x1f\x7f/\\<>]', name):
        raise tg.TelegramError("media_invalid_name")
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    mime = normalize_mime(mime)
    detected = image_format(content)
    if detected:
        actual, extension = detected
        if mime not in {actual, "application/octet-stream"} or ext not in {extension, "jpeg" if extension == "jpg" else extension}:
            raise tg.TelegramError("media_type_mismatch")
        return "IMAGE", actual, extension
    if mime.startswith(("image/", "video/", "audio/")) or ext in {"jpg", "jpeg", "png", "gif", "webp", "svg", "mp4", "mp3", "ogg", "wav", "webm", "mov"}:
        raise tg.TelegramError("media_type_not_supported")
    # Initial document set is deliberately explicit. Executables/HTML are not
    # reclassified as documents by changing the filename or webhook MIME.
    if ext == "pdf" and content.startswith(b"%PDF-") and mime in {"application/pdf", "application/octet-stream"}:
        return "DOCUMENT", "application/pdf", "pdf"
    office = {
        "docx": ("word/document.xml", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        "xlsx": ("xl/workbook.xml", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        "pptx": ("ppt/presentation.xml", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
    }
    if ext in office:
        member, actual = office[ext]
        try:
            # Bound central-directory parsing before ZipFile allocates ZipInfo
            # objects. Large/ZIP64 archives are outside this initial document set.
            end = content.rfind(b"PK\x05\x06", max(0, len(content) - 65557))
            if end < 0 or end + 22 > len(content):
                raise ValueError("missing_zip_directory")
            _, disk, cd_disk, count_disk, count, directory_size, offset, _ = struct.unpack_from("<4s4H2LH", content, end)
            if (not content.startswith(b"PK\x03\x04") or disk or cd_disk or not 0 < count <= 4096
                    or count_disk != count or directory_size > 512 * 1024 or offset + directory_size > end):
                raise ValueError("zip_directory_outside_limits")
            with ZipFile(BytesIO(content)) as archive:
                members = set(archive.namelist())
                valid = member in members and "[Content_Types].xml" in members and not any("vbaproject" in item.lower() for item in members)
        except (BadZipFile, ValueError):
            valid = False
        if valid and mime in {actual, "application/zip", "application/octet-stream"}:
            return "DOCUMENT", actual, ext
    legacy = {"doc": "application/msword", "xls": "application/vnd.ms-excel", "ppt": "application/vnd.ms-powerpoint"}
    if ext in legacy and content.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1") and mime in {legacy[ext], "application/octet-stream"}:
        return "DOCUMENT", legacy[ext], ext
    if ext in {"txt", "csv"} and mime in {"text/plain", "text/csv", "application/octet-stream"}:
        try:
            decoder = codecs.getincrementaldecoder("utf-8-sig")()
            for offset in range(0, len(content), 65536):
                if "\x00" in decoder.decode(content[offset:offset + 65536]):
                    raise UnicodeError("binary_text")
            decoder.decode(b"", final=True)
            prefix = content[:1024].decode("utf-8-sig", errors="ignore").lstrip().lower()
        except UnicodeError:
            raise tg.TelegramError("media_text_encoding_not_supported") from None
        if not prefix.startswith(("<!doctype", "<html", "<svg", "<?xml", "<script")):
            return "DOCUMENT", "text/plain" if ext == "txt" else "text/csv", ext
    raise tg.TelegramError("media_type_not_supported")


def store(db, message, content, mime, name):
    media_type, mime, ext = inspect_file(content, mime, name)
    now = datetime.utcnow()
    db.query(TelegramFile).filter(TelegramFile.expires_at <= now).delete(synchronize_session=False)
    key = "tg-%s.%s" % (uuid.uuid4().hex, ext)
    db.add(TelegramFile(key=key, message_id=message.id, content=content, content_type=mime,
                        name=name, expires_at=now + RETENTION))
    return key, media_type


def get(db, key, message_id=None):
    entry = db.get(TelegramFile, key)
    if entry is None or entry.expires_at <= datetime.utcnow() or (message_id is not None and entry.message_id != message_id):
        raise tg.TelegramError("media_file_unavailable")
    return entry


def signed_url(db, key, message_id):
    get(db, key, message_id)
    return make_signed_url(key)


def cleanup(session_factory):
    with session_factory() as db:
        db.query(TelegramFile).filter(TelegramFile.expires_at <= datetime.utcnow()).delete(synchronize_session=False)
        db.commit()
