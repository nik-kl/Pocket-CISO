"""Pocket CISO engine: local RAG & governance (Pipeline A: ingestion/diff/approval, Pipeline B: stateless chat).

Shares data/pocket-ciso-db.json with the Next.js app (same schema, audit hash chain and HMAC tokens) and keeps
embeddings + the lexical index in data/vectors.json. This module is the public interface for the HTTP layer
(engine/api.py), the eval harness and the tests; the implementation lives in the submodules.
"""
from .config import ApiError, DATA_DIR, LLM_KEEP_ALIVE, LLM_NUM_CTX, MAX_UPLOAD_BYTES, now_iso, sha256, VERSION
from .llm import engine_label, OLLAMA
from .store import append_audit, check_role, read_db, VECTORS, write_db
from .ingest.parse import parse_document, safe_doc_name
from .ingest.chunk import chunk_document
from .search.normalize import analyze, cistem, fold, INSTRUCTION_RE, Vocab
from .search.facts import extract_facts, FACTS_VERSION, values_in
from .search.lexical import DF0_CAP, key_terms, lexical_index
from .search.dense import CAND_PER_DOC, diversify, fuse, reset_query_caches as _reset_dense_caches
from .staging import approve_diff, same_series, stage_document, stage_text
from .inbox import start_inbox_watcher, sync_watched_inbox
from .answer.verify import answer_problems, entities, has_value, value_dim
from .answer.gate import (
    DENSE_GATE, MAX_PARTS, NO_ANSWER, definition_term, defines, fix_function_words, question_parts, SEARCH_LABEL, span_windows,
    UNCONFIRMED_LABEL, VALUE_Q, WHO_Q,
)
from .answer.compose import CONTRADICTION_LABEL, display_span
from .answer.pipeline import reset_retrieval_cache, run_fast, run_query
from .answer.keysentence import key_sentence, question_kind

def reset_query_caches() -> None:
    """Query-vector, DB and stage-1 retrieval caches (eval: a second pass must not be served from them)."""
    _reset_dense_caches()
    reset_retrieval_cache()
