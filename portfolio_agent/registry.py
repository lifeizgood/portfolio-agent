"""Registry: logins and the accounts under them (spec §5.4, §5.5).

A **login** is one set of portal credentials (URL, allowed domains, 2FA). A
login can hold many **accounts**; each account keeps its own history. Accounts
are found by the fetcher: a new one is created as `pending`, and its first value
waits in the inbox. Approving that value makes the account active. The user
edits logins and accounts in the UI; agents never change them.
"""

import re
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator

from . import crypto, store
from .schemas import AccountRows
from .timeutil import now_iso

ACCOUNT_TYPES = {"brokerage", "retirement", "wealth_management", "private_fund", "bank", "loan",
                 "credit_card", "other"}
ACCESS_METHODS = {"browser", "api", "email_statement", "manual"}
TWOFA_METHODS = {"remembered_device", "sms", "email", "authenticator"}
CADENCES = {"nightly", "quarterly"}
LOGIN_STATUSES = {"active", "closed"}
ACCOUNT_STATUSES = {"active", "pending", "ignored", "closed"}


def _one_of(v: str, allowed: set[str]) -> str:
    if v not in allowed:
        raise ValueError(f"must be one of {sorted(allowed)}")
    return v


def _https(v):
    if v in (None, ""):
        return None
    u = urlparse(v.strip())
    if u.scheme != "https" or not u.hostname:
        raise ValueError("must be a full https:// URL")
    return v.strip()


class LoginIn(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    institution: str = Field(min_length=1, max_length=80)
    access_method: str = "browser"
    login_url: str | None = None
    allowed_domains: str | None = None   # comma-separated; defaults from login_url
    twofa_method: str = "remembered_device"
    cadence: str = "nightly"
    holds_public: bool = True
    status: str = "active"
    closed_on: str | None = None
    # Rules for the accounts this login's pages list
    skip_categories: str | None = None          # e.g. "Investments" (shown by another login)
    liability_categories: str | None = "Loans, Credit cards, Mortgages, Lines of credit"
    skip_other_institutions: bool = False       # skip accounts the page shows as held elsewhere
    # Login steps for the institution's playbook (all optional; blank = auto-detect)
    username_selector: str | None = None
    password_selector: str | None = None
    submit_selector: str | None = None
    holdings_urls: list[str] = []          # pages to capture; together they show every account

    @field_validator("access_method")
    @classmethod
    def _a(cls, v):
        return _one_of(v, ACCESS_METHODS)

    @field_validator("twofa_method")
    @classmethod
    def _f(cls, v):
        return _one_of(v, TWOFA_METHODS)

    @field_validator("cadence")
    @classmethod
    def _c(cls, v):
        return _one_of(v, CADENCES)

    @field_validator("status")
    @classmethod
    def _s(cls, v):
        return _one_of(v, LOGIN_STATUSES)

    @field_validator("login_url")
    @classmethod
    def _u(cls, v):
        return _https(v)

    @field_validator("holdings_urls", mode="before")
    @classmethod
    def _hs(cls, v):
        if v is None:
            return []
        if isinstance(v, str):
            v = v.splitlines()
        return [_https(x) for x in v if x and x.strip()]


class AccountEdit(BaseModel):
    display_name: str = Field(min_length=1, max_length=80)
    account_type: str = "brokerage"
    status: str = "active"
    closed_on: str | None = None
    is_liability: bool = False

    @field_validator("account_type")
    @classmethod
    def _t(cls, v):
        return _one_of(v, ACCOUNT_TYPES)

    @field_validator("status")
    @classmethod
    def _s(cls, v):
        return _one_of(v, ACCOUNT_STATUSES)


def default_domains(login_url: str | None) -> str | None:
    """Registrable domain of the login page, e.g. www.mymerrill.com -> mymerrill.com."""
    if not login_url:
        return None
    host = urlparse(login_url).hostname or ""
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def domains(login: dict) -> list[str]:
    raw = login.get("allowed_domains") or default_domains(login.get("login_url")) or ""
    return [d.strip().lower().lstrip(".") for d in raw.split(",") if d.strip()]


def _inside(url: str, doms: list[str]) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in doms)


def check(login: LoginIn) -> list[str]:
    """Code checks on every registry edit (spec §5.4)."""
    errs = []
    doms = domains(login.model_dump())
    if login.access_method == "browser" and not login.login_url:
        errs.append("A browser login needs a login page URL.")
    if login.login_url and not _inside(login.login_url, doms):
        errs.append(f"The login page is not inside the allowed domains {doms}.")
    for u in login.holdings_urls:
        if not _inside(u, doms):
            errs.append(f"{u} is not inside the allowed domains {doms}.")
    if login.status == "closed" and not login.closed_on:
        errs.append("A closed login needs a close date.")
    return errs


