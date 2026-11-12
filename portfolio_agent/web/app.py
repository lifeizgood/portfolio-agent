"""FastAPI backend for the local dashboard (spec §1.2). Serves on 127.0.0.1 only.

Every number is computed in code here; the browser just draws it.

Local-only hardening: Host header must be localhost (blocks DNS rebinding), and
every state-changing request needs the X-PA-Client header, which a page on
another origin can't send without a CORS preflight that this server never grants.
"""

import threading
from datetime import date
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import config, registry, series, store
from ..ingest_graph import approve_held
from ..runner import run_logins, select_logins
from ..timeutil import now, parse_iso

STATIC = Path(__file__).parent / "static"
EVIDENCE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".txt": "text/plain",
                  ".json": "text/plain", ".pdf": "application/pdf",
                  ".html": "text/plain"}   # portal HTML is shown as text, never rendered


class CredentialsIn(BaseModel):
    username: str
    password: str


class AnswerIn(BaseModel):
    answer: str


class OwnerNameIn(BaseModel):
    name: str


class KillIn(BaseModel):
    on: bool


class ReadUploadsIn(BaseModel):
    upload_ids: list[int]


_START_LOCK = threading.Lock()


def create_app(db_path: str | None = None, model_loader=None, fetcher=None,
               run_in_thread: bool = True) -> FastAPI:
    app = FastAPI(title="Portfolio Agent", docs_url=None, redoc_url=None)
    app.add_middleware(TrustedHostMiddleware,
                       allowed_hosts=["127.0.0.1", "localhost", "testserver"])

    def _default_loader():
        from ..llm import load_chat_model
        return load_chat_model()

    loader = model_loader or _default_loader

    # Login rules also cover accounts found before a rule was added or changed.
    c0 = store.connect(db_path)
    try:
        for lg in registry.list_logins(c0):
            registry.apply_login_rules(c0, lg)
    finally:
        c0.close()

    @app.middleware("http")
    async def require_client_header(request: Request, call_next):
        if request.method not in ("GET", "HEAD") and request.headers.get("x-pa-client") != "1":
            return JSONResponse({"detail": "missing X-PA-Client header"}, status_code=403)
        resp = await call_next(request)
        resp.headers["Cache-Control"] = "no-store"
        return resp

    def conn():
        c = store.connect(db_path)
        try:
            yield c
        finally:
            c.close()

    # --- dashboard data ----------------------------------------------------

    @app.get("/api/summary")
    def summary(c=Depends(conn)):
        accts = {a["id"]: a for a in registry.list_accounts(c)}
        active = [p for p in store.current_positions(c)
                  if accts.get(p["account_id"], {}).get("status") == "active"]
        return {"net_worth": sum(p["total"] for p in active),            # your share
                "net_worth_full": sum(p["full_total"] for p in active),  # whole accounts
                "has_part_owned": any(p["share_pct"] for p in active),
                "latest_retrieval": max((p["retrieved_at"] for p in active), default=None),
                "last_nightly": store.last_run(c, "nightly"),
                "last_refresh": store.last_run(c, "refresh"),
                "running": store.running_runs(c),
                # Per-login Refresh buttons: these logins are being refreshed (or share an
                # institution with one that is). Dashboard Refresh: is any due login free?
                "busy_login_ids": sorted(busy := store.busy_login_ids(c)),
                "refresh_all_available": any(lg["id"] not in busy
                                             for lg in select_logins(c, "refresh")),
                "inbox_open": len(store.open_review_items(c, registered_only=True)),
                "kill_switch": config.KILL_FILE.exists()}

    @app.get("/api/series")
    def get_series(start: date | None = None, end: date | None = None,
                   granularity: str = "daily", group: str = "account",
                   known_on: date | None = None, basis: str = "share", c=Depends(conn)):
        if basis not in ("share", "full"):
            raise HTTPException(400, "basis must be share or full")
        if granularity not in ("daily", "monthly") or group not in ("account", "institution", "type"):
            raise HTTPException(400, "bad granularity or group")
        ko = None
        if known_on:
            from ..timeutil import end_of_local_day, to_iso
            ko = to_iso(end_of_local_day(known_on))
        return series.build_series(c, start, end, granularity, group, ko, full=basis == "full")

    @app.get("/api/positions")
    def positions(c=Depends(conn)):
        accts = {a["id"]: a for a in registry.list_accounts(c) if a["status"] != "ignored"}
        out = []
        for p in store.current_positions(c):
            a = accts.get(p["account_id"])
            if not a:      # unregistered test data and ignored accounts stay out of the UI
                continue
            p["status"] = series.account_status(c, a)
            p["account"], p["login_name"] = a["display_name"], a["login_name"]
            p["owner_name"] = a["owner_name"]
            out.append(p)
        seen = {p["account_id"] for p in out}
        for a in accts.values():   # registered accounts with no stored value yet
            if a["id"] not in seen and a["status"] != "closed":
                out.append({"account_id": a["id"], "account": a["display_name"],
                            "login_name": a["login_name"], "institution": a["institution"],
                            "total": None, "full_total": None, "share_pct": None,
                            "holdings": [], "snapshot_id": None,
                            "status": series.account_status(c, a)})
        return out

    @app.get("/api/snapshots/{sid}")
    def snapshot(sid: int, c=Depends(conn)):
        s = store.get_snapshot(c, sid)
        if not s:
            raise HTTPException(404)
        return {**s, "evidence_files": _evidence_files(s["evidence"])}

    @app.get("/api/snapshots/{sid}/evidence/{name}")
    def snapshot_evidence(sid: int, name: str, c=Depends(conn)):
        s = store.get_snapshot(c, sid)
        if not s:
            raise HTTPException(404)
        return _serve_evidence(s["evidence"], name)

    # --- inbox -----------------------------------------------------------------

    @app.get("/api/inbox")
    def inbox(c=Depends(conn)):
        items = store.open_review_items(c, registered_only=True)
        for i in items:
            i["evidence_files"] = _evidence_files(i["evidence"])
        return items

    @app.get("/api/inbox/{iid}/evidence/{name}")
    def inbox_evidence(iid: int, name: str, c=Depends(conn)):
        i = store.get_inbox_item(c, iid)
        if not i:
            raise HTTPException(404)
        return _serve_evidence(i["evidence"], name)

    @app.post("/api/inbox/{iid}/approve")
    def approve(iid: int, c=Depends(conn)):
        item = _open_item(c, iid)
        if item["kind"] == "held_value":
            return {"snapshot_id": approve_held(c, iid)}
        if item["kind"] == "playbook_proposal":
            p = item["payload"]
            steps = registry.steps_for(c, p["institution"])
            steps.pop("holdings_url", None)
            for k, v in p["change"].items():
                if k == "holdings_urls":
                    steps[k] = list(dict.fromkeys([*steps.get(k, []), *v]))
                else:
                    steps[k] = v
            store.save_steps(c, p["institution"], steps)
            store.resolve_inbox_item(c, iid, "approved")
            return {"ok": True}
        if item["kind"] == "registry_suggestion" and item["payload"].get("change", {}).get("status") == "closed":
            ch = item["payload"]["change"]
            registry.mark_closed(c, item["payload"]["account_id"], ch["closed_on"])
            store.resolve_inbox_item(c, iid, "approved")
            return {"ok": True}
        raise HTTPException(400, "This item can't be approved; dismiss it instead.")

    @app.post("/api/inbox/{iid}/reject")
    def reject(iid: int, c=Depends(conn)):
        item = _open_item(c, iid)
        p = item["payload"] or {}
        if item["kind"] == "held_value" and p.get("new_account") and item["account_id"]:
            # "Exclude": leave this account out of future fetches and reports.
            registry.ignore_account(c, item["account_id"], "excluded by the user")
        store.resolve_inbox_item(c, iid, "rejected")
        return {"ok": True}

    @app.post("/api/inbox/{iid}/answer")
    def answer(iid: int, body: AnswerIn, c=Depends(conn)):
        item = _open_item(c, iid)
        if item["kind"] != "twofa_code":
            raise HTTPException(400, "Only verification-code requests take an answer.")
        store.set_answer(c, iid, body.answer)
        return {"ok": True}

    # --- registry: logins, their accounts, credentials ---------------------

    @app.get("/api/logins")
    def logins(c=Depends(conn)):
        return registry.list_logins(c)

    @app.post("/api/logins")
    def add_login(body: registry.LoginIn, c=Depends(conn)):
        try:
            return registry.get_login(c, registry.save_login(c, body))
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.put("/api/logins/{lid}")
    def edit_login(lid: int, body: registry.LoginIn, c=Depends(conn)):
        if not registry.get_login(c, lid):
            raise HTTPException(404)
        try:
            return registry.get_login(c, registry.save_login(c, body, lid))
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.put("/api/logins/{lid}/credentials")
    def put_credentials(lid: int, body: CredentialsIn, c=Depends(conn)):
        try:
            registry.set_credentials(c, lid, body.username, body.password)
        except ValueError as e:
            raise HTTPException(404, str(e))
        return {"ok": True}

    @app.get("/api/logins/{lid}/credentials")
    def reveal_credentials(lid: int, c=Depends(conn)):
        """The eye icon (spec D18). Localhost only; see module docstring."""
        creds = registry.get_credentials(c, lid)
        if not creds:
            raise HTTPException(404, "No login saved.")
        return creds

    @app.get("/api/accounts")
    def accounts(c=Depends(conn)):
        return registry.list_accounts(c)

    @app.put("/api/accounts/{aid}")
    def edit_account(aid: int, body: registry.AccountEdit, c=Depends(conn)):
        try:
            return registry.update_account(c, aid, body)
        except ValueError as e:
            raise HTTPException(400, str(e))

    # --- owners (part-owned entities such as an LLC) ---------------------------

    @app.get("/api/owners")
    def owners(c=Depends(conn)):
        return registry.list_owners(c)

    @app.post("/api/owners")
    def add_owner(body: registry.OwnerIn, c=Depends(conn)):
        try:
            return registry.create_owner(c, body)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.put("/api/owners/{oid}")
    def edit_owner(oid: int, body: OwnerNameIn, c=Depends(conn)):
        if not registry.get_owner(c, oid):
            raise HTTPException(404)
        try:
            return registry.rename_owner(c, oid, body.name)
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/api/owners/{oid}/shares")
    def add_owner_share(oid: int, body: registry.OwnerShareIn, c=Depends(conn)):
        try:
            return registry.add_owner_share(c, oid, body)
        except ValueError as e:
            raise HTTPException(404, str(e))

    @app.delete("/api/owners/{oid}")
    def delete_owner(oid: int, c=Depends(conn)):
        try:
            registry.delete_owner(c, oid)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"ok": True}

    # --- runs ------------------------------------------------------------------

    @app.post("/api/refresh")
    def refresh(c=Depends(conn)):
        """Dashboard Refresh: every active login that holds public securities."""
        return start_refresh(c, None)

    @app.post("/api/logins/{lid}/refresh")
    def refresh_login(lid: int, c=Depends(conn)):
        """Refresh one institution's login, whatever its cadence (Accounts & logins tab)."""
        lg = registry.get_login(c, lid)
        if not lg:
            raise HTTPException(404)
        if lg["status"] != "active" or lg["access_method"] != "browser":
            raise HTTPException(400, "Only an active browser login can be refreshed.")
        return start_refresh(c, [lid])

    # --- uploaded statements ----------------------------------------------------

    @app.get("/api/logins/{lid}/statements")
    def list_statements(lid: int, c=Depends(conn)):
        if not registry.get_login(c, lid):
            raise HTTPException(404)
        return [_upload_view(u) for u in store.list_uploads(c, lid)]

    @app.post("/api/logins/{lid}/statements")
    async def upload_statement(lid: int, request: Request, c=Depends(conn)):
        """One PDF per request, sent as the raw body; X-Filename carries its name."""
        from urllib.parse import unquote

        from ..statements import UploadError, save_upload
        lg = registry.get_login(c, lid)
        if not lg:
            raise HTTPException(404)
        if lg["status"] != "active":
            raise HTTPException(400, "This login is closed.")
        size = int(request.headers.get("content-length") or 0)
        if size > config.MAX_UPLOAD_MB * 1024 * 1024:
            raise HTTPException(413, f"The file is over {config.MAX_UPLOAD_MB} MB.")
        data = await request.body()
        try:
            up = save_upload(c, lg, unquote(request.headers.get("x-filename") or ""), data)
        except UploadError as e:
            raise HTTPException(400, str(e))
        return _upload_view(up)

    @app.post("/api/logins/{lid}/statements/read")
    def read_statements(lid: int, body: ReadUploadsIn, c=Depends(conn)):
        """Read the given uploads with the model, in the background, as one run."""
        from ..statements import read_uploads
        lg = registry.get_login(c, lid)
        if not lg:
            raise HTTPException(404)
        ids = list(dict.fromkeys(body.upload_ids))
        ups = [store.get_upload(c, i) for i in ids]
        if not ids or any(u is None or u["login_id"] != lid for u in ups):
            raise HTTPException(400, "Unknown statement for this login.")
        _expire_stuck_runs(c)
        with _START_LOCK:
            if lid in store.busy_login_ids(c):
                raise HTTPException(409, "This login is already being refreshed or read.")
            run_id = store.start_run(c, "upload")
            store.set_run_logins(c, run_id, [lid], "login")

        def work():
            wc = store.connect(db_path)
            try:
                read_uploads(wc, run_id, lg, ids, loader)
            except Exception as e:   # keep the run from looking stuck
                store.finish_run(wc, run_id, "failed", f"{type(e).__name__}: {e}")
            finally:
                wc.close()

        if run_in_thread:
            threading.Thread(target=work, daemon=True).start()
        else:
            work()
        return {"run_id": run_id}

    @app.get("/api/statements/{uid}/file")
    def statement_file(uid: int, c=Depends(conn)):
        up = store.get_upload(c, uid)
        if not up or not Path(up["path"]).is_file():
            raise HTTPException(404)
        return FileResponse(up["path"], media_type="application/pdf",
                            headers={"Content-Security-Policy": "sandbox",
                                     "X-Content-Type-Options": "nosniff"})

    def start_refresh(c, login_ids: list[int] | None) -> dict:
        dashboard = login_ids is None
        """Refreshes run in parallel, one per institution at a time (logins at one
        institution share a browser profile), up to MAX_PARALLEL_RUNS at once."""
        _expire_stuck_runs(c)
        if config.KILL_FILE.exists():
            raise HTTPException(409, "The kill switch is on.")
        with _START_LOCK:   # two clicks at once must not both pass the busy check
            if len(store.running_runs(c)) >= config.MAX_PARALLEL_RUNS:
                raise HTTPException(409, f"{config.MAX_PARALLEL_RUNS} refreshes are already "
                                    "running; try again when one finishes.")
            busy = store.busy_login_ids(c)
            if login_ids is not None:
                if set(login_ids) & busy:
                    raise HTTPException(409, "This login, or another at the same institution, "
                                        "is already being refreshed.")
            else:   # dashboard Refresh: every due login not already being refreshed
                due = [lg["id"] for lg in select_logins(c, "refresh")]
                login_ids = [i for i in due if i not in busy]
                if due and not login_ids:
                    raise HTTPException(409, "Every login is already being refreshed.")
            run_id = store.start_run(c, "refresh")
            store.set_run_logins(c, run_id, login_ids,
                                 "dashboard" if dashboard else "login")

        def work():
            wc = store.connect(db_path)
            try:
                run_logins(wc, "refresh", loader, fetcher=fetcher, login_ids=login_ids,
                           run_id=run_id)
            except Exception as e:   # keep the run from looking stuck
                store.finish_run(wc, run_id, "failed", f"{type(e).__name__}: {e}")
            finally:
                wc.close()

        if run_in_thread:
            threading.Thread(target=work, daemon=True).start()
        else:
            work()
        return {"run_id": run_id}

    @app.post("/api/runs/{rid}/cancel")
    def cancel_run(rid: int, c=Depends(conn)):
        """Cancel button: the run stops at its next check; values it already stored stay."""
        if not store.get_run(c, rid):
            raise HTTPException(404)
        if not store.request_cancel(c, rid):
            raise HTTPException(409, "This run has already finished.")
        return {"ok": True}

    @app.get("/api/runs")
    def runs(limit: int = 100, c=Depends(conn)):
        return store.list_runs(c, min(limit, 500))

    @app.get("/api/runs/{rid}")
    def get_run(rid: int, detail: bool = False, c=Depends(conn)):
        r = store.get_run(c, rid, detail=detail)
        if not r:
            raise HTTPException(404)
        if detail:
            for i in r["items"]:
                i["evidence_files"] = _evidence_files(i["evidence"])
                i.pop("evidence")       # the folder path stays on the server
        return r

    @app.get("/api/runs/{rid}/items/{item_id}/evidence/{name}")
    def run_evidence(rid: int, item_id: int, name: str, c=Depends(conn)):
        r = store.get_run(c, rid, detail=True)
        item = next((i for i in (r or {}).get("items", []) if i["id"] == item_id), None)
        if not item:
            raise HTTPException(404)
        return _serve_evidence(item["evidence"], name)

    @app.post("/api/kill")
    def kill(body: KillIn):
        if body.on:
            config.KILL_FILE.parent.mkdir(parents=True, exist_ok=True)
            config.KILL_FILE.touch()
        elif config.KILL_FILE.exists():
            config.KILL_FILE.unlink()
        return {"kill_switch": config.KILL_FILE.exists()}

    app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
    return app


