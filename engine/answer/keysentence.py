"""Key sentence of a search hit (stage 1): the verbatim sentence that fits the question type, highlighted for the user."""
from __future__ import annotations

import re

from .gate import VALUE_Q, WHO_Q, definition_term, definition_text_rank
from .verify import entities, has_value, value_dim
from ..search.normalize import fold

# Organisational units that answer a who-question without a role noun ("die Rechtsabteilung", "das Facility Management",
# "the Works Council"): generic suffixes only, never names from a corpus. Used for the key sentence, not for the answer gate.
ORG_UNIT_RE = re.compile(r"\b[\wäöüß-]*(?:abteilung|bereich|referat|gremium|ausschuss|management|verwaltung|zentrale|dienst|stelle|"
                         r"(?:betriebs|personal|bei|aufsichts)rat|department|division|unit|committee|board|council|center|centre)\b", re.I)


def question_kind(question: str, value: bool | None = None, who: bool | None = None, flag_text: str = "") -> tuple[str, str | None]:
    """→ (kind, argument): ("definition", term), ("value", dimension), ("who", None) or ("other", None). `value`/`who`
    override the wording rules when the caller already knows the question type (unit cues, typo-fixed question words).
    May/must questions stay "other": preferring a normative sentence lost 5 top-1 key sentences on dev1 and won none."""
    term = definition_term(question)
    if term:
        return "definition", term
    f = flag_text or fold(question)
    if value if value is not None else VALUE_Q.search(f):
        return "value", value_dim(f)
    if who if who is not None else WHO_Q.search(f):
        return "who", None
    return "other", None


def names_actor(text: str) -> bool:
    return bool(entities(text) or ORG_UNIT_RE.search(text))


def key_sentence(kind: str, arg: str | None, sentences: list[str], cov: list[float], section: str = "") -> int:
    """Index of the key sentence. Value question → a sentence with a value of the asked dimension; who-question → a sentence
    naming a role or organisational unit; definition → the explaining sentence (explains before passing mention); otherwise,
    and when no sentence fits the type, the highest key-term coverage. Ties: higher coverage, then the earlier sentence."""
    if not sentences:
        return 0
    order = sorted(range(len(sentences)), key=lambda k: (-cov[k], k))
    if kind == "value":
        fit = [k for k in order if has_value(arg or "value", sentences[k])]
    elif kind == "who":
        fit = [k for k in order if names_actor(sentences[k])]
    elif kind == "definition" and arg:
        ranks = {k: definition_text_rank(sentences[k], arg, section) for k in order}
        fit = sorted((k for k in order if ranks[k] is not None), key=lambda k: (ranks[k], -cov[k], k))
    else:
        fit = []
    return (fit or order)[0]
