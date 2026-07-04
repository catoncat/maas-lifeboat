"""Backend retry and fallback strategy."""

from __future__ import annotations

import asyncio
import json
import random
import time
from typing import Any, AsyncIterator

import httpx

from . import config
from .errors import retryable
from .logging import console_log, console_log_attempt
from .protocols import anthropic_to_openai, normalize_model, openai_to_anthropic
from .sse import anthropic_stream_to_openai, openai_stream_to_anthropic
from .types import AttemptResult, Interface, PreparedStream, PreparedStreamFailure


def alternate_interface(interface: Interface) -> Interface:
    return "anthropic" if interface == "openai" else "openai"


def attempt_interfaces(native: Interface, max_attempts: int | None = None) -> list[Interface]:
    limit = max_attempts or config.MAX_BACKEND_ATTEMPTS
    if limit <= 1:
        return [native]
    interfaces = [native, native]
    if config.CROSS_INTERFACE_FALLBACK:
        alt = alternate_interface(native)
        while len(interfaces) < limit:
            interfaces.append(alt if len(interfaces) % 2 == 0 else native)
    return interfaces[:limit]


def planned_attempt_count(native: Interface) -> int:
    return len(attempt_interfaces(native, config.MAX_BACKEND_ATTEMPTS + config.ALL_BUSY_RECOVERY_ATTEMPTS))


def all_attempts_busy(attempts: list[AttemptResult]) -> bool:
    return bool(attempts) and all(str(attempt.error_code) == "10310" for attempt in attempts)


async def delay_before_all_busy_recovery(request_id: str | None) -> None:
    delay = config.ALL_BUSY_RECOVERY_DELAY_S
    if delay > 0 and config.RETRY_JITTER_S > 0:
        delay += random.uniform(0, config.RETRY_JITTER_S)
    if delay > 0:
        console_log(f"all-busy recovery wait id={request_id or '-'} sleep={round(delay, 3)}s")
        await asyncio.sleep(delay)


async def delay_before_attempt(step_index: int, interface: Interface, previous: Interface) -> None:
    if step_index == 0:
        return
    base_delay = config.SAME_RETRY_DELAY_S if interface == previous else config.ALT_RETRY_DELAY_S
    delay = min(config.MAX_RETRY_DELAY_S, base_delay * (config.RETRY_BACKOFF_MULTIPLIER ** max(0, step_index - 1)))
    if delay > 0 and config.RETRY_JITTER_S > 0:
        delay += random.uniform(0, config.RETRY_JITTER_S)
    if delay > 0:
        await asyncio.sleep(delay)


def parse_error_body(status_code: int, text: str) -> tuple[dict[str, Any] | None, Any, str | None, str | None]:
    obj: dict[str, Any] | None = None
    if text.strip().startswith("{"):
        try:
            parsed = json.loads(text)
            obj = parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            obj = None
    err = obj.get("error") if isinstance(obj, dict) else None
    code = err.get("code") if isinstance(err, dict) else None
    error_type = err.get("type") if isinstance(err, dict) else None
    message = err.get("message") if isinstance(err, dict) else text[:280] or f"upstream HTTP {status_code}"
    return obj, code, error_type, message


async def first_nonempty_chunk(chunks: AsyncIterator[bytes]) -> bytes | None:
    async for chunk in chunks:
        if chunk:
            return chunk
    return None


async def stream_with_first(first: bytes, chunks: AsyncIterator[bytes], context_manager: Any) -> AsyncIterator[bytes]:
    try:
        yield first
        async for chunk in chunks:
            if chunk:
                yield chunk
    finally:
        await context_manager.__aexit__(None, None, None)


