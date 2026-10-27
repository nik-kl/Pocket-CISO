"""Text analysis for retrieval: sentence split, folding, stemming (CISTEM), compounds, units, glossary, instruction spans."""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import defaultdict
from functools import lru_cache
from pathlib import Path
from typing import Iterable


def norm(s: str) -> str:
    return s.lower().replace("ä", "a").replace("ö", "o").replace("ü", "u").replace("ß", "ss")


def tokenize(s: str) -> list[str]:
    return [t for t in re.split(r"[^a-z0-9]+", norm(s)) if t]


STOP = set(
    ("wie was wer wo wann wieso warum welche welcher welches bis muss musst mussen darf durfen kann konnen ich mein meine meinen mir mich wir uns das der die den dem des "
     "ein eine einen einem einer und oder im in am an auf aus bei mit nach von vom zu zur zum fur uber auch nicht sein ist sind wird werden wurde hat haben es sie er ob als wenn "
     "dass noch nur so machen gibt gilt soll sollen sollte dann dies diese ihr ihre wir man oder sowie sowohl jede jeder jedes alle dabei dafur damit durch sich "
     "eines einem einer neuen neues neuen dafür dafuer laut welcher welches "
     "a an am an and are as at be been being but by can does for from have how i if in is it may my of on or our the their them these they this to us was we what who with").split()
)

def stem(t: str) -> str:
    return t[:7] if len(t) >= 7 else t


def content_stems(text: str) -> list[str]:
    return [stem(t) for t in tokenize(text) if len(t) >= 3 and t not in STOP and not t.isdigit()]


NUM_RE = re.compile(r"\d+(?:[.,]\d+)?")


def numbers_of(text: str) -> set[str]:
    return {n.replace(",", ".") for n in NUM_RE.findall(text)}


SENT_BREAK = re.compile(r"(?<=[.!?])\s+(?=[A-ZÄÖÜ0-9„\"(])")
# Abbreviations after which a period does not end a sentence ("z. B.", "bzw.", "Art. 33", "max. 90 Tage", "e.g.").
ABBREV = frozenset("z b bzw ca inkl exkl nr art abs max min mind ggf evtl vgl u a d h e g i s etc usw sog zzgl gem lt str dr st vs no "
                   "approx incl resp cf fig sec bspw ff".split())


def split_sentences(text: str) -> list[str]:
    """Sentence spans; generated table statements ("Tabelle [§…]: …") always start a new span."""
    pieces = re.split(r"\s+(?=Tabelle \[)", text)
    if len(pieces) > 1:
        return [s for p in pieces for s in split_sentences(p)]
    out: list[str] = []
    start = 0
    for m in SENT_BREAK.finditer(text):
        before = text[start : m.start()]
        word = re.search(r"([A-Za-zÄÖÜäöüß]+)\.$", before)
        if word and (word.group(1).lower() in ABBREV or len(word.group(1)) == 1):
            continue
        if re.search(r"(?:^|[:|]\s*)\d{1,3}\.$", before):  # enumeration "Action: 1. Host …"
            continue
        piece = text[start : m.start()].strip()
        if piece:
            out.append(piece)
        start = m.end()
    tail = text[start:].strip()
    if tail:
        out.append(tail)
    return out


_FOLD = str.maketrans({"ä": "a", "ö": "o", "ü": "u", "ß": "ss", "é": "e", "è": "e", "á": "a", "à": "a"})


def fold(s: str) -> str:
    """NFKC (PDF ligatures 'ﬁ' → 'fi'), case folding, umlaut and ß folding."""
    return unicodedata.normalize("NFKC", s).casefold().translate(_FOLD)


# Function words, question words, auxiliaries and modal verbs (folded, DE + EN). Negations and modal verbs matter to the
# verifier, not to retrieval.
R_STOP = frozenset("""
ab aber alle allem allen aller alles als also am an andere anderen anderer anderes auch auf aus ausser bei beim bin bis
bisher bist bitte da dabei dadurch dafur dagegen daher damit danach dann daran darauf daraus darf darfst darin darum darunter
das dass davon dazu dem den denen denn der deren des dessen dich die dies diese diesem diesen dieser dieses dir doch dort du
durch durfen durfte durften ein eine einem einen einer eines einige einmal er es etwa etwas euch euer eure fur gab gibt gilt
gelten genau gemass gem geben habe haben hat hatte hatten hier hin ich ihm ihn ihnen ihr ihre ihrem ihren ihrer im in indem
ins ist ja je jede jedem jeden jeder jedes jemand jetzt kann kannst konnen konnte konnten lt laut machen macht man mehr mein
meine meinem meinen meiner meines mich mir mit muss musst mussen musste mussten nach nachdem neben noch nun nur ob oder ohne
per pro schon sehr sein seine seinem seinen seiner seit selbst sich sie sind so solche soll sollen sollte sollten sondern
sonst sowie sowohl tun uber um und uns unser unsere unserem unseren unserer unter viel viele vom von vor wahrend war waren
warum was weil welche welchem welchen welcher welches wem wen wenn wer werde werden wessen wie wieso wieviel wieviele weshalb
wir wird wo wobei wodurch wofur woher wohin womit worden wurde wurden zu zum zur zwar zwischen eigentlich gerne bzw nicht kein
keine keinem keinen keiner keines nie niemals oft wann lange denn mal bitte darfst eigene eigenen eigener
a about above after again against all also am an and any are as at be because been before being below between both but by
can cannot could did do does doing done down during each else ever every few for from further had has have having he her
here hers him his how however i if in into is it its itself just may me might more most must my no nor not now of off on
once only or other our ours out over own per please same shall she should so some such than that the their theirs them then
there these they this those through to too under until up us very was we were what when where which while who whom whose why
will with within without would you your yours according regarding exactly exact many much often long allowed
""".split())
# Document-type words: weak evidence for a topic (weight 0.3), still useful to rank the referenced document.
GENERIC_WORDS = frozenset("richtlinie richtlinien policy policies unternehmen company firma organisation regel regeln vorgabe vorgaben dokument document".split())

