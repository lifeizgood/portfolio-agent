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


@dataclass(frozen=True)
class Guide:
    keywords: tuple[str, ...]          # matched against the login's institution, lowercased
    notes: str
    keep_through: str | None = None    # regex: text after the LAST match is cut
    cut_from: str | None = None        # else regex: text from the first match on is cut


ROCKEFELLER = Guide(
    keywords=("rockefeller", "rcm"),
    keep_through=r"^TOTAL PORTFOLIO VALUE\b.*$",
    cut_from=r"^Footnotes and Cost Basis Information",    # an account with no positions
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
- Amounts in parentheses are negative. Options written (sold): the quantity in parentheses is
  negative and so is the market value; keep both negative. An option's price is per share and
  one contract is 100 shares. symbol = the option symbol line under it (e.g. ABC260116C50).
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
- Transactions, pending transactions and endnotes are not holdings.""")

GUIDES = (ROCKEFELLER, SCHWAB)


def guide_for(institution: str | None) -> Guide | None:
    name = (institution or "").lower()
    words = set(re.findall(r"[a-z0-9]+", name))
    for g in GUIDES:
        if any(k in words or (len(k) > 4 and k in name) for k in g.keywords):
            return g
    return None


def trim(text: str, guide: Guide | None) -> str:
    """Cut what follows the holdings, when the guide says where they end."""
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