# --- logins ------------------------------------------------------------------

_LOGIN_COLS = ["name", "institution", "access_method", "login_url", "allowed_domains",
               "twofa_method", "cadence", "holds_public", "status", "closed_on",
               "skip_categories", "liability_categories", "skip_other_institutions"]
_STEP_KEYS = ["username_selector", "password_selector", "submit_selector", "holdings_urls"]


def save_login(conn, login: LoginIn, login_id: int | None = None) -> int:
    errs = check(login)
    if errs:
        raise ValueError(" ".join(errs))
    data = login.model_dump()
    data["allowed_domains"] = ",".join(domains(data)) or None
    data["holds_public"] = int(bool(data["holds_public"]))
    data["skip_other_institutions"] = int(bool(data["skip_other_institutions"]))
    vals = [data[c] for c in _LOGIN_COLS]
    if login_id is None:
        cur = conn.execute(
            f"INSERT INTO logins ({', '.join(_LOGIN_COLS)}, created_at, updated_at)"
            f" VALUES ({', '.join('?' * len(_LOGIN_COLS))}, ?, ?)", (*vals, now_iso(), now_iso()))
        login_id = cur.lastrowid
    else:
        conn.execute(f"UPDATE logins SET {', '.join(c + '=?' for c in _LOGIN_COLS)}, updated_at=?"
                     " WHERE id=?", (*vals, now_iso(), login_id))
    conn.commit()
    steps = store.get_steps(conn, login.institution)
    steps.pop("holdings_url", None)        # older single-URL form
    steps.update({k: data[k] for k in _STEP_KEYS})
    store.save_steps(conn, login.institution, steps)
    return login_id


def steps_for(conn, institution: str) -> dict:
    steps = store.get_steps(conn, institution)
    urls = list(steps.get("holdings_urls") or [])
    if steps.get("holdings_url") and steps["holdings_url"] not in urls:
        urls.insert(0, steps["holdings_url"])
    steps["holdings_urls"] = urls
    return steps


def get_login(conn, login_id: int) -> dict | None:
    r = conn.execute("SELECT * FROM logins WHERE id=?", (login_id,)).fetchone()
    return _login_extras(conn, dict(r)) if r else None


def list_logins(conn) -> list[dict]:
    return [_login_extras(conn, dict(r)) for r in conn.execute("SELECT * FROM logins ORDER BY name")]


def _login_extras(conn, lg: dict) -> dict:
    lg["holds_public"] = bool(lg["holds_public"])
    lg["skip_other_institutions"] = bool(lg.get("skip_other_institutions"))
    lg["has_credentials"] = conn.execute(
        "SELECT 1 FROM login_credentials WHERE login_id=?", (lg["id"],)).fetchone() is not None
    steps = steps_for(conn, lg["institution"])
    for k in _STEP_KEYS:
        lg[k] = steps.get(k)
    lg["accounts"] = [a for a in list_accounts(conn) if a["login_id"] == lg["id"]]
    r = conn.execute("SELECT MAX(finished_at) AS t FROM run_items WHERE login_id=?"
                     " AND outcome IN ('stored','held','mixed')", (lg["id"],)).fetchone()
    lg["last_fetched"] = r["t"] if r else None
    return lg


# --- accounts ----------------------------------------------------------------

_ACCOUNT_SQL = """
SELECT a.id, a.login_id, a.display_name, a.account_type, a.status, a.closed_on,
       a.portal_name, a.portal_suffix, a.portal_key, a.created_at, a.category,
       a.is_liability,
       COALESCE(l.institution, a.institution) AS institution,
       COALESCE(l.cadence, a.cadence) AS cadence, l.name AS login_name
FROM accounts a LEFT JOIN logins l ON l.id = a.login_id
"""


def list_accounts(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(_ACCOUNT_SQL + " ORDER BY a.display_name")]


def get_account(conn, account_id: int) -> dict | None:
    r = conn.execute(_ACCOUNT_SQL + " WHERE a.id=?", (account_id,)).fetchone()
    return dict(r) if r else None


def update_account(conn, account_id: int, edit: AccountEdit) -> dict:
    if not get_account(conn, account_id):
        raise ValueError("No such account.")
    if edit.status == "closed" and not edit.closed_on:
        raise ValueError("A closed account needs a close date.")
    if conn.execute("SELECT 1 FROM accounts WHERE display_name=? AND id<>?",
                    (edit.display_name, account_id)).fetchone():
        raise ValueError("Another account already has that name.")
    conn.execute("UPDATE accounts SET display_name=?, account_type=?, status=?, closed_on=?,"
                 " is_liability=?, updated_at=? WHERE id=?",
                 (edit.display_name, edit.account_type, edit.status, edit.closed_on,
                  int(edit.is_liability), now_iso(), account_id))
    if edit.status == "ignored":   # excluded: its waiting values no longer need an answer
        conn.execute("UPDATE review_queue SET status='superseded', resolved_at=? WHERE"
                     " status='open' AND kind='held_value' AND account_id=?",
                     (now_iso(), account_id))
    conn.commit()
    return get_account(conn, account_id)


