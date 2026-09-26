"""Deterministic safety checks: emergencies, opt-out/opt-in, data deletion, medical-advice guard.

Runs before any LLM call. Keyword + regex lists cover English, Hinglish, Hindi and Marathi.
"""
from __future__ import annotations

import re
import unicodedata

_EMERGENCY_PATTERNS = [
    # English
    r"chest\s*pain", r"heart\s*attack", r"can'?t\s*breathe", r"cannot\s*breathe", r"not\s*able\s*to\s*breathe",
    r"breathless", r"short(ness)?\s*of\s*breath", r"difficulty\s*(in\s*)?breathing", r"trouble\s*breathing",
    r"unconscious", r"not\s*respond(ing)?", r"passed\s*out", r"fainted", r"collapsed",
    r"heavy\s*bleeding", r"bleeding\s*(a\s*lot|heavily|badly|profusely)", r"lot\s*of\s*blood", r"vomiting\s*blood",
    r"seizure", r"convulsion", r"\bfits?\b",
    r"suicid", r"kill\s*(my|him|her)self", r"end\s*my\s*life", r"self[\s-]*harm", r"want\s*to\s*die",
    r"stroke", r"face\s*(is\s*)?droop", r"slurred\s*speech", r"paraly[sz]",
    r"overdose", r"poison", r"snake\s*bite", r"severe\s*burn", r"choking",
    # Hinglish
    r"(seene|chhati|chati|chest)\s*(me|mein|main)\s*dard", r"saa?ns\s*(nahi|nahin|lene\s*me|phool)",
    r"behosh", r"khoon\s*(beh|bah|nikal)", r"(mirgi|daura\s*pad)", r"aatm\s*hatya|atmahatya",
    r"marna\s*chaht", r"lakwa", r"zeher|jahar",
    # Hindi (Devanagari)
    r"सीने\s*में\s*दर्द", r"छाती\s*में\s*दर्द", r"सांस\s*(नहीं|लेने\s*में)", r"साँस\s*(नहीं|लेने\s*में)",
    r"बेहोश", r"खून\s*(बह|निकल)", r"मिर्गी", r"दौरा\s*पड़", r"आत्महत्या", r"मरना\s*चाह", r"लकवा", r"ज़हर|जहर",
    r"दिल\s*का\s*दौरा",
    # Marathi
    r"छातीत\s*(दुख|वेदना|कळ)", r"श्वास\s*घेता\s*येत\s*नाही", r"दम\s*लागत", r"बेशुद्ध", r"रक्तस्त्राव",
    r"रक्त\s*वाहत", r"फेफरे", r"झटके", r"अर्धांगवायू", r"विष\s*प्या", r"जीव\s*द्याय",
]
_EMERGENCY_RE = re.compile("|".join(f"(?:{p})" for p in _EMERGENCY_PATTERNS), re.IGNORECASE)

_STOP = {"stop", "unsubscribe", "stop all", "बंद", "बंद करो", "बंद करें", "बंद करा", "थांबवा", "रोको"}
_START = {"start", "subscribe", "शुरू", "शुरू करो", "सुरू", "सुरू करा"}
_DELETE = [
    r"delete\s*my\s*data", r"erase\s*my\s*data", r"remove\s*my\s*data",
    r"मेरा\s*डेटा\s*(हटा|डिलीट|मिटा)", r"माझा\s*डेटा\s*(हटवा|डिलीट|काढ)",
]
_DELETE_RE = re.compile("|".join(_DELETE), re.IGNORECASE)
_GREETING = {
    "hi", "hii", "hello", "hey", "namaste", "namaskar", "hello doctor", "good morning", "good evening",
    "नमस्ते", "नमस्कार", "हाय", "हॅलो", "menu", "मेनू",
}
_ARRIVED = re.compile(
    r"(i\s*(have|'ve)\s*arrived|i\s*am\s*here|i'?m\s*here|reached\s*(the\s*)?clinic|pahunch\s*gay|"
    r"पहुँच\s*गय|पहुंच\s*गय|पोहोचलो|पोहोचले|आलो\s*आहे|आले\s*आहे)",
    re.IGNORECASE,
)
_MEDICAL = re.compile(
    r"(medicine|tablet|dose|dosage|\bmg\b|prescri|antibiotic|paracetamol|what\s*should\s*i\s*take|"
    r"which\s*(medicine|tablet)|dawa|dawai|goli|kya\s*(lu|loon|khau|khaun)|treatment\s*for|cure\s*for|"
    r"दवा|दवाई|गोली|औषध|गोळी|उपचार|इलाज|कोणते\s*औषध)",
    re.IGNORECASE,
)
# units are matched lower-case on purpose so an address like "12 MG Road" is not a dosage
_DOSAGE_OUT = re.compile(r"\b\d+(\.\d+)?\s*(mg|ml|mcg|g|iu)\b|\b\d+\s*(?i:tablets?|capsules?|drops|puffs)\b")


def _norm(text: str) -> str:
    return unicodedata.normalize("NFC", (text or "").strip()).lower()


def is_emergency(text: str | None) -> bool:
    return bool(text) and bool(_EMERGENCY_RE.search(_norm(text)))


def is_stop(text: str | None) -> bool:
    return _norm(text).rstrip(".!") in _STOP


def is_start(text: str | None) -> bool:
    return _norm(text).rstrip(".!") in _START


def is_delete_request(text: str | None) -> bool:
    return bool(text) and bool(_DELETE_RE.search(_norm(text)))


def is_greeting(text: str | None) -> bool:
    return _norm(text).rstrip(".!?") in _GREETING


def is_arrived(text: str | None) -> bool:
    return bool(text) and bool(_ARRIVED.search(_norm(text)))


def asks_medical_advice(text: str | None) -> bool:
    return bool(text) and bool(_MEDICAL.search(_norm(text)))


def output_has_dosage(text: str | None) -> bool:
    """Guard on LLM output: never let a dosage through, whatever the model wrote."""
    return bool(text) and bool(_DOSAGE_OUT.search(text))
