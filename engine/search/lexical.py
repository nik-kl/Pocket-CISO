"""Lexical index (BM25, spans, facts) per role view, key-term analysis of a question and key-term coverage."""
from __future__ import annotations

import hashlib
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any

from ..config import ROLE_RANK, sha256
from .facts import chunk_facts
from .normalize import (
    FREQ_WORDS, GENERIC_WORDS, GLOSSARY, INSTRUCTION_RE, R_STOP, UNIT_CANON, Vocab, _LEX,
    _cue, _edit_distance, _num, _qty, _unit_at, analyze, cistem, fold,
    split_sentences,
)
from ..store import VECTORS


INDEX_VERSION = "lex-3"
BM25_K1, BM25_B = 1.2, 0.75
RRF_K = 60


def section_title(section: str) -> str:
    return re.sub(r"\s*\(Teil \d+\)$", "", re.sub(r"^§\S*\s*", "", section))


def doc_title(doc_name: str) -> str:
    return re.sub(r"[_]+", " ", re.sub(r"\.[A-Za-z0-9]{1,5}$", "", doc_name))


def corpus_signature(chunks: list[dict[str, Any]]) -> str:
    h = hashlib.sha256(f"{INDEX_VERSION}|{GLOSSARY.digest}".encode())
    for c in sorted(chunks, key=lambda x: x["id"]):
        h.update(f"\x1e{c['id']}\x1f{c['docName']}\x1f{c['section']}\x1f{c['minRole']}\x1f{sha256(c['content'])}".encode())
    return h.hexdigest()


class LexIndex:
    def __init__(self, chunks: list[dict[str, Any]], payload: dict[str, Any] | None = None) -> None:
        self.chunks = sorted(chunks, key=lambda c: c["id"])
        self.sig = corpus_signature(self.chunks)
        if payload and payload.get("sig") == self.sig:
            self.words = set(payload["words"])
            self.doc_terms = [list(t) for t in payload["terms"]]
            self.span_text = [list(s) for s in payload["spanText"]]
            self.span_terms = [[frozenset(t) for t in s] for s in payload["spanTerms"]]
            self.ctx_terms = [frozenset(t) for t in payload["ctx"]]
        else:
            self.words = {w for c in self.chunks for w in _LEX.findall(fold(f"{c['content']} {c['section']} {doc_title(c['docName'])}")) if w.isalpha()}
            vocab = Vocab(self.words | GLOSSARY.words)
            self.doc_terms, self.span_text, self.span_terms, self.ctx_terms = [], [], [], []
            for c in self.chunks:
                ctx = f"{section_title(c['section'])} {doc_title(c['docName'])}"
                self.doc_terms.append(analyze(f"{c['content']} {ctx}", vocab))
                spans = split_sentences(c["content"]) or [c["content"]]
                self.span_text.append(spans)
                self.span_terms.append([frozenset(analyze(s, vocab)) for s in spans])
                self.ctx_terms.append(frozenset(analyze(ctx, vocab)))
        self.pos = {c["id"]: i for i, c in enumerate(self.chunks)}
        # spans: stable ids "<chunk id>:<k>", instruction-like text flagged (never used in answers), section sequences across
        # neighbouring chunks ("§2 X" and "§2 X (Teil 2)") for context windows
        self.span_ids = [[f"{c['id']}:{k}" for k in range(len(self.span_text[i]))] for i, c in enumerate(self.chunks)]
        self.span_instr = [[bool(INSTRUCTION_RE.search(fold(t))) for t in spans] for spans in self.span_text]
        self.section_key = [(c["docName"], section_title(c["section"])) for c in self.chunks]
        self.facts_by_span: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
        for i, c in enumerate(self.chunks):
            for f in chunk_facts(c):
                if 0 <= f["span"] < len(self.span_text[i]):
                    self.facts_by_span[(i, f["span"])].append(f)
        self.section_seq: dict[tuple[str, str], list[tuple[int, int]]] = defaultdict(list)
        part_no = [int((re.search(r"\(Teil (\d+)\)$", c["section"]) or [0, 1])[1]) for c in self.chunks]
        for i in sorted(range(len(self.chunks)), key=lambda x: (self.section_key[x], part_no[x], self.chunks[x]["id"])):
            self.section_seq[self.section_key[i]] += [(i, k) for k in range(len(self.span_text[i]))]
        self.tf = [Counter(t) for t in self.doc_terms]
        self.dl = [len(t) for t in self.doc_terms]
        self._views: dict[str, RoleView] = {}

    def payload(self) -> dict[str, Any]:
        return {"version": INDEX_VERSION, "sig": self.sig, "words": sorted(self.words), "terms": self.doc_terms, "spanText": self.span_text,
                "spanTerms": [[sorted(t) for t in s] for s in self.span_terms], "ctx": [sorted(t) for t in self.ctx_terms]}

    def view(self, role: str) -> "RoleView":
        if role not in self._views:
            self._views[role] = RoleView(self, role)
        return self._views[role]


