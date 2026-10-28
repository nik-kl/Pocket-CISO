"""Sentence verifier: numbers/units, negation, modality, named roles and content words must match the cited spans."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from ..search.facts import values_in
from ..search.normalize import FREQ_WORDS, GLOSSARY, TABLE_LABEL, Vocab, analyze, fold


# Named roles and bodies: acronyms (CISO, BSI, SOC) and role nouns, also as the tail of a compound ("Küchenleitung", "CISO-Office")
ENTITY_RE = re.compile(r"\b(?:[A-ZÄÖÜ]{2,}[A-Za-z0-9]*(?:-[A-Za-zÄÖÜäöü]+)*|[\wÄÖÜäöüß-]*(?i:office|team|owner|leiter\w*|leitung|lead|admin\w*|beauftragte\w*|"
                       r"commander|desk|führung|vorstand|vorgesetzte\w*|behörde\w*|counsel|betrieb|mitarbeit\w+|dienstleister\w*|führungskräfte|"
                       r"benutzer\w*|nutzer\w*|employee\w*|staff|personal))\b")
NEG_RE = re.compile(r"\b(?:nicht|kein\w*|nie|niemals|ohne|untersagt\w*|verbot\w*|unzulassig\w*|ausgeschlossen|not|no|never|without|prohibit\w*|forbidden|cannot)\b")
MODAL_CLASSES = {
    "obligation": re.compile(r"\b(?:muss|mussen|musst|zwingend|verpflichtend\w*|verpflichtet|pflicht\w*|erforderlich|notwendig|must|shall|mandatory|required|requires|obligatory)\b"),
    "permission": re.compile(r"\b(?:darf|durfen|erlaubt|gestattet|zulassig|may|allowed|permitted|permits|authorized)\b"),
    "recommendation": re.compile(r"\b(?:soll|sollen|sollte|sollten|empfohlen|empfiehlt|should|recommended)\b"),
    "possibility": re.compile(r"\b(?:kann|konnen|can|could)\b"),
}

YES_NO = re.compile(r"^\s*(?:ja|nein|yes|no)\b")


def modal_classes(text: str) -> frozenset[str]:
    f = fold(text)
    return frozenset(k for k, rx in MODAL_CLASSES.items() if rx.search(f))


def entities(text: str) -> set[str]:
    return {e.strip("-") for e in ENTITY_RE.findall(text) if len(e.strip("-")) >= 2}


def numeric_terms(terms: Iterable[str]) -> set[str]:
    """Numbers, quantities ('24h') and identifiers ('tier2', 'tls1.3') — must be reproduced exactly."""
    return {t for t in terms if t[:1].isdigit() or re.fullmatch(r"[a-z]+\d[\d.]*", t)}


@dataclass
class Sentence:
    text: str
    spans: list[str]  # span ids
    source: str  # llm | verbatim
    verified: bool = True
    reasons: list[str] | None = None


def verify_sentence(text: str, spans: list[str], vocab: Vocab) -> list[str]:
    """Empty list = supported. Every number with its unit and every identifier must appear identically, negation polarity and
    modality must match the best-matching cited span, named roles/bodies must appear in the cited spans, ≥ 70 % of the
    content terms must be supported."""
    if not spans:
        return ["keine Quelle"]
    st = set(analyze(text, vocab))
    src_terms = [set(analyze(s, vocab)) for s in spans]
    union = set().union(*src_terms)
    problems: list[str] = []
    missing_nums = numeric_terms(st) - numeric_terms(union)
    if missing_nums:
        problems.append("Zahl/Einheit: " + ", ".join(sorted(missing_nums)))
    words = {t for t in st if t.isalpha() and len(t) >= 3}
    expanded = set(union)
    for t in union:
        for gi in GLOSSARY.single.get(t, []):
            for m in GLOSSARY.groups[gi]:
                expanded |= m
    # every content word must come from the cited spans (or a glossary equivalent); one word of slack for inflection noise
    if len(words - expanded) > 1:
        problems.append("Inhalt nicht belegt: " + ", ".join(sorted(words - expanded)))
    if YES_NO.match(fold(text)):  # "Ja, …" / "No, …" is a verdict drawn by the model, not text of the source
        problems.append("Ja/Nein-Urteil")
    best = max(range(len(spans)), key=lambda k: (len(st & src_terms[k]), -k))
    f_text, f_best = fold(text), fold(spans[best])
    if bool(NEG_RE.search(f_text)) != bool(NEG_RE.search(f_best)):
        problems.append("Verneinung")
    if modal_classes(text) != modal_classes(spans[best]):
        problems.append("Modalität")
    src = fold(" ".join(spans))
    for e in entities(text):
        if fold(e) not in src:
            problems.append(f"Rolle/Stelle: {e}")
    return problems


# ── value questions: the answer must carry a value of the asked dimension ──
# A value question ("wie oft", "how many", "bis wann", …) answered by a sentence of the right section without the value is a
# wrong answer even when the value sentence sat in the same window. Dimension from the question wording (generic
# interrogatives only); value check on the text with section references and table labels removed.
VALUE_DIMS = (
    ("frequency", re.compile(r"\b(?:wie oft|wie haufig|how often|how frequently|in welche\w* (?:abstand|abstanden|turnus|rhythmus)|intervall\w*|"
                            r"interval\w*|turnus|rhythmus)\b")),
    ("count", re.compile(r"\b(?:wie viele?\w*|wieviele?|how many|anzahl|number of)\b")),
    ("duration", re.compile(r"\b(?:wie lange?|fur wie lange?|how long|dauer|zeitraum)\b")),
    ("deadline", re.compile(r"\b(?:bis wann|ab wann|wann|innerhalb welche\w*|binnen welche\w*|nach wie viel\w*|wie schnell|wie fruh|frist\w*|"
                           r"deadline|by when|when|how (?:soon|quickly|fast)|within how)\b")),
)
_NUM_WORDS = r"(?:eine?[mnrs]?|zwei|drei|vier|funf|sechs|sieben|acht|neun|zehn|elf|zwolf|halbe\w*|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|an?|half an?)"
_TIME_UNITS = r"(?:sekunde|minute|stunde|werktag|arbeitstag|tag|woche|monat|quartal|jahr|second|minute|hour|business day|working day|day|week|month|quarter|year)"
DURATION_IN_WORDS = re.compile(r"\b" + _NUM_WORDS + r"\s+" + _TIME_UNITS)
FREQ_IN_WORDS = re.compile(r"\b(?:(?:alle|every|each|je|pro|per)\s+(?:\d+|" + _NUM_WORDS + r"\s+)?\s*" + _TIME_UNITS + r"|\d+\s*-?\s*mal\b|"
                           r"(?:ein|zwei|drei|vier)mal|once|twice|\d+\s+times)")
DEADLINE_WORDS = re.compile(r"\b(?:unverzuglich\w*|sofort\w*|umgehend\w*|spatestens|immediately|promptly|without (?:undue )?delay|"
                            r"no later than|at the latest)\b")
DATE_OR_TIME = re.compile(r"\b\d{1,2}\.\s?\d{1,2}\.(?:\d{2,4})?|\b\d{1,2}(?::\d{2})?\s*uhr\b|\b\d{1,2}:\d{2}\b")
COUNT_WORDS = re.compile(r"\b(?:zwei|drei|vier|funf|sechs|sieben|acht|neun|zehn|elf|zwolf|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\b")
INTERVAL_WORDS = re.compile(r"intervall|interval|zyklus|cycle|turnus|rhythmus|frequen|haufigkeit")
_SECTION = re.compile(r"§\s*[\d.]+")


def value_dim(question: str) -> str:
    """Asked value dimension of a value question: frequency, count, duration, deadline, or 'value' (any value)."""
    f = fold(question)
    return next((dim for dim, rx in VALUE_DIMS if rx.search(f)), "value")


def has_value(dim: str, text: str) -> bool:
    """Does `text` carry a value of dimension `dim` (see value_dim)?"""
    plain = _SECTION.sub(" ", TABLE_LABEL.sub("", text))
    f = fold(plain)
    vals = values_in(plain)
    time = any(d in ("time", "wd") for d, _, _ in vals) or bool(DURATION_IN_WORDS.search(f))  # working days count as time
    # a duration labelled as interval ("Prüfintervall: 12 Monate", "cycle: 6 months") is a frequency
    freq = any(w in FREQ_WORDS for w in re.findall(r"[a-z]+", f)) or bool(FREQ_IN_WORDS.search(f)) or (time and bool(INTERVAL_WORDS.search(f)))
    if dim == "frequency":
        return freq
    if dim == "duration":
        return time
    if dim == "deadline":
        return time or bool(DEADLINE_WORDS.search(f) or DATE_OR_TIME.search(f))
    if dim == "count":
        return bool(vals) or time or bool(COUNT_WORDS.search(f))
    return bool(vals) or time or freq


# Safety checks on a judge JA (the LLM confirms spans that answer something else); general wording only, no corpus terms.
IDENT_RE = re.compile(r"\b[A-ZÄÖÜ]{2,}s?\b|\b[A-Za-z]+\d[\w.]*\b")  # acronyms and identifiers of the question ('PINs', 'TLS1.3')
EXCEPTION_RE = re.compile(r"\b(?:abweichend\w*|ausnahme\w*|sonderregel\w*|exception\w*|deviat\w*|notwithstanding)\b")
GLOSSARY_LINE = re.compile(r"^[^:|]{2,60}?(?:\s*\([^)]*\))?:\s+(?:der|die|das|ein\w*|the|an?)\b", re.I)  # "Term (Alias): Der …"


def _stems(text: str) -> set[str]:
    # shortcut: 5-letter prefixes miss prefixed forms ("gemeldet" vs "Meldefrist") and then block; upgrade to the stemmer if that costs answers
    return {w[:5] for w in re.findall(r"[a-z]{4,}", fold(text))}


def span_problem(question: str, kind: str, dim: str | None, span: str, path: str) -> str | None:
    """Why one span cannot carry the answer: an acronym/identifier of the question is missing from span and outline path;
    a glossary definition for a question that asks no definition; a table row whose asked value sits under a column
    label unrelated to the question (value question)."""
    if any(fold(a).rstrip("s") not in fold(span + " " + path) for a in IDENT_RE.findall(question)):
        return "Kennung fehlt"
    table = span.startswith("Tabelle [")
    if kind != "definition" and not table and GLOSSARY_LINE.search(span):
        return "Definition statt Antwort"
    if kind == "value" and table:
        cells = [c.split(": ", 1) for c in TABLE_LABEL.sub("", span).split(" | ") if ": " in c]
        if not any(has_value(dim or "value", v) and _stems(question) & _stems(label) for label, v in cells):
            return "Wert aus anderer Tabellenspalte"
    return None


def judge_veto(question: str, kind: str, dim: str | None, spans: list[tuple[str, str]]) -> str | None:
    """Reason to overrule the judge's JA, or None. `spans` = (text, outline path). A one-word fragment is no question; an
    exception/deviating rule the question does not ask for is no answer to the rule; otherwise one span must pass span_problem."""
    if len(question.split()) == 1 and "?" not in question:
        return "Fragment"
    if not EXCEPTION_RE.search(fold(question)) and any(EXCEPTION_RE.search(fold(s)) for s, _ in spans):
        return "ungefragte Ausnahme"
    problems = [span_problem(question, kind, dim, s, p) for s, p in spans]
    return None if not problems or None in problems else problems[0]
