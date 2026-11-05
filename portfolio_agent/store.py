"""Portfolio store: long-term factual memory, kept in SQLite.

Bitemporal: every snapshot records `as_of_date` (when the value was true) and
`retrieved_at` (when we learned it). Rows are never overwritten; a restated Q2
value is a new row with the same as_of_date and a later retrieved_at. Reads pick
the latest retrieval for each as-of date.

Only snapshots that passed the validator are written here. Everything else goes
to the review queue.
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .schemas import Holding, Snapshot

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id              INTEGER PRIMARY KEY,
    institution     TEXT NOT NULL,
    account         TEXT NOT NULL,
    as_of_date      TEXT NOT NULL,
    retrieved_at    TEXT NOT NULL,
    source          TEXT NOT NULL,
    evidence        TEXT,            -- file path or URL the values came from
    total           REAL NOT NULL,
    statement_total REAL
);
CREATE TABLE IF NOT EXISTS holdings (
    snapshot_id  INTEGER NOT NULL REFERENCES snapshots(id),
    symbol       TEXT,
    description  TEXT NOT NULL,
    quantity     REAL,
    price        REAL,
    value        REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS review_queue (
    id           INTEGER PRIMARY KEY,
    created_at   TEXT NOT NULL,
    institution  TEXT,
    account      TEXT,
    reason       TEXT NOT NULL,
    evidence     TEXT,
    payload      TEXT,               -- the rejected extraction, as JSON
    status       TEXT NOT NULL DEFAULT 'open'
);
CREATE TABLE IF NOT EXISTS playbooks (
    institution  TEXT PRIMARY KEY,
    notes        TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_snap_acct ON snapshots(institution, account, as_of_date);
"""

# Latest retrieval for each (institution, account, as_of_date).
_CURRENT = """
SELECT s.* FROM snapshots s
WHERE s.retrieved_at = (
    SELECT MAX(s2.retrieved_at) FROM snapshots s2
    WHERE s2.institution = s.institution AND s2.account = s.account
      AND s2.as_of_date = s.as_of_date)
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(path or config.DB_PATH)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def write_snapshot(conn, snap: Snapshot, evidence: str | None = None,
                   retrieved_at: str | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO snapshots (institution, account, as_of_date, retrieved_at, source,"
        " evidence, total, statement_total) VALUES (?,?,?,?,?,?,?,?)",
        (snap.institution, snap.account, snap.as_of_date.isoformat(),
         retrieved_at or now_iso(), snap.source, evidence, snap.total, snap.statement_total),
    )
    sid = cur.lastrowid
    conn.executemany(
        "INSERT INTO holdings (snapshot_id, symbol, description, quantity, price, value)"
        " VALUES (?,?,?,?,?,?)",
        [(sid, h.symbol, h.description, h.quantity, h.price, h.value) for h in snap.holdings],
    )
    conn.commit()
    return sid


def last_snapshot(conn, institution: str, account: str) -> Snapshot | None:
    """Most recent as-of date for an account (its latest retrieval)."""
    row = conn.execute(
        f"SELECT * FROM ({_CURRENT}) WHERE institution=? AND account=?"
        " ORDER BY as_of_date DESC LIMIT 1", (institution, account)).fetchone()
    return _to_snapshot(conn, row) if row else None


def current_positions(conn) -> list[dict]:
    """Latest known snapshot per account, with staleness info for display."""
    rows = conn.execute(
        f"SELECT * FROM ({_CURRENT}) c WHERE as_of_date = ("
        "  SELECT MAX(as_of_date) FROM snapshots x"
        "  WHERE x.institution=c.institution AND x.account=c.account)"
        " ORDER BY institution, account").fetchall()
    out = []
    for r in rows:
        snap = _to_snapshot(conn, r)
        out.append({"institution": r["institution"], "account": r["account"],
                    "as_of_date": r["as_of_date"], "retrieved_at": r["retrieved_at"],
                    "total": r["total"],
                    "holdings": [h.model_dump() for h in snap.holdings]})
    return out


def history(conn, account: str | None = None) -> list[dict]:
    """Value over time by as-of date (latest retrieval of each)."""
    sql = f"SELECT institution, account, as_of_date, retrieved_at, total FROM ({_CURRENT})"
    args: tuple = ()
    if account:
        sql += " WHERE account = ?"
        args = (account,)
    sql += " ORDER BY as_of_date, institution, account"
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def add_review_item(conn, reason: str, institution: str | None = None,
                    account: str | None = None, evidence: str | None = None,
                    payload: dict | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO review_queue (created_at, institution, account, reason, evidence, payload)"
        " VALUES (?,?,?,?,?,?)",
        (now_iso(), institution, account, reason, evidence,
         json.dumps(payload, default=str) if payload else None))
    conn.commit()
    return cur.lastrowid


def open_review_items(conn) -> list[dict]:
    rows = conn.execute(
        "SELECT id, created_at, institution, account, reason, evidence"
        " FROM review_queue WHERE status='open' ORDER BY id").fetchall()
    return [dict(r) for r in rows]


def get_playbook(conn, institution: str) -> str | None:
    row = conn.execute("SELECT notes FROM playbooks WHERE institution=?",
                       (institution,)).fetchone()
    return row["notes"] if row else None


def save_playbook(conn, institution: str, notes: str) -> None:
    conn.execute(
        "INSERT INTO playbooks (institution, notes, updated_at) VALUES (?,?,?)"
        " ON CONFLICT(institution) DO UPDATE SET notes=excluded.notes,"
        " updated_at=excluded.updated_at", (institution, notes, now_iso()))
    conn.commit()


def _to_snapshot(conn, row) -> Snapshot:
    hs = conn.execute(
        "SELECT symbol, description, quantity, price, value FROM holdings WHERE snapshot_id=?",
        (row["id"],)).fetchall()
    return Snapshot(institution=row["institution"], account=row["account"],
                    as_of_date=row["as_of_date"], statement_total=row["statement_total"],
                    source=row["source"], holdings=[Holding(**dict(h)) for h in hs])