class MaasGateway:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    def path_for(self, interface: Interface) -> str:
        return "/v2/chat/completions" if interface == "openai" else "/anthropic/v1/messages"

    def headers_for(self, interface: Interface) -> dict[str, str]:
        if interface == "openai":
            return {"Authorization": f"Bearer {config.API_KEY}", "Content-Type": "application/json"}
        return {
            "Authorization": f"Bearer {config.API_KEY}",
            "x-api-key": config.API_KEY,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }

    def payload_for(self, native: Interface, interface: Interface, payload: dict[str, Any], stream: bool) -> dict[str, Any]:
        body = normalize_model({**payload, "stream": stream}) if interface == native else {**payload, "stream": stream}
        if interface != native:
            body = openai_to_anthropic(body) if native == "openai" else anthropic_to_openai(body)
        return body

    def stream_chunks(self, native: Interface, interface: Interface, response: httpx.Response) -> AsyncIterator[bytes]:
        if native == interface:
            return response.aiter_bytes()
        if native == "openai" and interface == "anthropic":
            return anthropic_stream_to_openai(response)
        return openai_stream_to_anthropic(response)

    async def call_openai(self, payload: dict[str, Any]) -> AttemptResult:
        return await self._post("openai", self.path_for("openai"), normalize_model(payload), self.headers_for("openai"))

    async def call_anthropic(self, payload: dict[str, Any]) -> AttemptResult:
        return await self._post("anthropic", self.path_for("anthropic"), normalize_model(payload), self.headers_for("anthropic"))

    async def prepare_stream_strategy(self, native: Interface, payload: dict[str, Any], request_id: str | None = None) -> PreparedStream | PreparedStreamFailure:
        attempts: list[AttemptResult] = []
        base_attempts = len(attempt_interfaces(native))
        interfaces = attempt_interfaces(native, config.MAX_BACKEND_ATTEMPTS + config.ALL_BUSY_RECOVERY_ATTEMPTS)
        previous = native
        final: AttemptResult | None = None
        for index, interface in enumerate(interfaces):
            if index == base_attempts:
                if not all_attempts_busy(attempts):
                    break
                await delay_before_all_busy_recovery(request_id)
            elif index < base_attempts:
                await delay_before_attempt(index, interface, previous)
            else:
                await delay_before_attempt(index - base_attempts, interface, previous)
            started = time.perf_counter()
            cm = self.client.stream(
                "POST",
                config.BASE_URL + self.path_for(interface),
                json=self.payload_for(native, interface, payload, stream=True),
                headers=self.headers_for(interface),
                timeout=config.TIMEOUT_S,
            )
            try:
                response = await cm.__aenter__()
                if response.status_code < 200 or response.status_code >= 300:
                    text = (await response.aread()).decode("utf-8", "replace")
                    await cm.__aexit__(None, None, None)
                    obj, code, error_type, message = parse_error_body(response.status_code, text)
                    result = AttemptResult(
                        interface=interface,
                        ok=False,
                        status_code=response.status_code,
                        elapsed_s=round(time.perf_counter() - started, 3),
                        response_json=obj,
                        error_code=code,
                        error_type=error_type,
                        error_message=message,
                    )
                    attempts.append(result)
                    final = result
                    console_log_attempt(request_id, index + 1, len(interfaces), result)
                    if retryable(result):
                        previous = interface
                        continue
                    return PreparedStreamFailure(final=result, attempts=attempts, total_attempts=len(interfaces))

                chunks = self.stream_chunks(native, interface, response)
                try:
                    first = await asyncio.wait_for(first_nonempty_chunk(chunks), timeout=config.STREAM_FIRST_CHUNK_TIMEOUT_S)
                except TimeoutError:
                    await cm.__aexit__(None, None, None)
                    result = AttemptResult(
                        interface=interface,
                        ok=False,
                        status_code=response.status_code,
                        elapsed_s=round(time.perf_counter() - started, 3),
                        error_type="FirstChunkTimeout",
                        error_message=f"upstream stream did not produce a first chunk within {config.STREAM_FIRST_CHUNK_TIMEOUT_S}s",
                    )
                    attempts.append(result)
                    final = result
                    console_log_attempt(request_id, index + 1, len(interfaces), result)
                    previous = interface
                    continue
                except Exception as exc:
                    await cm.__aexit__(type(exc), exc, exc.__traceback__)
                    result = AttemptResult(
                        interface=interface,
                        ok=False,
                        status_code=response.status_code,
                        elapsed_s=round(time.perf_counter() - started, 3),
                        error_type="StreamReadError",
                        error_message=str(exc)[:280],
                    )
                    attempts.append(result)
                    final = result
                    console_log_attempt(request_id, index + 1, len(interfaces), result)
                    previous = interface
                    continue

                if first is None:
                    await cm.__aexit__(None, None, None)
                    result = AttemptResult(
                        interface=interface,
                        ok=False,
                        status_code=response.status_code,
                        elapsed_s=round(time.perf_counter() - started, 3),
                        error_type="EmptyStream",
                        error_message="upstream stream ended before first chunk",
                    )
                    attempts.append(result)
                    final = result
                    console_log_attempt(request_id, index + 1, len(interfaces), result)
                    previous = interface
                    continue

                result = AttemptResult(
                    interface=interface,
                    ok=True,
                    status_code=response.status_code,
                    elapsed_s=round(time.perf_counter() - started, 3),
                )
                attempts.append(result)
                console_log_attempt(request_id, index + 1, len(interfaces), result)
                return PreparedStream(interface=interface, chunks=stream_with_first(first, chunks, cm), attempts=attempts, total_attempts=len(interfaces))
            except Exception as exc:
                try:
                    await cm.__aexit__(type(exc), exc, exc.__traceback__)
                except Exception:
                    pass
                result = AttemptResult(
                    interface=interface,
                    ok=False,
                    status_code=None,
                    elapsed_s=round(time.perf_counter() - started, 3),
                    error_type=type(exc).__name__,
                    error_message=str(exc)[:280],
                )
                attempts.append(result)
                final = result
                console_log_attempt(request_id, index + 1, len(interfaces), result)
                previous = interface
        fallback = final or AttemptResult(native, False, 503, 0, error_type="server_error", error_message="MAAS gateway exhausted backend attempts")
        return PreparedStreamFailure(final=fallback, attempts=attempts, total_attempts=len(interfaces))

    async def _post(self, interface: Interface, path: str, payload: dict[str, Any], headers: dict[str, str]) -> AttemptResult:
        started = time.perf_counter()
        try:
            response = await self.client.post(config.BASE_URL + path, json=payload, headers=headers, timeout=config.TIMEOUT_S)
            elapsed = time.perf_counter() - started
            obj: dict[str, Any] | None
            try:
                obj = response.json()
            except Exception:
                obj = None
            err = obj.get("error") if isinstance(obj, dict) else None
            return AttemptResult(
                interface=interface,
                ok=200 <= response.status_code < 300,
                status_code=response.status_code,
                elapsed_s=round(elapsed, 3),
                response_json=obj,
                error_code=err.get("code") if isinstance(err, dict) else None,
                error_type=err.get("type") if isinstance(err, dict) else None,
                error_message=err.get("message") if isinstance(err, dict) else None,
            )
        except Exception as exc:
            elapsed = time.perf_counter() - started
            return AttemptResult(
                interface=interface,
                ok=False,
                status_code=None,
                elapsed_s=round(elapsed, 3),
                error_type=type(exc).__name__,
                error_message=str(exc)[:280],
            )

    async def run_strategy(self, native: Interface, payload: dict[str, Any], request_id: str | None = None) -> tuple[AttemptResult, list[AttemptResult]]:
        attempts: list[AttemptResult] = []

        async def invoke(interface: Interface, body: dict[str, Any]) -> AttemptResult:
            return await (self.call_openai(body) if interface == "openai" else self.call_anthropic(body))

        base_attempts = len(attempt_interfaces(native))
        interfaces = attempt_interfaces(native, config.MAX_BACKEND_ATTEMPTS + config.ALL_BUSY_RECOVERY_ATTEMPTS)
        previous = native
        final: AttemptResult | None = None
        for index, interface in enumerate(interfaces):
            if index == base_attempts:
                if not all_attempts_busy(attempts):
                    break
                await delay_before_all_busy_recovery(request_id)
            elif index < base_attempts:
                await delay_before_attempt(index, interface, previous)
            else:
                await delay_before_attempt(index - base_attempts, interface, previous)
            body = payload if interface == native else openai_to_anthropic(payload) if native == "openai" else anthropic_to_openai(payload)
            result = await invoke(interface, body)
            attempts.append(result)
            console_log_attempt(request_id, index + 1, len(interfaces), result)
            final = result
            if result.ok or not retryable(result):
                return result, attempts
            previous = interface
        return final or attempts[-1], attempts
