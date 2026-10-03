"""Observable beat-criteria matching for Author Studio gates.

The model rarely repeats the spine wording verbatim ("a letter from Hobb arrives").
Token overlap with light synonym / tense folding is enough for a no-LLM gate.
"""

from __future__ import annotations

import re

# Criterion-token → acceptable surface forms in draft prose.
_SYNONYMS: dict[str, tuple[str, ...]] = {
    "arrives": (
        "arrives", "arrived", "arrival", "arriving",
        "delivered", "delivery", "brought", "handed",
        "awaited", "waiting", "received", "receives",
    ),
    "reads": ("reads", "read", "reading"),
    "aloud": ("aloud", "out loud", "outloud"),
    "offers": ("offers", "offered", "offering"),
    "accepts": ("accepts", "accepted", "accepting"),
    "mentioned": ("mentioned", "mention", "mentions", "mentioning"),
    "letter": ("letter", "letters", "envelope", "correspondence", "missive", "parchment"),
    "takes": ("takes", "took", "taking", "taken"),
    "arm": ("arm", "arms", "elbow"),
    "snow": ("snow", "snows", "snowfall", "snowing", "frost", "flakes"),
    "cup": ("cup", "cups", "teacup"),
    "tea": ("tea", "teapot", "teacup"),
}

_STOP = {
    "the", "and", "with", "that", "from", "must", "land", "onto", "into",
    "her", "his", "she", "they", "them", "this", "have", "been", "were",
    "when", "about", "their", "then", "than", "only", "also", "just",
}


def _variants(tok: str) -> list[str]:
    t = tok.lower()
    out = list(_SYNONYMS.get(t, (t,)))
    # Light tense folding when no synonym table entry.
    if t not in _SYNONYMS:
        if t.endswith("ies") and len(t) > 4:
            out.append(t[:-3] + "y")
        elif t.endswith("es") and len(t) > 4:
            out.append(t[:-2])
            out.append(t[:-1])
        elif t.endswith("s") and len(t) > 3:
            out.append(t[:-1])
        elif t.endswith("ed") and len(t) > 4:
            out.append(t[:-2])
            out.append(t[:-1])
        elif t.endswith("ing") and len(t) > 5:
            out.append(t[:-3])
    # de-dupe, keep order
    seen: set[str] = set()
    uniq: list[str] = []
    for v in out:
        if v and v not in seen:
            seen.add(v)
            uniq.append(v)
    return uniq


def _present(tok: str, blob: str) -> bool:
    for v in _variants(tok):
        if " " in v:
            if v in blob:
                return True
        else:
            if re.search(rf"\b{re.escape(v)}\b", blob):
                return True
    return False


def criteria_hits(text: str, criteria: list[str]) -> tuple[list[str], list[str]]:
    """Return (satisfied, missing) for observable beat criteria."""
    blob = (text or "").lower()
    # Normalise a couple of common compounds for phrase synonyms.
    blob = blob.replace("out-loud", "out loud")
    ok: list[str] = []
    missing: list[str] = []
    for c in criteria:
        raw = (c or "").strip()
        if not raw:
            continue
        low = raw.lower()
        if low in blob:
            ok.append(raw)
            continue
        toks = [t for t in re.findall(r"[a-z0-9]{3,}", low) if t not in _STOP]
        if not toks:
            ok.append(raw)
            continue
        hits = sum(1 for t in toks if _present(t, blob))
        need = max(1, (len(toks) + 1) // 2)
        # Proper-name-ish tokens (capitalised in the criterion) should usually
        # appear — otherwise "a letter arrives" could pass without Hobb.
        proper = [
            t for t in re.findall(r"\b([A-Z][a-z]{2,})\b", raw)
            if t.lower() not in _STOP
        ]
        proper_ok = all(_present(p.lower(), blob) for p in proper) if proper else True
        if hits >= need and proper_ok:
            ok.append(raw)
        else:
            missing.append(raw)
    return ok, missing
