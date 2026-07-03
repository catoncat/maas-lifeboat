"""Shared gateway data types."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, AsyncIterator, Literal


Interface = Literal["openai", "anthropic"]


@dataclass
class AttemptResult:
    interface: Interface
    ok: bool
    status_code: int | None
    elapsed_s: float
    response_json: dict[str, Any] | None = None
    error_type: str | None = None
    error_code: Any = None
    error_message: str | None = None


@dataclass
class PreparedStream:
    interface: Interface
    chunks: AsyncIterator[bytes]
    attempts: list[AttemptResult]
    total_attempts: int


@dataclass
class PreparedStreamFailure:
    final: AttemptResult
    attempts: list[AttemptResult]
    total_attempts: int
