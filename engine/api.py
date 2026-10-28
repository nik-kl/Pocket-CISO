"""Pocket CISO — FastAPI service (port 8000). Thin HTTP layer over the engine package (engine/__init__.py).

Run from the repository root:  engine/.venv/bin/python -m engine.api   (or: uvicorn engine.api:app --host 127.0.0.1 --port 8000)
Env:  OLLAMA_BASE_URL, LLM_MODEL, EMBED_MODEL, CORS_ORIGINS, POCKET_CISO_DATA_DIR,
      AUTO_SYNC=1 (watch data/inbox/ every AUTO_SYNC_SECONDS=30), WATCHED_INBOX_DIR
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import AsyncIterator

import uvicorn
from fastapi import FastAPI, Header, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import engine as rag
from engine.config import AUTO_SYNC, AUTO_SYNC_SECONDS, OLLAMA_BASE_URL


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    stop = rag.start_inbox_watcher()  # only runs with AUTO_SYNC=1
    yield
    if stop:
        stop.set()


app = FastAPI(title="Pocket CISO RAG Engine", version=rag.VERSION, lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in os.getenv("CORS_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000").split(",") if o.strip()],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)


def err(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"ok": False, "code": code, "error": message})


@app.exception_handler(rag.ApiError)
async def _api_error(_: Request, e: rag.ApiError) -> JSONResponse:
    return err(e.status, e.code, e.error)


@app.exception_handler(RequestValidationError)
async def _validation_error(_: Request, e: RequestValidationError) -> JSONResponse:
    return err(422, "BAD_REQUEST", "Ungültige Anfrage: " + "; ".join(f"{'.'.join(str(p) for p in x['loc'][1:])}: {x['msg']}" for x in e.errors()[:3]))


@app.exception_handler(Exception)
async def _unhandled(_: Request, e: Exception) -> JSONResponse:
    print(f"[pocket-ciso] unhandled {type(e).__name__}: {e}")
    return err(500, "INTERNAL", "Interner Fehler.")


def bearer(authorization: str | None) -> str | None:
    return authorization[7:].strip() if authorization and authorization.startswith("Bearer ") else None


class QueryBody(BaseModel):
    question: str = Field(min_length=3, max_length=500)


class StageTextBody(BaseModel):
    title: str = Field(default="", max_length=120)
    targetDoc: str = Field(default="", max_length=120)
    newText: str = Field(default="", max_length=20000)
    text: str = Field(default="", max_length=200000)
    docName: str = Field(default="", max_length=120)
    author: str | None = Field(default=None, max_length=80)
    minRole: str = "L1"
    replaces: str | None = Field(default=None, max_length=120)


class ApproveBody(BaseModel):
    override: bool = False
    overrideReason: str = Field(default="", max_length=500)


MAX_REQUEST_BYTES = rag.MAX_UPLOAD_BYTES + 256 * 1024


def service_status() -> dict:
    """GET /health payload: Ollama models, DB readiness, vector and chunk counts."""
    names = rag.OLLAMA.tags()
    try:
        db: dict | None = rag.read_db()
    except rag.ApiError:
        db = None
    return {
        "ok": True, "status": "ok", "service": "pocket-ciso-rag", "version": rag.VERSION, "python": True,
        "dbReady": db is not None,
        "ollama": {"connected": names is not None, "baseUrl": OLLAMA_BASE_URL, "llmModel": rag.OLLAMA.llm_model(), "judgeModel": rag.OLLAMA.judge_model(), "embedModel": rag.OLLAMA.embed_model(), "models": names or []},
        "engineMode": rag.engine_label(), "vectorCount": rag.VECTORS.count(), "vectorEngine": rag.VECTORS.engine,
        "autoSync": AUTO_SYNC, "autoSyncSeconds": AUTO_SYNC_SECONDS if AUTO_SYNC else None,
        "liveChunks": len(db["live_chunks"]) if db else 0, "pendingDiffs": sum(1 for d in db["staged_diffs"] if d["status"] == "pending") if db else 0,
    }


@app.get("/health")
def health() -> dict:
    return service_status()


@app.post("/query")
def query(body: QueryBody, authorization: str | None = Header(default=None)) -> dict:
    """Pipeline B — stateless: only the question is accepted, there is no session/history parameter."""
    return rag.run_query(bearer(authorization), body.question)


@app.post("/query-fast")
def query_fast(body: QueryBody, authorization: str | None = Header(default=None)) -> dict:
    """Two-stage answers, stage 1: verbatim search hits with a key sentence, no LLM, no audit entry (stage 2 = /query)."""
    return rag.run_fast(bearer(authorization), body.question)


@app.post("/stage-file")
async def stage_file(request: Request, authorization: str | None = Header(default=None)) -> dict:
    """Pipeline A — multipart (file + author + minRole [+ replaces, title]) OR JSON text."""
    token = bearer(authorization)
    raw_length = request.headers.get("content-length", "")
    if not raw_length.isdigit():
        return err(411, "LENGTH_REQUIRED", "Content-Length ist erforderlich.")
    if int(raw_length) > MAX_REQUEST_BYTES:
        return err(413, "TOO_LARGE", "Upload-Anfrage größer als 10 MB.")
    rag.check_role(token, "L2")
    if request.headers.get("content-type", "").startswith("multipart/form-data"):
        form = await request.form()
        upload = form.get("file")
        text_field = str(form.get("text") or "")
        min_role = str(form.get("minRole") or "L1")
        author = (str(form.get("author") or "").strip() or None)
        replaces = (str(form.get("replaces") or "").strip() or None)
        title = (str(form.get("title") or "").strip() or None)
        if upload is not None and hasattr(upload, "read"):
            data = await upload.read(rag.MAX_UPLOAD_BYTES + 1)
            name = rag.safe_doc_name(getattr(upload, "filename", "") or "upload.txt")
            text = await run_in_threadpool(rag.parse_document, name, data)
        elif text_field.strip():
            name = rag.safe_doc_name(str(form.get("docName") or "Eingefuegter_Text.md"))
            text = text_field
        else:
            return err(400, "NO_INPUT", "Datei oder Text erforderlich.")
        return await run_in_threadpool(lambda: rag.stage_document(token, doc_name=name, text=text, title=title, min_role=min_role, author=author, replaces=replaces))

    try:
        body = StageTextBody.model_validate(await request.json())
    except ValueError:  # malformed JSON and pydantic.ValidationError are both ValueErrors
        return err(422, "BAD_REQUEST", "Ungültiger JSON-Body.")
    if body.newText.strip():
        return await run_in_threadpool(lambda: rag.stage_text(token, title=body.title or body.targetDoc, target_doc=body.targetDoc, new_text=body.newText, min_role=body.minRole, author=body.author))
    if body.text.strip() and body.docName.strip():
        return await run_in_threadpool(lambda: rag.stage_document(token, doc_name=rag.safe_doc_name(body.docName), text=body.text, title=body.title or None, min_role=body.minRole, author=body.author, replaces=body.replaces))
    return err(400, "NO_INPUT", "newText bzw. text + docName erforderlich.")


@app.post("/sync-inbox")
def sync_inbox(authorization: str | None = Header(default=None)) -> dict:
    """Zero-touch connector: scan the watched inbox (default data/inbox/) and stage new/modified files for CISO approval (L2+)."""
    return rag.sync_watched_inbox(bearer(authorization))


@app.post("/approve/{diff_id}")
def approve(diff_id: str, body: ApproveBody | None = None, authorization: str | None = Header(default=None)) -> dict:
    return rag.approve_diff(bearer(authorization), diff_id, bool(body and body.override), body.overrideReason if body else "")


if __name__ == "__main__":
    uvicorn.run(app, host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "8000")))
