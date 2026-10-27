"""Sentence verifier: numbers/units, negation, modality, named roles and content words must match the cited spans."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable

from ..search.normalize import FREQ_WORDS, GENERIC_WORDS, GLOSSARY, R_STOP, TABLE_LABEL, Vocab, analyze, cistem, fold


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


# ── answer type at the evidence span (judge with evidence duty) ──
# The judge names the one span that answers; that span must then carry the kind of answer the question asks for. Generic
# language classes only, no wording from the eval questions; each rule has a unit test on an invented example.
# value question → a number (not a section reference), a frequency word, a duration in words or a deadline word
SECTION_REF = re.compile(r"§\s*[\d.]+")
DURATION_WORDS = re.compile(r"\b(?:eine?[mnrs]?|zwei|drei|vier|funf|sechs|sieben|acht|neun|zehn|zwolf|halbe\w*|one|two|three|four|five|six|"
                            r"seven|eight|nine|ten|twelve|an?|half an?)\s+(?:minute|stunde|werktag|arbeitstag|tag|woche|monat|quartal|jahr|"
                            r"minute|hour|business day|working day|day|week|month|quarter|year)")
DEADLINE_WORDS = re.compile(r"\b(?:unverzuglich\w*|sofort\w*|umgehend\w*|unmittelbar\w*|spatestens|fristgerecht|immediately|promptly|"
                            r"without (?:undue )?delay|no later than|at the latest)\b")
# 'who' question → a named role/body (entities) or a generic organisational unit
ORG_UNIT = re.compile(r"\b[\w-]*(?:abteilung|referat|gremium|ausschuss|komitee|feuerwehr|sekretariat|department|committee|board)\b")
# plural 'welche/which' question → several items; German 'welche' is plural only with a plural finite verb, English
# 'which' only with are/were/have ("Welche Geräte sind …", "Which tools are …")
PLURAL_WHICH = re.compile(r"\b(?:welche|which)\b")
PLURAL_VERB = re.compile(r"\b(?:sind|waren|mussen|durfen|konnen|sollen|werden|gelten|haben|gehoren|fallen|are|were|have)\b")
LIST_SEP = re.compile(r"\s*(?:[,;|•]|:\s|\s(?:und|oder|sowie|bzw|and|or)\s)\s*")
# otherwise the evidence span itself (+ section/document title, counted half) must carry the key terms of the part —
# the same floor the gate demands from the whole window
ANSWER_COVERAGE = 0.5


def _plain(span: str) -> str:
    return SECTION_REF.sub(" ", TABLE_LABEL.sub("", span))


def has_value(span: str) -> bool:
    f = fold(_plain(span))
    return bool(re.search(r"\d", f) or DURATION_WORDS.search(f) or DEADLINE_WORDS.search(f) or any(w in FREQ_WORDS for w in re.findall(r"[a-z]+", f)))


def has_role(span: str) -> bool:
    text = _plain(span)
    return bool(entities(text) or ORG_UNIT.search(fold(text)))


def plural_which(question: str) -> bool:
    f = fold(question)
    return bool(PLURAL_WHICH.search(f) and PLURAL_VERB.search(f))


def is_enumeration(span: str) -> bool:
    """Three or more listed items, or two or more numbers."""
    text = _plain(span)
    items = [x for x in LIST_SEP.split(fold(text)) if x.strip()]
    return len(items) >= 3 or len(set(re.findall(r"\d+(?:[.,]\d+)?", text))) >= 2


def answer_type_problem(question: str, span: str, value: bool, who: bool, cov: float) -> str:
    """'' when the evidence span carries the asked kind of answer, else the reason. value/who: the part's VALUE_Q/WHO_Q
    flags; cov: key-term coverage of the span (with its section/document title)."""
    if value:
        return "" if has_value(span) else "Belegstelle ohne Zahl/Dauer/Frist"
    if who:
        return "" if has_role(span) else "Belegstelle ohne Rolle/Stelle"
    if plural_which(question):
        return "" if is_enumeration(span) else "Belegstelle ohne Aufzählung"
    return "" if cov >= ANSWER_COVERAGE else f"Belegstelle deckt die Teilfrage nicht ab ({cov:.2f})"


# ── focus words (JUDGE_FOCUS=1, measurement variant on top of JUDGE_EVIDENCE) ──
# The core noun(s) of the part must occur at the span the judge named (stem or glossary synonym), otherwise the span is
# about something else ("Welche Geräte …" answered by a sentence about the opening hours of the same room). Nouns: German
# with casing → capitalised words (the first word only when it is no function word); English or all-lower-case → the
# content words after an article, preposition, possessive or question word, or at the start (up to two: "the hand dryers").
# Stop words, document-type words (Richtlinie, Policy, …), permission/obligation predicates (erlaubt, required, …) and the
# document named with "laut/gemäß/according to" are no focus.
_WORD = re.compile(r"[A-Za-zÄÖÜäöüß0-9][A-Za-zÄÖÜäöüß0-9-]*")
_EN_FUNC = frozenset("the a an is are do does how what who whom which when where why can must should may to of for in on my our we i".split())
_DE_FUNC = frozenset("der die das den dem des ist sind wie wer was welche welcher welches wann wo ich wir muss darf fur von zu im ein eine und".split())
_NOUN_CUE = frozenset("der die das den dem des ein eine einen einem einer eines mein meine meinen meinem unser unsere unseren unserem "
                      "fur bei von vom zu zum zur im in mit auf an nach uber aus durch gegen ohne welche welcher welches welchen welchem "
                      "the a an of for about to in on at with by from into my our your their this that these those which what whose "
                      "much many".split())
_SOURCE_CUE = frozenset("laut gemass according per".split())
_PREDICATE = frozenset("erlaubt verboten zulassig gestattet untersagt erforderlich notwendig pflicht verpflichtend permitted prohibited "
                       "forbidden required mandatory necessary".split())


def focus_words(question: str) -> list[str]:
    """Core nouns of a question part (folded surface forms, in question order)."""
    raw = _WORD.findall(question)
    low = [fold(w) for w in raw]
    en = sum(w in _EN_FUNC for w in low) > sum(w in _DE_FUNC for w in low)
    cased = not en and any(w[:1].isupper() for w in raw[1:])

    def content(k: int) -> bool:
        w = low[k]
        return len(w) >= 2 and not w.isdigit() and w not in R_STOP and w not in GENERIC_WORDS and w not in _PREDICATE and \
            not (k and low[k - 1] in _SOURCE_CUE)

    out: list[str] = []
    for k, w in enumerate(raw):
        if not content(k):
            continue
        if cased:
            noun = w[:1].isupper() or w.isupper()
        else:
            j = k - 1
            while j >= 0 and k - j <= 2 and content(j):
                j -= 1  # a run of content words after the cue: "the risk acceptance"
            noun = (j < 0 and k <= 1) or (j >= 0 and k - j <= 2 and low[j] in _NOUN_CUE) or (en and w[:1].isupper() and k > 0)
        if noun and low[k] not in out:
            out.append(low[k])
    return out


def _present(word: str, terms: set[str]) -> bool:
    parts = [cistem(x) for x in re.findall(r"[a-z]+", word) if x not in R_STOP]
    if parts and all(x in terms for x in parts):
        return True
    stem = cistem(re.sub(r"[^a-z]", "", word))
    return any(any(m <= terms for m in GLOSSARY.groups[gi]) for t in {stem, *parts} for gi in GLOSSARY.single.get(t, []))


def focus_problem(question: str, span: str, vocab: Vocab) -> str:
    """'' when a focus word of the part occurs at the span (stem or glossary synonym) or the part has none, else the reason."""
    focus = focus_words(question)
    if not focus:
        return ""
    terms = set(analyze(span, vocab))
    return "" if any(_present(w, terms) for w in focus) else "Fokuswort fehlt an der Belegstelle: " + ", ".join(focus)