def _open_item(c, iid: int) -> dict:
    item = store.get_inbox_item(c, iid)
    if not item or item["status"] != "open":
        raise HTTPException(404, "No open item with that id.")
    return item


def _upload_view(up: dict) -> dict:
    """An upload as the UI shows it; the file's location stays on the server."""
    return {k: up.get(k) for k in ("id", "filename", "size", "uploaded_at", "run_id",
                                   "outcome", "detail", "duplicate")}


def _expire_stuck_runs(c) -> None:
    for r in store.running_runs(c):
        if (now() - parse_iso(r["started_at"])).total_seconds() > 2 * 3600:
            store.finish_run(c, r["id"], "failed", "Run never finished (process stopped?).")


def _evidence_files(evidence: str | None) -> list[str]:
    if not evidence:
        return []
    p = Path(evidence)
    if p.is_dir():
        return sorted(f.name for f in p.iterdir() if f.suffix.lower() in EVIDENCE_TYPES)
    return [p.name] if p.is_file() and p.suffix.lower() in EVIDENCE_TYPES else []


def _serve_evidence(evidence: str | None, name: str) -> FileResponse:
    """Serve only files inside the evidence recorded for that row (View Source)."""
    if not evidence or name not in _evidence_files(evidence):
        raise HTTPException(404)
    p = Path(evidence)
    target = (p / name) if p.is_dir() else p
    media = EVIDENCE_TYPES[target.suffix.lower()]
    return FileResponse(target, media_type=media,
                        headers={"Content-Security-Policy": "sandbox",
                                 "X-Content-Type-Options": "nosniff"})


def main():
    import uvicorn
    uvicorn.run(create_app(), host=config.HOST, port=config.PORT, log_level="warning")

