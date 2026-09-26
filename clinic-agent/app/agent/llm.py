"""Claude tool-calling loop (section 8.2) with a free, offline mock backend (LLM_MODE=mock)."""
from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from typing import Protocol

from app.agent import flows, tools
from app.agent.prompts import build_system_prompt
from app.agent.safety import output_has_dosage
from app.agent.state import Ctx
from app.config import get_clinic, get_settings
from app.i18n import t

log = logging.getLogger(__name__)

MAX_ITERATIONS = 5
MAX_TOKENS = 600
TEMPERATURE = 0.2
TIMEOUT_S = 20.0


class LLMBackend(Protocol):
    async def create(self, *, system: str, messages: list, tools: list): ...

    async def classify_hi_mr(self, text: str) -> str | None: ...


class AnthropicLLM:
    def __init__(self) -> None:
        import anthropic

        s = get_settings()
        self.model = s.LLM_MODEL
        self.client = anthropic.AsyncAnthropic(api_key=s.ANTHROPIC_API_KEY or None, timeout=TIMEOUT_S, max_retries=1)

    async def create(self, *, system, messages, tools):
        return await self.client.messages.create(
            model=self.model,
            max_tokens=MAX_TOKENS,
            temperature=TEMPERATURE,
            system=system,
            messages=messages,
            tools=tools,
        )

    async def classify_hi_mr(self, text: str) -> str | None:
        try:
            resp = await self.client.messages.create(
                model=self.model,
                max_tokens=5,
                temperature=0,
                system="Classify the language of the user's message. Reply with exactly one word: hi (Hindi) or mr (Marathi).",
                messages=[{"role": "user", "content": text[:500]}],
            )
        except Exception as exc:  # classification is best-effort
            log.warning("language classification failed: %s", type(exc).__name__)
            return None
        out = "".join(b.text for b in resp.content if b.type == "text").strip().lower()
        return out if out in ("hi", "mr") else None


_llm: LLMBackend | None = None


def get_llm() -> LLMBackend:
    global _llm
    if _llm is None:
        if get_settings().LLM_MODE == "live":
            _llm = AnthropicLLM()
        else:
            from app.agent.mock_llm import MockLLM

            _llm = MockLLM()
    return _llm


def set_llm(backend: LLMBackend | None) -> None:
    global _llm
    _llm = backend


def history_messages(ctx: Ctx) -> list[dict]:
    """Last <=10 turns as alternating user/assistant text messages, starting with user."""
    out: list[dict] = []
    for turn in ctx.turns():
        role = "assistant" if turn["role"] == "assistant" else "user"
        if out and out[-1]["role"] == role:
            out[-1]["content"] += "\n" + turn["text"]
        else:
            out.append({"role": role, "content": turn["text"]})
    while out and out[0]["role"] != "user":
        out.pop(0)
    return out


def _block_to_dict(b) -> dict:
    if b.type == "text":
        return {"type": "text", "text": b.text}
    if b.type == "tool_use":
        return {"type": "tool_use", "id": b.id, "name": b.name, "input": b.input}
    return {"type": b.type}


async def run_agent(ctx: Ctx, user_text: str) -> str | None:
    """Returns the final text to send (None if nothing more to say)."""
    backend = get_llm()
    system = build_system_prompt(ctx.lang)
    messages = history_messages(ctx)
    if messages and messages[-1]["role"] == "user":
        messages[-1]["content"] += "\n" + user_text
    else:
        messages.append({"role": "user", "content": user_text})
    for _ in range(MAX_ITERATIONS):
        try:
            resp = await backend.create(system=system, messages=messages, tools=tools.TOOLS)
        except Exception as exc:
            log.error("llm call failed: %s", type(exc).__name__)
            await flows.say(ctx, "fallback_error", phone=get_clinic().phone)
            return None
        if resp.stop_reason != "tool_use":
            text = "".join(b.text for b in resp.content if b.type == "text").strip()
            if output_has_dosage(text):
                text = t("medical.refuse", ctx.lang)
            return text or None
        messages.append({"role": "assistant", "content": [_block_to_dict(b) for b in resp.content]})
        results = []
        for b in resp.content:
            if b.type != "tool_use":
                continue
            try:
                out = await tools.execute(b.name, b.input, ctx)
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": json.dumps(out, default=str)})
            except tools.ToolError as exc:
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": str(exc), "is_error": True})
            except Exception as exc:
                log.exception("tool %s crashed", b.name)
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": f"internal error: {type(exc).__name__}", "is_error": True})
        messages.append({"role": "user", "content": results})
    await flows.handoff(ctx, "assistant could not complete the request")
    return None


def text_response(text: str) -> SimpleNamespace:
    return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=text)])


def tool_response(name: str, args: dict, call_id: str = "toolu_mock") -> SimpleNamespace:
    return SimpleNamespace(
        stop_reason="tool_use",
        content=[SimpleNamespace(type="tool_use", id=call_id, name=name, input=args)],
    )
