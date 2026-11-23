"""How to read each institution's monthly statement (layout notes for the extractor).

These describe page layout only: section names, column order, which line holds the
total. They never contain anyone's names, account numbers or values. A login whose
institution name contains a guide's keyword gets its notes, ahead of any notes the
user saved for that institution.

Each guide can also say where the holdings end. Everything after that point
(activity, income, footnotes, glossary) is history or legal text, so it is cut
before the model reads the statement: shorter input is faster and leaves less to
misread. If the marker isn't found, nothing is cut.
"""

import re
from dataclasses import dataclass
from datetime import date

from .timeutil import parse_date_text


@dataclass(frozen=True)
class Guide:
    keywords: tuple[str, ...]          # matched against the login's institution, lowercased
    notes: str
    keep_through: str | None = None    # regex: text after the LAST match is cut
    cut_from: str | None = None        # else regex: text from the first match on is cut
    account_number: str | None = None  # regex, group 1: the statement's one account number
    pages: int | None = None           # read only the first N pages (the rest is cut)
    portal_notes: str | None = None    # how to read the institution's portal page
    portal_marker: str | None = None   # text on that page: read it as soon as it shows
    # regex for the header of the portal table's per-row date column ("As of Date"):
    # code reads each row's date there, overriding the model's
    portal_date_column: str | None = None


ROCKEFELLER = Guide(
    keywords=("rockefeller", "rcm"),
    keep_through=r"^TOTAL PORTFOLIO VALUE\b.*$",
    cut_from=r"^Footnotes and Cost Basis Information",    # an account with no positions
    account_number=r"Account Number:?\s*([A-Z0-9]{2,4}-?\d{4,})",
    notes="""Rockefeller Capital Management statement (accounts carried by National Financial Services):
- ONE account per statement. Its name is the line right under "STATEMENT FOR THE PERIOD ...",
  shaped "<OWNER NAME> - <registration>", e.g. "ALEX SAMPLE - Individual TOD" or
  "ALEX SAMPLE - Roth IRA". Use that whole line as the account name.
  account_suffix = the last 4 digits of "Account Number: AAA-123456".
- as_of = the date in "ENDING VALUE (AS OF MM/DD/YY)"; if missing, the last day of the period.
- statement_total = the FIRST amount on the "TOTAL PORTFOLIO VALUE" line (the same as ENDING
  VALUE, Current Period column). Never use the Year-to-Date column, "Ending Value with Accrued
  Dividends", "Margin Positions Market Value" or "Margin Equity". grand_total = null.
- Holdings are the rows under "Holdings", in sections such as CASH AND CASH EQUIVALENTS,
  HOLDINGS > EQUITIES, HOLDINGS > OPTIONS, OTHER. A holding's amounts row reads: quantity,
  price, CURRENT MARKET VALUE, then estimated annual income, cost and gain/loss. value = the
  current market value (the third amount), never cost, income or gain. "Average Unit Cost",
  "Client Investment" and "Dividend/Capital Gain Option" lines are not holdings. Lines starting
  "Total" are subtotals, not holdings.
- Cash: "BANK DEPOSIT SWEEP PROGRAM" and "Cash" / "NET CASH POSITION" rows are holdings
  (description "Cash" or the sweep name). Net cash can be negative (money borrowed on margin).
- Amounts in parentheses are negative.
- Options: one contract is 100 shares and the price is per share. A SOLD (written) option
  shows its quantity in parentheses, e.g. (1): give quantity as a negative number (-1) and price
  as the positive per-share price. Its value is negative: -(contracts x 100 x price), e.g.
  -(1 x 100 x $1.00) = -100, whatever sign the market value column shows.
  symbol = the option symbol line under it (e.g. ABC260116C50).
- An account with no positions ("There were no positions in your account ...") is still
  returned, with holdings [] and statement_total = its ENDING VALUE (usually 0).
- A LOAN: when TOTAL PORTFOLIO VALUE is negative and the only holding is negative cash / NET
  CASH POSITION (a margin loan), the account is a debt: category "Loans", one holding with
  description "Margin loan balance" and value = the amount owed as a POSITIVE number, and
  statement_total = that same positive number.
- Activity, Income, Bank Deposit Detail, Messages and Footnotes are not holdings.""")

