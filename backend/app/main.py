from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.api import admin, chat, health, investors
from app.config import get_settings
from app.db import init_db
from app.telemetry import REQUEST_ID_HEADER, RequestIdMiddleware, configure_logging

configure_logging(get_settings().log_format, get_settings().log_level)


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="RA - Rebirth Assistant API", version=__version__, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in get_settings().cors_origins.split(",") if o.strip()],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=[REQUEST_ID_HEADER],
)
# Outermost, so every response (CORS preflights and errors included) carries an X-Request-ID.
app.add_middleware(RequestIdMiddleware)
for r in (health.router, investors.router, chat.router, admin.router):
    app.include_router(r, prefix="/api")
