"""LLM answerability judge (JA/NEIN, temp 0, seed 7; no reply = NEIN) and, as measurement variant (JUDGE_EVIDENCE=1),
the judge with evidence duty: number of the span that answers, or 0."""
from __future__ import annotations

import re

from ..config import LLM_TIMEOUT
from ..llm import OLLAMA
from ..search.normalize import fold


JUDGE_SYSTEM = (
    "Du prüfst, ob freigegebene Textstellen eine Frage direkt beantworten. Antworte ausschließlich mit JA oder NEIN. "
    "JA nur, wenn die Textstellen genau das Gefragte aussagen; NEIN, wenn sie ein anderes Thema, einen anderen Gegenstand "
    "oder nur verwandte Begriffe enthalten."
)
EVIDENCE_SYSTEM = (
    "Du prüfst, ob eine der nummerierten, freigegebenen Textstellen eine Frage direkt beantwortet. Antworte ausschließlich "
    "mit der Nummer dieser Textstelle, z. B. 2. Antworte 0, wenn keine Textstelle genau das Gefragte aussagt, etwa weil sie "
    "ein anderes Thema, einen anderen Gegenstand oder nur verwandte Begriffe enthält."
)


def judge_answerable(question: str, spans: list[str]) -> bool | None:
    """LLM answerability check (temp 0, seed 7, ≤ 3 tokens): True/False, None when no LLM is reachable."""
    out = OLLAMA.chat(JUDGE_SYSTEM, "Frage: " + question + "\nTextstellen:\n" + "\n".join(f"- {t}" for t in spans) + "\nAntwort (JA/NEIN):",
                      timeout=LLM_TIMEOUT, max_tokens=3, judge=True)
    return None if out is None else fold(out).startswith("ja")


def judge_evidence(question: str, spans: list[str]) -> int | None:
    """→ 1-based number of the span that directly answers the question, 0 = none (also for an unparsable or out-of-range
    reply: fail closed), None when no LLM is reachable. Temp 0, seed 7, ≤ 4 tokens."""
    out = OLLAMA.chat(EVIDENCE_SYSTEM, "Frage: " + question + "\nTextstellen:\n" + "\n".join(f"[{n}] {t}" for n, t in enumerate(spans, start=1))
                      + "\nAntwort (Nummer oder 0):", timeout=LLM_TIMEOUT, max_tokens=4, judge=True)
    if out is None:
        return None
    m = re.match(r"\D{0,12}?(\d+)", out)
    n = int(m.group(1)) if m else 0
    return n if 0 < n <= len(spans) else 0
