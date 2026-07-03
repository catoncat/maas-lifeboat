#!/usr/bin/env python3
"""Gentle local gateway dogfood probe with client-side JSONL ledger.

This script calls the local OpenAI-compatible gateway, not the upstream MAAS
provider directly. It records request metadata only: no prompts, API keys, or
full response text.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx


DEFAULT_BASE_URL = "http://127.0.0.1:18788/v1"
DEFAULT_LEDGER = "logs/gateway_dogfood.jsonl"
DEFAULT_MODEL = "astron-code-latest"
DEFAULT_PROMPT = "Reply with exactly: OK"


def load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key and key not in os.environ:
            os.environ[key] = value


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha16(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def payload_for(model: str, prompt: str, max_tokens: int, stream: bool) -> dict[str, Any]:
    return {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0,
        "stream": stream,
    }


def parse_error(status: int | None, body: bytes) -> tuple[Any, str | None, str | None, str | None]:
    if not body:
        return None, None, None, None
    text = body.decode("utf-8", "replace")
    obj: dict[str, Any] | None = None
    if text.lstrip().startswith("{"):
        try:
            parsed = json.loads(text)
            obj = parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            obj = None
    err = obj.get("error") if isinstance(obj, dict) else None
    if isinstance(err, dict):
        return obj, err.get("code"), err.get("type"), err.get("message")
    if status and status >= 400:
        return obj, None, None, text[:280]
    return obj, None, None, None


@dataclass(frozen=True)
class Job:
    sequence_no: int


async def probe_once(
    client: httpx.AsyncClient,
    *,
    base_url: str,
    api_key: str,
    model: str,
    prompt: str,
    max_tokens: int,
    stream: bool,
    first_chunk_timeout: float,
    run_id: str,
    concurrency: int,
    job: Job,
    ledger: Path,
) -> None:
    request_id = str(uuid.uuid4())
    payload = payload_for(model, prompt, max_tokens, stream)
    payload_hash = sha16(payload)
    started = time.perf_counter()
    status: int | None = None
    retry_after: str | None = None
    bytes_read = 0
    first_chunk_arrived = False
    first_chunk_latency_s: float | None = None
    finish_class = "unknown"
    error_class: str | None = None
    error_code: Any = None
    error_type: str | None = None
    error_message: str | None = None
    response_json: dict[str, Any] | None = None

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        if stream:
            async with client.stream("POST", base_url.rstrip("/") + "/chat/completions", json=payload, headers=headers) as response:
                status = response.status_code
                retry_after = response.headers.get("Retry-After")
                if status < 200 or status >= 300:
                    body = await response.aread()
                    bytes_read = len(body)
                    response_json, error_code, error_type, error_message = parse_error(status, body)
                    finish_class = "http_error"
                else:
                    iterator = response.aiter_bytes()
                    try:
                        first = await asyncio.wait_for(iterator.__anext__(), timeout=first_chunk_timeout)
                        first_chunk_arrived = bool(first)
                        first_chunk_latency_s = round(time.perf_counter() - started, 3)
                        bytes_read += len(first)
                        async for chunk in iterator:
                            bytes_read += len(chunk)
                        finish_class = "ok"
                    except StopAsyncIteration:
                        finish_class = "empty_stream"
                    except TimeoutError:
                        finish_class = "first_chunk_timeout"
                        error_class = "FirstChunkTimeout"
        else:
            response = await client.post(base_url.rstrip("/") + "/chat/completions", json=payload, headers=headers)
            status = response.status_code
            retry_after = response.headers.get("Retry-After")
            body = response.content
            bytes_read = len(body)
            if 200 <= status < 300:
                finish_class = "ok"
                try:
                    parsed = response.json()
                    response_json = parsed if isinstance(parsed, dict) else None
                except Exception:
                    response_json = None
            else:
                response_json, error_code, error_type, error_message = parse_error(status, body)
                finish_class = "http_error"
    except Exception as exc:
        finish_class = "transport_error"
        error_class = type(exc).__name__
        error_message = str(exc)[:280]

    ok = finish_class == "ok" and status is not None and 200 <= status < 300
    row = {
        "timestamp": utc_now(),
        "run_id": run_id,
        "request_id": request_id,
        "sequence_no": job.sequence_no,
        "surface": "openai",
        "stream": stream,
        "concurrency": concurrency,
        "status": status,
        "ok": ok,
        "retry_after": retry_after,
        "error_code": error_code,
        "error_type": error_type,
        "error_message": error_message,
        "latency_s": round(time.perf_counter() - started, 3),
        "bytes": bytes_read,
        "first_chunk_arrived": first_chunk_arrived,
        "first_chunk_latency_s": first_chunk_latency_s,
        "finish_class": finish_class,
        "error_class": error_class,
        "payload_hash": payload_hash,
        "response_id": response_json.get("id") if isinstance(response_json, dict) else None,
    }
    append_jsonl(ledger, row)
    print(
        f"{row['timestamp']} run={run_id} seq={job.sequence_no} stream={str(stream).lower()} "
        f"ok={str(ok).lower()} status={status} retry_after={retry_after} "
        f"latency={row['latency_s']}s first_chunk={first_chunk_latency_s} finish={finish_class}",
        flush=True,
    )


async def run(args: argparse.Namespace) -> None:
    load_dotenv(Path(args.env_file))
    api_key = os.environ.get(args.api_key_env, "")
    if not api_key:
        raise SystemExit(f"{args.api_key_env} is required; set it in {args.env_file} or the environment")
    if args.concurrency > 2 and not args.allow_concurrency_above_2:
        raise SystemExit("Refusing concurrency > 2 without --allow-concurrency-above-2")

    run_id = args.run_id or f"gateway-dogfood-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}"
    ledger = Path(args.ledger)
    jobs = [Job(sequence_no=index + 1) for index in range(args.repeat)]

    timeout = httpx.Timeout(args.timeout, connect=min(args.timeout, 15.0))
    limits = httpx.Limits(max_connections=max(args.concurrency, 1), max_keepalive_connections=max(args.concurrency, 1))
    async with httpx.AsyncClient(trust_env=False, timeout=timeout, limits=limits) as client:
        semaphore = asyncio.Semaphore(args.concurrency)
        tasks: list[asyncio.Task[None]] = []

        async def launch(job: Job) -> None:
            async with semaphore:
                await probe_once(
                    client,
                    base_url=args.base_url,
                    api_key=api_key,
                    model=args.model,
                    prompt=args.prompt,
                    max_tokens=args.max_tokens,
                    stream=args.stream,
                    first_chunk_timeout=args.first_chunk_timeout,
                    run_id=run_id,
                    concurrency=args.concurrency,
                    job=job,
                    ledger=ledger,
                )

        for job in jobs:
            tasks.append(asyncio.create_task(launch(job)))
            if args.rate_interval > 0:
                await asyncio.sleep(args.rate_interval)
        await asyncio.gather(*tasks)

    print(f"ledger={ledger} run_id={run_id} requests={len(jobs)}", flush=True)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--env-file", default=".env.local")
    p.add_argument("--api-key-env", default="MAAS_GATEWAY_API_KEY")
    p.add_argument("--base-url", default=os.environ.get("OPENAI_BASE_URL", DEFAULT_BASE_URL))
    p.add_argument("--model", default=os.environ.get("MAAS_MODEL", DEFAULT_MODEL))
    p.add_argument("--prompt", default=DEFAULT_PROMPT)
    p.add_argument("--max-tokens", type=int, default=8)
    p.add_argument("--repeat", type=int, default=4)
    p.add_argument("--stream", action="store_true")
    p.add_argument("--timeout", type=float, default=90.0)
    p.add_argument("--first-chunk-timeout", type=float, default=30.0)
    p.add_argument("--rate-interval", type=float, default=0.0)
    p.add_argument("--concurrency", type=int, default=2)
    p.add_argument("--allow-concurrency-above-2", action="store_true")
    p.add_argument("--run-id")
    p.add_argument("--ledger", default=DEFAULT_LEDGER)
    return p


def main() -> int:
    try:
        asyncio.run(run(parser().parse_args()))
        return 0
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
