"""Portfolio store: long-term factual memory, kept in SQLite (spec §5).

Bitemporal with time of day: every snapshot records `as_of_datetime` (when the
value was true) and `retrieved_at` (when we learned it, i.e. the spec's
`retrieved_at_datetime`), both UTC ISO text. Rows are never overwritten: a
Refresh on the same day is a new row, and a restatement is a new row with the
same as-of and a later retrieved_at. Reads pick the latest retrieval for each
as-of. Out-of-order arrival is fine (spec C4).

Only snapshots that passed the validator (and, when held, were approved) are
written here. Everything else waits in the inbox (`review_queue`).
"""

import json
import os
import sqlite3
from pathlib import Path

from . import config
from .schemas import Holding, Snapshot
from .timeutil import now_iso, to_iso

SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
    id              INTEGER PRIMARY KEY,
    institution     TEXT NOT NULL,
    account         TEXT NOT NULL,
    as_of_date      TEXT NOT NULL,   -- local date of as_of_datetime, for display
    retrieved_at    TEXT NOT NULL,   -- retrieved_at_datetime, UTC ISO
    source          TEXT NOT NULL,
    evidence        TEXT,            -- file or folder the values came from (View Source)
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
    payload      TEXT,               -- e.g. the rejected or held extraction, as JSON
    status       TEXT NOT NULL DEFAULT 'open'
);
CREATE TABLE IF NOT EXISTS playbooks (
    institution  TEXT PRIMARY KEY,
    notes        TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS accounts (
    id               INTEGER PRIMARY KEY,
    display_name     TEXT NOT NULL UNIQUE,
    institution      TEXT NOT NULL,
    account_type     TEXT NOT NULL DEFAULT 'brokerage',
    access_method    TEXT NOT NULL DEFAULT 'browser',
    login_url        TEXT,
    allowed_domains  TEXT,            -- comma-separated; browser allowlist
    twofa_method     TEXT NOT NULL DEFAULT 'remembered_device',
    cadence          TEXT NOT NULL DEFAULT 'nightly',
    holds_public     INTEGER NOT NULL DEFAULT 1,
    status           TEXT NOT NULL DEFAULT 'active',
    closed_on        TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS logins (
    id               INTEGER PRIMARY KEY,
    name             TEXT NOT NULL UNIQUE,  -- e.g. "Merrill Lynch"
    institution      TEXT NOT NULL,
    access_method    TEXT NOT NULL DEFAULT 'browser',
    login_url        TEXT,
    allowed_domains  TEXT,            -- comma-separated; browser allowlist
    twofa_method     TEXT NOT NULL DEFAULT 'remembered_device',
    cadence          TEXT NOT NULL DEFAULT 'nightly',
    holds_public     INTEGER NOT NULL DEFAULT 1,
    status           TEXT NOT NULL DEFAULT 'active',
    closed_on        TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS login_credentials (
    login_id     INTEGER PRIMARY KEY REFERENCES logins(id),
    secret       BLOB NOT NULL,       -- encrypted; see crypto.py
    updated_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS credentials (     -- pre-split; migrated to login_credentials
    account_id   INTEGER PRIMARY KEY REFERENCES accounts(id),
    secret       BLOB NOT NULL,       -- encrypted; see crypto.py
    updated_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id           INTEGER PRIMARY KEY,
    kind         TEXT NOT NULL,        -- nightly | refresh
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT NOT NULL DEFAULT 'running',
    summary      TEXT
);
CREATE TABLE IF NOT EXISTS run_items (
    id           INTEGER PRIMARY KEY,
    run_id       INTEGER NOT NULL REFERENCES runs(id),
    account_id   INTEGER,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    outcome      TEXT,                 -- stored | held | review | stopped | error
    detail       TEXT,
    trace        TEXT                  -- JSON list of steps; never contains secrets
);
CREATE TABLE IF NOT EXISTS login_attempts (
    id           INTEGER PRIMARY KEY,
    account_id   INTEGER NOT NULL,
    run_kind     TEXT NOT NULL,
    at           TEXT NOT NULL
);
"""

# Columns added after the first release; added in place on older databases.
_MIGRATIONS = {
    "snapshots": {"as_of_datetime": "TEXT", "account_id": "INTEGER", "run_id": "INTEGER"},
    "review_queue": {"kind": "TEXT NOT NULL DEFAULT 'review'", "account_id": "INTEGER",
                     "answer": "TEXT", "resolved_at": "TEXT", "login_id": "INTEGER"},
    "playbooks": {"steps": "TEXT"},
    # One login holds many accounts; each account keeps its own history.
    "accounts": {"login_id": "INTEGER", "portal_key": "TEXT", "portal_name": "TEXT",
                 "portal_suffix": "TEXT", "category": "TEXT",
                 "is_liability": "INTEGER NOT NULL DEFAULT 0"},   # stored as negative values
    # Per-login rules: which listed accounts to skip, which are debts.
    "logins": {"skip_categories": "TEXT", "liability_categories": "TEXT",
               "skip_other_institutions": "INTEGER NOT NULL DEFAULT 0"},
    "run_items": {"login_id": "INTEGER", "evidence": "TEXT"},
    "login_attempts": {"login_id": "INTEGER"},
    "runs": {"pid": "INTEGER"},
    "holdings": {"price_as_of": "TEXT"},   # a holding's own as-of date, when it has one       # process doing the run, to spot orphaned runs
}

# One account = registry id when known, else institution/name (file ingests).
_KEY = "COALESCE('id:' || {t}.account_id, 'n:' || {t}.institution || '/' || {t}.account)"

# Latest retrieval for each (account, as_of_datetime).
_CURRENT = f"""
SELECT s.*, {_KEY.format(t='s')} AS acct_key FROM snapshots s
WHERE s.id = (
    SELECT s2.id FROM snapshots s2
    WHERE {_KEY.format(t='s2')} = {_KEY.format(t='s')}
      AND s2.as_of_datetime = s.as_of_datetime
    ORDER BY s2.retrieved_at DESC, s2.id DESC LIMIT 1)   -- exactly one row, even on ties
"""


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    path = Path(path or config.DB_PATH)
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn) -> None:
    for table, cols in _MIGRATIONS.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for col, decl in cols.items():
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
    # Rows written before timestamps existed: midnight UTC of their as-of date.
    conn.execute("UPDATE snapshots SET as_of_datetime = as_of_date || 'T00:00:00+00:00'"
                 " WHERE as_of_datetime IS NULL")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_snap_acct"
                 " ON snapshots(institution, account, as_of_datetime)")
    _split_logins(conn)
    conn.commit()


def _split_logins(conn) -> None:
    """Pre-split databases kept login details on the account row. Each such row
    becomes a login with the same id. Rows that never stored a value were really
    logins, so they are removed; their accounts are rediscovered on the next fetch."""
    old = conn.execute("SELECT * FROM accounts WHERE login_id IS NULL AND login_url IS NOT NULL"
                       ).fetchall()
    for a in old:
        conn.execute(
            "INSERT OR IGNORE INTO logins (id, name, institution, access_method, login_url,"
            " allowed_domains, twofa_method, cadence, holds_public, status, closed_on,"
            " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (a["id"], a["display_name"], a["institution"], a["access_method"], a["login_url"],
             a["allowed_domains"], a["twofa_method"], a["cadence"], a["holds_public"],
             a["status"], a["closed_on"], a["created_at"], a["updated_at"]))
        conn.execute("INSERT OR IGNORE INTO login_credentials (login_id, secret, updated_at)"
                     " SELECT account_id, secret, updated_at FROM credentials WHERE account_id=?",
                     (a["id"],))
        conn.execute("UPDATE login_attempts SET login_id=account_id WHERE account_id=?"
                     " AND login_id IS NULL", (a["id"],))
        conn.execute("UPDATE run_items SET login_id=account_id WHERE account_id=?"
                     " AND login_id IS NULL", (a["id"],))
        conn.execute("UPDATE review_queue SET login_id=? WHERE account_id=?", (a["id"], a["id"]))
        if conn.execute("SELECT 1 FROM snapshots WHERE account_id=?", (a["id"],)).fetchone():
            conn.execute("UPDATE accounts SET login_id=?, login_url=NULL WHERE id=?",
                         (a["id"], a["id"]))
        else:
            conn.execute("UPDATE review_queue SET account_id=NULL WHERE account_id=?", (a["id"],))
            conn.execute("DELETE FROM credentials WHERE account_id=?", (a["id"],))
            conn.execute("DELETE FROM accounts WHERE id=?", (a["id"],))


# --- snapshots ---------------------------------------------------------------

def write_snapshot(conn, snap: Snapshot, evidence: str | None = None,
                   retrieved_at: str | None = None, run_id: int | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO snapshots (institution, account, account_id, as_of_date, as_of_datetime,"
        " retrieved_at, source, evidence, total, statement_total, run_id)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (snap.institution, snap.account, snap.account_id, snap.as_of_date.isoformat(),
         to_iso(snap.as_of_datetime), retrieved_at or now_iso(), snap.source, evidence,
         snap.total, snap.statement_total, run_id),
    )
    sid = cur.lastrowid
    conn.executemany(
        "INSERT INTO holdings (snapshot_id, symbol, description, quantity, price, value,"
        " price_as_of) VALUES (?,?,?,?,?,?,?)",
        [(sid, h.symbol, h.description, h.quantity, h.price, h.value,
          h.price_as_of.isoformat() if h.price_as_of else None) for h in snap.holdings],
    )
    conn.commit()
    return sid


def _key_args(snap: Snapshot) -> tuple[str, tuple]:
    if snap.account_id is not None:
        return "account_id = ?", (snap.account_id,)
    return "account_id IS NULL AND institution = ? AND account = ?", (snap.institution, snap.account)


def last_snapshot(conn, institution: str, account: str,
                  account_id: int | None = None) -> Snapshot | None:
    """Latest as-of for an account (its latest retrieval)."""
    probe = Snapshot(institution=institution, account=account, account_id=account_id,
                     as_of_datetime="2000-01-01T00:00:00+00:00", holdings=[])
    where, args = _key_args(probe)
    row = conn.execute(f"SELECT * FROM ({_CURRENT}) WHERE {where}"
                       " ORDER BY as_of_datetime DESC LIMIT 1", args).fetchone()
    return _to_snapshot(conn, row) if row else None


def previous_snapshot(conn, snap: Snapshot) -> Snapshot | None:
    """Nearest earlier accepted value for the same account (spec D19)."""
    where, args = _key_args(snap)
    row = conn.execute(f"SELECT * FROM ({_CURRENT}) WHERE {where} AND as_of_datetime < ?"
                       " ORDER BY as_of_datetime DESC LIMIT 1",
                       (*args, to_iso(snap.as_of_datetime))).fetchone()
    return _to_snapshot(conn, row) if row else None


def has_any_snapshot(conn, snap: Snapshot) -> bool:
    where, args = _key_args(snap)
    return conn.execute(f"SELECT 1 FROM snapshots WHERE {where} LIMIT 1", args).fetchone() is not None


def current_positions(conn) -> list[dict]:
    """Latest known snapshot per account, with its timestamps for display."""
    rows = conn.execute(
        f"SELECT * FROM ({_CURRENT}) c WHERE as_of_datetime = ("
        f"  SELECT MAX(as_of_datetime) FROM snapshots x WHERE {_KEY.format(t='x')} = c.acct_key)"
        " ORDER BY institution, account").fetchall()
    out = []
    for r in rows:
        snap = _to_snapshot(conn, r)
        out.append({"snapshot_id": r["id"], "account_id": r["account_id"],
                    "institution": r["institution"], "account": r["account"],
                    "as_of_date": r["as_of_date"], "as_of_datetime": r["as_of_datetime"],
                    "retrieved_at": r["retrieved_at"], "source": r["source"],
                    "total": r["total"], "has_evidence": bool(r["evidence"]),
                    "holdings": [h.model_dump() for h in snap.holdings]})
    return out


def history(conn, account: str | None = None) -> list[dict]:
    """Value over time by as-of (latest retrieval of each)."""
    sql = (f"SELECT id AS snapshot_id, acct_key, account_id, institution, account, as_of_date,"
           f" as_of_datetime, retrieved_at, total FROM ({_CURRENT})")
    args: tuple = ()
    if account:
        sql += " WHERE account = ?"
        args = (account,)
    sql += " ORDER BY as_of_datetime, institution, account"
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def history_known_on(conn, known_on_iso: str) -> list[dict]:
    """History using only values retrieved by a past moment ("as known on")."""
    rows = conn.execute(
        f"SELECT s.id AS snapshot_id, {_KEY.format(t='s')} AS acct_key, s.account_id,"
        " s.institution, s.account, s.as_of_date, s.as_of_datetime, s.retrieved_at, s.total"
        " FROM snapshots s WHERE s.id = ("
        "  SELECT s2.id FROM snapshots s2"
        f"  WHERE {_KEY.format(t='s2')} = {_KEY.format(t='s')}"
        "    AND s2.as_of_datetime = s.as_of_datetime AND s2.retrieved_at <= ?"
        "  ORDER BY s2.retrieved_at DESC, s2.id DESC LIMIT 1)"
        " ORDER BY s.as_of_datetime", (known_on_iso,)).fetchall()
    return [dict(r) for r in rows]


def get_snapshot(conn, snapshot_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM snapshots WHERE id=?", (snapshot_id,)).fetchone()
    if not row:
        return None
    snap = _to_snapshot(conn, row)
    return {**dict(row), "holdings": [h.model_dump() for h in snap.holdings]}


def _to_snapshot(conn, row) -> Snapshot:
    hs = conn.execute(
        "SELECT symbol, description, quantity, price, value, price_as_of FROM holdings"
        " WHERE snapshot_id=?",
        (row["id"],)).fetchall()
    return Snapshot(institution=row["institution"], account=row["account"],
                    account_id=row["account_id"],
                    as_of_datetime=row["as_of_datetime"] or row["as_of_date"],
                    statement_total=row["statement_total"],
                    source=row["source"], holdings=[Holding(**dict(h)) for h in hs])


# --- inbox (review queue) ----------------------------------------------------
# kinds: review (rejected twice) | held_value | question | twofa_code |
#        playbook_proposal | registry_suggestion | alert

def add_inbox_item(conn, kind: str, reason: str, institution: str | None = None,
                   account: str | None = None, account_id: int | None = None,
                   evidence: str | None = None, payload: dict | None = None,
                   login_id: int | None = None) -> int:
    cur = conn.execute(
        "INSERT INTO review_queue (created_at, kind, institution, account, account_id, login_id,"
        " reason, evidence, payload) VALUES (?,?,?,?,?,?,?,?,?)",
        (now_iso(), kind, institution, account, account_id, login_id, reason, evidence,
         json.dumps(payload, default=str) if payload else None))
    conn.commit()
    return cur.lastrowid


def add_review_item(conn, reason: str, institution: str | None = None,
                    account: str | None = None, evidence: str | None = None,
                    payload: dict | None = None, account_id: int | None = None,
                    login_id: int | None = None) -> int:
    return add_inbox_item(conn, "review", reason, institution, account, account_id,
                          evidence, payload, login_id)


def expire_stale_code_requests(conn) -> None:
    """A verification-code request is only useful while its run waits for it."""
    from datetime import timedelta

    from .timeutil import now

    cutoff = to_iso(now() - timedelta(seconds=config.TWOFA_WAIT_S + 60))
    conn.execute("UPDATE review_queue SET status='expired', resolved_at=? WHERE status='open'"
                 " AND kind='twofa_code' AND created_at < ?", (now_iso(), cutoff))
    conn.commit()


def open_review_items(conn, registered_only: bool = False) -> list[dict]:
    """registered_only: hide items not tied to a login or account (e.g. sample
    statements ingested from the command line for testing)."""
    expire_stale_code_requests(conn)
    where = " AND (login_id IS NOT NULL OR account_id IS NOT NULL)" if registered_only else ""
    rows = conn.execute(
        "SELECT id, created_at, kind, institution, account, account_id, login_id, reason,"
        f" evidence, payload FROM review_queue WHERE status='open'{where} ORDER BY id").fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["payload"] = json.loads(d["payload"]) if d["payload"] else None
        out.append(d)
    return out


def get_inbox_item(conn, item_id: int) -> dict | None:
    r = conn.execute("SELECT * FROM review_queue WHERE id=?", (item_id,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["payload"] = json.loads(d["payload"]) if d["payload"] else None
    return d


def resolve_inbox_item(conn, item_id: int, status: str) -> None:
    conn.execute("UPDATE review_queue SET status=?, resolved_at=?, answer=NULL WHERE id=?",
                 (status, now_iso(), item_id))
    conn.commit()


def set_answer(conn, item_id: int, answer: str) -> None:
    conn.execute("UPDATE review_queue SET answer=? WHERE id=? AND status='open'", (answer, item_id))
    conn.commit()


def take_answer(conn, item_id: int) -> str | None:
    """Read an answer once and clear it (2FA codes must not linger in the DB)."""
    r = conn.execute("SELECT answer FROM review_queue WHERE id=?", (item_id,)).fetchone()
    if not r or not r["answer"]:
        return None
    conn.execute("UPDATE review_queue SET answer=NULL, status='answered', resolved_at=?"
                 " WHERE id=?", (now_iso(), item_id))
    conn.commit()
    return r["answer"]


def open_items_for_account(conn, account_id: int) -> int:
    return conn.execute("SELECT COUNT(*) FROM review_queue WHERE status='open' AND account_id=?"
                        " AND kind IN ('review','held_value')", (account_id,)).fetchone()[0]


# --- playbooks (procedural memory) -------------------------------------------

def get_playbook(conn, institution: str) -> str | None:
    row = conn.execute("SELECT notes FROM playbooks WHERE institution=?",
                       (institution,)).fetchone()
    return row["notes"] if row and row["notes"] else None


def save_playbook(conn, institution: str, notes: str) -> None:
    conn.execute(
        "INSERT INTO playbooks (institution, notes, updated_at) VALUES (?,?,?)"
        " ON CONFLICT(institution) DO UPDATE SET notes=excluded.notes,"
        " updated_at=excluded.updated_at", (institution, notes, now_iso()))
    conn.commit()


def get_steps(conn, institution: str) -> dict:
    """Structured login/navigation steps (login selectors, holdings URL)."""
    row = conn.execute("SELECT steps FROM playbooks WHERE institution=?",
                       (institution,)).fetchone()
    return json.loads(row["steps"]) if row and row["steps"] else {}


def save_steps(conn, institution: str, steps: dict) -> None:
    clean = {k: v for k, v in steps.items() if v not in (None, "")}
    conn.execute(
        "INSERT INTO playbooks (institution, notes, steps, updated_at) VALUES (?,?,?,?)"
        " ON CONFLICT(institution) DO UPDATE SET steps=excluded.steps,"
        " updated_at=excluded.updated_at",
        (institution, "", json.dumps(clean), now_iso()))
    conn.commit()


# --- runs and login attempts -------------------------------------------------

def start_run(conn, kind: str) -> int:
    cur = conn.execute("INSERT INTO runs (kind, started_at, pid) VALUES (?,?,?)",
                       (kind, now_iso(), os.getpid()))
    conn.commit()
    return cur.lastrowid


def finish_run(conn, run_id: int, status: str, summary: str = "") -> None:
    conn.execute("UPDATE runs SET finished_at=?, status=?, summary=? WHERE id=?",
                 (now_iso(), status, summary, run_id))
    conn.commit()


def start_run_item(conn, run_id: int, login_id: int) -> int:
    cur = conn.execute("INSERT INTO run_items (run_id, login_id, started_at) VALUES (?,?,?)",
                       (run_id, login_id, now_iso()))
    conn.commit()
    return cur.lastrowid


def finish_run_item(conn, item_id: int, outcome: str, detail: str = "",
                    trace: list | None = None, evidence: str | None = None) -> None:
    conn.execute("UPDATE run_items SET finished_at=?, outcome=?, detail=?, trace=?, evidence=?"
                 " WHERE id=?", (now_iso(), outcome, detail, json.dumps(trace or [], default=str),
                                 evidence, item_id))
    conn.commit()


def get_run(conn, run_id: int, detail: bool = False) -> dict | None:
    """A run and its items (one per login). detail=True adds each step's fields,
    the evidence folder and the values the run stored, for the Debug tab."""
    r = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if not r:
        return None
    items = conn.execute(
        "SELECT i.id, i.login_id, l.name AS login_name, i.started_at, i.finished_at, i.outcome,"
        " i.detail, i.trace, i.evidence FROM run_items i LEFT JOIN logins l ON l.id = i.login_id"
        " WHERE i.run_id=? ORDER BY i.id", (run_id,)).fetchall()
    out = []
    for i in items:
        d = dict(i)
        steps = json.loads(d.pop("trace") or "[]")
        d["steps"] = [s.get("action", "") + (f":{s['state']}" if s.get("state") else "")
                      for s in steps]
        if detail:
            d["trace"] = steps          # never contains passwords or codes
            if not d["evidence"]:       # older runs: the folder is in the trace
                d["evidence"] = next((s.get("folder") for s in steps if s.get("folder")), None)
        else:
            d.pop("evidence", None)
        out.append(d)
    run = {**dict(r), "items": out}
    if detail:
        run["stored"] = [dict(x) for x in conn.execute(
            "SELECT id, account, account_id, as_of_datetime, total FROM snapshots"
            " WHERE run_id=? ORDER BY account", (run_id,))]
        run["inbox"] = [{"id": x["id"], "kind": x["kind"], "status": x["status"],
                         "account": x["account"], "reason": x["reason"]}
                        for x in conn.execute("SELECT * FROM review_queue WHERE created_at >= ?"
                                              " AND created_at <= COALESCE(?, '9999') ORDER BY id",
                                              (r["started_at"], r["finished_at"]))]
    return run


def list_runs(conn, limit: int = 100) -> list[dict]:
    expire_dead_runs(conn)
    rows = conn.execute(
        "SELECT r.*, (SELECT COUNT(*) FROM run_items i WHERE i.run_id = r.id) AS logins,"
        " (SELECT GROUP_CONCAT(DISTINCT l.name) FROM run_items i JOIN logins l ON l.id = i.login_id"
        "  WHERE i.run_id = r.id) AS login_names"
        " FROM runs r ORDER BY r.id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def last_run(conn, kind: str | None = None) -> dict | None:
    sql = "SELECT id FROM runs"
    args: tuple = ()
    if kind:
        sql += " WHERE kind=?"
        args = (kind,)
    r = conn.execute(sql + " ORDER BY id DESC LIMIT 1", args).fetchone()
    return get_run(conn, r["id"]) if r else None


def running_runs(conn) -> list[dict]:
    expire_dead_runs(conn)
    return [dict(r) for r in conn.execute("SELECT * FROM runs WHERE status='running'")]


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def expire_dead_runs(conn) -> None:
    """A run whose process is gone (server restarted, Ctrl+C) can't finish: mark it
    failed so it stops blocking Refresh."""
    for r in conn.execute("SELECT id, pid FROM runs WHERE status='running'").fetchall():
        if not _alive(r["pid"]):
            conn.execute("UPDATE runs SET status='failed', finished_at=?, summary=? WHERE id=?",
                         (now_iso(), "Stopped: the process running it ended (server restarted?).",
                          r["id"]))
            conn.execute("UPDATE run_items SET finished_at=?, outcome='error', detail=?"
                         " WHERE run_id=? AND finished_at IS NULL",
                         (now_iso(), "Run interrupted.", r["id"]))
    conn.commit()


def record_login(conn, login_id: int, run_kind: str) -> None:
    conn.execute("INSERT INTO login_attempts (account_id, login_id, run_kind, at)"
                 " VALUES (?,?,?,?)", (login_id, login_id, run_kind, now_iso()))
    conn.commit()


def logins_since(conn, login_id: int, since_iso: str, run_kind: str) -> int:
    return conn.execute("SELECT COUNT(*) FROM login_attempts WHERE login_id=? AND run_kind=?"
                        " AND at >= ?", (login_id, run_kind, since_iso)).fetchone()[0]


def latest_retrieval(conn, account_id: int) -> str | None:
    r = conn.execute("SELECT MAX(retrieved_at) AS m FROM snapshots WHERE account_id=?",
                     (account_id,)).fetchone()
    return r["m"] if r else None



def redate_stale_portal_values(conn, max_age_days: int) -> list[dict]:
    """One-off repair: portal values whose as-of is far before their retrieval (a
    misread year) are re-dated to the retrieval time. Values and evidence are kept.
    Open held values in the inbox get the same correction."""
    from datetime import timedelta

    from .timeutil import parse_iso

    fixed = []
    for r in conn.execute("SELECT id, account, as_of_datetime, retrieved_at FROM snapshots"
                          " WHERE source='portal'").fetchall():
        got, asof = parse_iso(r["retrieved_at"]), parse_iso(r["as_of_datetime"])
        if got - asof > timedelta(days=max_age_days):
            conn.execute("UPDATE snapshots SET as_of_datetime=?, as_of_date=? WHERE id=?",
                         (to_iso(got), got.astimezone().date().isoformat(), r["id"]))
            fixed.append({"snapshot": r["id"], "account": r["account"],
                          "was": r["as_of_datetime"], "now": to_iso(got)})
    for it in conn.execute("SELECT id, payload FROM review_queue WHERE status='open'"
                           " AND kind='held_value'").fetchall():
        p = json.loads(it["payload"] or "{}")
        snap, got = p.get("snapshot") or {}, p.get("retrieved_at")
        if snap.get("source") == "portal" and got and snap.get("as_of_datetime"):
            if parse_iso(got) - parse_iso(snap["as_of_datetime"]) > timedelta(days=max_age_days):
                fixed.append({"inbox": it["id"], "account": snap.get("account"),
                              "was": snap["as_of_datetime"], "now": got})
                snap["as_of_datetime"] = got
                conn.execute("UPDATE review_queue SET payload=? WHERE id=?",
                             (json.dumps(p, default=str), it["id"]))
    conn.commit()
    return fixed


def holding_observations(conn, known_on_iso: str | None = None) -> list[dict]:
    """Every stored holding row with its snapshot's timing, for registered accounts.
    known_on_iso limits it to what had been retrieved by then ("as known on")."""
    sql = ("SELECT s.id AS snapshot_id, s.account_id, s.as_of_datetime, s.retrieved_at,"
           " h.symbol, h.description, h.value, h.price_as_of"
           " FROM holdings h JOIN snapshots s ON s.id = h.snapshot_id"
           " WHERE s.account_id IS NOT NULL")
    args: tuple = ()
    if known_on_iso:
        sql += " AND s.retrieved_at <= ?"
        args = (known_on_iso,)
    return [dict(r) for r in conn.execute(sql + " ORDER BY s.as_of_datetime, s.id", args)]