SCHWAB = Guide(
    keywords=("schwab",),
    cut_from=r"^Transactions - Summary",
    account_number=r"Account Number:?\s*(\d{4}-\d{4})\b",
    notes="""Charles Schwab statement:
- ONE account per statement. Account name: the "Account Nickname" if one is shown, else the
  account title near the top (e.g. "Schwab One Account of ALEX SAMPLE").
  account_suffix = the last 4 digits of the "Account Number" (shaped 1234-5678).
- as_of = the last day of the "Statement Period" (e.g. "October 1-31, 2026" -> 2026-10-31).
- statement_total = "Ending Account Value" in the This Statement column (not YTD).
  grand_total = null.
- Holdings are the rows under "Positions - ..." sections: Cash and Cash Investments, Equities,
  Exchange Traded Funds, Mutual Funds, Fixed Income, Options and so on. "Positions - Summary"
  is a summary, not holdings. Lines starting "Total" are subtotals, not holdings.
- In equity, ETF and fund rows the amounts read: quantity, price, MARKET VALUE, cost basis,
  unrealized gain/loss, estimated yield, estimated annual income, % of account.
  value = the market value (the third amount).
- In the Cash row the amounts read: beginning balance, ENDING balance, change in period,
  pending/unsettled cash, interest rate, % of account. value = the ending balance (the
  second amount); description "Cash".
- Ignore "⁺" marks and stray commas after descriptions. Amounts in parentheses are negative.
- Options: one contract is 100 shares and the price is per share. A SOLD (written) option
  shows its quantity in parentheses, e.g. (1): give quantity as a negative number (-1) and price
  as the positive per-share price. Its value is negative: -(contracts x 100 x price), e.g.
  -(1 x 100 x $1.00) = -100, whatever sign the market value column shows.
- Transactions, pending transactions and endnotes are not holdings.""")

FREC = Guide(
    keywords=("frec",),
    pages=1,
    notes="""Frec statement (only its FIRST PAGE is given; the rest was cut on purpose):
- ONE account per statement. Account name: the account title shown on the first page.
  account_suffix = the last 4 characters of the account number, if the first page shows one.
- as_of = the closing date of the statement period (the last day of the period).
- The account's value is the CLOSING BALANCE on the "TOTAL PRICED PORTFOLIO" line. If that
  line shows an opening and a closing balance, use the closing one, never the opening one.
- Return exactly ONE holding: description "Total priced portfolio", value = that closing
  balance, quantity null, price null. Don't list individual positions.
- statement_total = the same closing balance. grand_total = null.""",
    portal_notes="""Frec dashboard (the page after sign-in):
- Each account listed on the dashboard is its own account. Account name: as shown.
  account_suffix: the last 4 characters of its account number, if shown, else null.
- Its value is the account's TOTAL value as shown on the dashboard. Return exactly ONE
  holding per account: description "Total value", value = that total, quantity null,
  price null. statement_total = the same number. Don't list individual positions.
- grand_total = the overall total for all accounts, if the dashboard shows one, else null.""")

STRATEGIC_PARTNERS = Guide(
    keywords=("strategic partners", "blackstone"),
    notes="",
    portal_marker="Performance Summary",
    portal_date_column=r"as of(?: date)?",
    portal_notes="""Strategic Partners (Blackstone) investor portal:
- Read only the "Performance Summary (USD)" table. Each row is one fund investment, and
  each fund is its own account. Account name: the fund's name in that row. account_suffix:
  null.
- The fund's value is its "Unrealized Value" column: what is still invested. Never use
  "Current Value" or "Total Current Value" (they add "Realized Value", money already paid
  out to the investor), and never Realized Value, Commitment, Contributions, Distributions,
  Net Gain, IRR or multiple columns.
- The page text ends with each table again, one row per line ("cell | cell | ..."):
  use those rows to tell the columns apart, and the header row to find Unrealized Value
  and As of. A row whose first cell is "Total" is the total row.
- Give each account its own "as_of" = the date in that row's "As of Date" column, the
  table's last column, e.g. "Jun-30-2026" -> "2026-06-30" (YYYY-MM-DD).
  If the rows show no date, use the "As of" date shown for the whole table (it may be
  listed under "Values shown in fields and dropdowns"). The page's top-level "as_of" is
  null: the values are as of each fund's own date.
- Give exactly ONE holding per fund: description = the fund name, value = the Unrealized
  Value, quantity null, price null. statement_total = the same Unrealized Value (never the
  fund's current value).
- grand_total = "Total Unrealized Value" (the total of the Unrealized Value column), never
  "Total Current Value". A total row is not an account.""")

