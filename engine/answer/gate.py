"""Answer gate: question parts, span windows, window selection (coverage, cross-section evidence) and the dense path."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .verify import entities, numeric_terms
from ..search.lexical import KeyTerm, QueryTerms, RoleView, coverage
from ..search.normalize import FREQ_WORDS, INSTRUCTION_RE, R_STOP, _LEX, _edit_distance, cistem, fold, norm


NO_ANSWER = "Keine belegbare Antwort im freigegebenen Bestand."
SEARCH_LABEL = "Fundstellen, keine KI-Antwort"  # no LLM reachable
UNCONFIRMED_LABEL = "Fundstellen, nicht als Antwort bestätigt"  # LLM judge said NEIN / gave no reply
# Answer gate per question part (step 5, tuned on the dev set only; curve from a permissive run with every feature logged):
#   GATE_COVERAGE 0.5 — weighted share of the part's key terms found in the selected window (+ section/doc title);
#   GATE_MISSING  0.5 — no single unmatched key term may carry more than half of the part's key-term weight;
#   answerability judge — the LLM must reply JA to "do these spans state exactly what is asked" (temp 0, seed 7); no reply ⇒ NEIN.
# Lexical/dense features alone cannot separate "supported but off-topic" spans: at coverage 0.95 and missing ≤ 0.15 there
# were still 6 false answers with Ollama (8 without). With the judge every grid point from coverage 0.30–0.95 × missing
# 0.15–1.0 × dense 0.5–0.65 had 0 false answers, so the lexical values sit mid-grid (±0.05 changes correct answers by ≤ 3,
# false answers stay 0). Windows above the lexical floor that are not confirmed (no LLM, judge NEIN or silent) are returned
# as search hits (kind "search"), never as an answer; "fallback" only when no window reaches the lexical floor.
GATE_COVERAGE = 0.5
GATE_MISSING = 0.5
# Dense path (class 2): a part below the lexical floor still reaches the judge with the spans of its best dense chunk when
# that cosine (bge-m3) is >= DENSE_GATE. Dev curve over question parts (top dense chunk from the expected source /
# unanswerable parts): 0.58 → 168/203 · 2/26, 0.60 → 152 · 2, 0.62 → 131 · 0, 0.64 → 109 · 0, 0.68 → 75 · 0; highest
# unanswerable part 0.6045 → 0.62 keeps a margin of 0.015 above it, the judge stays the second barrier.
DENSE_GATE = 0.62
MAX_PARTS = 4

INTERROG = (r"(?:wie|wer|wem|wen|was|wo|wohin|woher|wann|welche\w*|warum|wieso|weshalb|ob|darf|durfen|muss|mussen|ist|sind|gibt|gilt|kann|"
            r"konnen|soll|sollen|braucht|brauchen|bis|ab|innerhalb|binnen|nach|how|who|whom|what|where|when|which|why|whether|is|are|do|does|"
            r"did|can|could|must|may|should|shall|will|within)")
# split at sentence ends; inside question sentences (ending with "?", or every sentence when none does) also at "und/oder"
# + question word and at a comma + question word or modal verb — a statement such as "… und müssen schnell handeln:" stays
# context. Matched on a length-preserving fold (lower case, umlauts → base letter) so "müssen", "Dürfen" split like "mussen".
SENT_SPLIT = re.compile(r"(?<=[?!.;:])\s+")
PART_SPLIT = re.compile(r",?\s+(?:und|sowie|oder|and|or)\s+(?=" + INTERROG + r"\b)|,\s+(?=(?:wie|wer|wen|wem|was|wo|wann|welche\w*|"
                        r"darf|durfen|muss|mussen|kann|konnen|how|who|whom|what|where|when|which|can|may|must)\b)")
_SPLIT_FOLD = str.maketrans("äöüÄÖÜß", "aouaous")
QUESTION_START = re.compile(r"^\s*(?:und\s+|and\s+)?" + INTERROG + r"\b")
VALUE_Q = re.compile(r"\b(?:wie (?:lange?|viele?\w*|oft|schnell|hoch|gross|fruh)|bis wann|ab wann|ab welche\w*|bis zu welche\w*|innerhalb welche\w*|"
                     r"binnen welche\w*|in welche\w* (?:frist|zeit|intervall)|welche\w* (?:frist|intervall|wert|hohe|dauer|zeit|lange|mindestlange|gultigkeit)|"
                     r"nach wie viel\w*|wieviele?|how (?:long|many|much|often|quickly|soon|fast)|within how|what is the (?:maximum|minimum|deadline|sla|interval)|"
                     r"which (?:deadline|interval|sla)|frist|deadline|intervall|interval)\b")
WHO_Q = re.compile(r"\b(?:wer|wem|wen|who|whom)\b")



def fix_function_words(question: str, view: RoleView) -> str:
    """Folded question with typos of function/question words corrected for the question-type rules ('wie lnage' → 'wie
    lange'): a word unknown to the role's corpus within one edit of a stop word of similar length (same rule key_terms
    uses to drop such typos from the search terms)."""
    out = []
    for w in fold(question).split():
        core = re.sub(r"\W", "", w)
        if len(core) >= 4 and core not in R_STOP and cistem(core) not in view.df:
            fix = next((s for s in sorted(R_STOP) if len(s) >= 4 and abs(len(s) - len(core)) <= 1 and _edit_distance(core, s, 1) <= 1), None)
            if fix:
                w = w.replace(core, fix)
        out.append(w)
    return " ".join(out)


@dataclass
class Part:
    text: str
    qt: QueryTerms
    value: bool
    who: bool
    windows: list[list[tuple[int, int]]]
    lexical_ok: bool = False
    capped_only: bool = False
    pooled: bool = False  # judged against the windows of the answered parts
    dense_ok: bool = False
    ranked: list[int] = field(default_factory=list)
    dense_of: dict[int, float] = field(default_factory=dict)  # dense cosine of the ranked chunks (stage-1 details)
    features: dict[str, Any] | None = None
    contradiction: list[tuple[list[tuple[int, int]], dict[str, set[float]]]] | None = None
    coverage: float = 0.0
    answered: bool = False
    flag_text: str = ""  # folded question with function-word typos fixed (question-type rules)
    shown: list[list[tuple[int, int]]] = field(default_factory=list)  # spans shown per window (answered parts)
    reason: str = ""


def question_parts(question: str) -> tuple[list[str], list[str], bool]:
    """→ (question parts, context sentences, injection_removed). Instructions to the assistant are dropped before analysis."""
    sentences = [x for x in SENT_SPLIT.split(question.strip()) if x.strip()]
    any_q = any(x.rstrip().endswith("?") for x in sentences)
    cuts: list[str] = []
    for sent in sentences:
        folded = sent.translate(_SPLIT_FOLD).lower()
        pos = 0
        if len(folded) == len(sent) and (sent.rstrip().endswith("?") or not any_q):
            for m in PART_SPLIT.finditer(folded):
                cuts.append(sent[pos:m.start()])
                pos = m.end()
        cuts.append(sent[pos:])
    pieces = [p.strip(" ,;:") for p in cuts if p and p.strip(" ,;:")]
    clean = [p for p in pieces if not INSTRUCTION_RE.search(fold(p))]
    removed = len(clean) < len(pieces)
    parts = [p for p in clean if QUESTION_START.match(fold(p)) or p.rstrip().endswith("?")]
    context = [p for p in clean if p not in parts]
    if not parts and context:
        parts, context = context, []
    return parts[:MAX_PARTS], context, removed


def section_seq(idx: "LexIndex", view: RoleView, i: int) -> list[tuple[int, int]]:
    allowed = set(view.allowed)
    return [s for s in idx.section_seq[idx.section_key[i]] if s[0] in allowed and not idx.span_instr[s[0]][s[1]]]


def span_windows(idx: "LexIndex", view: RoleView, chunk_ids: list[int]) -> list[list[tuple[int, int]]]:
    """Candidate windows: one span, or with up to two more preceding spans of the same section (neighbouring chunks
    included)."""
    seen: set[tuple[tuple[int, int], ...]] = set()
    out: list[list[tuple[int, int]]] = []
    for i in chunk_ids:
        seq = section_seq(idx, view, i)
        for pos, anchor in enumerate(seq):
            if anchor[0] != i:
                continue
            for back in range(0, 3):
                if pos - back < 0:
                    break
                w = tuple(seq[pos - back : pos + 1])
                if back and any(idx.span_text[x][y].startswith("Tabelle [") for x, y in w):
                    break  # table rows are self-contained statements; context windows are for running text only
                if w not in seen:
                    seen.add(w)
                    out.append(list(w))
    return out


def window_terms(idx: "LexIndex", w: list[tuple[int, int]]) -> frozenset[str]:
    return frozenset().union(*(idx.span_terms[i][k] for i, k in w))


def window_ok(idx: "LexIndex", part: Part, w: list[tuple[int, int]]) -> bool:
    """A value question needs a number/quantity in the window, a 'who' question a named role or body."""
    text = " ".join(idx.span_text[i][k] for i, k in w)
    if part.value and not numeric_terms(window_terms(idx, w)) and not any(x in FREQ_WORDS for x in _LEX.findall(fold(text))):
        return False
    if part.who and not entities(text):
        return False
    return True


def dense_window(idx: "LexIndex", part: Part, dense: dict[int, float]) -> tuple[list[tuple[int, int]], float] | None:
    """Window for the dense path: the best dense chunk (>= DENSE_GATE) — its spans sharing a key term with the part (≤ 3),
    or the whole chunk when it has ≤ 2 spans and none shares one; must pass window_ok (value/role present)."""
    if not dense:
        return None
    i = min(dense, key=lambda j: (-dense[j], j))
    if dense[i] < DENSE_GATE:
        return None
    ks = [k for k in range(len(idx.span_text[i])) if not idx.span_instr[i][k]]
    hit = [k for k in ks if coverage(part.qt, idx.span_terms[i][k]) > 0][:3]
    w = [(i, k) for k in hit or (ks if len(ks) <= 2 else [])]
    return (w, dense[i]) if w and window_ok(idx, part, w) else None


def fact_chunks(idx: "LexIndex", view: RoleView, part: Part, limit: int = 6) -> list[int]:
    """Exact fact lookup for value questions: spans that carry an extracted fact (table cell, value sentence), ranked by
    key-term coverage over the whole allowed corpus — independent of the fused top-k."""
    allowed = set(view.allowed)
    scored = sorted((-coverage(part.qt, idx.span_terms[i][k], idx.ctx_terms[i]), i, k)
                    for (i, k) in idx.facts_by_span if i in allowed and not idx.span_instr[i][k])
    out: list[int] = []
    for _, i, _ in scored:
        if i not in out:
            out.append(i)
        if len(out) >= limit:
            break
    return out


DOC_REF = re.compile(r"\b(?:laut|gemass|according to|as per)\s+(?:der|dem|den|des|die|das|the|our|unserer?|unserem)?\s*([a-z0-9-]+)")


def doc_refs(part: Part, terms: frozenset[str], ctx: frozenset[str]) -> list[KeyTerm]:
    """Key terms naming the selected window's document: found only in its section/document title, or introduced as a
    source in the question ("laut BIA", "gemäß BCM-Matrix", "according to the playbook") and present in that title."""
    named = {w for m in DOC_REF.finditer(fold(part.text)) for w in m.group(1).split("-")}
    return [kt for kt in part.qt.keys if kt.weight > 0 and any(a <= ctx for a in kt.alts)
            and (not any(a <= terms for a in kt.alts) or kt.surface in named)]


def select_windows(idx: "LexIndex", view: RoleView, part: Part, ranked: list[int]) -> None:
    rank_of = {i: r for r, i in enumerate(ranked)}
    cands = span_windows(idx, view, ranked)

    def key(w: list[tuple[int, int]]) -> tuple[float, float, int, int, str]:
        cov = coverage(part.qt, window_terms(idx, w), idx.ctx_terms[w[-1][0]])
        rank = min(rank_of.get(i, len(ranked)) for i, _ in w)
        # coverage first (+ a small bonus for a better fused rank), then shorter window, rank, span id (deterministic)
        return (-(cov + 0.1 / (1 + rank)), -cov, len(w), rank, w[-1])

    scored = []
    for w in cands:
        alone = coverage(part.qt, idx.span_terms[w[-1][0]][w[-1][1]], idx.ctx_terms[w[-1][0]])
        if alone <= 0:
            continue  # the answering span itself must match the question
        if len(w) > 1 and coverage(part.qt, window_terms(idx, w), idx.ctx_terms[w[-1][0]]) < \
                coverage(part.qt, window_terms(idx, w[-1:]), idx.ctx_terms[w[-1][0]]) + 0.1:
            continue  # a further preceding span must add coverage, otherwise the shorter window wins
        scored.append((key(w), w))
    scored.sort(key=lambda x: x[0])
    good = [w for _, w in scored if window_ok(idx, part, w)]
    if not good:
        part.reason = "kein Wert/keine Rolle in den Fundstellen" if scored else "keine Fundstelle"
        return
    first = good[0]
    chosen = [first]
    got = window_terms(idx, first)
    base = coverage(part.qt, got, idx.ctx_terms[first[-1][0]])
    doc = idx.chunks[first[-1][0]]["docName"]
    refs = doc_refs(part, got, idx.ctx_terms[first[-1][0]])
    for w in good[1:]:
        if set(w) & set(first):
            continue
        if any(not any(a <= window_terms(idx, w) | idx.ctx_terms[w[-1][0]] for a in kt.alts) for kt in refs):
            continue  # the question names the selected document ("laut BIA"): no window from a document it does not name
        gain = coverage(part.qt, got | window_terms(idx, w)) - coverage(part.qt, got)
        close = idx.section_key[w[-1][0]] != idx.section_key[first[-1][0]] and \
            coverage(part.qt, window_terms(idx, w), idx.ctx_terms[w[-1][0]]) >= 0.9 * base
        if gain >= 0.2 or close:  # complementary statement, or an equally good one from another section
            chosen.append(w)
            got = got | window_terms(idx, w)
            break
    # evidence across sections (class 4): up to 3 windows from different sections of the selected document, each carrying another part of the key
    # terms (gain >= 0.2); only the first must hold the value/role ("Dieselvorrat reicht für 48 Stunden" + "Die
    # Netzersatzanlage versorgt das Rechenzentrum"). The judge then rates the combination.
    for _, w in scored:
        if len(chosen) >= 3:
            break
        if idx.chunks[w[-1][0]]["docName"] != doc or any(set(w) & set(c) for c in chosen):
            continue
        if any(idx.section_key[w[-1][0]] == idx.section_key[c[-1][0]] for c in chosen) or \
                any(idx.span_text[i][k].startswith("Tabelle [") for i, k in w + first):
            continue  # another section of the document; table rows are self-contained and never combined
        if coverage(part.qt, got | window_terms(idx, w)) - coverage(part.qt, got) >= 0.2:
            chosen.append(w)
            got = got | window_terms(idx, w)
    part.windows = chosen
    part.coverage = max(base, coverage(part.qt, got, idx.ctx_terms[first[-1][0]]))


# Abbreviation definitions ("Langform (X)", "X (Langform)", glossary table rows): moved to the glossary builder in step 4.


def _spells(long_form: str, term: str) -> bool:
    """An abbreviation's letters occur in order in the long form, starting at the first letter of one of its last words
    ('Recovery Time Objective' → RTO, 'Informationssicherheitsmanagementsystem' → ISMS); other terms: any long form."""
    letters = [ch for ch in fold(term) if ch.isalnum()]
    if sum(ch.isupper() for ch in term) < 2:
        return True
    words = long_form.split()
    for start in range(len(words) - 1, -1, -1):
        cand, pos = fold(" ".join(words[start:])), 0
        if not cand.startswith(letters[0]):
            continue
        for ch in letters:
            pos = cand.find(ch, pos) + 1
            if not pos:
                break
        else:
            return True
    return False


def defines(span: str, term: str) -> bool:
    """Does the span spell out `term`? 'Recovery Time Objective (RTO)', 'RTO (Recovery Time Objective)', or a table row
    'Kürzel: RTO | Bedeutung: Recovery Time Objective'."""
    t = re.escape(term)
    long_form = r"([A-Za-zÄÖÜäöüß][\w&-]*(?:[ -][\w&-]+){0,7})"
    for m in re.finditer(long_form + r"\s*\(\s*" + t + r"\s*\)", span):
        if _spells(m.group(1), term):
            return True
    for m in re.finditer(r"\b" + t + r"\s*\(\s*" + long_form + r"\s*\)", span):
        if len(m.group(1)) >= 6 and _spells(m.group(1), term):
            return True
    if span.startswith("Tabelle ["):
        cells = [c.split(": ", 1)[-1].strip(" .") for c in span.split(": ", 1)[-1].split(" | ")]
        return any(fold(c) == fold(term) for c in cells) and any(len(c.split()) >= 2 and fold(term) not in fold(c) for c in cells)
    return False


GLOSSARY_SECTION = re.compile(r"glossar|begriff|abkurzung|definition|glossary|abbreviation|terms|terminolog")
