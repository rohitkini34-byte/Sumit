"""Relative date / time-of-day parsing for EN, Hinglish, HI, MR (used by the mock LLM and tests)."""
from __future__ import annotations

import re
from datetime import date, time, timedelta

_WEEKDAYS = {
    0: ["monday", "mon", "somvar", "सोमवार"],
    1: ["tuesday", "tue", "mangalvar", "मंगलवार", "मंगळवार"],
    2: ["wednesday", "wed", "budhvar", "बुधवार"],
    3: ["thursday", "thu", "guruvar", "गुरुवार", "वीरवार"],
    4: ["friday", "fri", "shukravar", "शुक्रवार"],
    5: ["saturday", "sat", "shanivar", "शनिवार"],
    6: ["sunday", "sun", "ravivar", "रविवार"],
}
_MONTHS = {m: i + 1 for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"])}

_TODAY = re.compile(r"\b(today|aaj|abhi)\b|आज|आजच")
_TOMORROW = re.compile(r"\b(tomorrow|tmrw|kal|kaal)\b|कल|उद्या|उद्याच")
_DAY_AFTER = re.compile(r"day\s*after\s*tomorrow|\bparso\b|परसों|परवा")
_MORNING = re.compile(r"\b(morning|subah|subha)\b|सुबह|सकाळ|सकाळी")
_EVENING = re.compile(r"\b(evening|shaam|sham|night)\b|शाम|संध्याकाळ|संध्याकाळी|सायंकाळ")
_AFTER = re.compile(r"\b(after|baad|nantar)\s*(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm)?|(\d{1,2})\s*(?:baje\s*)?(?:ke\s*)?baad|(\d{1,2})\s*नंतर|(\d{1,2})\s*के\s*बाद")


def parse_date(text: str, today: date) -> date | None:
    s = text.lower()
    if _DAY_AFTER.search(s):
        return today + timedelta(days=2)
    if _TOMORROW.search(s):
        return today + timedelta(days=1)  # "kal" is ambiguous; a past meaning is impossible for booking
    if _TODAY.search(s):
        return today
    for wd, names in _WEEKDAYS.items():
        for n in names:
            if re.search(rf"(?<![\wऀ-ॿ]){re.escape(n)}(?![\wऀ-ॿ])", s):
                delta = (wd - today.weekday()) % 7
                if delta == 0:
                    delta = 7
                if re.search(r"\bnext\s+week\b|अगले\s*हफ्ते|पुढच्या\s*आठवड्यात", s) and delta < 7:
                    delta += 7
                return today + timedelta(days=delta)
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", s)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.search(r"\b(\d{1,2})[/-](\d{1,2})\b", s)
    if m:
        return _roll(today, int(m.group(1)), int(m.group(2)))
    m = re.search(r"\b(\d{1,2})\s*(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b", s)
    if m:
        return _roll(today, int(m.group(1)), _MONTHS[m.group(2)])
    return None


def _roll(today: date, day: int, month: int) -> date | None:
    try:
        d = date(today.year, month, day)
    except ValueError:
        return None
    if d < today:
        d = date(today.year + 1, month, day)
    return d


def parse_part_of_day(text: str) -> str | None:
    s = text.lower()
    after = parse_not_before(text)
    if after is not None:
        return "evening" if after.hour >= 15 else "morning"
    if _EVENING.search(s):
        return "evening"
    if _MORNING.search(s):
        return "morning"
    return None


def parse_not_before(text: str) -> time | None:
    m = _AFTER.search(text.lower())
    if not m:
        return None
    hour = next(int(g) for g in (m.group(2), m.group(5), m.group(6), m.group(7)) if g)
    minute = int(m.group(3)) if m.group(3) else 0
    ampm = m.group(4)
    if ampm == "pm" and hour < 12:
        hour += 12
    elif ampm is None and 1 <= hour <= 8:
        hour += 12  # "after 6" at a clinic means 6 pm
    if hour > 23:
        return None
    return time(hour, minute)
