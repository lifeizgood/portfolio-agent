"""Data shapes shared by the extractor, validator and store."""

from datetime import date

from pydantic import BaseModel, Field


class Holding(BaseModel):
    symbol: str | None = Field(None, description="Ticker if public, else null")
    description: str
    quantity: float | None = None
    price: float | None = None
    value: float


class Snapshot(BaseModel):
    """One account's holdings as of one date, as read from a page or statement."""

    institution: str
    account: str = Field(description="Account name or nickname, never the account number")
    as_of_date: date = Field(description="Date the values were true, not when published")
    statement_total: float | None = Field(
        None, description="Total value printed on the statement, used for reconciliation"
    )
    holdings: list[Holding]
    source: str = "statement"

    @property
    def total(self) -> float:
        return sum(h.value for h in self.holdings)


class ValidationResult(BaseModel):
    ok: bool
    errors: list[str] = []    # hard failures: snapshot is rejected
    warnings: list[str] = []  # soft flags: stored, but worth a look
