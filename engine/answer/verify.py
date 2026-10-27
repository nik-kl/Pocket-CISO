"""Sentence verifier: numbers/units, negation, modality, named roles and content words must match the cited spans."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from ..search.normalize import GLOSSARY, Vocab, analyze, fold


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
