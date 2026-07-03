#!/usr/bin/env python3
"""Gentle MAAS provider probe with per-request JSONL ledger.

The script intentionally logs hashes and metadata only. It never writes prompts,
API keys, or proxy URLs to the ledger.
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
from typing import Any, Literal

import httpx


Interface = Literal["openai", "anthropic"]

DEFAULT_BASE_URL = "https://maas-coding-api.cn-huabei-1.xf-yun.com"
DEFAULT_MODEL = "astron-code-latest"
DEFAULT_LEDGER = "logs/probe_maas.jsonl"
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


def openai_payload(model: str, prompt: str, max_tokens: int, stream: bool) -> dict[str, Any]:
    return {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0,
        "stream": stream,
    }


def anthropic_payload(model: str, prompt: str, max_tokens: int, stream: bool) -> dict[str, Any]:
    return {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0,
        "stream": stream,
    }


def payload_for(interface: Interface, model: str, prompt: str, max_tokens: int, stream: bool) -> dict[str, Any]:
    if interface == "openai":
        return openai_payload(model, prompt, max_tokens, stream)
    return anthropic_payload(model, prompt, max_tokens, stream)


def path_for(interface: Interface) -> str:
    return "/v2/chat/completions" if interface == "openai" else "/anthropic/v1/messages"


def headers_for(interface: Interface, api_key: str) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    if interface == "anthropic":
        headers["x-api-key"] = api_key
        headers["anthropic-version"] = "2023-06-01"
    return headers


@dataclass(frozen=True)
class Job:
    sequence_no: int
    interface: Interface
    pair_id: str | None


def build_jobs(interfaces: list[Interface], repeat: int, pattern: str) -> list[Job]:
    jobs: list[Job] = []
    seq = 0
    if pattern == "paired":
        if set(interfaces) != {"openai", "anthropic"}:
            raise SystemExit("--pattern paired requires --interfaces both")
        for index in range(repeat):
            pair_id = f"pair-{index + 1:04d}"
            for interface in ("openai", "anthropic"):
                seq += 1
                jobs.append(Job(seq, interface, pair_id))
        return jobs

    for index in range(repeat):
        for interface in interfaces:
            seq += 1
            jobs.append(Job(seq, interface, None))
    return jobs


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
    route_label: str,
    concurrency: int,
    job: Job,
    ledger: Path,
) -> None:
    request_id = str(uuid.uuid4())
    payload = payload_for(job.interface, model, prompt, max_tokens, stream)
    payload_hash = sha16(payload)
    started = time.perf_counter()
    status: int | None = None
    bytes_read = 0
    first_chunk_arrived = False
    first_chunk_latency_s: float | None = None
    finish_class = "unknown"
    error_class: str | None = None
    error_code: Any = None
    error_type: str | None = None
    error_message: str | None = None
    response_json: dict[str, Any] | None = None

    try:
        if stream:
            async with client.stream(
                "POST",
                base_url + path_for(job.interface),
                json=payload,
                headers=headers_for(job.interface, api_key),
            ) as response:
                status = response.status_code
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
            response = await client.post(
                base_url + path_for(job.interface),
                json=payload,
                headers=headers_for(job.interface, api_key),
            )
            status = response.status_code
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
        "attempt_no": 1,
        "sequence_no": job.sequence_no,
        "pair_id": job.pair_id,
        "interface": job.interface,
        "route_label": route_label,
        "stream": stream,
        "concurrency": concurrency,
        "status": status,
        "ok": ok,
        "provider_error_code": error_code,
        "provider_error_type": error_type,
        "provider_error_message": error_message,
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
        f"{row['timestamp']} run={run_id} seq={job.sequence_no} interface={job.interface} "
        f"route={route_label} stream={str(stream).lower()} ok={str(ok).lower()} "
        f"status={status} code={error_code} latency={row['latency_s']}s finish={finish_class}",
        flush=True,
    )


async def run(args: argparse.Namespace) -> None:
    env_file = Path(args.env_file)
    load_dotenv(env_file)
    api_key = os.environ.get(args.api_key_env, "")
    if not api_key:
        raise SystemExit(f"{args.api_key_env} is required; set it in {env_file} or the environment")

    base_url = args.base_url or os.environ.get("MAAS_BASE_URL", DEFAULT_BASE_URL)
    proxy_url = None if args.route_label == "direct" else os.environ.get(args.proxy_env, "")
    if args.proxy_url:
        proxy_url = args.proxy_url
    if args.route_label != "direct" and not proxy_url:
        raise SystemExit(f"route '{args.route_label}' requires --proxy-url or {args.proxy_env}")
    if args.concurrency > 2 and not args.allow_concurrency_above_2:
        raise SystemExit("Refusing concurrency > 2 without --allow-concurrency-above-2")

    interfaces: list[Interface]
    if args.interfaces == "both":
        interfaces = ["openai", "anthropic"]
    else:
        interfaces = [args.interfaces]
    jobs = build_jobs(interfaces, args.repeat, args.pattern)
    run_id = args.run_id or f"probe-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}"
    ledger = Path(args.ledger)

    timeout = httpx.Timeout(args.timeout, connect=min(args.timeout, 15.0))
    limits = httpx.Limits(max_connections=max(args.concurrency, 1), max_keepalive_connections=max(args.concurrency, 1))
    async with httpx.AsyncClient(proxy=proxy_url, trust_env=False, timeout=timeout, limits=limits) as client:
        semaphore = asyncio.Semaphore(args.concurrency)
        tasks: list[asyncio.Task[None]] = []

        async def launch(job: Job) -> None:
            async with semaphore:
                await probe_once(
                    client,
                    base_url=base_url.rstrip("/"),
                    api_key=api_key,
                    model=args.model,
                    prompt=args.prompt,
                    max_tokens=args.max_tokens,
                    stream=args.stream,
                    first_chunk_timeout=args.first_chunk_timeout,
                    run_id=run_id,
                    route_label=args.route_label,
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
    p.add_argument("--api-key-env", default="MAAS_API_KEY")
    p.add_argument("--base-url")
    p.add_argument("--model", default=os.environ.get("MAAS_MODEL", DEFAULT_MODEL))
    p.add_argument("--prompt", default=DEFAULT_PROMPT)
    p.add_argument("--max-tokens", type=int, default=8)
    p.add_argument("--interfaces", choices=["openai", "anthropic", "both"], default="openai")
    p.add_argument("--pattern", choices=["serial", "paired"], default="serial")
    p.add_argument("--repeat", type=int, default=10)
    p.add_argument("--stream", action="store_true")
    p.add_argument("--timeout", type=float, default=45.0)
    p.add_argument("--first-chunk-timeout", type=float, default=20.0)
    p.add_argument("--rate-interval", type=float, default=1.0)
    p.add_argument("--concurrency", type=int, default=1)
    p.add_argument("--allow-concurrency-above-2", action="store_true")
    p.add_argument("--route-label", default="direct")
    p.add_argument("--proxy-env", default="MAAS_PROXY_URL")
    p.add_argument("--proxy-url")
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
