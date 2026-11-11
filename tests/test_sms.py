"""SMS code reader against a fake Messages database (fictional messages)."""

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from portfolio_agent import sms


def fake_db(path, rows):
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE message (ROWID INTEGER PRIMARY KEY, text TEXT, attributedBody BLOB,"
              " date INTEGER, is_from_me INTEGER)")
    for text, blob, when, mine in rows:
        c.execute("INSERT INTO message (text, attributedBody, date, is_from_me) VALUES (?,?,?,?)",
                  (text, blob, sms._apple_time(when), mine))
    c.commit()
    c.close()


def typedstream(s: str) -> bytes:   # how newer macOS stores message text
    b = s.encode()
    return b"\x04\x0bstreamtyped\x81\xe8\x03\x84\x01@\x84\x84\x84\x08NSString\x01\x94\x84\x01+" \
        + bytes([len(b)]) + b + b"\x86\x84\x02iI"


def test_extract_code_needs_code_words():
    assert sms.extract_code("BofA: Your authorization code is 482915. Never share it.") == "482915"
    assert sms.extract_code("Your balance is 12345 dollars") is None
    assert sms.extract_code("Dinner at 7?") is None


def test_reads_only_new_incoming_messages(tmp_path):
    now = datetime.now(timezone.utc)
    db = tmp_path / "chat.db"
    fake_db(db, [
        ("Old code 111111", None, now - timedelta(minutes=10), 0),           # before the request
        ("My code is 222222", None, now + timedelta(seconds=5), 1),           # sent by me
        (None, typedstream("Your verification code is 333333"), now + timedelta(seconds=8), 0),
    ])
    assert sms.codes_since(now, db) == ["333333"]
    assert sms.wait_for_code(now, timeout_s=3, path=db) == "333333"


def test_unreadable_database_explains_full_disk_access(tmp_path):
    with pytest.raises(sms.SmsUnavailable, match="Full Disk Access"):
        sms.codes_since(datetime.now(timezone.utc), tmp_path / "missing.db")
