"""Request ledger and console logging helpers."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .types import AttemptResult


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha16(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def console_log(message: str) -> None:
    print(f"[maas-gateway] {message}", flush=True)


def console_log_attempt(request_id: str | None, attempt_no: int, total: int, result: AttemptResult) -> None:
    msg = (result.error_message or "").replace("\n", " ")[:160]
    console_log(
        "attempt "
        f"id={request_id or '-'} "
        f"n={attempt_no}/{total} "
        f"interface={result.interface} "
        f"ok={str(result.ok).lower()} "
        f"status={result.status_code} "
        f"code={result.error_code} "
        f"elapsed={result.elapsed_s}s"
        f"{' message=' + msg if msg else ''}"
    )


def attempt_to_log(result: AttemptResult) -> dict[str, Any]:
    return {
        "interface": result.interface,
        "ok": result.ok,
        "status_code": result.status_code,
        "elapsed_s": result.elapsed_s,
        "error_code": result.error_code,
        "error_type": result.error_type,
        "error_message": result.error_message,
        "response_id": result.response_json.get("id") if isinstance(result.response_json, dict) else None,
    }
