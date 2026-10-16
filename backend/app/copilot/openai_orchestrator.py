"""Copilot turn on OpenAI Chat Completions (COPILOT_PROVIDER=openai).

Same tools, guardrails, compliance post-processing, audit and streamed events as the Claude
path; only the API calls differ. The guardrail runs on its rules (no LLM classifier on this
provider). Each stored message is the full OpenAI message dict, replayed as-is.
`reasoning_effort="none"` because gpt-5.x rejects function tools with reasoning on Chat
Completions; the tools do the analysis and the model explains it.
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator

import openai

from app.copilot.tools import TOOL_LABELS, tool_definitions

log = logging.getLogger(__name__)

TOOLS = [{"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]}}
         for t in tool_definitions()]

_client: openai.AsyncOpenAI | None = None


def get_openai_client() -> openai.AsyncOpenAI:
    global _client
    if _client is None:
        _client = openai.AsyncOpenAI(max_retries=2)
    return _client


async def run_turn_openai(turn, user_text: str) -> AsyncIterator[dict]:
    from app.copilot.orchestrator import finish_turn, open_turn

    try:
        async for ev in open_turn(turn, user_text, client=None):
            yield ev
        client = get_openai_client()
        messages = [{"role": "system", "content": turn.session.system_prompt}] + turn.history()
        tools_used: set[str] = set()
        text_parts: list[str] = []
        model = turn.session.model
        for _ in range(turn.settings.copilot_max_tool_rounds):
            stream = await client.chat.completions.create(
                model=turn.session.model, messages=messages, tools=TOOLS, stream=True, stream_options={"include_usage": True},
                reasoning_effort="none", max_completion_tokens=turn.settings.openai_max_tokens,
            )
            text, calls, finish, usage = "", {}, None, None
            async for chunk in stream:
                if chunk.usage:
                    usage = chunk.usage
                model = chunk.model or model
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                delta = choice.delta
                if delta.content:
                    text += delta.content
                    yield {"event": "text", "data": {"delta": delta.content}}
                for tc in delta.tool_calls or []:
                    entry = calls.setdefault(tc.index, {"id": None, "name": "", "arguments": ""})
                    if tc.id:
                        entry["id"] = tc.id
                    if tc.function and tc.function.name:
                        if not entry["name"]:
                            yield {"event": "status", "data": {"text": TOOL_LABELS.get(tc.function.name, "Working on it")}}
                        entry["name"] += tc.function.name
                    if tc.function and tc.function.arguments:
                        entry["arguments"] += tc.function.arguments
                finish = choice.finish_reason or finish

            message: dict = {"role": "assistant", "content": text or None}
            ordered = [calls[i] for i in sorted(calls)]
            if ordered:
                message["tool_calls"] = [{"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": c["arguments"]}} for c in ordered]
            row = turn.append("assistant", message, visible=False, model=model, prompt_version=turn.session.prompt_version,
                              usage={"input_tokens": usage.prompt_tokens, "output_tokens": usage.completion_tokens} if usage else None)
            messages.append(message)
            if text:
                text_parts.append(text)
            if finish != "tool_calls" or not ordered:
                break  # stop, or length with a possibly truncated tool call: never run it

            audit = []
            for c in ordered:
                try:
                    args = json.loads(c["arguments"] or "{}")
                except json.JSONDecodeError:
                    output, ok = {"error": "invalid JSON arguments"}, False
                else:
                    output, ok = await asyncio.to_thread(turn.ctx.run, c["name"], args)
                tools_used.add(c["name"])
                call = {"id": c["id"], "name": c["name"], "input": c["arguments"], "ok": ok, "output": output}
                audit.append(call)
                turn.tool_log.append(call)
                tool_msg = {"role": "tool", "tool_call_id": c["id"], "content": json.dumps(output, default=str)}
                turn.append("tool", tool_msg, visible=False)
                messages.append(tool_msg)
                if c["name"] == "run_recommendations" and turn.ctx.cards:
                    yield {"event": "cards", "data": {"cards": turn.ctx.cards}}
            row.tool_calls = [{k: v for k, v in a.items() if k != "output"} | {"output_preview": json.dumps(a["output"], default=str)[:2000]} for a in audit]
            turn.db.commit()

        yield finish_turn(turn, user_text, turn.verdict, turn.ticket, tools_used, text_parts, model)
    except openai.AuthenticationError:
        yield {"event": "error", "data": {"message": "The copilot isn't connected: the OpenAI API key was rejected."}}
    except openai.RateLimitError as exc:
        if "insufficient_quota" in str(exc):
            yield {"event": "error", "data": {"message": "RA's chat is offline: the OpenAI account is out of credits."}}
        else:
            yield {"event": "error", "data": {"message": "RA is busy right now. Please try again in a minute."}}
    except openai.APIConnectionError:
        yield {"event": "error", "data": {"message": "Couldn't reach the AI service. Check the server's network connection."}}
    except openai.APIStatusError as exc:
        log.exception("OpenAI API error")
        yield {"event": "error", "data": {"message": f"The AI service returned an error ({exc.status_code}). Please try again."}}
    except Exception:
        log.exception("Unexpected copilot failure (openai)")
        yield {"event": "error", "data": {"message": "Something went wrong. Please try again."}}
