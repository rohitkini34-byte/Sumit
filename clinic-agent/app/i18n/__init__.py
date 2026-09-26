"""Fixed bot strings. Every patient-facing sentence lives in the JSON files, never inline."""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

LANGS = ("en", "hi", "mr")
_DIR = Path(__file__).resolve().parent


@lru_cache
def _strings(lang: str) -> dict[str, str]:
    return json.loads((_DIR / f"{lang}.json").read_text(encoding="utf-8"))


def t(key: str, lang: str = "en", **kw) -> str:
    lang = lang if lang in LANGS else "en"
    s = _strings(lang).get(key) or _strings("en").get(key)
    if s is None:
        raise KeyError(key)
    return s.format(**kw) if kw else s


def fill_numbered(text: str, params: list[str]) -> str:
    """Render a Meta-style body ({{1}}, {{2}}...) with values."""
    return re.sub(r"\{\{(\d+)\}\}", lambda m: str(params[int(m.group(1)) - 1]), text)


# ------------------------------------------------------------- language detection

_DEVANAGARI = re.compile(r"[ऀ-ॿ]")
# Words that are common in Marathi but not in Hindi (and vice versa).
_MR_MARKERS = {
    "आहे", "आहेत", "उद्या", "मला", "मिळेल", "का?", "हवी", "हवे", "पाहिजे", "नाही", "संध्याकाळी",
    "सकाळी", "करायची", "करायचे", "तुम्ही", "माझी", "माझे", "वेळ", "कधी", "आम्हाला", "होईल",
    "येईल", "झाला", "झाली", "आणि", "करा",
}
_HI_MARKERS = {
    "है", "हैं", "चाहिए", "मुझे", "कल", "मेरा", "मेरी", "क्या", "नहीं", "शाम", "सुबह", "करना",
    "आप", "और", "कितना", "समय", "मिलेगा", "होगा", "करें", "कीजिए", "हूँ",
}


def has_devanagari(text: str) -> bool:
    return bool(_DEVANAGARI.search(text or ""))


def heuristic_hi_mr(text: str) -> str | None:
    """Return 'hi' / 'mr' when marker words make it clear, else None (ask the LLM)."""
    words = set(re.findall(r"[ऀ-ॿ]+\??", text))
    words |= {w.rstrip("?") for w in words}
    mr = len(words & _MR_MARKERS)
    hi = len(words & _HI_MARKERS)
    if mr > hi:
        return "mr"
    if hi > mr:
        return "hi"
    return None


def detect_language_rule_based(text: str) -> str:
    if not has_devanagari(text):
        return "en"
    return heuristic_hi_mr(text) or "hi"
