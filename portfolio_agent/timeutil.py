"""Time helpers. Every stored timestamp is timezone-aware UTC ISO text, so string
order equals time order; dates for charts are taken in the Mac's local zone."""

from datetime import date, datetime, time, timezone


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
        if len(s) == 10:  # YYYY-MM-DD
            return parse_as_of(date.fromisoformat(s))
        dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.astimezone()  # attach the local zone
    return dt


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def end_of_local_day(d: date) -> datetime:
    return datetime.combine(d, time(23, 59, 59)).astimezone()
