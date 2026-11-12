"""Ingestion as a LangGraph workflow: one branch of the nightly or Refresh run.

    load -> extract -> validate --ok--> commit: per account, store it or hold it
                ^          |
                |      failed, 1st try: retry once with the validator's errors
                +----------+
                           |
                       failed again -> inbox (review; never guess a value)

One document (a portal page or a statement) can hold several accounts. Each is
validated and stored on its own, under its own registry account, so every
account keeps its own history. Input is a file (`path`) or text a fetcher
captured (`text`, plus `evidence`, the folder View Source shows). The order is
fixed in code; the LLM only does the extraction step.
"""

from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from . import config, registry, store
from .documents import read_text
from .extractor import ExtractionError, extract
from .schemas import Extraction, Snapshot
from .timeutil import now_iso, parse_iso
from .validator import unlisted_accounts, validate, validate_extraction

MAX_ATTEMPTS = 2   # retry semantics are deferred (spec C2); current behavior kept


class IngestState(TypedDict, total=False):
    path: str
    text: str
    evidence: str
    institution: str | None   # optional hint, used to fetch the playbook
    login: dict | None        # registry login, when the document came from one
    source: str               # portal | statement | manual
    run_id: int | None
    retrieved_at: str         # when the page or file was captured
    attempts: int
    extraction: dict | None
    errors: list[str]
    warnings: list[str]
    results: list[dict]       # one per account: {account, account_id, outcome, ...}
    outcome: str              # stored | held | review | mixed
    snapshot_id: int
    review_id: int
    details: list[dict]       # account details pages: [{label, suffix}], number read by code


def apply_detail_numbers(ext: Extraction, details: list[dict]) -> Extraction:
    """An account's number comes from its own details page, read by code, not from
    the model: the account whose name is the link that was clicked gets that page's
    last 4. Only exact, one-to-one name matches are used."""
    from .browser.portal import link_name
    by_name: dict[str, set] = {}
    for d in details:
        if d.get("suffix"):
            by_name.setdefault(link_name(d["label"]), set()).add(d["suffix"])
    accounts = []
    for rows in ext.accounts:
        found = by_name.get(link_name(rows.account), set())
        if len(found) == 1 and sum(link_name(r.account) == link_name(rows.account)
                                   for r in ext.accounts) == 1:
            rows = rows.model_copy(update={"account_suffix": next(iter(found))})
        accounts.append(rows)
    return ext.model_copy(update={"accounts": accounts})


def _snapshot(ext: Extraction, rows, institution: str, source: str,
              liability: bool = False) -> Snapshot:
    """A liability (loan, card) is owed, so its values are stored negative."""
    sign = -1 if liability else 1
    holdings = [h.model_copy(update={"value": sign * abs(h.value),
                                     "price": None if liability else h.price,
                                     "quantity": None if liability else h.quantity})
                for h in rows.holdings]
    total = rows.statement_total
    return Snapshot(institution=institution, account=rows.account,
                    as_of_datetime=ext.as_of_datetime,
                    statement_total=None if total is None else sign * abs(total),
                    holdings=holdings, source=source)


def _liability(login, known, rows) -> bool:
    if known:
        return bool(known.get("is_liability"))
    return bool(login) and registry.is_liability_category(login, rows)


def _closed_account(conn, login: dict | None, rows, ext: Extraction, state) -> dict:
    """The portal marks this account closed. A new one is recorded as closed, with no
    inbox item. An account we track as open gets a suggestion: only the user changes
    an account's status (spec §5.4)."""
    if not login:
        return {"account": rows.account, "account_id": None, "outcome": "closed"}
    closed_on = ext.as_of_datetime.astimezone().date().isoformat()
    known = registry.find_account(conn, login["id"], rows)
    if not known:
        acct = registry.resolve_account(conn, login, rows)
        registry.mark_closed(conn, acct["id"], closed_on)
        return {"account": acct["display_name"], "account_id": acct["id"], "outcome": "closed"}
    if known["status"] in ("closed", "ignored"):
        return {"account": known["display_name"], "account_id": known["id"], "outcome": "closed"}
    already = conn.execute("SELECT 1 FROM review_queue WHERE kind='registry_suggestion'"
                           " AND status='open' AND account_id=?", (known["id"],)).fetchone()
    if not already:
        store.add_inbox_item(
            conn, "registry_suggestion",
            f"{login['name']} now shows {known['display_name']} as closed. Approve to mark it "
            f"closed as of {closed_on}; it then drops out of net worth.",
            institution=login["institution"], account=known["display_name"],
            account_id=known["id"], login_id=login["id"], evidence=state["evidence"],
            payload={"account_id": known["id"], "change": {"status": "closed",
                                                           "closed_on": closed_on}})
    return {"account": known["display_name"], "account_id": known["id"],
            "outcome": "closure_suggested"}