class RoleView:
    """Everything a role's query may touch: RBAC filter BEFORE scoring — document frequencies, vocabulary (compound
    splitting, typo correction) and candidates come from the chunks the role may read only."""

    def __init__(self, idx: LexIndex, role: str) -> None:
        self.idx = idx
        self.allowed = [i for i, c in enumerate(idx.chunks) if ROLE_RANK[c["minRole"]] <= ROLE_RANK[role]]
        self.n = len(self.allowed)
        self.df: Counter[str] = Counter()
        words: set[str] = set()
        for i in self.allowed:
            self.df.update(set(idx.doc_terms[i]))
            words |= set(_LEX.findall(fold(f"{idx.chunks[i]['content']} {idx.chunks[i]['section']} {doc_title(idx.chunks[i]['docName'])}")))
        self.vocab = Vocab({w for w in words if w.isalpha()} | GLOSSARY.words)
        self.avgdl = (sum(idx.dl[i] for i in self.allowed) / self.n) if self.n else 1.0
        self.postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
        for i in self.allowed:
            for t, f in idx.tf[i].items():
                self.postings[t].append((i, f))
        self.stems_by_initial: dict[str, list[str]] = defaultdict(list)
        for t in sorted(self.df):
            if t.isalpha() and len(t) >= 4:
                self.stems_by_initial[t[0]].append(t)

    def idf(self, t: str) -> float:
        df = self.df.get(t, 0)
        return math.log(1 + (self.n - df + 0.5) / (df + 0.5))

    def bm25(self, weights: dict[str, float]) -> dict[int, float]:
        scores: dict[int, float] = defaultdict(float)
        for t, w in sorted(weights.items()):
            for i, f in self.postings.get(t, ()):
                norm_len = f + BM25_K1 * (1 - BM25_B + BM25_B * self.idx.dl[i] / self.avgdl)
                scores[i] += w * self.idf(t) * f * (BM25_K1 + 1) / norm_len
        return scores

    def fuzzy(self, stem_: str) -> str | None:
        """Typo tolerance: the closest known stem (edit distance ≤ 1, ≤ 2 for long words); ties → higher df, then alphabetical."""
        if len(stem_) < 4:
            return None
        cap = 2 if len(stem_) >= 8 else 1
        best: tuple[int, int, str] | None = None
        for cand in self.stems_by_initial.get(stem_[0], ()):
            d = _edit_distance(stem_, cand, cap)
            if d <= cap:
                key = (d, -self.df[cand], cand)
                if best is None or key < best:
                    best = key
        return best[2] if best else None


_LEX_CACHE: LexIndex | None = None


