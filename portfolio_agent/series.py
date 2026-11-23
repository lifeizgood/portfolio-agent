"""Time series for the dashboard charts (spec §1.2). All numbers computed in code.

Values are built per holding, because one account can hold positions priced on
different dates (a private fund "Last Updated 06/30/2026" on a page read in
November). For each day, an account's value is the sum of its holdings, where
each holding:
  - counts from its own as-of date (its price_as_of, else the page's as-of),
  - uses its latest value dated at or before that day, carried forward until a
    newer one arrives; older values stay in the store and keep their dates,
  - stops when a later reading of the account no longer lists it (sold), or
    after the account's close date.
Restatements: for the same holding and date, the latest retrieval wins.

Monthly rule: each month uses the latest day in that month that has any data;
months without data are skipped, so a missed nightly run is fine.
"""

import re
from collections import defaultdict
from datetime import date, datetime, timedelta

from . import config, registry, store
from .timeutil import end_of_local_day, now, parse_iso


def _hkey(symbol: str | None, description: str) -> str:
    return (symbol or "").upper() or "d:" + re.sub(r"[^a-z0-9]", "", description.lower())


def _points(conn, known_on: str | None = None, full: bool = False) -> dict[int, dict]:
    accounts = {a["id"]: a for a in registry.list_accounts(conn)}
    by_acct: dict[int, dict] = {}
    for r in store.holding_observations(conn, known_on, full):   # your share unless full
        a = accounts.get(r["account_id"])
        if not a or a["status"] in ("ignored", "pending"):
            continue          # only accounts you track; test data has no account
        e = by_acct.setdefault(a["id"], {
            "label": a["display_name"], "institution": a["institution"],
            "type": a["account_type"],
            "closed_on": date.fromisoformat(a["closed_on"]) if a.get("closed_on") else None,
            "obs": defaultdict(list), "snaps": {}})
        snap_at = parse_iso(r["as_of_datetime"])
        eff = end_of_local_day(date.fromisoformat(r["price_as_of"])) if r["price_as_of"] else snap_at
        eff = min(eff, snap_at)       # a holding is never dated after its page
        key = _hkey(r["symbol"], r["description"])
        e["obs"][key].append((eff, r["retrieved_at"], r["value"]))
        e["snaps"].setdefault(r["snapshot_id"], (snap_at, set()))[1].add(key)
    for e in by_acct.values():
        e["snaps"] = sorted(e["snaps"].values(), key=lambda x: x[0])
    return by_acct


def _value_at(e: dict, moment: datetime):
    """(value, as-of label) of one account at a moment, or (None, None)."""
    if e["closed_on"] and moment.astimezone().date() > e["closed_on"]:
        return None, None
    total, used, label = 0.0, False, None
    for key, obs in e["obs"].items():
        known = [o for o in obs if o[0] <= moment]
        if not known:
            continue
        eff, _, value = max(known, key=lambda o: (o[0], o[1]))
        # Sold: a later reading of the account (up to this moment) doesn't list it.
        if any(eff < at <= moment and key not in keys for at, keys in e["snaps"]):
            continue
        total += value
        used = True
        d = eff.astimezone().date().isoformat()
        label = d if label is None or d > label else label
    return (total, label) if used else (None, None)


def _data_days(pts: dict) -> list[date]:
    return sorted({o[0].astimezone().date() for e in pts.values()
                   for obs in e["obs"].values() for o in obs})


def build_series(conn, start: date | None = None, end: date | None = None,
                 granularity: str = "daily", group: str = "account",
                 known_on: str | None = None, full: bool = False) -> dict:
    pts = _points(conn, known_on, full)
    if not pts:
        return {"dates": [], "series": {}, "total": [], "carried": {}}
    all_days = _data_days(pts)
    first = all_days[0]
    end = end or now().astimezone().date()
    start = max(start or first, first)

    if granularity == "monthly":
        data_days = [d for d in all_days if start <= d <= end]
        last_in_month: dict[tuple, date] = {}
        for d in data_days:
            last_in_month[(d.year, d.month)] = d
        days = sorted(last_in_month.values())
    else:
        days = [start + timedelta(n) for n in range((end - start).days + 1)]

    group_of = {"account": "label", "institution": "institution", "type": "type"}[group]
    series: dict[str, list] = defaultdict(lambda: [None] * len(days))
    carried: dict[str, list] = defaultdict(lambda: [None] * len(days))
    total = [None] * len(days)
    for i, d in enumerate(days):
        moment = end_of_local_day(d)
        for e in pts.values():
            v, as_of = _value_at(e, moment)
            if v is None:
                continue
            name = e[group_of]
            series[name][i] = (series[name][i] or 0) + v
            if group == "account" and as_of != d.isoformat():
                carried[name][i] = as_of   # value carried forward from this as-of date
            total[i] = (total[i] or 0) + v
    return {"dates": [d.isoformat() for d in days], "series": dict(series),
            "total": total, "carried": dict(carried)}


def account_status(conn, account: dict) -> str:
    """ok | stale | needs_review | pending | ignored | closed (spec §9.2 completeness check)."""
    if account["status"] in ("closed", "ignored", "pending"):
        return account["status"]
    if store.open_items_for_account(conn, account["id"]):
        return "needs_review"
    last = store.latest_retrieval(conn, account["id"])
    if not last:
        return "stale"
    age_h = (now() - parse_iso(last)).total_seconds() / 3600
    limit = {"nightly": config.STALE_HOURS_NIGHTLY,
             "monthly": 24 * config.STALE_DAYS_MONTHLY,
             "quarterly": 24 * config.STALE_DAYS_QUARTERLY}.get(account["cadence"], 24 * 30 * 6)
    return "stale" if age_h > limit else "ok"
