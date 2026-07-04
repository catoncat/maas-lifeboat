"""FastAPI application for the MAAS gateway."""

from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx
from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import config
from .errors import (
    anthropic_error_response,
    anthropic_error_status,
    check_client_auth,
    openai_error_response,
    openai_error_status,
    require_provider_key,
    retry_after_seconds,
)
from .logging import append_jsonl, attempt_to_log, console_log, sha16, utc_now
from .pressure import AccountPressureGate, PressurePermit
from .protocols import anthropic_response_to_openai, openai_response_to_anthropic
from .strategy import MaasGateway, planned_attempt_count
from .types import PreparedStreamFailure


def pressure_to_log(
    permit: PressurePermit,
    *,
    cooldown_set_s: float | None = None,
    retry_after_s: int | None = None,
    queue_scope: str = "request",
) -> dict[str, float | int | str | None]:
    return {
        "inflight_limit": permit.inflight_limit,
        "queue_scope": queue_scope,
        "queue_wait_s": permit.queue_wait_s,
        "cooldown_wait_s": permit.cooldown_wait_s,
        "total_wait_s": permit.waited_s,
        "busy_cooldown_set_s": cooldown_set_s,
        "retry_after_s": retry_after_s,
    }


def make_app() -> FastAPI:
    proxy = config.PROXY_URL or None
    client = httpx.AsyncClient(proxy=proxy, trust_env=not bool(proxy))
    gateway = MaasGateway(client)
    pressure_gate = AccountPressureGate(config.MAX_INFLIGHT_REQUESTS, config.BUSY_COOLDOWN_S)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            yield
        finally:
            await client.aclose()

    app = FastAPI(title="MAAS Lifeboat", version="0.1.0", lifespan=lifespan)

    @app.get("/v1/models")
    async def models(authorization: str | None = Header(default=None)) -> JSONResponse:
        check_client_auth(authorization)
        return JSONResponse(
            {
                "object": "list",
                "data": [
                    {
                        "id": config.MODEL,
                        "object": "model",
                        "owned_by": "maas-gateway",
                        "name": config.MODEL,
                        "context_window": config.MODEL_CONTEXT_WINDOW,
                        "contextWindow": config.MODEL_CONTEXT_WINDOW,
                        "max_tokens": config.MODEL_MAX_TOKENS,
                        "maxTokens": config.MODEL_MAX_TOKENS,
                        "input": ["text"],
                        "reasoning": False,
                    }
                ],
            }
        )

    @app.post("/v1/chat/completions")
    async def openai_chat(request: Request, authorization: str | None = Header(default=None)):
        require_provider_key()
        check_client_auth(authorization)
        payload = await request.json()
        if payload.get("stream"):
            request_id = str(uuid.uuid4())
            request_start_ts = utc_now()
            started = time.perf_counter()
            console_log(f"request start id={request_id} surface=openai stream=true payload={sha16(payload)} max_attempts={planned_attempt_count('openai')}")
            permit = await pressure_gate.acquire(request_id, "openai")
            try:
                prepared = await gateway.prepare_stream_strategy("openai", payload, request_id)
            except Exception:
                permit.release()
                raise
            if isinstance(prepared, PreparedStreamFailure):
                cooldown_set_s = pressure_gate.observe_attempts(request_id, "openai", prepared.attempts)
                retry_after_s = retry_after_seconds(openai_error_status(prepared.final), prepared.attempts)
                permit.release()
                console_log(f"request end id={request_id} surface=openai stream=true ok=false attempts={len(prepared.attempts)} elapsed={round(time.perf_counter() - started, 3)}s")
                append_jsonl(
                    config.LEDGER,
                    {
                        "ts": utc_now(),
                        "request_start_ts": request_start_ts,
                        "request_id": request_id,
                        "surface": "openai",
                        "stream": True,
                        "payload_sha256_16": sha16(payload),
                        "ok": False,
                        "elapsed_s": round(time.perf_counter() - started, 3),
                        "pressure": pressure_to_log(permit, cooldown_set_s=cooldown_set_s, retry_after_s=retry_after_s),
                        "attempts": [attempt_to_log(a) for a in prepared.attempts],
                    },
                )
                return openai_error_response(prepared.final, prepared.attempts)

            # The queue protects the fragile acceptance/retry window.  Once we
            # have a real first chunk buffered, holding the permit for the whole
            # stream would make long coding replies block unrelated conversations.
            permit.release()

            async def generate() -> AsyncIterator[bytes]:
                try:
                    async for chunk in prepared.chunks:
                        yield chunk
                finally:
                    cooldown_set_s = pressure_gate.observe_attempts(request_id, "openai", prepared.attempts)
                    ok = any(a.ok for a in prepared.attempts)
                    console_log(f"request end id={request_id} surface=openai stream=true ok={str(ok).lower()} attempts={len(prepared.attempts)} elapsed={round(time.perf_counter() - started, 3)}s")
                    append_jsonl(
                        config.LEDGER,
                        {
                            "ts": utc_now(),
                            "request_start_ts": request_start_ts,
                            "request_id": request_id,
                            "surface": "openai",
                            "stream": True,
                            "payload_sha256_16": sha16(payload),
                            "ok": ok,
                            "elapsed_s": round(time.perf_counter() - started, 3),
                            "pressure": pressure_to_log(permit, cooldown_set_s=cooldown_set_s, queue_scope="first_chunk"),
                            "attempts": [attempt_to_log(a) for a in prepared.attempts],
                        },
                    )

            return StreamingResponse(generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

        request_id = str(uuid.uuid4())
        request_start_ts = utc_now()
        started = time.perf_counter()
        console_log(f"request start id={request_id} surface=openai stream=false payload={sha16(payload)} max_attempts={planned_attempt_count('openai')}")
        async with await pressure_gate.acquire(request_id, "openai") as permit:
            final, attempts = await gateway.run_strategy("openai", payload, request_id)
            cooldown_set_s = pressure_gate.observe_attempts(request_id, "openai", attempts)
        success = final.ok and isinstance(final.response_json, dict)
        retry_after_s = None if success else retry_after_seconds(openai_error_status(final), attempts)
        append_jsonl(
            config.LEDGER,
            {
                "ts": utc_now(),
                "request_start_ts": request_start_ts,
                "request_id": request_id,
                "surface": "openai",
                "stream": False,
                "payload_sha256_16": sha16(payload),
                "ok": success,
                "elapsed_s": round(time.perf_counter() - started, 3),
                "pressure": pressure_to_log(permit, cooldown_set_s=cooldown_set_s, retry_after_s=retry_after_s),
                "attempts": [attempt_to_log(a) for a in attempts],
            },
        )
        if not success:
            console_log(f"request end id={request_id} surface=openai stream=false ok=false attempts={len(attempts)} elapsed={round(time.perf_counter() - started, 3)}s")
            return openai_error_response(final, attempts)
        body = final.response_json if final.interface == "openai" else anthropic_response_to_openai(final.response_json or {})
        console_log(f"request end id={request_id} surface=openai stream=false ok=true attempts={len(attempts)} elapsed={round(time.perf_counter() - started, 3)}s")
        return JSONResponse(body)

    @app.post("/anthropic/v1/messages")
    async def anthropic_messages(request: Request, authorization: str | None = Header(default=None)):
        require_provider_key()
        check_client_auth(authorization)
        payload = await request.json()
        if payload.get("stream"):
            request_id = str(uuid.uuid4())
            request_start_ts = utc_now()
            started = time.perf_counter()
            console_log(f"request start id={request_id} surface=anthropic stream=true payload={sha16(payload)} max_attempts={planned_attempt_count('anthropic')}")
            permit = await pressure_gate.acquire(request_id, "anthropic")
            try:
                prepared = await gateway.prepare_stream_strategy("anthropic", payload, request_id)
            except Exception:
                permit.release()
                raise
            if isinstance(prepared, PreparedStreamFailure):
                cooldown_set_s = pressure_gate.observe_attempts(request_id, "anthropic", prepared.attempts)
                retry_after_s = retry_after_seconds(anthropic_error_status(prepared.final), prepared.attempts)
                permit.release()
                console_log(f"request end id={request_id} surface=anthropic stream=true ok=false attempts={len(prepared.attempts)} elapsed={round(time.perf_counter() - started, 3)}s")
                append_jsonl(
                    config.LEDGER,
                    {
                        "ts": utc_now(),
                        "request_start_ts": request_start_ts,
                        "request_id": request_id,
                        "surface": "anthropic",
                        "stream": True,
                        "payload_sha256_16": sha16(payload),
                        "ok": False,
                        "elapsed_s": round(time.perf_counter() - started, 3),
                        "pressure": pressure_to_log(permit, cooldown_set_s=cooldown_set_s, retry_after_s=retry_after_s),
                        "attempts": [attempt_to_log(a) for a in prepared.attempts],
                    },
                )
                return anthropic_error_response(prepared.final, prepared.attempts)

            # The queue protects the fragile acceptance/retry window.  Once we
            # have a real first chunk buffered, holding the permit for the whole
            # stream would make long coding replies block unrelated conversations.
            permit.release()

            async def generate() -> AsyncIterator[bytes]:
                try:
                    async for chunk in prepared.chunks:
                        yield chunk
                finally:
                    cooldown_set_s = pressure_gate.observe_attempts(request_id, "anthropic", prepared.attempts)
                    ok = any(a.ok for a in prepared.attempts)
                    console_log(f"request end id={request_id} surface=anthropic stream=true ok={str(ok).lower()} attempts={len(prepared.attempts)} elapsed={round(time.perf_counter() - started, 3)}s")
                    append_jsonl(
                        config.LEDGER,
                        {
                            "ts": utc_now(),
                            "request_start_ts": request_start_ts,
                            "request_id": request_id,
                            "surface": "anthropic",
                            "stream": True,
                            "payload_sha256_16": sha16(payload),
                            "ok": ok,
                            "elapsed_s": round(time.perf_counter() - started, 3),
                            "pressure": pressure_to_log(permit, cooldown_set_s=cooldown_set_s, queue_scope="first_chunk"),
                            "attempts": [attempt_to_log(a) for a in prepared.attempts],
                        },
                    )

            return StreamingResponse(generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

        request_id = str(uuid.uuid4())
        request_start_ts = utc_now()
        started = time.perf_counter()
        console_log(f"request start id={request_id} surface=anthropic stream=false payload={sha16(payload)} max_attempts={planned_attempt_count('anthropic')}")
        async with await pressure_gate.acquire(request_id, "anthropic") as permit:
            final, attempts = await gateway.run_strategy("anthropic", payload, request_id)
            cooldown_set_s = pressure_gate.observe_attempts(request_id, "anthropic", attempts)
        success = final.ok and isinstance(final.response_json, dict)
        retry_after_s = None if success else retry_after_seconds(anthropic_error_status(final), attempts)
        append_jsonl(
            config.LEDGER,
            {
                "ts": utc_now(),
                "request_start_ts": request_start_ts,
                "request_id": request_id,
                "surface": "anthropic",
                "stream": False,
                "payload_sha256_16": sha16(payload),
                "ok": success,
                "elapsed_s": round(time.perf_counter() - started, 3),
                "pressure": pressure_to_log(permit, cooldown_set_s=cooldown_set_s, retry_after_s=retry_after_s),
                "attempts": [attempt_to_log(a) for a in attempts],
            },
        )
        if not success:
            console_log(f"request end id={request_id} surface=anthropic stream=false ok=false attempts={len(attempts)} elapsed={round(time.perf_counter() - started, 3)}s")
            return anthropic_error_response(final, attempts)
        body = final.response_json if final.interface == "anthropic" else openai_response_to_anthropic(final.response_json or {})
        console_log(f"request end id={request_id} surface=anthropic stream=false ok=true attempts={len(attempts)} elapsed={round(time.perf_counter() - started, 3)}s")
        return JSONResponse(body)

    return app


app = make_app()