UNIT_CANON: dict[str, str] = {}
for _canon, _words in {
    "h": "h std stunde stunden hour hours hr hrs", "min": "min minute minuten minutes mins", "s": "sek sekunde sekunden second seconds sec",
    "wd": "werktag werktage werktagen", "d": "tag tage tagen day days", "w": "woche wochen week weeks", "mo": "monat monate monaten month months",
    "y": "jahr jahre jahren year years", "chr": "zeichen character characters chars", "pct": "prozent percent", "eur": "eur euro",
    "mb": "mb", "gb": "gb", "tb": "tb",
}.items():
    for _w in _words.split():
        UNIT_CANON[_w] = _canon
FREQ_WORDS = {"jahrlich": "12mo", "annual": "12mo", "annually": "12mo", "yearly": "12mo", "halbjahrlich": "6mo", "semiannual": "6mo",
              "vierteljahrlich": "3mo", "quarterly": "3mo", "monatlich": "1mo", "monthly": "1mo", "wochentlich": "7d", "weekly": "7d",
              "taglich": "1d", "daily": "1d", "biennial": "24mo"}
_LEX = re.compile(r"\d+(?:[.,]\d+)*|[a-z]+|%")


def _num(tok: str) -> str:
    if re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", tok):  # 250.000 / 50,000 → 250000 / 50000
        return re.sub(r"[.,]", "", tok).lstrip("0") or "0"
    tok = tok.replace(",", ".")
    return tok if "." in tok else (tok.lstrip("0") or "0")


def _unit_at(toks: list[str], k: int) -> str | None:
    if k >= len(toks):
        return None
    return "pct" if toks[k] == "%" else UNIT_CANON.get(toks[k])


def _qty(value: str, unit: str) -> str:
    """Canonical quantity term: years → months, weeks → days ("1 year" = "12 Monate", "2 Jahre" = "24 Monate")."""
    factor = {"y": ("mo", 12), "w": ("d", 7)}.get(unit)
    if factor:
        try:
            v = float(value) * factor[1]
            value, unit = (str(int(v)) if v.is_integer() else str(v)), factor[0]
        except ValueError:
            pass
    return f"{value}{unit}"


def _cue(unit: str) -> str:
    return {"y": "mo", "w": "d"}.get(unit, unit)


@lru_cache(maxsize=50_000)
def cistem(w: str) -> str:
    """CISTEM stemmer (Weißweiler & Fraser 2017), case-insensitive variant; input is already folded."""
    if len(w) <= 3:
        return w
    participle = (w.startswith("ge") and len(w) >= 6) or w.endswith("et")
    w = re.sub(r"^ge(.{4,})", r"\1", w)
    w = w.replace("sch", "$").replace("ei", "%").replace("ie", "&")
    w = re.sub(r"(.)\1", r"\1*", w)
    while len(w) > 3:
        if len(w) > 5:
            w2 = re.sub(r"e[mr]$", "", w)
            if w2 == w:
                w2 = re.sub(r"nd$", "", w)
            if w2 != w:
                w = w2
                continue
        w2 = w
        if participle:  # case-insensitive variant: only participles lose their final -t (gemeldet ~ melden), once
            w2, participle = re.sub(r"t$", "", w), False
        if w2 == w:
            w2 = re.sub(r"[esn]$", "", w)
        if w2 == w:
            break
        w = w2
    w = re.sub(r"(.)\*", r"\1\1", w)
    w = w.replace("$", "sch").replace("%", "ei").replace("&", "ie")
    return w[:-3] if w.endswith("ung") and len(w) >= 7 else w  # Meldung ~ melden, Leitung ~ leitet