STAR_MOUNTAIN = Guide(
    keywords=("star mountain",),
    notes="""Star Mountain capital statement:
- ONE fund per statement. Account name: the fund's name as shown on the statement.
- as_of = the date shown at the TOP of the statement (the "as of" or period-end date).
- The fund's value is the "Ending Balance" for the period (the current period column,
  never year-to-date or inception-to-date, never the beginning balance).
- One holding: description = the fund name, value = that Ending Balance; statement_total =
  the same number.""")

GUIDES = (ROCKEFELLER, SCHWAB, FREC, STRATEGIC_PARTNERS, STAR_MOUNTAIN)


def guide_for(institution: str | None) -> Guide | None:
    name = (institution or "").lower()
    words = set(re.findall(r"[a-z0-9]+", name))
    for g in GUIDES:
        if any(k in words or (len(k) > 4 and k in name) for k in g.keywords):
            return g
    return None


def trim(text: str, guide: Guide | None) -> str:
    """Cut what follows the holdings, when the guide says where they end, and every
    page after the first N when the guide reads only those."""
    if guide and guide.pages:
        m = re.search(rf"^\[page {guide.pages + 1}\]$", text, re.M)
        if m:
            text = text[:m.start()].rstrip() + "\n[... later pages left out ...]"
    end = None
    if guide and guide.keep_through:
        for m in re.finditer(guide.keep_through, text, re.M):
            end = m.end()
    if end is None and guide and guide.cut_from:
        m = re.search(guide.cut_from, text, re.M)
        end = m.start() if m else None
    if end is None:
        return text
    return text[:end].rstrip() + "\n[... activity, footnotes and disclosures left out ...]"


def _name_key(s: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", (s or "").lower()))


def _cells(line: str) -> list[str]:
    """A table row from the captured text: "cell | cell" rows, or the tab-separated
    cells of the page's visible text."""
    sep = " | " if " | " in line else "\t" if "\t" in line else None
    return [c.strip() for c in line.split(sep)] if sep else []


def row_dates(text: str, guide: Guide | None) -> dict[str, date]:
    """Each table row's own date, read by code from the column whose header matches the
    guide's portal_date_column: {row name (lowercased words): date}. The row's name is
    its first cell with a letter in it. Rows without a date there (a total) are left out."""
    if not guide or not guide.portal_date_column:
        return {}
    head = re.compile(rf"^{guide.portal_date_column}$", re.I)
    out: dict[str, date] = {}
    col = width = None
    for line in text.splitlines():
        cells = _cells(line)
        if len(cells) < 2:
            continue
        hits = [i for i, c in enumerate(cells) if head.match(c)]
        if hits:
            col, width = hits[0], len(cells)
            continue
        if col is None:
            continue
        # the same column counted from the start, or from the end when a row has an
        # extra (expand/collapse) cell at the front
        for i in (col, len(cells) - (width - col)):
            d = parse_date_text(cells[i]) if 0 <= i < len(cells) else None
            if d:
                name = next((c for c in cells if re.search(r"[A-Za-z]", c)), "")
                if _name_key(name):
                    out.setdefault(_name_key(name), d)
                break
    return out


def account_date(account: str, dates: dict[str, date]) -> date | None:
    """The date read for this account's row; else the one date every row shows."""
    if not dates:
        return None
    if (d := dates.get(_name_key(account))) is not None:
        return d
    return next(iter(set(dates.values()))) if len(set(dates.values())) == 1 else None


def statement_number(text: str, guide: Guide | None) -> str | None:
    """Last 4 of the account number a one-account statement shows, read by code. None
    unless exactly one distinct number is found (a consolidated statement shows several)."""
    if not guide or not guide.account_number:
        return None
    found = {re.sub(r"[^A-Z0-9]", "", m.group(1).upper())
             for m in re.finditer(guide.account_number, text, re.I)}
    return next(iter(found))[-4:] if len(found) == 1 else None
