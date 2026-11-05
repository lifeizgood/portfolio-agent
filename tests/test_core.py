"""Tests that run without downloading a model: a fake chat model plays the LLM.

    pytest -q
"""

import json
from datetime import date
from pathlib import Path

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel

from portfolio_agent import store
from portfolio_agent.ingest_graph import build_ingest_graph
from portfolio_agent.jsonutil import extract_json
from portfolio_agent.react_graph import build_react_graph
from portfolio_agent.schemas import Holding, Snapshot
from portfolio_agent.tools import build_tools, run_tool, safe_eval
from portfolio_agent.validator import validate

SAMPLES = Path(__file__).resolve().parent.parent / "data" / "samples"
TODAY = date(2026, 11, 4)

GOOD = {
    "institution": "Example Brokerage", "account": "Joint Taxable",
    "as_of_date": "2026-09-30", "statement_total": 187451.50,
    "holdings": [
        {"symbol": "VTI", "description": "Vanguard Total Stock Market ETF",
         "quantity": 420, "price": 318.25, "value": 133665.00},
        {"symbol": "BND", "description": "Vanguard Total Bond Market ETF",
         "quantity": 510, "price": 73.10, "value": 37281.00},
        {"symbol": None, "description": "Money market sweep", "quantity": None,
         "price": None, "value": 16505.50},
    ],
}


@pytest.fixture
def conn():
    return store.connect(":memory:")


def snap(**over) -> Snapshot:
    return Snapshot.model_validate({**GOOD, **over})


# --- validator ---------------------------------------------------------------

def test_valid_snapshot_passes():
    assert validate(snap(), today=TODAY).ok


def test_sum_mismatch_rejected():
    r = validate(snap(statement_total=200000), today=TODAY)
    assert not r.ok and "statement total" in r.errors[0]


def test_qty_price_mismatch_rejected():
    bad = json.loads(json.dumps(GOOD))
    bad["holdings"][0]["value"] = 13366.50  # dropped a digit
    bad["statement_total"] = None
    r = validate(Snapshot.model_validate(bad), today=TODAY)
    assert not r.ok and "Vanguard Total Stock" in r.errors[0]


def test_future_and_backwards_dates_rejected():
    assert not validate(snap(as_of_date="2027-01-01"), today=TODAY).ok
    prev = snap()
    assert not validate(snap(as_of_date="2026-06-30"), previous=prev, today=TODAY).ok


def test_large_unexplained_move_rejected_but_explained_move_ok():
    prev = snap(as_of_date="2026-06-30")
    half = snap(statement_total=None, holdings=[
        Holding(description="cash", value=prev.total * 0.5).model_dump()])
    assert not validate(half, previous=prev, today=TODAY).ok
    assert validate(half, previous=prev, today=TODAY, market_move=-0.45).ok


# --- store (bitemporal) ------------------------------------------------------

def test_restatement_keeps_history_and_reads_latest(conn):
    store.write_snapshot(conn, snap(), retrieved_at="2026-10-02T00:00:00")
    restated = snap(statement_total=None, holdings=[
        Holding(description="cash", value=190000).model_dump()])
    store.write_snapshot(conn, restated, retrieved_at="2026-10-20T00:00:00")
    rows = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    assert rows == 2                                  # nothing overwritten
    hist = store.history(conn)
    assert len(hist) == 1 and hist[0]["total"] == 190000  # latest retrieval wins
    assert store.current_positions(conn)[0]["retrieved_at"] == "2026-10-20T00:00:00"


# --- helpers -----------------------------------------------------------------

def test_extract_json_handles_fences_and_prose():
    assert extract_json('Sure!\n```json\n{"a": {"b": "}"}}\n```') == {"a": {"b": "}"}}


def test_safe_eval_blocks_code():
    assert safe_eval("(105,000 - 98000) / 98000") == pytest.approx(7000 / 98000)
    with pytest.raises(ValueError):
        safe_eval("__import__('os').system('ls')")


# --- ingest graph ------------------------------------------------------------

def test_ingest_stores_valid_statement(conn):
    model = FakeListChatModel(responses=["```json\n" + json.dumps(GOOD) + "\n```"])
    out = build_ingest_graph(model, conn, today=TODAY).invoke(
        {"path": str(SAMPLES / "brokerage_2026-09-30.txt")})
    assert out["outcome"] == "stored" and out["attempts"] == 1
    assert store.history(conn)[0]["total"] == pytest.approx(187451.50)


def test_ingest_retries_once_then_succeeds(conn):
    wrong = {**GOOD, "statement_total": 18745.15}  # misread total
    model = FakeListChatModel(responses=[json.dumps(wrong), json.dumps(GOOD)])
    out = build_ingest_graph(model, conn, today=TODAY).invoke(
        {"path": str(SAMPLES / "brokerage_2026-09-30.txt")})
    assert out["outcome"] == "stored" and out["attempts"] == 2


def test_ingest_sends_persistent_failure_to_review(conn):
    model = FakeListChatModel(responses=["not json", "still not json"])
    out = build_ingest_graph(model, conn, today=TODAY).invoke(
        {"path": str(SAMPLES / "bad_statement_mismatch.txt")})
    assert out["outcome"] == "review"
    assert len(store.open_review_items(conn)) == 1
    assert store.history(conn) == []                  # nothing guessed into memory


# --- ReAct graph -------------------------------------------------------------

def test_react_uses_tools_then_finishes(conn):
    store.write_snapshot(conn, snap())
    steps = [
        {"thought": "I need current values.", "action": "current_positions", "action_input": {}},
        {"thought": "Compute the change in VTI.", "action": "calculate",
         "action_input": {"expression": "420 * (324.10 - 318.25)"}},
        {"thought": "Done.", "action": "finish",
         "action_input": {"answer": "Total 187,451.50 as of 2026-09-30."}},
    ]
    model = FakeListChatModel(responses=[json.dumps(s) for s in steps])
    out = build_react_graph(model, conn).invoke({"question": "What is my net worth?"})
    assert out["answer"].startswith("Total 187,451.50")
    assert [t["action"] for t in out["trace"]] == ["current_positions", "calculate", "finish"]
    assert "187451.5" in out["trace"][0]["observation"]
    assert float(out["trace"][1]["observation"]) == pytest.approx(2457.0)


def test_react_recovers_from_bad_json_and_unknown_tool(conn):
    replies = [
        "I think I should check positions",                       # not JSON
        json.dumps({"thought": "x", "action": "sell_all", "action_input": {}}),
        json.dumps({"thought": "ok", "action": "finish", "action_input": {"answer": "done"}}),
    ]
    model = FakeListChatModel(responses=replies)
    out = build_react_graph(model, conn).invoke({"question": "q"})
    assert out["answer"] == "done"
    assert "unknown tool" in out["trace"][1]["observation"]   # no trading tool exists


def test_react_stops_at_budget(conn):
    loop = json.dumps({"thought": "again", "action": "review_queue", "action_input": {}})
    model = FakeListChatModel(responses=[loop] * 10)
    out = build_react_graph(model, conn, budget=3).invoke({"question": "q"})
    assert out["steps"] == 3 and "couldn't answer" in out["answer"]


def test_tools_are_read_only(conn):
    names = set(build_tools(conn))
    assert not names & {"trade", "transfer", "write_snapshot", "sell", "buy"}
    assert "error" in run_tool(build_tools(conn), "get_quote", {"symbol": "ZZZZ"})