def find_account(conn, login_id: int, rows: AccountRows) -> dict | None:
    r = conn.execute("SELECT id FROM accounts WHERE login_id=? AND portal_key=?",
                     (login_id, rows.key)).fetchone()
    return get_account(conn, r["id"]) if r else None


def resolve_account(conn, login: dict, rows: AccountRows) -> dict:
    """The registry account for an extracted account; new ones start as pending."""
    found = find_account(conn, login["id"], rows)
    if found:
        return found
    name = rows.account + (f" …{rows.account_suffix}" if rows.account_suffix else "")
    display = f"{login['name']} {name}"[:80]
    n = 2
    while conn.execute("SELECT 1 FROM accounts WHERE display_name=?", (display,)).fetchone():
        display = f"{login['name']} {name} ({n})"[:80]
        n += 1
    liability = is_liability_category(login, rows)
    cur = conn.execute(
        "INSERT INTO accounts (display_name, institution, login_id, portal_key, portal_name,"
        " portal_suffix, account_type, status, category, is_liability, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (display, login["institution"], login["id"], rows.key, rows.account,
         rows.account_suffix, _guess_type(rows.account, rows.category), "pending",
         rows.category, int(liability), now_iso(), now_iso()))
    conn.commit()
    return get_account(conn, cur.lastrowid)


def _norm(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _listed(csv: str | None, value: str | None) -> bool:
    v = _norm(value)
    return bool(v) and any(_norm(x) == v for x in (csv or "").split(","))


def is_liability_category(login: dict, rows: AccountRows) -> bool:
    return _listed(login.get("liability_categories"), rows.category)


def skip_reason(login: dict, rows: AccountRows) -> str | None:
    """Why this login's rules leave the account out, or None to keep it."""
    if _listed(login.get("skip_categories"), rows.category):
        return f"category {rows.category!r} is skipped for this login"
    if login.get("skip_other_institutions") and rows.held_at:
        mine = {_norm(login.get("institution")), _norm(login.get("name"))}
        held = _norm(rows.held_at)
        if held and not any(m and (m in held or held in m) for m in mine):
            return f"held at {rows.held_at}, not {login['institution']}"
    return None


def ignore_account(conn, account_id: int, why: str) -> None:
    """A login rule leaves this account out: mark it ignored and drop its pending items."""
    conn.execute("UPDATE accounts SET status='ignored', updated_at=? WHERE id=?",
                 (now_iso(), account_id))
    conn.execute("UPDATE review_queue SET status='superseded', resolved_at=? WHERE status='open'"
                 " AND kind='held_value' AND account_id=?", (now_iso(), account_id))
    conn.commit()


def _guess_type(name: str, category: str | None = None) -> str:
    c = (category or "").lower()
    if "loan" in c or "mortgage" in c or "line of credit" in c:
        return "loan"
    if "credit card" in c:
        return "credit_card"
    if "bank" in c or "checking" in c or "saving" in c:
        return "bank"
    n = name.lower()
    if any(w in n for w in ("ira", "401", "403", "roth", "retire", "rollover", "sep")):
        return "retirement"
    return "brokerage"


def mark_closed(conn, account_id: int, closed_on: str) -> None:
    conn.execute("UPDATE accounts SET status='closed', closed_on=?, updated_at=? WHERE id=?",
                 (closed_on, now_iso(), account_id))
    conn.commit()


def activate_account(conn, account_id: int) -> None:
    conn.execute("UPDATE accounts SET status='active', updated_at=? WHERE id=? AND status='pending'",
                 (now_iso(), account_id))
    conn.commit()


# --- credentials (per login) -------------------------------------------------

def set_credentials(conn, login_id: int, username: str, password: str) -> None:
    if not get_login(conn, login_id):
        raise ValueError("No such login.")
    token = crypto.encrypt({"username": username, "password": password})
    conn.execute("INSERT INTO login_credentials (login_id, secret, updated_at) VALUES (?,?,?)"
                 " ON CONFLICT(login_id) DO UPDATE SET secret=excluded.secret,"
                 " updated_at=excluded.updated_at", (login_id, token, now_iso()))
    conn.commit()


def get_credentials(conn, login_id: int) -> dict | None:
    """Decrypted {username, password}. Only the Auth tool and the UI's eye icon call this."""
    r = conn.execute("SELECT secret FROM login_credentials WHERE login_id=?",
                     (login_id,)).fetchone()
    return crypto.decrypt(r["secret"]) if r else None