class Vocab:
    """Compound splitting against the corpus vocabulary: longest known head, optional linking element, known or recursively
    split remainder (an unknown inflected tail ≥ 5 chars is kept): 'wiederanlaufzeiten' → wiederanlauf + zeiten."""

    def __init__(self, words: Iterable[str]) -> None:
        self.words = frozenset(w for w in words if len(w) >= 4 and w not in R_STOP)
        self.stems = frozenset(cistem(w) for w in self.words)
        self._memo: dict[str, list[str]] = {}

    def split(self, w: str) -> list[str]:
        if w not in self._memo:
            parts = self._split(w, 0)
            self._memo[w] = parts if len(parts) > 1 else []
        return self._memo[w]

    def _split(self, w: str, depth: int) -> list[str]:
        fallback: list[str] | None = None
        for i in range(len(w) - 4, 3, -1):
            head = w[:i]
            if head not in self.words:
                continue
            rest = w[i:]
            for link in ("", "s", "es", "n", "en"):
                if not rest.startswith(link) or len(rest) - len(link) < 4:
                    continue
                r = rest[len(link):]
                if r in self.words:
                    return [head, r]
                if depth < 2:
                    sub = self._split(r, depth + 1)
                    if len(sub) > 1:
                        return [head] + sub
            if fallback is None and len(rest) >= 5:
                fallback = [head, rest]
        if fallback is None and depth == 0:  # unknown head, known (inflected) tail: "firmenlaptop" → firmen + laptop
            for i in range(4, len(w) - 3):
                if cistem(w[i:]) in self.stems and w[i:] not in R_STOP:
                    return [w[:i], w[i:]]
        return fallback or [w]


def analyze(text: str, vocab: Vocab | None = None) -> list[str]:
    """Index/query terms: stems, compound parts, numbers ('50000'), quantities ('24h', '12mo'), unit cues ('u:h') and
    identifiers ('tier2', 'tls1.3', 'aes256')."""
    toks = _LEX.findall(fold(text))
    out: list[str] = []
    i, n = 0, len(toks)
    while i < n:
        t = toks[i]
        if t[0].isdigit():
            v = _num(t)
            out.append(v)
            u = _unit_at(toks, i + 1)
            if u:
                out += [_qty(v, u), "u:" + _cue(u)]
                i += 2
                continue
        elif t != "%":
            if t in FREQ_WORDS:
                out.append(FREQ_WORDS[t])
            if t in UNIT_CANON and len(t) > 3:
                out.append("u:" + _cue(UNIT_CANON[t]))
            elif len(t) >= 2 and t not in R_STOP:
                if i + 1 < n and toks[i + 1][0].isdigit() and not _unit_at(toks, i + 2):
                    out.append(t + _num(toks[i + 1]))
                out.append(cistem(t))
                if vocab is not None and len(t) >= 8:
                    out += [cistem(p) for p in vocab.split(t)]
        i += 1
    return out


def _edit_distance(a: str, b: str, cap: int) -> int:
    """Damerau-Levenshtein (optimal string alignment) with early exit above `cap`."""
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev2: list[int] = []
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
            if i > 1 and j > 1 and ca == b[j - 2] and a[i - 2] == cb:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        if min(cur) > cap:
            return cap + 1
        prev2, prev = prev, cur
    return prev[-1]


GLOSSARY_PATH = Path(__file__).resolve().parent.parent / "glossary.json"  # engine/glossary.json


class Glossary:
    """engine/glossary.json: groups of interchangeable domain terms (DE/EN pairs, abbreviations)."""

    def __init__(self, path: Path) -> None:
        try:
            raw = path.read_bytes()
            groups = json.loads(raw).get("groups", [])
        except (OSError, ValueError):
            raw, groups = b"", []
        self.digest = hashlib.sha256(raw).hexdigest()[:16]
        self.words = {w for g in groups for m in g for w in _LEX.findall(fold(m)) if w.isalpha()}
        self.groups: list[list[frozenset[str]]] = []
        self.single: dict[str, list[int]] = defaultdict(list)
        self.multi: list[tuple[frozenset[str], int]] = []
        for g in groups:
            members = [frozenset(t for t in analyze(m) if not t.startswith("u:")) for m in g]
            members = [m for m in members if m]
            gi = len(self.groups)
            self.groups.append(members)
            for m in members:
                if len(m) == 1:
                    self.single[next(iter(m))].append(gi)
                else:
                    self.multi.append((m, gi))


GLOSSARY = Glossary(GLOSSARY_PATH)

# Text addressed to the assistant instead of being policy content ("ignore all previous instructions", "answer only …").
INSTRUCTION_RE = re.compile(
    r"(\bignor\w*\s+(?:alle\w*|all|any|the|previous|prior|vorherig\w*|bisherig\w*|obig\w*)\b|\bvergiss\s+(?:alle\w*|die|deine|your)\b"
    r"|\bforget\s+(?:all|everything|your|the)\b|system\s*-?\s*prompt|^\s*system\s*:|\bdu bist (?:jetzt|nun|ab sofort)\b|\byou are now\b"
    r"|\b(?:antworte|answer|respond|reply)\w*\b.{0,40}\b(?:nur|only|ausschliesslich|exclusively|everything|alles|jede\w*|every)\b"
    r"|\bact as\b|\bpretend\b|\bassistenzsystem\w*|\bjailbreak\b)", re.I)

TABLE_LABEL = re.compile(r"^Tabelle \[[^\]]*\]:\s*")
