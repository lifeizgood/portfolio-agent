"""SMS 2FA code reader (spec §1.1, §9.2, D2). Code only: never exposed to the LLM.

Reads the Messages database (~/Library/Messages/chat.db) read-only, and only
messages received after the moment a login asked the portal to send a code
(a time window: senders can't be identified reliably). It never marks, archives
or deletes a message. The code itself is returned to the caller and never logged.

macOS requires Full Disk Access for the app that runs this (Terminal, iTerm,
VS Code...): System Settings → Privacy & Security → Full Disk Access.
"""

import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config

APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
CODE = re.compile(r"(?<![\d$.,])(\d{4,8})(?![\d,]|\.\d)")
CODE_WORDS = re.compile(r"code|passcode|verification|authoriz|one[- ]time|otp|security", re.I)

# Spec §9.2: one SMS code expected at a time, so two logins can't swap codes.
SMS_LOCK = threading.Lock()


class SmsUnavailable(Exception):
    """The Messages database can't be read (usually: no Full Disk Access)."""


def _apple_time(dt: datetime) -> int:
    return int((dt - APPLE_EPOCH).total_seconds() * 1_000_000_000)   # nanoseconds


def _decode_attributed(blob: bytes | None) -> str | None:
    """Newer macOS keeps the text only in `attributedBody` (a typedstream)."""
    if not blob or b"NSString" not in blob:
        return None
    try:
        rest = blob.split(b"NSString", 1)[1][5:]
        if rest[0] == 0x81:
            n, rest = int.from_bytes(rest[1:3], "little"), rest[3:]
        else:
            n, rest = rest[0], rest[1:]
        return rest[:n].decode("utf-8", errors="ignore")
    except Exception:
        return None


def extract_code(text: str) -> str | None:
    """The 4–8 digit code in a message that talks about a code, else None."""
    if not text or not CODE_WORDS.search(text):
        return None
    m = CODE.search(text)
    return m.group(1) if m else None


def _open(path: Path) -> sqlite3.Connection:
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5)
        conn.execute("SELECT 1 FROM message LIMIT 1")
        return conn
    except sqlite3.Error as e:
        raise SmsUnavailable(
            f"Can't read the Messages database ({e}). Give Full Disk Access to the app "
            "running `serve` (System Settings → Privacy & Security → Full Disk Access), "
            "then restart it.") from e


def codes_since(since: datetime, path: Path | None = None) -> list[str]:
    """Codes in messages received after `since`, newest first."""
    conn = _open(Path(path or config.MESSAGES_DB))
    try:
        rows = conn.execute(
            "SELECT text, attributedBody FROM message WHERE is_from_me = 0 AND date >= ?"
            " ORDER BY date DESC LIMIT 20", (_apple_time(since),)).fetchall()
    finally:
        conn.close()
    out = []
    for text, blob in rows:
        code = extract_code(text or _decode_attributed(blob) or "")
        if code:
            out.append(code)
    return out


def wait_for_code(since: datetime, timeout_s: int | None = None,
                  path: Path | None = None, should_stop=lambda: False) -> str | None:
    """Poll for a code that arrived after `since`. None on timeout."""
    deadline = time.monotonic() + (timeout_s or config.SMS_WAIT_S)
    while time.monotonic() < deadline:
        if should_stop():
            return None
        codes = codes_since(since, path)
        if codes:
            return codes[0]
        time.sleep(2)
    return None
