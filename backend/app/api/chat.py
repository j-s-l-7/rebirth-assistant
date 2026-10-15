import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_investor
from app.copilot.compliance import DISCLOSURE_TEXT
from app.copilot.orchestrator import new_session, run_turn
from app.db import SessionLocal, get_db
from app.models import ChatMessage, ChatSession, Investor

router = APIRouter(tags=["chat"])


@router.post("/investors/{investor_id}/chat/sessions")
def create_session(inv: Investor = Depends(get_investor), db: Session = Depends(get_db)):
    s = new_session(db, inv)
    return {"id": s.id, "title": s.title, "started_at": s.started_at.isoformat(), "model": s.model}


@router.get("/investors/{investor_id}/chat/sessions")
def list_sessions(inv: Investor = Depends(get_investor), db: Session = Depends(get_db)):
    rows = db.scalars(select(ChatSession).where(ChatSession.investor_id == inv.id).order_by(ChatSession.started_at.desc()).limit(30))
    return [{"id": s.id, "title": s.title, "started_at": s.started_at.isoformat()} for s in rows]


@router.get("/chat/sessions/{session_id}/messages")
def list_messages(session_id: str, db: Session = Depends(get_db)):
    if db.get(ChatSession, session_id) is None:
        raise HTTPException(404, "Conversation not found")
    rows = db.scalars(select(ChatMessage).where(ChatMessage.session_id == session_id, ChatMessage.visible.is_(True)).order_by(ChatMessage.seq))
    return [{"id": m.id, "role": m.role, "text": m.display_text, "cards": m.cards or [], "disclosures": m.disclosures or [],
             "created_at": m.created_at.isoformat()} for m in rows]


class MessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


@router.post("/chat/sessions/{session_id}/messages")
async def send_message(session_id: str, body: MessageIn):
    async def events():
        async for ev in run_turn(SessionLocal, session_id, body.text.strip()):
            yield f"event: {ev['event']}\ndata: {json.dumps(ev['data'], default=str)}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/chat/disclosures")
def disclosures():
    return DISCLOSURE_TEXT
