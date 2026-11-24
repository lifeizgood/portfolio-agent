"""Daily Refresh: `serve` starts the Dashboard's Refresh once a day at a set time.

The time and an on/off switch are app settings, changed in the dashboard. The timer
lives in the `serve` process, so it runs only while the dashboard is running and the
Mac is awake. If the Mac was asleep or `serve` wasn't running at that time, the Refresh
starts as soon as it is back, the same day; a day missed entirely is skipped. One try a
day: a Refresh that was refused (kill switch on, every login busy) isn't retried.
"""

import re
from datetime import datetime, timedelta

from . import config, store

_TIME = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


def settings(conn) -> dict:
    return {"enabled": store.get_setting(conn, "daily_refresh_enabled", "1") == "1",
            "time": store.get_setting(conn, "daily_refresh_time", config.DAILY_REFRESH_TIME)}


def save(conn, enabled: bool, time: str, now_local: datetime) -> dict:
    """A time set to earlier than now starts tomorrow, not straight away."""
    time = (time or "").strip()
    if not _TIME.match(time):
        raise ValueError("Enter the time as HH:MM, 24-hour (for example 20:15).")
    store.set_setting(conn, "daily_refresh_enabled", "1" if enabled else "0")
    store.set_setting(conn, "daily_refresh_time", time)
    if now_local >= _at(now_local.date(), time):
        mark_tried(conn, now_local)
    return settings(conn)


def _at(day, time: str) -> datetime:
    h, m = map(int, time.split(":"))
    return datetime(day.year, day.month, day.day, h, m)


def due(conn, now_local: datetime) -> bool:
    """Today's time has passed and today's daily Refresh hasn't been tried yet."""
    s = settings(conn)
    if not s["enabled"]:
        return False
    today = now_local.date()
    return (now_local >= _at(today, s["time"])
            and store.get_setting(conn, "daily_refresh_last") != today.isoformat())


def mark_tried(conn, now_local: datetime) -> None:
    store.set_setting(conn, "daily_refresh_last", now_local.date().isoformat())


def next_at(conn, now_local: datetime) -> datetime | None:
    s = settings(conn)
    if not s["enabled"]:
        return None
    if due(conn, now_local):
        return now_local
    t = _at(now_local.date(), s["time"])
    return t if t > now_local else _at(now_local.date() + timedelta(days=1), s["time"])
