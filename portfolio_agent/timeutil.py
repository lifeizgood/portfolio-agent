"""Time helpers. Every stored timestamp is timezone-aware UTC ISO text, so string
order equals time order; dates for charts are taken in the Mac's local zone."""

import re
from datetime import date, datetime, time, timezone

_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}


def parse_date_text(s) -> date | None:
    """A date as portals print it: 2026-06-30, 06/30/2026, Jun-30-2026, Jun 30, 2026,
    June 30 2026 or 30-Jun-2026. None when the text is not one date."""
    t = str(s or "").strip().rstrip(".").lower()
    try:
        if m := re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", t):
            return date(int(m[1]), int(m[2]), int(m[3]))
        if m := re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", t):
            return date(int(m[3]), int(m[1]), int(m[2]))
        if m := re.fullmatch(r"([a-z]{3})[a-z]*\.?[-\s]+(\d{1,2}),?[-\s]+(\d{4})", t):
            return date(int(m[3]), _MONTHS[m[1]], int(m[2])) if m[1] in _MONTHS else None
        if m := re.fullmatch(r"(\d{1,2})[-\s]+([a-z]{3})[a-z]*\.?,?[-\s]+(\d{4})", t):
            return date(int(m[3]), _MONTHS[m[2]], int(m[1])) if m[2] in _MONTHS else None
    except ValueError:
        return None
    return None


def now() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return to_iso(now())


def to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def local(dt: datetime) -> datetime:
    return dt.astimezone()


def parse_as_of(value) -> datetime:
    """Parse an as-of value into an aware datetime.

    Spec D16: when the source gives no time of day, use the current time. A dated
    source keeps its own date, so a June 30 statement stays on June 30.
    Naive times are read as the Mac's local time.
    """
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        dt = datetime.combine(value, datetime.now().time().replace(microsecond=0))
    else:
        s = str(value).strip().replace("Z", "+00:00")
        if len(s) == 10 and s[4] == "-":  # YYYY-MM-DD
            return parse_as_of(date.fromisoformat(s))
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            d = parse_date_text(s)       # e.g. "Jun-30-2026", as a portal prints it
            if d is None:
                raise
            return parse_as_of(d)
    if dt.tzinfo is None:
        dt = dt.astimezone()  # attach the local zone
    return dt


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def end_of_local_day(d: date) -> datetime:
    return datetime.combine(d, time(23, 59, 59)).astimezone()
