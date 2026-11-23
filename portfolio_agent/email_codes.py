"""Verification codes sent by email, read from Gmail over IMAP (spec S19). Code only:
never exposed to the LLM.

The owner chose this: it is the one connection to an outside service, and it only
reads. It signs in to Gmail with an app password (stored encrypted, like portal
logins), opens the mailbox read-only and looks only at messages that arrived after
the portal was asked to send a code and that come from the login's own domains (or
its "code sender" setting). Messages are fetched with PEEK, so they stay unread;
nothing is marked, moved or deleted. Neither the code nor any message text is logged,
traced or stored.
"""

import email
import email.message
import imaplib
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from html import unescape

from . import config, crypto
from .sms import extract_code
from .timeutil import now_iso

# One emailed code expected at a time, so two logins can't swap codes.
EMAIL_LOCK = threading.Lock()
ALL_MAIL = '"[Gmail]/All Mail"'     # covers mail that a filter moved out of the Inbox


class EmailUnavailable(Exception):
    """Gmail can't be read: not set up, wrong app password, or no connection."""


# --- settings (encrypted) ---------------------------------------------------------

def save_settings(conn, address: str, app_password: str) -> None:
    address = address.strip()
    if "@" not in address:
        raise ValueError("Enter the Gmail address that receives the codes.")
    pw = re.sub(r"\s+", "", app_password or "")      # Google shows it in groups of 4
    if len(pw) < 8:
        raise ValueError("Enter the 16-letter app password from your Google account.")
    secret = crypto.encrypt({"address": address, "app_password": pw})
    conn.execute("INSERT INTO app_secrets (name, secret, updated_at) VALUES ('email_reader',?,?)"
                 " ON CONFLICT(name) DO UPDATE SET secret=excluded.secret,"
                 " updated_at=excluded.updated_at", (secret, now_iso()))
    conn.commit()


def get_settings(conn) -> dict | None:
    r = conn.execute("SELECT secret FROM app_secrets WHERE name='email_reader'").fetchone()
    return crypto.decrypt(r[0]) if r else None


def clear_settings(conn) -> None:
    conn.execute("DELETE FROM app_secrets WHERE name='email_reader'")
    conn.commit()


# --- reading ------------------------------------------------------------------------

def _reason(e: Exception) -> str:
    """Google's short reason for a refused sign-in, e.g. "[AUTHENTICATIONFAILED] Invalid
    credentials". It never contains the password."""
    raw = e.args[0] if e.args else ""
    text = raw.decode(errors="ignore") if isinstance(raw, bytes) else str(raw)
    return re.sub(r"\s+", " ", text).strip()[:160] or type(e).__name__


def _connect(settings: dict) -> imaplib.IMAP4_SSL:
    try:
        m = imaplib.IMAP4_SSL(config.IMAP_HOST, config.IMAP_PORT, timeout=30)
    except OSError as e:
        raise EmailUnavailable(f"Couldn't reach Gmail ({type(e).__name__}).") from e
    try:
        m.login(settings["address"], settings["app_password"])
    except imaplib.IMAP4.error as e:
        raise EmailUnavailable(f"Gmail refused the sign-in ({_reason(e)}). Check the address "
                               "and the app password under Accounts & logins.") from e
    for box in (ALL_MAIL, "INBOX"):
        typ, _ = m.select(box, readonly=True)
        if typ == "OK":
            return m
    m.logout()
    raise EmailUnavailable("Couldn't open the Gmail mailbox.")


def check(settings: dict) -> None:
    """Sign in and out once, to confirm the settings work. Reads no messages."""
    _connect(settings).logout()


def sender_matches(from_header: str, senders: list[str]) -> bool:
    """The From address is on one of the login's domains (or contains a sender word)."""
    name, addr = parseaddr(from_header or "")
    addr = addr.lower()
    domain = addr.rsplit("@", 1)[-1]
    for s in senders:
        s = s.strip().lower().lstrip("@.")
        if not s:
            continue
        if "." in s and (domain == s or domain.endswith("." + s)):
            return True
        if "." not in s and (s in addr or s in name.lower()):
            return True
    return False


def message_text(msg: email.message.Message) -> str:
    """Subject plus the body text (plain text, else HTML with the tags removed)."""
    plain, html = [], []
    for part in msg.walk():
        if part.get_content_maintype() != "text" or part.get_filename():
            continue
        try:
            text = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8",
                                                        errors="ignore")
        except Exception:
            continue
        (plain if part.get_content_subtype() == "plain" else html).append(text)
    body = "\n".join(plain) or re.sub(r"<[^>]+>", " ", unescape(
        re.sub(r"(?is)<(style|script)\b.*?</\1>", " ", "\n".join(html))))
    return f"{msg.get('Subject', '')}\n{body}"


def _arrived(meta: bytes) -> datetime | None:
    t = imaplib.Internaldate2tuple(meta)
    return datetime.fromtimestamp(time.mktime(t), timezone.utc) if t else None


def codes_since(settings: dict, since: datetime, senders: list[str]) -> list[str]:
    """Codes in messages from the senders that arrived after `since`, newest first."""
    m = _connect(settings)
    try:
        day = (since - timedelta(days=1)).strftime("%d-%b-%Y")
        typ, data = m.uid("SEARCH", None, "SINCE", day)
        uids = (data[0] or b"").split()[-30:] if typ == "OK" else []
        out = []
        for uid in reversed(uids):          # newest first
            typ, head = m.uid("FETCH", uid, "(INTERNALDATE BODY.PEEK[HEADER.FIELDS (FROM)])")
            if typ != "OK" or not head or not isinstance(head[0], tuple):
                continue
            arrived = _arrived(head[0][0])
            if arrived is None or arrived < since:
                break                      # older from here on
            if not sender_matches(head[0][1].decode(errors="ignore").split(":", 1)[-1], senders):
                continue
            typ, full = m.uid("FETCH", uid, "(BODY.PEEK[])")
            if typ != "OK" or not full or not isinstance(full[0], tuple):
                continue
            code = extract_code(message_text(email.message_from_bytes(full[0][1])))
            if code:
                out.append(code)
        return out
    finally:
        try:
            m.logout()
        except Exception:
            pass


def wait_for_code(settings: dict, since: datetime, senders: list[str],
                  timeout_s: int | None = None, should_stop=lambda: False) -> str | None:
    """Poll Gmail for a code that arrived after `since`. None on timeout."""
    deadline = time.monotonic() + (timeout_s or config.EMAIL_WAIT_S)
    while time.monotonic() < deadline:
        if should_stop():
            return None
        codes = codes_since(settings, since, senders)
        if codes:
            return codes[0]
        time.sleep(5)
    return None
