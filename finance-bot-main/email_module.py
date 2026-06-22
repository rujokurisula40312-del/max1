"""IMAP poller: forwards new emails matching a sender filter to Telegram."""
import os
import asyncio
import logging
import imaplib
import email
import email.utils
from email.header import decode_header
from typing import Iterable

from maxapi.types.input_media import InputMediaBuffer

logger = logging.getLogger(__name__)

IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993
POLL_INTERVAL_SEC = 60
TG_TEXT_LIMIT = 3500
ATTACH_LIMIT_BYTES = 45 * 1024 * 1024

# Hardcoded — Railway env scope flaky for these. Password stays in env to keep
# the secret out of git.
EMAIL_USER_HARDCODED = "persefona.ya1@gmail.com"
EMAIL_SENDER_FILTER_HARDCODED = "@cruclub.ru"


def _decode(value) -> str:
    if not value:
        return ""
    parts = decode_header(value)
    out = []
    for chunk, enc in parts:
        if isinstance(chunk, bytes):
            try:
                out.append(chunk.decode(enc or "utf-8", errors="replace"))
            except LookupError:
                out.append(chunk.decode("utf-8", errors="replace"))
        else:
            out.append(chunk)
    return "".join(out).strip()


def _strip_html(html: str) -> str:
    """Убирает HTML-теги и возвращает чистый текст."""
    import re
    text = re.sub(r"<style[^>]*>.*?</style>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<script[^>]*>.*?</script>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</p>|</div>|</tr>|</li>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&quot;", '"', text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _looks_like_html(text: str) -> bool:
    import re
    return bool(re.search(r"<(div|html|body|table|td|p|span|style)\b", text, re.IGNORECASE))


def _extract_text(msg: email.message.Message) -> str:
    if msg.is_multipart():
        plain = None
        html = None
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition") or "")
            if "attachment" in disp:
                continue
            if ctype == "text/plain" and plain is None:
                payload = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                plain = payload.decode(charset, errors="replace")
            elif ctype == "text/html" and html is None:
                payload = part.get_payload(decode=True) or b""
                charset = part.get_content_charset() or "utf-8"
                html = payload.decode(charset, errors="replace")
        # если plain выглядит как HTML — игнорируем его, берём html-версию
        if plain and not _looks_like_html(plain):
            return plain
        if html:
            return _strip_html(html)
        if plain:
            return _strip_html(plain)
        return ""
    payload = msg.get_payload(decode=True) or b""
    charset = msg.get_content_charset() or "utf-8"
    raw = payload.decode(charset, errors="replace")
    if _looks_like_html(raw):
        return _strip_html(raw)
    return raw


def _extract_attachments(msg: email.message.Message):
    out = []
    if not msg.is_multipart():
        return out
    for part in msg.walk():
        disp = str(part.get("Content-Disposition") or "")
        if "attachment" not in disp.lower() and "inline" not in disp.lower():
            continue
        filename = _decode(part.get_filename()) or "attachment.bin"
        data = part.get_payload(decode=True)
        if not data:
            continue
        if len(data) > ATTACH_LIMIT_BYTES:
            logger.warning(f"Skip attachment {filename}: {len(data)} bytes > limit")
            continue
        out.append((filename, data))
    return out


def _matches_sender(from_header: str, filters: Iterable[str]) -> bool:
    if not from_header:
        return False
    addr = email.utils.parseaddr(from_header)[1].lower()
    for f in filters:
        f = f.strip().lower()
        if not f:
            continue
        if f.startswith("@"):
            if addr.endswith(f):
                return True
        elif addr == f or addr.endswith("@" + f):
            return True
    return False


def _fetch_new(user: str, password: str, filters: list[str]):
    """Connects, finds unseen messages from filters, fetches them, marks as seen.
    Returns list of dicts: {from, subject, date, text, attachments: [(name, bytes)]}.
    """
    results = []
    conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    try:
        conn.login(user, password)
        conn.select("INBOX")
        typ, data = conn.search(None, "UNSEEN")
        if typ != "OK":
            logger.warning(f"IMAP search failed: {typ}")
            return results
        ids = data[0].split() if data and data[0] else []
        for msg_id in ids:
            typ, msg_data = conn.fetch(msg_id, "(RFC822)")
            if typ != "OK" or not msg_data or not msg_data[0]:
                continue
            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)
            from_h = _decode(msg.get("From"))
            if not _matches_sender(from_h, filters):
                # leave foreign messages UNSEEN — restore the flag
                conn.store(msg_id, "-FLAGS", "\\Seen")
                continue
            results.append({
                "from": from_h,
                "subject": _decode(msg.get("Subject")) or "(без темы)",
                "date": _decode(msg.get("Date")),
                "text": _extract_text(msg),
                "attachments": _extract_attachments(msg),
            })
            # IMAP fetch with RFC822 already marks as Seen — keep it.
        return results
    finally:
        try:
            conn.close()
        except Exception:
            pass
        try:
            conn.logout()
        except Exception:
            pass


