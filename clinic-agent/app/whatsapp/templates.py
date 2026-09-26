"""Approved UTILITY templates (section 7.3): names, variables, quick-reply buttons.

Body text for each language lives in i18n/*.json under ``tpl.<name>.body`` using Meta's
{{n}} placeholders, so the same text is used for (a) the copy-paste list you submit to
Meta, (b) free-form messages inside the 24h window, and (c) the dev simulator.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.i18n import LANGS, fill_numbered, t


@dataclass(frozen=True)
class TemplateDef:
    name: str
    variables: tuple[str, ...]
    buttons: int = 0  # number of quick-reply buttons (labels in i18n: tpl.<name>.b1..bN)


TEMPLATES: dict[str, TemplateDef] = {
    d.name: d
    for d in [
        TemplateDef("appt_confirmation", ("name", "date", "token", "expected time", "report-by time", "doctor")),
        TemplateDef("appt_reminder_24h", ("name", "date", "token", "expected time"), 3),
        TemplateDef("appt_reminder_2h", ("name", "token", "current expected time", "address"), 3),
        TemplateDef("queue_turn_soon", ("token", "patients ahead", "expected time")),
        TemplateDef("queue_delay_notice", ("token", "new expected time"), 2),
        TemplateDef("queue_missed_turn", ("token", "what happens on arrival"), 2),
        TemplateDef("appt_no_show", ("name", "date"), 1),
        TemplateDef("followup_reminder", ("name", "doctor", "due date"), 2),
        TemplateDef("appt_cancelled", ("name", "date", "token"), 1),
        TemplateDef("appt_rescheduled", ("name", "new date", "new token", "new expected time", "report-by time")),
        TemplateDef("slot_offer_earlier", ("name", "current token + time", "earlier time", "minutes to reply"), 2),
        TemplateDef("waitlist_offer", ("name", "date", "time", "minutes to reply"), 2),
        TemplateDef("token_updated", ("name", "old token", "new token", "expected time")),
        TemplateDef("session_cancelled_by_clinic", ("name", "date", "session"), 2),
    ]
}


def body_text(name: str, lang: str) -> str:
    return t(f"tpl.{name}.body", lang)


def render(name: str, lang: str, params: list[str]) -> str:
    d = TEMPLATES[name]
    if len(params) != len(d.variables):
        raise ValueError(f"{name} needs {len(d.variables)} params, got {len(params)}")
    return fill_numbered(body_text(name, lang), [str(p) for p in params])


def button_labels(name: str, lang: str) -> list[str]:
    return [t(f"tpl.{name}.b{i}", lang) for i in range(1, TEMPLATES[name].buttons + 1)]


def meta_template_payload(to: str, name: str, lang: str, params: list[str], button_payloads: list[str]) -> dict:
    d = TEMPLATES[name]
    if len(button_payloads) != d.buttons:
        raise ValueError(f"{name} needs {d.buttons} button payloads")
    components: list[dict] = [
        {"type": "body", "parameters": [{"type": "text", "text": str(p)} for p in params]}
    ]
    for i, payload in enumerate(button_payloads):
        components.append(
            {
                "type": "button",
                "sub_type": "quick_reply",
                "index": str(i),
                "parameters": [{"type": "payload", "payload": payload}],
            }
        )
    return {
        "messaging_product": "whatsapp",
        "to": to.lstrip("+"),
        "type": "template",
        "template": {"name": name, "language": {"code": lang}, "components": components},
    }


def submission_list_markdown() -> str:
    """Copy-paste-ready list for Meta Business Manager (used to build the README section)."""
    out = []
    for d in TEMPLATES.values():
        out.append(f"### `{d.name}` (UTILITY)\n")
        out.append("Variables: " + ", ".join(f"{{{{{i + 1}}}}} {v}" for i, v in enumerate(d.variables)) + "\n")
        for lang in LANGS:
            out.append(f"- **{lang}**: {body_text(d.name, lang)}")
            if d.buttons:
                out.append(f"  - Quick replies: " + " / ".join(button_labels(d.name, lang)))
        out.append("")
    return "\n".join(out)
