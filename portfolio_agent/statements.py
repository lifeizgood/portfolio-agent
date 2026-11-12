"""Statements the user uploads for a login (access method "upload").

A login can get one statement per account or one consolidated statement for all
of them, so a month may bring several files. Each file is saved privately, then
read like any other document: the extractor finds every account in it, the
validator checks each, and each account is stored (or held for approval) under
its own registry entry. A statement is dated by its period's closing date.

Reading runs as a run of kind "upload", one item per file, so it shows in the
Debug tab and can be cancelled between files.
"""

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Callable

from . import config, store
from .documents import TRUNCATED, LockedPdf, read_text
from .ingest_graph import build_ingest_graph
from .statement_guides import guide_for, trim
from .timeutil import now, now_iso


class UploadError(ValueError):
    pass


# An upload read with one of these outcomes was ingested (see `ingested`).
READ_OK = {"stored", "held", "mixed", "unchanged"}


def ingested(conn, up: dict) -> bool:
    """Was this upload read correctly? Then the same statement uploaded again is ignored.
    Not ingested: never read, or it ended in review, stopped or failed, or you rejected a
    value from it in the Inbox as misread. Such a duplicate is read again. (Excluding a
    new account is your decision about the account, not a misread: that still counts.)"""
    if up["outcome"] not in READ_OK:
        return False
    for r in conn.execute("SELECT payload FROM review_queue WHERE kind='held_value'"
                          " AND status='rejected' AND evidence=?", (up["path"],)):
        payload = json.loads(r["payload"] or "{}")
        # only the latest reading counts: a file re-read after a misread starts afresh
        if payload.get("run_id") == up["run_id"] and not payload.get("new_account"):
            return False
    return True


def _earlier(conn, login_id: int, sha: str, text_sha: str | None) -> dict | None:
    """The earlier copy of this statement that counts: an ingested one if any."""
    found = store.find_uploads(conn, login_id, sha, text_sha)
    return next((u for u in found if ingested(conn, u)), found[0] if found else None)


def _text_hash(path: Path) -> str | None:
    """Hash of the statement's text, so a fresh download of the same statement (a
    different file, same contents) is recognized too."""
    try:
        text = read_text(path, 10**8)
    except Exception:
        return None
    norm = re.sub(r"\s+", " ", text).strip()
    return hashlib.sha256(norm.encode()).hexdigest() if len(norm) > 50 else None


def _safe_name(filename: str) -> str:
    base = Path(filename or "statement.pdf").name
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(base).stem).strip("._")[:80] or "statement"
    return stem + ".pdf"


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    for p in (config.STATEMENTS_DIR, path):
        try:
            os.chmod(p, 0o700)
        except OSError:
            pass


def save_upload(conn, login: dict, filename: str, data: bytes) -> dict:
    """Keep one uploaded PDF. The same file uploaded again for this login isn't saved
    twice: the earlier upload comes back, marked as a duplicate."""
    if not data:
        raise UploadError("The file is empty.")
    if len(data) > config.MAX_UPLOAD_MB * 1024 * 1024:
        raise UploadError(f"The file is over {config.MAX_UPLOAD_MB} MB.")
    if not data.startswith(b"%PDF-"):
        raise UploadError(f"{Path(filename or '').name or 'The file'} isn't a PDF.")
    digest = hashlib.sha256(data).hexdigest()
    seen = _earlier(conn, login["id"], digest, None)
    if seen and seen["sha256"] == digest:   # the very same file: nothing new to keep
        return {**seen, "duplicate": True, "already_read": ingested(conn, seen)}
    folder = config.STATEMENTS_DIR / f"login-{login['id']}"
    _private_dir(folder)
    target = folder / f"{now():%Y%m%dT%H%M%S}-{digest[:8]}-{_safe_name(filename)}"
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    text_digest = _text_hash(target)
    same = _earlier(conn, login["id"], "-", text_digest) if text_digest else None
    if same and ingested(conn, same):    # same statement, downloaded again: ignore it
        target.unlink()
        return {**same, "duplicate": True, "already_read": True}
    uid = store.add_upload(conn, login["id"], Path(filename or target.name).name[:200],
                           str(target), digest, len(data), text_digest)
    return {**store.get_upload(conn, uid), "duplicate": False, "already_read": False}