async def _send_to_telegram(bot, chat_ids: list[int], item: dict):
    text = item.get("text") or ""
    if len(text) > TG_TEXT_LIMIT:
        text = text[:TG_TEXT_LIMIT] + "\n…(обрезано)"
    header = (
        f"📧 <b>Новое письмо</b>\n"
        f"<b>От:</b> {_html_escape(item['from'])}\n"
        f"<b>Тема:</b> {_html_escape(item['subject'])}\n"
        f"<b>Дата:</b> {_html_escape(item['date'])}"
    )
    body = f"{header}\n\n{_html_escape(text)}" if text.strip() else header
    for chat_id in chat_ids:
        try:
            await bot.send_message(user_id=chat_id, text=body)
        except Exception as e:
            logger.error(f"send_message {chat_id}: {e}")
            continue
        for fname, data in item["attachments"]:
            try:
                buf = InputMediaBuffer(buffer=data, filename=fname)
                await bot.send_message(user_id=chat_id, attachments=[buf])
            except Exception as e:
                logger.error(f"send_document {fname} -> {chat_id}: {e}")


def _html_escape(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


async def email_poll_loop(bot, chat_ids: list[int]):
    user = os.environ.get("EMAIL_USER", "").strip() or EMAIL_USER_HARDCODED
    filt_raw = os.environ.get("EMAIL_SENDER_FILTER", "").strip() or EMAIL_SENDER_FILTER_HARDCODED
    password = os.environ.get("EMAIL_PASSWORD", "").strip()
    if not password:
        env_keys = sorted([k for k in os.environ.keys() if "MAIL" in k.upper()])
        logger.info(
            f"Email poller disabled: EMAIL_PASSWORD env var is empty. "
            f"Mail-related env keys present: {env_keys}"
        )
        return
    filters = [f.strip() for f in filt_raw.split(",") if f.strip()]
    logger.info(f"Email poller started: user={user}, filters={filters}, chats={chat_ids}")
    loop = asyncio.get_event_loop()
    while True:
        try:
            items = await loop.run_in_executor(None, _fetch_new, user, password, filters)
            if items:
                logger.info(f"Email poller: {len(items)} new message(s)")
            for item in items:
                await _send_to_telegram(bot, chat_ids, item)
        except Exception as e:
            logger.error(f"Email poll error: {e}")
        await asyncio.sleep(POLL_INTERVAL_SEC)


def start_email_poller(bot, chat_ids: list[int]):
    """Schedule the poller as a background task. Safe to call once at startup."""
    asyncio.create_task(email_poll_loop(bot, chat_ids))


def fetch_recent_senders(n: int = 10) -> list[str]:
    """Возвращает адреса отправителей последних N писем (все, без фильтра)."""
    user = os.environ.get("EMAIL_USER", "").strip() or EMAIL_USER_HARDCODED
    password = os.environ.get("EMAIL_PASSWORD", "").strip()
    if not password:
        return ["❌ EMAIL_PASSWORD не задан"]
    results = []
    try:
        conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
        conn.login(user, password)
        conn.select("INBOX")
        typ, data = conn.search(None, "ALL")
        ids = data[0].split() if data and data[0] else []
        for msg_id in ids[-n:]:
            typ, msg_data = conn.fetch(msg_id, "(RFC822.HEADER)")
            if typ != "OK" or not msg_data or not msg_data[0]:
                continue
            raw = msg_data[0][1]
            msg = email.message_from_bytes(raw)
            from_h = _decode(msg.get("From", ""))
            subj = _decode(msg.get("Subject", ""))[:50]
            results.append(f"{from_h} | {subj}")
        conn.logout()
    except Exception as e:
        results.append(f"❌ Ошибка: {e}")
    return results or ["Писем не найдено"]
