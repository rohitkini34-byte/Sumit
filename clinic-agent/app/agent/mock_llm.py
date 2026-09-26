"""LLM_MODE=mock: a free, offline, rule-based stand-in for Claude.

It speaks the same response shape as the Anthropic SDK (content blocks + stop_reason) and
drives the *real* tools, so the whole bot works end-to-end with no API key. It is used by
the automated tests and is handy for demos; switch LLM_MODE=live for natural conversation.
"""
from __future__ import annotations

import json
import re
from datetime import date

from app.agent import dates
from app.agent.llm import text_response, tool_response
from app.agent.safety import asks_medical_advice
from app.i18n import heuristic_hi_mr, t

_LANGS = {"English": "en", "Hindi": "hi", "Marathi": "mr"}

_INJECTION = re.compile(r"ignore\s+(all\s+)?(previous\s+|the\s+)?(rules|instructions)|book\s+\d+\s+(slots|appointments)|system\s*prompt", re.I)
_HUMAN = re.compile(r"\b(human|staff|person|receptionist|real\s*person|talk\s*to\s*someone)\b|किसी\s*से\s*बात|स्टाफ|माणसाशी", re.I)
_LATE = re.compile(r"\b(late|der\s*ho|der\s*se|delay)\b|देर|उशीर|उशिरा", re.I)
_QUEUE = re.compile(r"my\s*turn|when\s*(is|will)\s*my|how\s*long|kitna\s*time|kitni\s*der|kab\s*(aayega|ayega|hoga)|bari|baari|बारी|नंबर\s*कधी|किती\s*वेळ|queue|token\s*status", re.I)
_CANCEL = re.compile(r"\bcancel\b|radd|रद्द|कैंसल|कॅन्सल", re.I)
_RESCHED = re.compile(r"reschedule|change\s*(my\s*)?(time|appointment|date)|move\s*my|shift\s*my|badal|बदल", re.I)
_MY = re.compile(r"my\s*appointments?|upcoming|mere\s*appointment|मेरे\s*अपॉइंटमेंट|माझ्या\s*अपॉइंटमेंट|माझी\s*अपॉइंटमेंट", re.I)
_ADDRESS = re.compile(r"address|location|where\s*is|kahan|kaha\s*hai|directions|पता|कहाँ|पत्ता|कुठे", re.I)
_FEE = re.compile(r"\bfees?\b|charges?|cost|price|kitne\s*paise|फीस|शुल्क|किती\s*पैसे", re.I)
_HOURS = re.compile(r"timings?|hours|open|kab\s*khul|कब\s*खुल|वेळा|उघड", re.I)
_PHONE = re.compile(r"phone|contact|call\s*you|number\s*of\s*clinic|फोन|नंबर\s*दो", re.I)
_WAITLIST = re.compile(r"wait\s*-?list|waiting\s*list|वेटिंग|प्रतीक्षा", re.I)
_LANG = re.compile(r"(in|me|mein)\s*(hindi|marathi|english)|(hindi|marathi|english)\s*(me|mein|madhe)|हिंदी\s*में|मराठीत", re.I)
_BOOK = re.compile(r"appointment|appt|book|slot|visit|come|aana|milna|dikhana|chahiye|milega|consult|अपॉइंटमेंट|मिलना|दिखाना|वेळ|भेट|यायचे|मिळेल|बुक", re.I)
_MINUTES = re.compile(r"(\d{1,3})\s*(min|minute|मिनट|मिनिट)", re.I)


def _last_user_text(messages: list) -> str:
    for m in reversed(messages):
        if m["role"] == "user" and isinstance(m["content"], str):
            return m["content"].split("\n")[-1]
    return ""


def _today(system: str) -> date:
    m = re.search(r"Today is (\d{4}-\d{2}-\d{2})", system)
    return date.fromisoformat(m.group(1))


def _lang(system: str, text: str) -> str:
    m = re.search(r"preferred language: (\w+)", system)
    lang = _LANGS.get(m.group(1), "en") if m else "en"
    if re.search(r"[ऀ-ॿ]", text):
        lang = heuristic_hi_mr(text) or (lang if lang in ("hi", "mr") else "hi")
    return lang


class MockLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def classify_hi_mr(self, text: str) -> str | None:
        return heuristic_hi_mr(text)

    async def create(self, *, system: str, messages: list, tools: list):
        self.calls += 1
        last = messages[-1]
        user_text = _last_user_text(messages)
        lang = _lang(system, user_text)
        if isinstance(last["content"], list) and last["content"] and last["content"][0].get("type") == "tool_result":
            return self._after_tool(messages, user_text, lang)
        return self._first_step(user_text, lang, _today(system))

    # ---------------------------------------------------------------- step 1
    def _first_step(self, text: str, lang: str, today: date):
        if _INJECTION.search(text):
            return text_response(t("mock.injection", lang))
        if asks_medical_advice(text):
            return text_response(t("medical.refuse", lang))
        if _HUMAN.search(text):
            return tool_response("handoff_to_human", {"reason": "patient asked for a person"})
        m = _LANG.search(text)
        if m:
            word = (m.group(2) or m.group(3) or "").lower()
            code = {"hindi": "hi", "marathi": "mr", "english": "en"}.get(word)
            if "हिंदी" in text:
                code = "hi"
            if "मराठीत" in text:
                code = "mr"
            if code:
                return tool_response("set_language", {"lang": code})
        if _LATE.search(text) and not re.search(r"\bbook\b|बुक", text, re.I):
            mins = _MINUTES.search(text)
            return tool_response("report_running_late", {"minutes_late": int(mins.group(1)) if mins else 15})
        if _QUEUE.search(text):
            return tool_response("get_my_queue_status", {})
        if _CANCEL.search(text):
            return tool_response("list_my_appointments", {})
        if _RESCHED.search(text):
            return tool_response("list_my_appointments", {})
        if _MY.search(text):
            return tool_response("list_my_appointments", {})
        if _WAITLIST.search(text):
            d = dates.parse_date(text, today) or today
            return tool_response("join_waitlist", {"date": d.isoformat(), "part_of_day": dates.parse_part_of_day(text) or "any"})
        for rx, topic in ((_ADDRESS, "address"), (_FEE, "fee"), (_PHONE, "phone")):
            if rx.search(text) and not _BOOK.search(text):
                return tool_response("get_clinic_info", {"topic": topic})
        d = dates.parse_date(text, today)
        part = dates.parse_part_of_day(text)
        if _BOOK.search(text) or d is not None or part is not None:
            args = {
                "date_from": (d or today).isoformat(),
                "date_to": (d or date.fromordinal(today.toordinal() + 3)).isoformat(),
                "part_of_day": part or "any",
            }
            nb = dates.parse_not_before(text)
            if nb:
                args["not_before"] = nb.strftime("%H:%M")
            return tool_response("get_available_slots", args)
        if _HOURS.search(text):
            return tool_response("get_clinic_info", {"topic": "hours"})
        return text_response(t("mock.fallback", lang))

    # ---------------------------------------------------------------- step 2+
    def _after_tool(self, messages: list, text: str, lang: str):
        prev = messages[-2]
        call = next(b for b in prev["content"] if b.get("type") == "tool_use")
        res_block = messages[-1]["content"][0]
        if res_block.get("is_error"):
            return text_response(t("mock.fallback", lang))
        result = json.loads(res_block["content"])
        name = call["name"]
        if name == "list_my_appointments":
            appts = result.get("appointments", [])
            wants = "cancel" if _CANCEL.search(text) else "reschedule" if _RESCHED.search(text) else None
            if wants and appts:
                tool = "request_cancel" if wants == "cancel" else "request_reschedule"
                args = {"appointment_id": appts[0]["appointment_id"]} if len(appts) == 1 else {}
                return tool_response(tool, args, "toolu_mock_2")
            if not appts:
                return text_response(t("my.none", lang))
            lines = [t("my.header", lang)] + [
                t("my.item", lang, token=a["token"], date=a["date"], time=a["expected_time_approx"]) for a in appts
            ]
            return text_response("\n".join(lines))
        if name == "get_my_queue_status":
            if not result.get("token"):
                return text_response(t("queue.none_today", lang))
            return text_response(
                t("queue.status", lang, token=result["token"], status=t(f"queue.status.{result['queue_status']}", lang),
                  ahead=result["ahead"], current=result.get("current") or t("queue.nobody", lang), eta=result.get("eta") or "-")
            )
        if name == "get_clinic_info":
            return text_response(result.get("info", ""))
        if name == "get_available_slots":
            return text_response(t("mock.slots_sent", lang) if result.get("slots") else t("slots.none", lang))
        if result.get("sent_to_patient"):
            return text_response("")
        return text_response(result.get("say", ""))