def read_uploads(conn, run_id: int, login: dict, upload_ids: list[int],
                 model_loader: Callable[[], object]) -> None:
    """Read each uploaded statement in turn and record the run."""
    counts: dict[str, int] = {}
    cancelled = lambda: store.cancel_requested(conn, run_id)
    not_started = 0
    for n, uid in enumerate(upload_ids):
        if cancelled():
            not_started = len(upload_ids) - n
            break
        up = store.get_upload(conn, uid)
        item = store.start_run_item(conn, run_id, login["id"])
        if ingested(conn, up):            # already ingested: ignored, never stored twice
            store.finish_run_item(conn, item, "skipped",
                                  f"{up['filename']}: already read ({up['outcome']}); ignored.",
                                  [], up["path"])
            counts["skipped"] = counts.get("skipped", 0) + 1
            continue
        trace = [{"action": "read_statement", "file": up["filename"], "at": now_iso()}]
        outcome, detail = "error", ""
        try:
            full = read_text(up["path"], 10**8)
            guide = guide_for(login["institution"])
            text = trim(full, guide)      # activity, footnotes and disclosures left out
            if len(text) > config.STATEMENT_MAX_CHARS:
                text = text[:config.STATEMENT_MAX_CHARS] + TRUNCATED
            trace.append({"action": "statement_text", "chars": len(full), "read": len(text),
                          "guide": guide.keywords[0] if guide else None,
                          "truncated": text.endswith(TRUNCATED)})
            if not text.replace("[page", "").strip(" \n0123456789]"):
                raise UploadError("No text could be read from this PDF (it may be a scan).")
            graph = build_ingest_graph(model_loader(), conn)
            out = graph.invoke({"text": text, "evidence": up["path"], "login": login,
                                "source": "statement", "run_id": run_id,
                                "retrieved_at": now_iso()})
            outcome = out["outcome"]
            if out.get("results"):
                detail = "; ".join(f"{r['account']}: {r['outcome']}" for r in out["results"])
            else:
                detail = "; ".join(out.get("errors") or [])
            if text.endswith(TRUNCATED):
                detail += (" (The statement was too long to read in full; later pages were "
                           "left out. If an account is missing, upload its own statement.)")
            for r in out.get("results") or []:
                counts[r["outcome"]] = counts.get(r["outcome"], 0) + 1
            if outcome == "review":
                counts["review"] = counts.get("review", 0) + 1
        except LockedPdf:
            outcome, detail = "stopped", ("This PDF is password-protected. Save an unlocked "
                                          "copy (open it and print to PDF), then upload that.")
            counts["stopped"] = counts.get("stopped", 0) + 1
        except UploadError as e:
            outcome, detail = "stopped", str(e)
            counts["stopped"] = counts.get("stopped", 0) + 1
        except Exception as e:     # not covered by the rules: say so in the run
            outcome, detail = "error", f"{type(e).__name__}: {e}"
            counts["error"] = counts.get("error", 0) + 1
        detail = f"{up['filename']}: {detail}"
        store.finish_run_item(conn, item, outcome, detail, trace, up["path"])
        store.set_upload_result(conn, uid, run_id, outcome, detail)
    if not_started:
        counts["not started"] = not_started
    summary = ", ".join(f"{n} {k}" for k, n in sorted(counts.items())) or "nothing to read"
    if cancelled():
        store.finish_run(conn, run_id, "cancelled", "Cancelled by you. " + summary)
    else:
        store.finish_run(conn, run_id, "done", summary)