def lexical_index(db: dict[str, Any]) -> LexIndex:
    """mtime/signature-checked: reuses the in-memory index, else the persisted one (vectors.json → 'lexical'), else rebuilds."""
    global _LEX_CACHE
    sig = corpus_signature(db["live_chunks"])
    if _LEX_CACHE is not None and _LEX_CACHE.sig == sig:
        return _LEX_CACHE
    with VECTORS.lock:
        VECTORS.reload()
        idx = LexIndex(db["live_chunks"], VECTORS.lexical)
        if VECTORS.lexical is None or VECTORS.lexical.get("sig") != sig:
            VECTORS.lexical = idx.payload()
            VECTORS.save()
    _LEX_CACHE = idx
    return idx


def refresh_lexical(live_chunks: list[dict[str, Any]]) -> None:
    """Approval time: rebuild and persist the lexical index (caller holds VECTORS.lock and saves afterwards)."""
    global _LEX_CACHE
    idx = LexIndex(live_chunks)
    VECTORS.lexical = idx.payload()
    _LEX_CACHE = idx


@dataclass
class KeyTerm:
    surface: str
    kind: str  # word | phrase | ident | qty | num
    alts: list[frozenset[str]]  # matched when ANY alternative is fully contained; alts[0] is the question's own form
    weight: float = 1.0
    raw_weight: float | None = None  # weight before the df = 0 cap (DF0_CAP); None = not capped


@dataclass
class QueryTerms:
    keys: list[KeyTerm]
    weights: dict[str, float]  # BM25 query term weights (own form 1.0, compound parts 0.8, glossary/typo alternatives 0.5)
    unit_cues: set[str]


DF0_CAP = 0.25


