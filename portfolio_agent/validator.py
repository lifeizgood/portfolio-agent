"""Validator: deterministic checks in plain Python, no LLM (spec §7).

A snapshot enters long-term memory only if `validate(...).ok` is True and it has
no holds (or the user approved it). Check numbers match the spec's V1..V9.
"""

import re
from datetime import date, datetime, timedelta

from . import config
from .schemas import Extraction, Snapshot, ValidationResult
from .timeutil import now


def validate(snap: Snapshot, previous: Snapshot | None = None,
             today: date | datetime | None = None,
             market_move: float | None = None, liability: bool = False) -> ValidationResult:
    """
    previous:    the nearest earlier accepted snapshot for the same account (spec D19)
    market_move: benchmark return over the same period (e.g. 0.04 = +4%), if known
    """
    errors: list[str] = []
    warnings: list[str] = []
    holds: list[str] = []

    # V1 non-empty (an account the page shows at exactly $0 may have no rows)
    if not snap.holdings and snap.statement_total != 0:
        errors.append("No holdings extracted.")

    # V2 holdings sum to the total shown on the page or statement
    if snap.statement_total:
        diff = abs(snap.total - snap.statement_total) / abs(snap.statement_total)
        if diff > config.SUM_TOLERANCE:
            errors.append(f"Holdings sum to {snap.total:,.2f} but the stated total is "
                          f"{snap.statement_total:,.2f} ({diff:.1%} off).")
    else:
        warnings.append("No stated total to reconcile against.")

    for h in snap.holdings:
        # V4 sign: a liability (owed) is never positive. In an asset account a single
        # holding may be negative (options written, cash borrowed on margin) only when
        # the account shows a total for V2 to check the sum against.
        if not liability and h.value < 0 and not snap.statement_total:
            errors.append(f"Negative value for {h.description}.")
        if liability and h.value > 0:
            errors.append(f"Positive value for {h.description} on a liability account.")
        # A holding's own date may be older than the page, never later.
        if h.price_as_of and h.price_as_of > snap.as_of_date:
            errors.append(f"{h.description}: as-of {h.price_as_of} is after the page's "
                          f"as-of {snap.as_of_date}.")
        # V3 qty x price vs the portal's own value (no market feed, spec C3)
        if h.quantity is not None and h.price is not None and h.value:
            implied = h.quantity * h.price
            off = lambda x: abs(x - h.value) / abs(h.value)
            if off(implied) > config.VALUE_TOLERANCE and off(implied * 100) <= config.VALUE_TOLERANCE:
                implied *= 100    # an option: price per share, 100 shares per contract
            if off(implied) > config.VALUE_TOLERANCE:
                errors.append(f"{h.description}: {h.quantity} x {h.price} = {implied:,.2f},"
                              f" but value is {h.value:,.2f}.")

    # V5 as-of not in the future. (V6, "not earlier than the last value", was
    # removed: out-of-order ingestion is valid, spec C4.)
    # Up to one day ahead is accepted: portals often date values in Eastern time (or
    # with the next business day), which can read as tomorrow here.
    if isinstance(today, datetime):
        today_d = today.astimezone().date()
    else:
        today_d = today or now().astimezone().date()
    latest_ok = today_d + timedelta(days=config.FUTURE_DAYS_OK)
    if snap.as_of_date > latest_ok:
        errors.append(f"As-of date {snap.as_of_date} is in the future.")

    # V8 continuity against the nearest earlier value. A big unexplained move may
    # well be real, so it is held for approval rather than rejected (spec C6:
    # re-extracting a correct reading can't help).
    if previous and previous.total and not errors:
        change = snap.total / previous.total - 1
        unexplained = change - (market_move or 0.0)
        if abs(unexplained) > config.LARGE_MOVE:
            holds.append(
                f"Total moved {change:+.1%} since {previous.as_of_date}"
                + (f" vs market {market_move:+.1%}" if market_move is not None else "")
                + ". Approve if real (deposit, withdrawal, big market move); reject if misread.")

    return ValidationResult(ok=not errors, errors=errors, warnings=warnings, holds=holds)


def validate_extraction(ext: Extraction) -> list[str]:
    """Checks across all accounts read from one document."""
    errors: list[str] = []
    if not ext.accounts:
        errors.append("No accounts extracted.")
    keys = [a.key for a in ext.accounts]
    if len(keys) != len(set(keys)):
        errors.append("The same account was extracted twice; list each account once.")
    by_suffix: dict[str, list[str]] = {}
    for a in ext.accounts:
        if a.account_suffix:
            by_suffix.setdefault(a.account_suffix, []).append(a.account)
    for suffix, names in by_suffix.items():
        if len(set(names)) > 1:
            errors.append(f"{' and '.join(repr(n) for n in names)} were all given the account "
                          f"number ending {suffix}. Give each account only the number shown "
                          "next to it (or on its details page), else null.")
    # The page's overall total must be covered. Accounts may add up to MORE than it:
    # portals leave some accounts out of the overall value, and those are still kept.
    if ext.grand_total and ext.accounts:
        found = sum(a.statement_total if a.statement_total is not None else a.total
                    for a in ext.accounts)
        short = (ext.grand_total - found) / ext.grand_total
        if short > config.SUM_TOLERANCE:
            errors.append(f"The accounts extracted add up to {found:,.2f}, less than the page's "
                          f"overall total of {ext.grand_total:,.2f} ({short:.1%} short). "
                          "An account is probably missing; extract every account shown.")
    return errors


# Account numbers shown on a line of their own, as portals list them under the
# account name: "12A-34567", "...1234", "xxxx1234", "****1234".
_ACCOUNT_LINE = re.compile(
    r"^\s*(?:[A-Z0-9]{3}-[A-Z0-9]{5}|(?:\.{3}|…|[xX*]{3,}[- ]?)[A-Z0-9]{4})\s*$", re.M)


def unlisted_accounts(text: str, ext: Extraction, not_accounts=()) -> list[str]:
    """Account numbers on the page whose last 4 characters match no extracted account.
    Deterministic, so a skipped account is caught even when it sits outside every total.
    Numbers the user said aren't accounts (a card, a routing number) are left out."""
    have = {(a.account_suffix or "")[-4:] for a in ext.accounts} | set(not_accounts)
    missing = []
    for m in _ACCOUNT_LINE.finditer(text or ""):
        last4 = re.sub(r"[^A-Z0-9]", "", m.group(0).upper())[-4:]
        if last4 not in have and last4 not in missing:
            missing.append(last4)
    return [f"The page lists an account ending {x} that wasn't extracted. Extract every "
            "account shown, including ones outside the overall total or in a separate "
            "section, and give each its account_suffix." for x in missing]
