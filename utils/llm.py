"""
Provider-agnostic LLM client.

Supports:
  - Ollama (local, default): any model via OpenAI-compatible API at localhost:11434
  - Any OpenAI-compatible endpoint (set LLM_PROVIDER=openai_compat + OLLAMA_BASE_URL)
  - Anthropic Claude (set LLM_PROVIDER=anthropic + ANTHROPIC_API_KEY)

The brain's research-based system prompt works with any provider —
it is injected as the system message every call.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

from config import settings
from utils.logger import log


# ── Normalized types ─────────────────────────────────────────────────────────

@dataclass
class ToolCall:
    id: str
    name: str
    input: dict


@dataclass
class LLMResponse:
    text: str
    stop_reason: str          # "end_turn" | "tool_use"
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw: Any = None           # original SDK response object


# ── Format helpers ────────────────────────────────────────────────────────────

def _to_openai_tools(anthropic_tools: list[dict]) -> list[dict]:
    """Convert Anthropic tool-schema format → OpenAI function-calling format."""
    result = []
    for t in anthropic_tools:
        result.append({
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
            },
        })
    return result


# ── Main client ───────────────────────────────────────────────────────────────

class LLMClient:
    """
    Single entry-point for all LLM calls in the application.

    Picks the provider automatically:
      1. If LLM_PROVIDER=anthropic AND a real ANTHROPIC_API_KEY is set → use Claude
      2. Otherwise → use Ollama (or any OpenAI-compatible endpoint)
    """

    def __init__(self):
        provider = (settings.llm_provider or "ollama").lower()
        api_key = settings.anthropic_api_key or ""
        real_key = api_key and api_key not in ("", "your_anthropic_api_key_here")

        if provider == "anthropic" and real_key:
            import anthropic as _anthropic
            self._provider = "anthropic"
            self._model = settings.anthropic_model
            self._client = _anthropic.Anthropic(api_key=api_key)
            log.info(f"LLM: Anthropic Claude ({self._model})")
        else:
            try:
                from openai import OpenAI
            except ImportError:
                raise RuntimeError(
                    "openai package is required for Ollama support.\n"
                    "Install it: pip install openai"
                )
            base = settings.ollama_base_url.rstrip("/")
            if not base.endswith("/v1"):
                base = base + "/v1"
            self._provider = "openai_compat"
            self._model = settings.ollama_model
            self._client = OpenAI(base_url=base, api_key="ollama")
            log.info(f"LLM: Ollama ({self._model}) at {base}")

    @property
    def provider(self) -> str:
        return self._provider

    # ── Simple chat (no tools) ────────────────────────────────────────────────

    def chat(
        self,
        messages: list[dict],
        system: Optional[str] = None,
        max_tokens: int = 4096,
    ) -> str:
        """Plain chat — returns assistant text."""
        if self._provider == "anthropic":
            kwargs: dict = dict(model=self._model, max_tokens=max_tokens, messages=messages)
            if system:
                kwargs["system"] = system
            resp = self._client.messages.create(**kwargs)
            return resp.content[0].text
        else:
            msgs = []
            if system:
                msgs.append({"role": "system", "content": system})
            msgs.extend(messages)
            resp = self._client.chat.completions.create(
                model=self._model,
                max_tokens=max_tokens,
                messages=msgs,
            )
            return resp.choices[0].message.content or ""

    # ── Agentic completion (with tools) ──────────────────────────────────────

    def complete_with_tools(
        self,
        messages: list[dict],
        system: str,
        tools: list[dict],
        max_tokens: int = 4096,
    ) -> LLMResponse:
        """
        One round of the agentic loop.
        Returns a normalized LLMResponse with text and/or tool_calls.
        """
        if self._provider == "anthropic":
            return self._anthropic_complete(messages, system, tools, max_tokens)
        else:
            return self._openai_complete(messages, system, tools, max_tokens)

    def _anthropic_complete(self, messages, system, tools, max_tokens) -> LLMResponse:
        resp = self._client.messages.create(
            model=self._model,
            max_tokens=max_tokens,
            system=system,
            tools=tools,
            messages=messages,
        )
        text = ""
        calls = []
        for block in resp.content:
            if hasattr(block, "text"):
                text = block.text
            elif block.type == "tool_use":
                calls.append(ToolCall(id=block.id, name=block.name, input=block.input))
        stop = "tool_use" if resp.stop_reason == "tool_use" else "end_turn"
        return LLMResponse(text=text, stop_reason=stop, tool_calls=calls, raw=resp)

    def _openai_complete(self, messages, system, tools, max_tokens) -> LLMResponse:
        openai_msgs = [{"role": "system", "content": system}] + messages
        openai_tools = _to_openai_tools(tools)

        resp = self._client.chat.completions.create(
            model=self._model,
            max_tokens=max_tokens,
            messages=openai_msgs,
            tools=openai_tools,
        )
        msg = resp.choices[0].message
        text = msg.content or ""
        calls = []
        if msg.tool_calls:
            for tc in msg.tool_calls:
                try:
                    inp = json.loads(tc.function.arguments)
                except Exception:
                    inp = {}
                calls.append(ToolCall(id=tc.id, name=tc.function.name, input=inp))

        finish = resp.choices[0].finish_reason
        stop = "tool_use" if finish == "tool_calls" else "end_turn"
        return LLMResponse(text=text, stop_reason=stop, tool_calls=calls, raw=resp)

    # ── Message builders ──────────────────────────────────────────────────────

    def build_next_messages(
        self,
        llm_response: LLMResponse,
        tool_results: list[dict],   # [{"tool_call_id": str, "content": str}, ...]
    ) -> list[dict]:
        """
        Build the messages to append after a tool-use round.
        Returns [assistant_msg, *tool_result_msgs] in provider format.
        """
        if self._provider == "anthropic":
            return self._anthropic_next(llm_response, tool_results)
        else:
            return self._openai_next(llm_response, tool_results)

    def _anthropic_next(self, resp: LLMResponse, results: list[dict]) -> list[dict]:
        assistant_msg = {"role": "assistant", "content": resp.raw.content}
        result_content = [
            {
                "type": "tool_result",
                "tool_use_id": r["tool_call_id"],
                "content": r["content"],
            }
            for r in results
        ]
        return [assistant_msg, {"role": "user", "content": result_content}]

    def _openai_next(self, resp: LLMResponse, results: list[dict]) -> list[dict]:
        raw_msg = resp.raw.choices[0].message
        tool_calls_data = []
        if raw_msg.tool_calls:
            for tc in raw_msg.tool_calls:
                tool_calls_data.append({
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                })
        assistant_msg = {
            "role": "assistant",
            "content": resp.text or None,
            "tool_calls": tool_calls_data,
        }
        result_msgs = [
            {"role": "tool", "tool_call_id": r["tool_call_id"], "content": r["content"]}
            for r in results
        ]
        return [assistant_msg] + result_msgs
