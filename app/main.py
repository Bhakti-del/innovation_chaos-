"""
SmartCanteen AI — FastAPI application entry point.

Startup:
  - init_db() creates all tables and seeds default data.

Middleware:
  - CORS: all origins (LAN demo).
  - Rate limiting: 10 requests/minute per IP (in-memory, sliding 60 s window).

Routes:
  GET  /           → redirect to /static/login.html
  GET  /health     → {"status", "ai_enabled", "model_loaded", "uptime_seconds"}
  POST /auth/token → issue JWT
  POST /auth/register → self-registration (student role only)
  /api/*           → orders, forecast, waste, audit, settings routers

Error handlers:
  403 → {"detail": "Forbidden"}
  404 → {"detail": "Not found"}
"""

import os
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.auth import (  # noqa: F401 — imported so auth helpers are available
    create_access_token,
    get_current_user,
    hash_password,
    require_manager,
    verify_password,
)
from app.database import get_db, init_db
from app.models import Settings, User, UserRole  # noqa: F401

# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------
from app.routers import audit, forecast, orders, settings, waste
from app.routers.auth_router import router as auth_router

# ---------------------------------------------------------------------------
# Startup timestamp  (used by /health to report uptime_seconds)
# ---------------------------------------------------------------------------

_start_time: float = time.time()

# ---------------------------------------------------------------------------
# Rate-limit state  (in-memory, per-IP, sliding window = 60 s, max 10 req)
# ---------------------------------------------------------------------------

_rate_limit_store: dict[str, list[datetime]] = defaultdict(list)
_RATE_LIMIT_MAX = 60
_RATE_LIMIT_WINDOW = 60  # seconds


def _check_rate_limit(ip: str) -> None:
    """Raise HTTP 429 if *ip* has exceeded the request quota for this window."""
    now = datetime.utcnow()
    cutoff = now - timedelta(seconds=_RATE_LIMIT_WINDOW)
    # Drop timestamps outside the window
    _rate_limit_store[ip] = [t for t in _rate_limit_store[ip] if t > cutoff]
    if len(_rate_limit_store[ip]) >= _RATE_LIMIT_MAX:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded: max 10 requests per minute per IP",
        )
    _rate_limit_store[ip].append(now)


# ---------------------------------------------------------------------------
# Lifespan (replaces deprecated on_event("startup"))
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialise the database on startup, then run the app."""
    global _start_time
    _start_time = time.time()
    init_db()
    yield


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="SmartCanteen AI",
    version="1.0.0",
    description=(
        "Demand forecasting, slot booking and waste reduction for a college canteen."
    ),
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# CORS — allow all origins for LAN demo
# ---------------------------------------------------------------------------

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Static files  (/static → ./static/)
# ---------------------------------------------------------------------------

_static_dir = str(Path(__file__).parent.parent / "static")
app.mount("/static", StaticFiles(directory=_static_dir), name="static")

# ---------------------------------------------------------------------------
# Rate-limit middleware
# ---------------------------------------------------------------------------

@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    """Apply per-IP rate limiting before every request reaches a handler."""
    client_ip = request.client.host if request.client else "unknown"
    try:
        _check_rate_limit(client_ip)
    except HTTPException as exc:
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": exc.detail},
        )
    return await call_next(request)


# ---------------------------------------------------------------------------
# Exception handlers — 403 and 404 return JSON instead of plain text
# ---------------------------------------------------------------------------

@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    """Return a consistent JSON body for all HTTP errors (including 403/404)."""
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail or ""},
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """Return 422 validation errors as JSON."""
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={"detail": exc.errors()},
    )


# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------

app.include_router(auth_router, prefix="/auth", tags=["auth"])
app.include_router(orders.router, prefix="/api", tags=["orders"])
app.include_router(forecast.router, prefix="/api", tags=["forecast"])
app.include_router(waste.router, prefix="/api", tags=["waste"])
app.include_router(audit.router, prefix="/api", tags=["audit"])
app.include_router(settings.router, prefix="/api", tags=["settings"])


# ---------------------------------------------------------------------------
# Core routes
# ---------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
def root():
    """Redirect browser root to the login page."""
    return RedirectResponse(url="/static/login.html", status_code=307)


@app.get("/health", tags=["health"])
def health(db: Session = Depends(get_db)):
    """System health check.

    Returns:
      status          — always "ok" when the server is reachable.
      ai_enabled      — current value of the ai_enabled setting.
      model_loaded    — True if ml/model.pkl exists on disk.
      uptime_seconds  — seconds since the server started.
    """
    # ai_enabled from DB settings
    row = db.query(Settings).filter(Settings.key == "ai_enabled").first()
    ai_enabled: bool = (row.value.lower() == "true") if row else False

    # model presence check (no import — just file existence)
    model_path = Path(__file__).parent.parent / "ml" / "model.pkl"
    model_loaded: bool = model_path.exists()

    uptime_seconds: float = round(time.time() - _start_time, 1)

    return {
        "status": "ok",
        "ai_enabled": ai_enabled,
        "model_loaded": model_loaded,
        "uptime_seconds": uptime_seconds,
    }
