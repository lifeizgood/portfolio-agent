"""Validator: deterministic checks in plain Python, no LLM.

A snapshot enters long-term memory only if `validate(...).ok` is True. This is
the answer to "how do you know the extracted numbers are right?"
"""

from datetime import date

from . import config
from .schemas import Snapshot, ValidationResult


def validate(snap: Snapshot, previous: Snapshot | None = None,
             today: date | None = None, market_move: float | None = None) -> ValidationResult:
    """
    previous:    the last accepted snapshot for the same account, if any
    market_move: benchmark return over the same period (e.g. 0.04 = +4%), if known;
                 used to tell a real move from a parsing error
    """
    today = today or date.today()
    errors: list[str] = []
    warnings: list[str] = []

    if not snap.holdings:
        errors.append("No holdings extracted.")

    # 1. Holdings must sum to the total printed on the statement.
    if snap.statement_total is not None and snap.statement_total > 0:
        diff = abs(snap.total - snap.statement_total) / snap.statement_total
        if diff > config.SUM_TOLERANCE:
            errors.append(f"Holdings sum to {snap.total:,.2f} but statement total is "
                          f"{snap.statement_total:,.2f} ({diff:.1%} off).")
    else:
        warnings.append("No statement total to reconcile against.")

    # 2. quantity x price should match value for each holding that has both.
    for h in snap.holdings:
        if h.value < 0:
            errors.append(f"Negative value for {h.description}.")
        if h.quantity is not None and h.price is not None and h.value:
            implied = h.quantity * h.price
            if abs(implied - h.value) / abs(h.value) > config.VALUE_TOLERANCE:
                errors.append(f"{h.description}: {h.quantity} x {h.price} = {implied:,.2f},"
                              f" but value is {h.value:,.2f}.")

    # 3. The as-of date must be sane.
    if snap.as_of_date > today:
        errors.append(f"As-of date {snap.as_of_date} is in the future.")
    if previous and snap.as_of_date < previous.as_of_date:
        errors.append(f"As-of date {snap.as_of_date} is earlier than the last accepted "
                      f"value ({previous.as_of_date}).")

    # 4. A large move must be explainable by the market; otherwise flag it.
    if previous and previous.total > 0 and not errors:
        change = snap.total / previous.total - 1
        unexplained = change - (market_move or 0.0)
        if abs(unexplained) > config.LARGE_MOVE:
            msg = (f"Total moved {change:+.1%} since {previous.as_of_date}"
                   + (f" vs market {market_move:+.1%}" if market_move is not None else "")
                   + "; check for a parsing error, deposit or withdrawal.")
            # Without a market reference we can't tell, so reject and ask a human.
            errors.append(msg)

    return ValidationResult(ok=not errors, errors=errors, warnings=warnings)
