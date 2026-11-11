"""Data shapes shared by the extractor, validator and store."""

import re
from datetime import date, datetime

from pydantic import BaseModel, Field, field_validator, model_validator

from .timeutil import parse_as_of


class Holding(BaseModel):
    symbol: str | None = Field(None, description="Ticker if public, else null")
    description: str
    quantity: float | None = None
    price: float | None = None
    value: float
    # A holding can be priced on its own date, older than the page: e.g. a private
    # fund showing "Last Updated 06/30/2026" on a page captured in November.
    price_as_of: date | None = Field(None, description="Date this holding's price/value is as of")

    @model_validator(mode="before")
    @classmethod
    def _aliases(cls, data):
        if isinstance(data, dict) and "price_as_of" not in data:
            for key in ("as_of", "last_updated", "as_of_date"):
                if key in data:
                    data = {**data, "price_as_of": data[key]}
                    break
        return data

    @field_validator("price_as_of", mode="before")
    @classmethod
    def _date(cls, v):
        if v in (None, "", "null", "--"):
            return None
        if isinstance(v, (date, datetime)):
            return v if not isinstance(v, datetime) else v.date()
        s = str(v).strip()
        m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)       # 06/30/2026
        if m:
            return date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
        return date.fromisoformat(s[:10])


def _as_of_aliases(data):
    # Callers and the extractor may send "as_of_date" / "as_of" instead.
    if isinstance(data, dict) and "as_of_datetime" not in data:
        data = dict(data)
        for key in ("as_of", "as_of_date"):
            if key in data:
                data["as_of_datetime"] = data.pop(key)
                break
    return data


class Snapshot(BaseModel):
    """One account's holdings at one moment, as read from a page or statement."""

    institution: str
    account: str = Field(description="Account display name, never the account number")
    as_of_datetime: datetime = Field(description="When the values were true, not when published")
    statement_total: float | None = Field(
        None, description="Total value shown on the page or statement, used for reconciliation"
    )
    holdings: list[Holding]
    source: str = "statement"          # portal | api | statement | manual
    account_id: int | None = None      # registry account, when known

    @model_validator(mode="before")
    @classmethod
    def _accept_as_of_date(cls, data):
        return _as_of_aliases(data)

    @field_validator("as_of_datetime", mode="before")
    @classmethod
    def _parse(cls, v):
        return parse_as_of(v)

    @property
    def as_of_date(self) -> date:
        return self.as_of_datetime.astimezone().date()

    @property
    def total(self) -> float:
        return sum(h.value for h in self.holdings)


class AccountRows(BaseModel):
    """One account found in an extracted document."""

    account: str = Field(description="Account name as shown, e.g. 'CMA-Edge'")
    account_suffix: str | None = Field(None, description="Last 4 characters of the account number")
    statement_total: float | None = None
    holdings: list[Holding] = []
    closed: bool = Field(False, description="The portal marks this account as closed")
    category: str | None = Field(None, description="Section heading it is listed under, e.g. Loans")
    held_at: str | None = Field(None, description="Institution the page shows for this account")

    @field_validator("closed", mode="before")
    @classmethod
    def _closed(cls, v):
        return bool(v)

    @field_validator("holdings", mode="before")
    @classmethod
    def _none_is_empty(cls, v):
        return [] if v is None else v       # caught by the non-empty check, then retried

    @field_validator("account_suffix", mode="before")
    @classmethod
    def _last4(cls, v):
        chars = re.sub(r"[^A-Z0-9]", "", str(v or "").upper())
        return chars[-4:] or None            # never keep more than the last 4 characters

    @property
    def total(self) -> float:
        return sum(h.value for h in self.holdings)

    @property
    def key(self) -> str:
        """How the same account is recognized on the next run."""
        name = re.sub(r"[^a-z0-9]", "", self.account.lower())
        return f"{name}|{self.account_suffix}" if self.account_suffix else name


class Extraction(BaseModel):
    """Everything the extractor read from one document: one or more accounts."""

    institution: str | None = None
    as_of_datetime: datetime | None = None    # None: the document shows no date (spec D16)
    grand_total: float | None = Field(None, description="Total of all accounts, if shown")
    accounts: list[AccountRows]

    @model_validator(mode="before")
    @classmethod
    def _shapes(cls, data):
        data = _as_of_aliases(data)
        # Older single-account shape: {"account", "statement_total", "holdings", ...}
        if isinstance(data, dict) and "accounts" not in data and "account" in data:
            data = dict(data)
            one = {k: data.pop(k) for k in ("account", "account_suffix", "statement_total",
                                            "holdings") if k in data}
            data["accounts"] = [one]
        if isinstance(data, dict) and data.get("accounts") is None:
            data = {**data, "accounts": []}
        return data

    @field_validator("as_of_datetime", mode="before")
    @classmethod
    def _parse(cls, v):
        return None if v in (None, "", "null") else parse_as_of(v)


class ValidationResult(BaseModel):
    ok: bool                  # no hard errors
    errors: list[str] = []    # hard failures: snapshot is rejected (retry, then review)
    warnings: list[str] = []  # soft flags: stored, but worth a look
    holds: list[str] = []     # values that look right but need the user's approval first
