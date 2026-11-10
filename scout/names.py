"""Person-name normalisation shared by ingest, founders import and matching."""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "phd", "md", "mba", "esq"}
_PARTICLES = {"de", "da", "del", "della", "di", "van", "von", "der", "den", "la", "le", "du", "dos", "das", "bin", "al", "el"}


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def normalize_name(name: str) -> str:
    """'José  García-López, Jr.' -> 'jose garcia lopez'. Lowercase ASCII, no punctuation, no suffixes."""
    s = strip_accents(name or "").lower()
    s = s.replace("-", " ").replace("'", "").replace("’", "")
    s = re.sub(r"[^a-z\s]", " ", s)
    toks = [t for t in s.split() if t not in _SUFFIXES]
    return " ".join(toks)


def split_name(norm: str) -> tuple[list[str], str]:
    """Return (given-name tokens, last name). Particles are folded into the last name."""
    toks = norm.split()
    if not toks:
        return [], ""
    if len(toks) == 1:
        return [], toks[0]
    last = [toks[-1]]
    i = len(toks) - 2
    while i > 0 and toks[i] in _PARTICLES:
        last.insert(0, toks[i])
        i -= 1
    return toks[: i + 1], " ".join(last)


def last_name_key(norm: str) -> str:
    _, last = split_name(norm)
    return last.split()[-1] if last else ""


def initials_compatible(a: str, b: str) -> bool:
    """True when the given names of a and b do not contradict each other (e.g. 'j smith' vs 'john smith',
    'j a smith' vs 'john smith'). Same last name required."""
    ga, la = split_name(a)
    gb, lb = split_name(b)
    if not la or la != lb:
        return False
    if not ga or not gb:
        return True
    # Compare leading initials of each given-name token, up to the shorter list.
    for x, y in zip(ga, gb):
        if x[0] != y[0]:
            return False
        if len(x) > 1 and len(y) > 1 and x != y:
            return False
    return True


def fuzzy_ratio(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def edit_distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def compare_names(a: str, b: str) -> tuple[str, float]:
    """Classify the relation between two normalised names.
    Returns (kind, ratio) with kind in {'exact', 'fuzzy', 'initials', 'none'}."""
    if not a or not b:
        return "none", 0.0
    if a == b:
        return "exact", 1.0
    r = fuzzy_ratio(a, b)
    if initials_compatible(a, b):
        # same last name and given names agree up to initials/abbreviation ("j smith" ~ "john smith",
        # "christopher l smith" ~ "christopher lacy smith"); treat as exact-ish when all given tokens are spelled out
        ga, _ = split_name(a)
        gb, _ = split_name(b)
        spelled = all(len(t) > 1 for t in ga + gb) and len(ga) == len(gb)
        return ("fuzzy" if spelled else "initials"), r
    # No edit-distance fuzziness on given names: "yufeng" vs "yifeng" or "jian" vs "jin" are different people,
    # and there is no lexical way to tell those from typos. Variants must come with corroborating signals.
    return "none", r
