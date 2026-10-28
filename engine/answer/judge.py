"""LLM answerability judge (JA/NEIN, temp 0, seed 7; no reply = NEIN)."""
from __future__ import annotations

from ..config import LLM_TIMEOUT
from ..llm import OLLAMA
from ..search.normalize import fold


JUDGE_SYSTEM = (
    "Du prüfst, ob freigegebene Textstellen eine Frage direkt beantworten. Antworte ausschließlich mit JA oder NEIN. "
    "JA nur, wenn die Textstellen genau das Gefragte aussagen; NEIN, wenn sie ein anderes Thema, einen anderen Gegenstand "
    "oder nur verwandte Begriffe enthalten."
)


def judge_answerable(question: str, spans: list[str]) -> bool | None:
    """LLM answerability check (temp 0, seed 7, ≤ 3 tokens): True/False, None when no LLM is reachable."""
    # the cue asks about the passages; "Antwort (JA/NEIN):" let models answer the question itself ("Darf ich …?" → NEIN)
    out = OLLAMA.chat(JUDGE_SYSTEM, "Frage: " + question + "\nTextstellen:\n" + "\n".join(f"- {t}" for t in spans) + "\nBeantworten die Textstellen die Frage? (JA/NEIN):",
                      timeout=LLM_TIMEOUT, max_tokens=3, judge=True)
    return None if out is None else fold(out).startswith("ja")
