"""ATLAS FastAPI server with token auth, rate limiting, and command endpoints."""

from __future__ import annotations

import time
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address

import classifier
import executor
import history
import llm_engine
import macros
import settings
import verifier

START_TIME = time.time()

limiter = Limiter(key_func=get_remote_address)
app = FastAPI(title="ATLAS API", version="1.0")
app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)


def _rate_limit_handler(request: Request, exc: Exception) -> Any:
    """Adapt slowapi handler signature for FastAPI exception registration."""
    if isinstance(exc, RateLimitExceeded):
        return _rate_limit_exceeded_handler(request, exc)
    raise exc


app.add_exception_handler(RateLimitExceeded, _rate_limit_handler)


class CommandRequest(BaseModel):
    """Input payload for command execution."""

    text: str
    source: Literal["cli", "voice", "api"]


class MacroRunRequest(BaseModel):
    """Input payload for macro run endpoint."""

    name: str
    input: str = ""


async def _enforce_token(request: Request) -> None:
    """Validate X-ATLAS-Token header for protected endpoints."""
    provided = request.headers.get("X-ATLAS-Token", "")
    expected = str(settings.get("api_token") or "")
    if not expected or provided != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")


@app.get("/status")
async def status(request: Request) -> dict[str, Any]:
    """Return runtime and configuration status metadata."""
    await _enforce_token(request)
    return {
        "model": settings.get("model"),
        "voice_input": bool(settings.get("voice_input")),
        "voice_output": bool(settings.get("voice_output")),
        "pin_set": bool(settings.get("pin_hash")),
        "session_memory": bool(settings.get("session_memory")),
        "uptime_s": int(time.time() - START_TIME),
    }


@app.get("/dry-run")
async def dry_run(
    request: Request,
    text: str = Query(default="", description="Command text to classify."),
) -> dict[str, Any]:
    """Classify a command without executing it."""
    await _enforce_token(request)

    body_data: dict[str, Any] = {}
    try:
        body_data = await request.json()
    except Exception:
        body_data = {}

    final_text = text or str(body_data.get("text") or "")
    result = classifier.classify(final_text) or llm_engine.query(final_text, [])
    return result


@app.post("/command")
@limiter.limit("60/minute")
async def command(request: Request, payload: CommandRequest) -> dict[str, Any]:
    """Parse and execute a command through the secure execution pipeline."""
    await _enforce_token(request)

    started = time.perf_counter()
    intent = classifier.classify(payload.text) or llm_engine.query(payload.text, [])

    action = str(intent.get("action", ""))
    params = intent.get("params", {})
    if not isinstance(params, dict):
        params = {}

    execution_result = executor.execute(action, params)
    verify_result = verifier.verify(action, params, execution_result)

    latency_ms = int((time.perf_counter() - started) * 1000)
    history.log(
        raw=payload.text,
        action=action,
        params=params,
        success=bool(execution_result.get("success", False)),
        latency_ms=latency_ms,
        risk=str(intent.get("risk", "")),
    )

    return {
        "action": action,
        "result": str(execution_result.get("message", "")),
        "verified": verify_result.ok,
        "latency_ms": latency_ms,
    }


@app.get("/history")
async def get_history(request: Request, n: int = 20, q: str = "") -> list[dict[str, Any]]:
    """Return recent or filtered command history records."""
    await _enforce_token(request)
    if q:
        return history.search(q)
    return history.list_recent(n)


@app.get("/macros")
async def get_macros(request: Request) -> dict[str, Any]:
    """Return configured macros."""
    await _enforce_token(request)
    return macros.list()


@app.post("/macros/run")
async def run_macro(request: Request, payload: MacroRunRequest) -> dict[str, Any]:
    """Run a named macro with optional input substitution value."""
    await _enforce_token(request)
    return macros.run(payload.name, payload.input)