def build_ingest_graph(model, conn, today=None):
    def load(state: IngestState) -> IngestState:
        text = state.get("text") or read_text(state["path"])
        return {"text": text, "attempts": 0, "errors": [], "results": [],
                "evidence": state.get("evidence") or state.get("path"),
                "retrieved_at": state.get("retrieved_at") or now_iso()}

    def do_extract(state: IngestState) -> IngestState:
        login = state.get("login")
        inst = login["institution"] if login else state.get("institution")
        playbook = store.get_playbook(conn, inst) if inst else None
        if state.get("source") == "statement":      # the institution's statement layout
            from .statement_guides import guide_for
            guide = guide_for(inst)
            if guide:
                playbook = "\n\n".join(x for x in (guide.notes, playbook) if x)
        attempts = state["attempts"] + 1
        try:
            ext = extract(model, state["text"], playbook, state.get("errors") or None,
                          captured_at=state["retrieved_at"] if state.get("source") == "portal" else None,
                          statement=state.get("source") == "statement")
        except ExtractionError as e:
            return {"extraction": None, "errors": [str(e)], "attempts": attempts}
        ext = apply_detail_numbers(ext, state.get("details") or [])
        if ext.as_of_datetime is None and state.get("source") == "statement":
            # A statement is dated by its period; the upload time would misdate it.
            return {"extraction": None, "attempts": attempts, "errors": [
                "No statement date was found. Give as_of as the closing date of the "
                "statement period."]}
        if ext.as_of_datetime is None:   # no date shown: use the capture time (spec D16)
            ext = ext.model_copy(update={"as_of_datetime": parse_iso(state["retrieved_at"])})
        return {"extraction": ext.model_dump(mode="json"), "attempts": attempts}

    def check(state: IngestState) -> IngestState:
        if state.get("extraction") is None:
            return {}  # keep the extraction error
        ext = Extraction.model_validate(state["extraction"])
        login = state.get("login")
        inst = login["institution"] if login else (ext.institution or state.get("institution") or "")
        errors = validate_extraction(ext) + unlisted_accounts(state["text"], ext)
        if state.get("source") == "portal":   # V13: a live page's date is about now
            age = parse_iso(state["retrieved_at"]) - ext.as_of_datetime
            if age.days > config.PORTAL_MAX_AGE_DAYS:
                errors.append(
                    f"As-of {ext.as_of_datetime.astimezone():%Y-%m-%d} is {age.days} days before "
                    f"this live page was captured ({state['retrieved_at'][:10]}). Use the date the "
                    "page shows, with the right year, or null if it shows none.")
        warnings: list[str] = []
        for rows in ext.accounts:
            if rows.closed or (login and registry.skip_reason(login, rows)):
                continue          # nothing is stored for these
            known = registry.find_account(conn, login["id"], rows) if login else None
            liability = _liability(login, known, rows)
            snap = _snapshot(ext, rows, inst, state.get("source") or "statement", liability)
            if known:
                snap = snap.model_copy(update={"account_id": known["id"]})
            r = validate(snap, previous=store.previous_snapshot(conn, snap), today=today,
                         liability=liability)
            errors += [f"{rows.account}: {e}" for e in r.errors]
            warnings += [f"{rows.account}: {w}" for w in r.warnings]
        return {"errors": errors, "warnings": warnings}

    def route(state: IngestState) -> str:
        if state.get("extraction") is not None and not state["errors"]:
            return "commit"
        return "extract" if state["attempts"] < MAX_ATTEMPTS else "review"

    def commit(state: IngestState) -> IngestState:
        """Per account: store it, or hold it for approval. Never both."""
        ext = Extraction.model_validate(state["extraction"])
        login = state.get("login")
        inst = login["institution"] if login else (ext.institution or state.get("institution") or "")
        results = []
        if login:   # an account whose number was misread before keeps its entry
            registry.relink_accounts(conn, login, [r for r in ext.accounts if not r.closed
                                                   and not registry.skip_reason(login, r)])
        for rows in ext.accounts:
            why = registry.skip_reason(login, rows) if login else None
            if why:   # a login rule leaves it out (e.g. Merrill accounts on a Bank of America page)
                known = registry.find_account(conn, login["id"], rows)
                if known and known["status"] != "ignored":
                    registry.ignore_account(conn, known["id"], why)
                results.append({"account": rows.account, "account_id": known and known["id"],
                                "outcome": "skipped", "why": why})
                continue
            if rows.closed:
                results.append(_closed_account(conn, login, rows, ext, state))
                continue
            known = registry.find_account(conn, login["id"], rows) if login else None
            snap = _snapshot(ext, rows, inst, state.get("source") or "statement",
                             _liability(login, known, rows))
            holds: list[str] = []
            acct, new_account = None, False
            if login:
                acct = registry.resolve_account(conn, login, rows)
                if acct["status"] in ("ignored", "closed"):
                    results.append({"account": acct["display_name"], "account_id": acct["id"],
                                    "outcome": "skipped"})
                    continue
                snap = snap.model_copy(update={"account": acct["display_name"],
                                               "account_id": acct["id"]})
                new_account = acct["status"] == "pending"
                if new_account:   # first time seen: the user decides whether to track it
                    holds.append(f"New account found under {login['name']}: {rows.account}"
                                 + (f" …{rows.account_suffix}" if rows.account_suffix else "")
                                 + (f" ({rows.category})" if rows.category else "")
                                 + ". Include it in your portfolio, or exclude it from all "
                                 "future fetches and reports? Check the value with View Source.")
            prev = store.previous_snapshot(conn, snap)
            holds += validate(snap, previous=prev, today=today,
                              liability=_liability(login, acct, rows)).holds
            if holds:
                if snap.account_id:   # a newer reading replaces an unanswered older one
                    conn.execute("UPDATE review_queue SET status='superseded', resolved_at=?"
                                 " WHERE kind='held_value' AND status='open' AND account_id=?",
                                 (now_iso(), snap.account_id))
                rid = store.add_inbox_item(
                    conn, "held_value", " ".join(holds), institution=inst,
                    account=snap.account, account_id=snap.account_id,
                    login_id=login["id"] if login else None, evidence=state["evidence"],
                    payload={"snapshot": snap.model_dump(mode="json"),
                             "new_account": new_account,
                             "run_id": state.get("run_id"),
                             "retrieved_at": state["retrieved_at"]})
                results.append({"account": snap.account, "account_id": snap.account_id,
                                 "outcome": "held", "review_id": rid, "holds": holds})
            else:
                sid = store.write_snapshot(conn, snap, evidence=state["evidence"],
                                           retrieved_at=state["retrieved_at"],
                                           run_id=state.get("run_id"))
                results.append({"account": snap.account, "account_id": snap.account_id,
                                "outcome": "stored", "snapshot_id": sid})
        outcomes = {r["outcome"] for r in results} - {"skipped", "closed", "closure_suggested"}
        out: IngestState = {"results": results,
                            "outcome": outcomes.pop() if len(outcomes) == 1 else
                            ("mixed" if outcomes else "skipped")}
        if len(results) == 1:   # single-account documents: keep the simple fields
            for k in ("snapshot_id", "review_id"):
                if k in results[0]:
                    out[k] = results[0][k]
        return out

    def review(state: IngestState) -> IngestState:
        login = state.get("login") or {}
        rid = store.add_review_item(
            conn, reason="; ".join(state["errors"]),
            institution=login.get("institution") or state.get("institution"),
            account=login.get("name"), login_id=login.get("id"), evidence=state["evidence"],
            payload={"extraction": state.get("extraction")} if state.get("extraction") else None)
        return {"outcome": "review", "review_id": rid}

    g = StateGraph(IngestState)
    g.add_node("load", load)
    g.add_node("extract", do_extract)
    g.add_node("validate", check)
    g.add_node("commit", commit)
    g.add_node("review", review)
    g.add_edge(START, "load")
    g.add_edge("load", "extract")
    g.add_edge("extract", "validate")
    g.add_conditional_edges("validate", route, ["commit", "extract", "review"])
    g.add_edge("commit", END)
    g.add_edge("review", END)
    return g.compile()


def approve_held(conn, item_id: int) -> int:
    """User approved a held value: write it to the store and activate a new account."""
    item = store.get_inbox_item(conn, item_id)
    if not item or item["kind"] != "held_value" or item["status"] != "open":
        raise ValueError("Not an open held value.")
    snap = Snapshot.model_validate(item["payload"]["snapshot"])
    sid = store.write_snapshot(conn, snap, evidence=item["evidence"],
                               retrieved_at=item["payload"].get("retrieved_at"),
                               run_id=item["payload"].get("run_id"))
    if snap.account_id:
        registry.activate_account(conn, snap.account_id)
    store.resolve_inbox_item(conn, item_id, "approved")
    return sid
