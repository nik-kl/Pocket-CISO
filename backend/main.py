"""Pocket CISO — FastAPI service (port 8000). Thin HTTP layer over rag_engine.py.

Run:  python main.py        (or: uvicorn main:app --host 127.0.0.1 --port 8000)
Env:  OLLAMA_BASE_URL, LLM_MODEL, EMBED_MODEL, MIN_SCORE, CORS_ORIGINS, POCKET_CISO_DATA_DIR
"""
from __future__ import annotations

import os

import uvicorn
from fastapi import FastAPI, Header, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import rag_engine as rag

app = FastAPI(title="Pocket CISO RAG Engine", version=rag.VERSION)

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


@app.get("/health")
def health() -> dict:
    return rag.health()


@app.post("/query")
def query(body: QueryBody, authorization: str | None = Header(default=None)) -> dict:
    """Pipeline B — stateless: only the question is accepted, there is no session/history parameter."""
    return rag.run_query(bearer(authorization), body.question)


@app.post("/stage-file")
async def stage_file(request: Request, authorization: str | None = Header(default=None)) -> dict:
    """Pipeline A — multipart (file + author + minRole [+ replaces, title]) OR JSON text."""
    token = bearer(authorization)
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

    body = StageTextBody.model_validate(await request.json())
    if body.newText.strip():
        return await run_in_threadpool(lambda: rag.stage_text(token, title=body.title or body.targetDoc, target_doc=body.targetDoc, new_text=body.newText, min_role=body.minRole, author=body.author))
    if body.text.strip() and body.docName.strip():
        return await run_in_threadpool(lambda: rag.stage_document(token, doc_name=rag.safe_doc_name(body.docName), text=body.text, title=body.title or None, min_role=body.minRole, author=body.author, replaces=body.replaces))
    return err(400, "NO_INPUT", "newText bzw. text + docName erforderlich.")


@app.post("/approve/{diff_id}")
def approve(diff_id: str, body: ApproveBody | None = None, authorization: str | None = Header(default=None)) -> dict:
    return rag.approve_diff(bearer(authorization), diff_id, bool(body and body.override))


if __name__ == "__main__":
    uvicorn.run(app, host=os.getenv("HOST", "127.0.0.1"), port=int(os.getenv("PORT", "8000")))
