"""Orchestrator for Stage 1 (spec §3, §4): picks logins, runs each branch
(fetch -> extract every account -> validate -> store per account), and records
the run.

A branch is one login. One fetch captures the page(s) showing all of that
login's accounts; each account is then stored under its own registry entry.
Routing is by rule. When a branch fails in a way the rules don't cover, it
stops and leaves a question in the inbox for the user's next visit (spec D9).
Branches run one at a time, within the spec's limit of 3 in parallel.
"""

import time
from typing import Callable

from . import config, registry, store
from .browser.guard import FetchStopped
from .ingest_graph import build_ingest_graph

Fetcher = Callable[[dict, dict | None, object], object]


def select_logins(conn, kind: str, login_ids: list[int] | None = None) -> list[dict]:
    logins = [lg for lg in registry.list_logins(conn)
              if lg["status"] == "active" and lg["access_method"] == "browser"]
    if login_ids:
        return [lg for lg in logins if lg["id"] in login_ids]
    if kind == "refresh":
        return [lg for lg in logins if lg["holds_public"]]
    return [lg for lg in logins if lg["cadence"] == "nightly"]   # quarterly funds: Stage 4


def run_logins(conn, kind: str, model_loader: Callable[[], object],
               fetcher: Fetcher | None = None, login_ids: list[int] | None = None,
               run_id: int | None = None) -> int:
    from .browser.portal import FetchContext, fetch_portal

    fetcher = fetcher or fetch_portal
    run_id = run_id or store.start_run(conn, kind)
    if config.KILL_FILE.exists():
        store.finish_run(conn, run_id, "stopped", "Kill switch is on.")
        return run_id

    counts: dict[str, int] = {}
    chosen = select_logins(conn, kind, login_ids)
    store.set_run_logins(conn, run_id, [lg["id"] for lg in chosen])
    for login in chosen:
        item = store.start_run_item(conn, run_id, login["id"])
        ctx = FetchContext(conn=conn, run_kind=kind, model_loader=model_loader,
                           request_code=lambda reason, lg=login: _ask_for_code(conn, lg, reason))
        where = {"institution": login["institution"], "account": login["name"],
                 "login_id": login["id"]}
        outcome, detail, evidence = "error", "", None
        try:
            creds = registry.get_credentials(conn, login["id"])
            ev = fetcher(login, creds, ctx)
            evidence = ev.folder
            graph = build_ingest_graph(model_loader(), conn)
            out = graph.invoke({"text": ev.text, "evidence": ev.folder, "login": login,
                                "source": "portal", "run_id": run_id,
                                "retrieved_at": ev.captured_at,
                                "details": getattr(ev, "details", None) or []})
            outcome = out["outcome"]
            if out.get("results"):
                detail = "; ".join(f"{r['account']}: {r['outcome']}" for r in out["results"])
            else:
                detail = "; ".join(out.get("errors") or [])
            for r in out.get("results") or []:
                counts[r["outcome"]] = counts.get(r["outcome"], 0) + 1
            if outcome == "review":
                counts["review"] = counts.get("review", 0) + 1
            if ev.proposal:
                store.add_inbox_item(
                    conn, "playbook_proposal",
                    f"Save this route for {login['institution']}: {ev.proposal['why']}",
                    evidence=ev.folder, payload=ev.proposal, **where)
        except FetchStopped as e:
            outcome, detail = "stopped", e.message
            evidence = getattr(e, "evidence_folder", None)
            counts["stopped"] = counts.get("stopped", 0) + 1
            kind_ = "alert" if e.kind in FetchStopped.ALERT_KINDS else "question"
            if e.kind != "kill":
                store.add_inbox_item(conn, kind_, e.message, payload={"stop": e.kind}, **where)
        except Exception as e:   # not covered by the rules: ask the user (spec D9)
            outcome, detail = "error", f"{type(e).__name__}: {e}"
            evidence = getattr(e, "evidence_folder", None)
            counts["error"] = counts.get("error", 0) + 1
            store.add_inbox_item(
                conn, "question", f"The {kind} run failed unexpectedly for {login['name']}: "
                f"{detail[:300]}. What should change?", payload={"stop": "unexpected"}, **where)
        store.finish_run_item(conn, item, outcome, detail, ctx.trace.steps, evidence)

    summary = ", ".join(f"{n} {k}" for k, n in sorted(counts.items())) or "no logins due"
    store.finish_run(conn, run_id, "done", summary)
    return run_id


def _ask_for_code(conn, login: dict, reason: str) -> str | None:
    """Ask in the inbox and wait for the user's code (spec C12 fallback)."""
    item = store.add_inbox_item(conn, "twofa_code", reason, institution=login["institution"],
                                account=login["name"], login_id=login["id"])
    deadline = time.monotonic() + config.TWOFA_WAIT_S
    while time.monotonic() < deadline:
        code = store.take_answer(conn, item)
        if code:
            return code
        time.sleep(2)
    store.resolve_inbox_item(conn, item, "expired")
    return None
