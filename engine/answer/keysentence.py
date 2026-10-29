"""Key sentence of a search hit (stage 1): the verbatim sentence that fits the question type, highlighted for the user."""
from __future__ import annotations

from .gate import VALUE_Q, WHO_Q
from .verify import has_value, names_actor, value_dim
from ..search.normalize import fold

def question_kind(question: str, value: bool | None = None, who: bool | None = None, flag_text: str = "") -> tuple[str, str | None]:
    """→ (kind, argument): ("value", dimension), ("who", None) or ("other", None). `value`/`who`
    override the wording rules when the caller already knows the question type (unit cues, typo-fixed question words).
    May/must questions stay "other": preferring a normative sentence lost 5 top-1 key sentences on dev1 and won none."""
    f = flag_text or fold(question)
    if value if value is not None else VALUE_Q.search(f):
        return "value", value_dim(f)
    if who if who is not None else WHO_Q.search(f):
        return "who", None
    return "other", None


def key_sentence(kind: str, arg: str | None, sentences: list[str], cov: list[float]) -> int:
    """Index of the key sentence. Value question → a sentence with a value of the asked dimension; who-question → a sentence
    naming a role or organisational unit; otherwise, and when no sentence fits the type, the highest key-term coverage. Ties: higher coverage, then the earlier sentence."""
    if not sentences:
        return 0
    order = sorted(range(len(sentences)), key=lambda k: (-cov[k], k))
    if kind == "value":
        fit = [k for k in order if has_value(arg or "value", sentences[k])]
    elif kind == "who":
        fit = [k for k in order if names_actor(sentences[k])]
    else:
        fit = []
    return (fit or order)[0]
