"""Statement ingestion as a LangGraph workflow.

    load -> extract -> validate --ok--> store
                ^          |
                |      failed, 1st try: retry once with the validator's errors
                +----------+
                           |
                       failed again -> review queue (never guess a value)

The order is fixed in code; the LLM only does the extraction step. That keeps
small or local models reliable and makes each step testable on its own.
"""

from typing import TypedDict

from langgraph.graph import END, START, StateGraph

from . import store
from .documents import read_text
from .extractor import ExtractionError, extract_snapshot
from .schemas import Snapshot
from .validator import validate

MAX_ATTEMPTS = 2


class IngestState(TypedDict, total=False):
    path: str
    institution: str | None   # optional hint, used to fetch the playbook
    text: str
    attempts: int
    snapshot: dict | None
    errors: list[str]
    warnings: list[str]
    outcome: str              # "stored" | "review"
    snapshot_id: int
    review_id: int


def build_ingest_graph(model, conn, today=None):
    def load(state: IngestState) -> IngestState:
        return {"text": read_text(state["path"]), "attempts": 0, "errors": []}

    def extract(state: IngestState) -> IngestState:
        playbook = store.get_playbook(conn, state["institution"]) if state.get("institution") else None
        attempts = state["attempts"] + 1
        try:
            snap = extract_snapshot(model, state["text"], playbook, state.get("errors") or None)
            return {"snapshot": snap.model_dump(mode="json"), "attempts": attempts}
        except ExtractionError as e:
            return {"snapshot": None, "errors": [str(e)], "attempts": attempts}

    def check(state: IngestState) -> IngestState:
        if state.get("snapshot") is None:
            return {}  # keep the extraction error
        snap = Snapshot.model_validate(state["snapshot"])
        prev = store.last_snapshot(conn, snap.institution, snap.account)
        result = validate(snap, previous=prev, today=today)
        return {"errors": result.errors, "warnings": result.warnings}

    def route(state: IngestState) -> str:
        if state.get("snapshot") is not None and not state["errors"]:
            return "save"
        return "extract" if state["attempts"] < MAX_ATTEMPTS else "review"

    def save(state: IngestState) -> IngestState:
        snap = Snapshot.model_validate(state["snapshot"])
        sid = store.write_snapshot(conn, snap, evidence=state["path"])
        return {"outcome": "stored", "snapshot_id": sid}

    def review(state: IngestState) -> IngestState:
        snap = state.get("snapshot") or {}
        rid = store.add_review_item(
            conn, reason="; ".join(state["errors"]),
            institution=snap.get("institution") or state.get("institution"),
            account=snap.get("account"), evidence=state["path"], payload=snap or None)
        return {"outcome": "review", "review_id": rid}

    g = StateGraph(IngestState)
    g.add_node("load", load)
    g.add_node("extract", extract)
    g.add_node("validate", check)
    g.add_node("save", save)
    g.add_node("review", review)
    g.add_edge(START, "load")
    g.add_edge("load", "extract")
    g.add_edge("extract", "validate")
    g.add_conditional_edges("validate", route, ["save", "extract", "review"])
    g.add_edge("save", END)
    g.add_edge("review", END)
    return g.compile()
