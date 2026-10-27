"""Zero-touch connector: watched inbox folder → staging queue (manual trigger and optional background thread)."""
from __future__ import annotations

import hashlib
import threading
from pathlib import Path
from typing import Any

from .config import (
    AUTO_SYNC, AUTO_SYNC_SECONDS, ApiError, CONNECTOR_ACTOR, CONNECTOR_AUTHOR, INBOX_DIR, MAX_UPLOAD_BYTES,
)
from .ingest.parse import ALLOWED_EXT, parse_document, safe_doc_name
from .llm import engine_label
from .staging import stage_document
from .store import authorize, locked_db, read_db


INBOX_LOCK = threading.Lock()  # manual trigger and background thread must never stage the same file twice


def _inbox_files() -> list[Path]:
    if not INBOX_DIR.is_dir():
        return []
    return sorted(
        p for p in INBOX_DIR.iterdir()
        if p.is_file() and not p.is_symlink() and not p.name.startswith(".") and p.suffix.lower() in ALLOWED_EXT
    )


def _remember_ingested(file: str, digest: str) -> None:
    with locked_db() as db:
        db["meta"].setdefault("ingestedFiles", {})[file] = digest


def _ingest_file(path: Path, trigger: str) -> tuple[str, Any]:
    """Dedupe by hash / pending diff, then stage. Returns ("staged", diff) | ("skipped", reason) | ("failed", error)."""
    name = path.name
    doc_name = safe_doc_name(name)
    if path.stat().st_size > MAX_UPLOAD_BYTES:
        return "failed", f"Datei größer als {MAX_UPLOAD_BYTES // 1024 // 1024} MB."
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()

    db = read_db()
    if db["meta"].get("ingestedFiles", {}).get(name) == digest:
        return "skipped", "unverändert seit dem letzten Sync"
    if any(d["status"] == "pending" and d["docName"] == doc_name for d in db["staged_diffs"]):
        return "skipped", "bereits in der Staging-Queue"

    try:
        text = parse_document(name, data)
        result = stage_document(
            None, doc_name=doc_name, text=text, min_role="L2", author=CONNECTOR_AUTHOR,
            as_actor=CONNECTOR_ACTOR, connector={"file": name, "sha256": digest, "trigger": trigger},
        )
    except ApiError as e:
        if e.code != "NO_CHANGES":
            return "failed", e.error
        _remember_ingested(name, digest)
        return "skipped", "identisch mit der Live-Version"
    return "staged", result["diff"]


def sync_watched_inbox(token: str | None, *, system: bool = False) -> dict[str, Any]:
    """Scan the watched inbox (default data/inbox/; .pdf .docx .md .txt) and stage new/modified files for CISO approval.
    `system=True` is used by the background watcher (no user token); manual triggers require L2+."""
    if system:
        trigger = "Auto-Sync (Hintergrund-Thread)"
    else:
        with locked_db() as db:
            who = authorize(db, token, "L2", "sync_inbox", "watched_inbox")
        trigger = f"{who['email']} ({who['role']})"

    read_db()  # DB_NOT_READY / DB_CORRUPT surface as one clear error instead of one failure per file
    report: dict[str, Any] = {"inbox": "data/inbox", "scanned": 0, "staged": [], "skipped": [], "failed": [], "engineMode": engine_label()}
    with INBOX_LOCK:
        files = _inbox_files()
        report["scanned"] = len(files)
        for path in files:
            try:
                kind, detail = _ingest_file(path, trigger)
            except Exception as e:  # noqa: BLE001 — one broken file must not stop the cycle
                print(f"[pocket-ciso] watched_inbox {path.name}: {type(e).__name__}: {e}")
                kind, detail = "failed", "Datei konnte nicht verarbeitet werden."
            if kind == "staged":
                report["staged"].append({"file": path.name, "diffId": detail["id"], "conflicts": len(detail["conflicts"])})
            elif kind == "skipped":
                report["skipped"].append({"file": path.name, "reason": detail})
            else:
                report["failed"].append({"file": path.name, "error": detail})
    return {"ok": True, "report": report}


def start_inbox_watcher() -> threading.Event | None:
    """AUTO_SYNC=1 → daemon thread that runs sync_watched_inbox every AUTO_SYNC_SECONDS (default 30). Returns the stop event."""
    if not AUTO_SYNC:
        return None
    stop = threading.Event()

    def loop() -> None:
        last_error = ""
        while not stop.wait(AUTO_SYNC_SECONDS):
            try:
                out = sync_watched_inbox(None, system=True)
                last_error = ""
                if out["report"]["staged"]:
                    print(f"[pocket-ciso] auto-sync staged: {', '.join(x['file'] for x in out['report']['staged'])}")
            except Exception as e:  # noqa: BLE001 — e.g. DB not initialised yet; log each distinct problem once
                msg = f"{type(e).__name__}: {getattr(e, 'error', e)}"
                if msg != last_error:
                    print(f"[pocket-ciso] auto-sync waiting: {msg}")
                last_error = msg

    threading.Thread(target=loop, name="inbox-watcher", daemon=True).start()
    return stop
