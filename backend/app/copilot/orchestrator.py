"""Copilot orchestrator: Claude Fable 5.1 with tool use, streamed to the UI as events.

Transcript discipline (Fable 5.1 "preserved thinking"): the API history is append-only.
Every assistant turn is stored exactly as returned (thinking blocks + signatures
included) and replayed verbatim; the system prompt and tool list are frozen per session.
Compliance edits apply only to the separately stored display text.
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from datetime import date

import anthropic
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.copilot.compliance import post_process, sources_from_tool_calls, untraced_numbers
from app.copilot.guardrails import classify, system_note
from app.copilot.prompts import PROMPT_VERSION, build_system_prompt
from app.copilot.tools import TOOL_LABELS, ToolContext, tool_definitions
from app.models import AuditLog, ChatMessage, ChatSession, Escalation, Investor

log = logging.getLogger(__name__)

BETAS_FALLBACK = "server-side-fallback-2026-07-01"
BETAS_BINDING = "thinking-binding-controls-2026-08-01"
MAX_TOKENS = 32_000
TOOLS = tool_definitions()

_client: anthropic.AsyncAnthropic | None = None


def get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(max_retries=2)
    return _client


def new_session(db: Session, investor: Investor) -> ChatSession:
    s = get_settings()
    session = ChatSession(investor_id=investor.id, system_prompt=build_system_prompt(date.today()), prompt_version=PROMPT_VERSION, model=s.copilot_model)
    db.add(session)
    db.commit()
    return session


def _replayable(blocks: list[dict]) -> list[dict]:
    """After a mid-output refusal fallback, drop model-internal blocks that precede the
    last fallback marker (they belong to the declined model)."""
    idx = max((i for i, b in enumerate(blocks) if b.get("type") == "fallback"), default=None)
    if idx is None:
        return blocks
    return [b for i, b in enumerate(blocks) if i > idx or b.get("type") not in ("thinking", "redacted_thinking", "tool_use")]


class Turn:
    def __init__(self, db: Session, session: ChatSession, investor: Investor):
        self.db, self.session, self.investor = db, session, investor
        self.settings = get_settings()
        self.ctx = ToolContext(db, investor, session.id)
        self.seq = (db.scalar(select(func.max(ChatMessage.seq)).where(ChatMessage.session_id == session.id)) or 0)
        self.turn_rows: list[ChatMessage] = []
        self.tool_log: list[dict] = []

    def history(self) -> list[dict]:
        rows = self.db.scalars(select(ChatMessage).where(ChatMessage.session_id == self.session.id).order_by(ChatMessage.seq)).all()
        return [{"role": r.role, "content": r.api_content} for r in rows if r.api_content is not None]

    def append(self, role: str, api_content, **fields) -> ChatMessage:
        self.seq += 1
        row = ChatMessage(session_id=self.session.id, seq=self.seq, role=role, api_content=api_content, **fields)
        self.db.add(row)
        self.db.commit()
        self.turn_rows.append(row)
        return row

    def last_visible_assistant_text(self) -> str:
        row = self.db.scalar(
            select(ChatMessage).where(ChatMessage.session_id == self.session.id, ChatMessage.role == "assistant", ChatMessage.visible.is_(True))
            .order_by(ChatMessage.seq.desc()).limit(1)
        )
        return row.display_text or "" if row else ""

    def request_kwargs(self, messages: list[dict]) -> dict:
        kw = dict(
            model=self.session.model,
            max_tokens=MAX_TOKENS,
            system=[{"type": "text", "text": self.session.system_prompt}],
            tools=TOOLS,
            messages=messages,
            output_config={"effort": self.settings.copilot_effort},
            cache_control={"type": "ephemeral"},
            thinking={"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "drop_block"}},
            betas=[BETAS_BINDING],
        )
        if self.settings.copilot_refusal_fallbacks:
            kw["fallbacks"] = "default"
            kw["betas"].append(BETAS_FALLBACK)
        return kw


async def run_turn(session_factory: sessionmaker, session_id: str, user_text: str) -> AsyncIterator[dict]:
    """Yields events: status, text, cards, done, error."""
    db = session_factory()
    try:
        session = db.get(ChatSession, session_id)
        if session is None:
            yield {"event": "error", "data": {"message": "Conversation not found."}}
            return
        investor = db.get(Investor, session.investor_id)
        turn = Turn(db, session, investor)
        client = get_client()

        verdict = await classify(client, user_text, turn.last_visible_assistant_text())
        turn.append("user", user_text, display_text=user_text, visible=True, guardrail=verdict)
        if session.title == "New conversation":
            session.title = user_text[:80]
            db.commit()

        ticket = None
        if verdict["route"] == "escalate":
            esc = Escalation(investor_id=investor.id, session_id=session.id, category=verdict.get("topic", "other"), reason=user_text[:1000])
            db.add(esc)
            db.commit()
            ticket = esc.id
            yield {"event": "status", "data": {"text": "Bringing in a human expert", "escalation_id": ticket}}
        note = system_note(verdict, ticket)
        if note:
            turn.append("system", note, visible=False)

        messages = turn.history()
        tools_used: set[str] = set()
        final = None
        json_retries = 0
        rounds = 0
        while rounds < turn.settings.copilot_max_tool_rounds:
            rounds += 1
            try:
                async with client.beta.messages.stream(**turn.request_kwargs(messages)) as stream:
                    async for event in stream:
                        if event.type == "text":
                            yield {"event": "text", "data": {"delta": event.text}}
                        elif event.type == "content_block_start" and getattr(event.content_block, "type", "") == "tool_use":
                            yield {"event": "status", "data": {"text": TOOL_LABELS.get(event.content_block.name, "Working on it")}}
                    final = await stream.get_final_message()
                json_retries = 0
            except ValueError:
                # A streamed tool input that couldn't be parsed at all: re-issue the round (bounded).
                json_retries += 1
                rounds -= 1
                if json_retries > 2:
                    raise
                continue

            if final.stop_reason == "refusal":
                break
            content = _replayable([b.to_dict(mode="json") for b in final.content])
            tool_uses = [b for b in final.content if b.type == "tool_use"]
            row = turn.append("assistant", content, visible=False, model=final.model, prompt_version=session.prompt_version,
                              usage=final.usage.to_dict(mode="json") if final.usage else None)
            messages.append({"role": "assistant", "content": content})
            if final.stop_reason != "tool_use" or not tool_uses:
                break  # end_turn, or max_tokens with a possibly truncated tool input: never run it

            results, calls = [], []
            for tu in tool_uses:
                output, ok = await asyncio.to_thread(turn.ctx.run, tu.name, tu.input)
                tools_used.add(tu.name)
                calls.append({"id": tu.id, "name": tu.name, "input": tu.input, "ok": ok, "output": output})
                results.append({"type": "tool_result", "tool_use_id": tu.id, "content": json.dumps(output, default=str), "is_error": not ok})
                if tu.name == "run_recommendations" and turn.ctx.cards:
                    yield {"event": "cards", "data": {"cards": turn.ctx.cards}}
            row.tool_calls = [{k: v for k, v in c.items() if k != "output"} | {"output_preview": json.dumps(c["output"], default=str)[:2000]} for c in calls]
            turn.tool_log.extend(calls)
            db.commit()
            turn.append("user", results, visible=False)
            messages.append({"role": "user", "content": results})

        if final is None or final.stop_reason == "refusal":
            # Keep the investor's message (consecutive user turns merge). A system note must be
            # followed by an assistant turn, so drop it from replay - but only if no assistant
            # round followed it, since removing a message mid-history invalidates thinking blocks.
            if not any(r.role == "assistant" and r.api_content is not None for r in turn.turn_rows):
                for r in turn.turn_rows:
                    if r.role == "system":
                        r.api_content = None
            msg = "I can't help with that one. I'm happy to help with anything about your mutual funds."
            turn.append("assistant", None, display_text=msg, visible=True, model=session.model, prompt_version=session.prompt_version,
                        guardrail={"refusal": True, "category": getattr(getattr(final, "stop_details", None), "category", None)})
            yield {"event": "done", "data": {"text": msg, "disclosures": [], "cards": []}}
            return

        text_parts = []
        for r in turn.turn_rows:
            if r.role == "assistant" and isinstance(r.api_content, list):
                text_parts += [b.get("text", "") for b in r.api_content if b.get("type") == "text"]
        raw_text = "\n\n".join(p.strip() for p in text_parts if p.strip())
        display, flags, disclosures = post_process(raw_text, tools_used)
        untraced = untraced_numbers(raw_text, sources_from_tool_calls(turn.tool_log, user_text)) if turn.tool_log else []

        last = next(r for r in reversed(turn.turn_rows) if r.role == "assistant")
        last.display_text = display
        last.visible = True
        last.disclosures = disclosures
        last.cards = turn.ctx.cards or None
        last.reco_run_id = turn.ctx.reco_run_id
        last.guardrail = {"compliance_flags": flags, "untraced_numbers": untraced, "classifier": verdict}
        if turn.ctx.reco_run_id:
            db.add(AuditLog(actor_type="ai", actor_id=investor.id, action="recommendation_explained", entity_name="reco_run", entity_id=turn.ctx.reco_run_id,
                            payload={"session_id": session.id, "message_id": last.id, "model": final.model, "prompt_version": session.prompt_version,
                                     "disclosures": disclosures, "euin": turn.settings.euin, "compliance_flags": flags}))
        db.commit()
        yield {"event": "done", "data": {"message_id": last.id, "text": display, "disclosures": disclosures, "cards": turn.ctx.cards,
                                         "escalation_id": ticket or turn.ctx.escalation_id, "model": final.model}}
    except anthropic.AuthenticationError:
        yield {"event": "error", "data": {"message": "The copilot isn't connected yet: set ANTHROPIC_API_KEY on the server."}}
    except anthropic.RateLimitError:
        yield {"event": "error", "data": {"message": "RA is busy right now. Please try again in a minute."}}
    except anthropic.APIConnectionError:
        yield {"event": "error", "data": {"message": "Couldn't reach the AI service. Check the server's network connection."}}
    except anthropic.APIStatusError as exc:
        log.exception("Copilot API error")
        yield {"event": "error", "data": {"message": f"The AI service returned an error ({exc.status_code}). Please try again."}}
    except (anthropic.AnthropicError, TypeError) as exc:
        # The SDK raises TypeError when no credentials can be resolved at all.
        if "authentication" in str(exc).lower() or "api_key" in str(exc).lower():
            yield {"event": "error", "data": {"message": "The copilot isn't connected yet: set ANTHROPIC_API_KEY on the server."}}
        else:
            log.exception("Copilot error")
            yield {"event": "error", "data": {"message": "Something went wrong. Please try again."}}
    except Exception:
        # Streaming boundary: never drop the stream silently; the UI needs a terminal event.
        log.exception("Unexpected copilot failure")
        yield {"event": "error", "data": {"message": "Something went wrong. Please try again."}}
    finally:
        db.close()