def key_terms(question: str, view: RoleView) -> QueryTerms:
    toks = _LEX.findall(fold(question))
    # German capitalises nouns: when the question uses casing, lower-case words after the first one (verbs, adjectives)
    # count half; an all-lower-case question keeps equal weights.
    words_cs = re.findall(r"[A-Za-zÄÖÜäöüß]+", question)
    folded_words = [fold(w) for w in words_cs]
    german = sum(w in DE_HINTS for w in folded_words) > sum(w in EN_HINTS for w in folded_words)
    cased = german and any(w[:1].isupper() for w in words_cs[1:])
    lower = {fold(w) for w in words_cs[1:] if w[:1].islower()} if cased else set()
    items: list[KeyTerm] = []
    cues: set[str] = set()
    i, n = 0, len(toks)
    while i < n:
        t = toks[i]
        if t[0].isdigit():
            v = _num(t)
            u = _unit_at(toks, i + 1)
            if u:
                items.append(KeyTerm(f"{t} {toks[i + 1]}", "qty", [frozenset({_qty(v, u)})]))
                cues.add(_cue(u))
                i += 2
                continue
            items.append(KeyTerm(t, "num", [frozenset({v})]))
        elif t != "%":
            if t in UNIT_CANON and len(t) > 3:
                cues.add(_cue(UNIT_CANON[t]))
            elif t in FREQ_WORDS:
                items.append(KeyTerm(t, "word", [frozenset({cistem(t)}), frozenset({FREQ_WORDS[t]})]))
            elif len(t) >= 2:
                w = t
                if w not in R_STOP and re.search(r"ae|oe|ue", w):  # "fuer", "Geschaeftsfuehrung" typed without umlauts
                    alt = w.replace("ae", "a").replace("oe", "o").replace("ue", "u")
                    if alt in R_STOP or cistem(alt) in view.df:
                        w = alt
                if w in R_STOP or (len(w) >= 5 and cistem(w) not in view.df  # typo of a function word ("wiviele")
                                   and any(_edit_distance(w, s, 1) <= 1 for s in R_STOP if abs(len(s) - len(w)) <= 1 and len(s) >= 4)):
                    i += 1
                    continue
                if i + 1 < n and toks[i + 1][0].isdigit() and not _unit_at(toks, i + 2):
                    items.append(KeyTerm(f"{t} {toks[i + 1]}", "ident", [frozenset({w + _num(toks[i + 1])})]))
                    i += 2
                    continue
                s = cistem(w)
                alts = [frozenset({s})]
                if len(w) >= 8:
                    parts = view.vocab.split(w)
                    if parts:
                        alts.append(frozenset(cistem(p) for p in parts))
                items.append(KeyTerm(t, "word", alts, 0.3 if w in GENERIC_WORDS else 0.5 if t in lower else 1.0))
        i += 1

    # glossary: multi-word members first (consume consecutive word items), then single-term members
    merged: list[KeyTerm] = []
    k = 0
    while k < len(items):
        hit = None
        for member, gi in GLOSSARY.multi:
            span = len(member)
            window = items[k : k + span]
            if len(window) == span and all(x.kind == "word" for x in window) and {next(iter(x.alts[0])) for x in window} == set(member):
                hit = (gi, window)
                break
        if hit:
            gi, window = hit
            merged.append(KeyTerm(" ".join(x.surface for x in window), "phrase", [frozenset().union(*(x.alts[0] for x in window))] + [m for m in GLOSSARY.groups[gi]]))
            k += len(hit[1])
            continue
        merged.append(items[k])
        k += 1
    for it in merged:
        if it.kind in ("word", "ident", "num"):
            for gi in GLOSSARY.single.get(next(iter(it.alts[0])), []):
                it.alts += [m for m in GLOSSARY.groups[gi] if m not in it.alts]
        if it.kind == "word" and not any(all(x in view.df for x in a) for a in it.alts):
            near = view.fuzzy(next(iter(it.alts[0])))
            if near:
                it.alts.append(frozenset({near}))
                for gi in GLOSSARY.single.get(near, []):
                    it.alts += [m for m in GLOSSARY.groups[gi] if m not in it.alts]

    weights: dict[str, float] = {}
    for it in merged:
        own = it.alts[0]
        it.weight *= sum(view.idf(x) for x in own) / len(own)
    # words found nowhere in the role's corpus (df = 0, also no glossary/typo alternative) get the highest idf and would
    # dominate the coverage; colloquial words ("Firmenkarre") must not block a semantic hit — cap them at a quarter of the
    # mean known-term weight and leave out-of-scope detection to the answerability judge. Numbers and identifiers keep
    # their weight ('Tier 7' that does not exist must keep blocking).
    known = [it.weight for it in merged if any(all(x in view.df for x in a) for a in it.alts)]
    if known:
        cap = DF0_CAP * sum(known) / len(known)
        for it in merged:
            if it.kind in ("word", "phrase") and not any(all(x in view.df for x in a) for a in it.alts) and it.weight > cap:
                it.raw_weight, it.weight = it.weight, cap
    for it in merged:
        own = it.alts[0]
        for a_i, alt in enumerate(it.alts):
            w = (1.0 if a_i == 0 else 0.8 if (a_i == 1 and it.kind == "word" and len(alt) > 1) else 0.5) * (0.3 if it.surface in GENERIC_WORDS else 1.0)
            for x in alt:
                weights[x] = max(weights.get(x, 0.0), w)
    return QueryTerms(merged, weights, cues)


def coverage(qt: QueryTerms, terms: frozenset[str], ctx: frozenset[str] = frozenset()) -> float:
    """Weighted share of the question's key terms present in `terms` (context-only matches — section/document title — count half)."""
    total = sum(k.weight for k in qt.keys)
    if total <= 0:
        return 0.0
    got = 0.0
    for k in qt.keys:
        if any(a <= terms for a in k.alts):
            got += k.weight
        elif any(a <= ctx for a in k.alts):
            got += 0.5 * k.weight
    return got / total


# German question detection for the noun-casing weight (English nouns are lower case): German vs English function words
DE_HINTS = frozenset("der die das den dem des und ist sind wie wer was wo wann ich wir nicht muss mussen darf durfen fur mit von bei welche welcher welches nach im ein eine einen".split())
EN_HINTS = frozenset("the is are what who how where when and of for to with must may can does do which an a in on".split())
