"""Answer checks: numbers/units, negation, modality and named roles of an answer against its quotes; value dimensions."""
from __future__ import annotations

import re
from typing import Iterable

from ..search.facts import values_in
from ..search.normalize import FREQ_WORDS, TABLE_LABEL, Vocab, analyze, fold


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

PROHIBITION_RE = re.compile(r"\b(?:untersagt\w*|verbot\w*|unzulassig\w*|prohibit\w*|forbidden|never|nie|niemals)\b")


def modal_classes(text: str) -> frozenset[str]:
    f = fold(text)
    return frozenset(k for k, rx in MODAL_CLASSES.items() if rx.search(f))


def entities(text: str) -> set[str]:
    return {e.strip("-") for e in ENTITY_RE.findall(text) if len(e.strip("-")) >= 2}


def numeric_terms(terms: Iterable[str]) -> set[str]:
    """Numbers, quantities ('24h') and identifiers ('tier2', 'tls1.3') — must be reproduced exactly."""
    return {t for t in terms if t[:1].isdigit() or re.fullmatch(r"[a-z]+\d[\d.]*", t)}


def answer_problems(answer: str, quotes: list[str], vocab: Vocab | None = None) -> list[str]:
    """Empty list = what the answer asserts stands in its quotes: every number with its unit and every identifier; the
    negation polarity of the best-matching quotes (all quotes when no term is shared, e.g. across languages) — a negation
    they lack ("ohne Beleg") or one they have and the answer drops ("fehlt"); no modality they lack; every named role."""
    if not answer:
        return []
    st = set(analyze(answer, vocab))
    src = [set(analyze(q, vocab)) for q in quotes]
    problems = []
    missing = numeric_terms(st) - numeric_terms(set().union(*src))
    if missing:
        problems.append("Zahl/Einheit: " + ", ".join(sorted(missing)))
    top = max(len(st & s) for s in src)
    best = [q for q, s in zip(quotes, src) if len(st & s) == top]
    neg = [bool(NEG_RE.search(fold(q))) for q in best]
    if NEG_RE.search(fold(answer)) and not all(neg):
        problems.append("Verneinung ohne Beleg")
    elif not NEG_RE.search(fold(answer)) and any(neg):
        problems.append("Verneinung fehlt")
    if any(not modal_classes(answer) <= modal_classes(q) for q in best):
        problems.append("Modalität")
    src_f = fold(" ".join(quotes))
    problems += [f"Rolle/Stelle: {e}" for e in sorted(entities(answer)) if fold(e) not in src_f]
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


# Organisational units that answer a who-question without a role noun ("die Rechtsabteilung", "das Facility Management",
# "the Works Council"): generic suffixes only, never names from a corpus. Used for the key sentence (stage 1).
ORG_UNIT_RE = re.compile(r"\b[\wäöüß-]*(?:abteilung|bereich|referat|gremium|ausschuss|management|verwaltung|zentrale|dienst|stelle|"
                         r"(?:betriebs|personal|bei|aufsichts)rat|department|division|unit|committee|board|council|center|centre)\b", re.I)


def names_actor(text: str) -> bool:
    return bool(entities(text) or ORG_UNIT_RE.search(text))
