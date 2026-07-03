#!/usr/bin/env python3
"""Compatibility entrypoint for the MAAS resilient gateway.

The implementation lives in focused modules under :mod:`gateway`. This module
keeps existing commands such as ``uvicorn gateway.maas_gateway:app`` working.
"""

from __future__ import annotations

import os

from .app import app, make_app
from .config import (
    ALT_RETRY_DELAY_S,
    API_KEY,
    BASE_URL,
    CLIENT_API_KEY,
    CROSS_INTERFACE_FALLBACK,
    LEDGER,
    MAX_BACKEND_ATTEMPTS,
    MAX_RETRY_DELAY_S,
    MODEL,
    MODEL_CONTEXT_WINDOW,
    MODEL_MAX_TOKENS,
    PROXY_URL,
    RETRY_BACKOFF_MULTIPLIER,
    RETRY_JITTER_S,
    SAME_RETRY_DELAY_S,
    STREAM_FIRST_CHUNK_TIMEOUT_S,
    TIMEOUT_S,
)
from .errors import anthropic_error_response, check_client_auth, gateway_error_message, openai_error_response, require_provider_key, retryable
from .logging import append_jsonl, attempt_to_log, console_log, console_log_attempt, sha16, utc_now
from .protocols import (
    anthropic_content_to_openai,
    anthropic_response_to_openai,
    anthropic_to_openai,
    anthropic_tool_choice_to_openai,
    anthropic_tools_to_openai,
    normalize_anthropic_tool_call_id,
    normalize_model,
    openai_content_to_anthropic,
    openai_response_to_anthropic,
    openai_to_anthropic,
    openai_tool_choice_to_anthropic,
    openai_tools_to_anthropic,
    parse_tool_arguments,
    stringify_tool_result,
)
from .sse import anthropic_stream_to_openai, iter_sse_json, openai_chunk, openai_stream_to_anthropic, sse
from .strategy import MaasGateway, alternate_interface, attempt_interfaces, delay_before_attempt
from .types import AttemptResult, Interface, PreparedStream, PreparedStreamFailure


__all__ = [
    "ALT_RETRY_DELAY_S",
    "API_KEY",
    "BASE_URL",
    "CLIENT_API_KEY",
    "CROSS_INTERFACE_FALLBACK",
    "LEDGER",
    "MAX_BACKEND_ATTEMPTS",
    "MAX_RETRY_DELAY_S",
    "MODEL",
    "MODEL_CONTEXT_WINDOW",
    "MODEL_MAX_TOKENS",
    "PROXY_URL",
    "RETRY_BACKOFF_MULTIPLIER",
    "RETRY_JITTER_S",
    "SAME_RETRY_DELAY_S",
    "STREAM_FIRST_CHUNK_TIMEOUT_S",
    "TIMEOUT_S",
    "AttemptResult",
    "Interface",
    "MaasGateway",
    "PreparedStream",
    "PreparedStreamFailure",
    "alternate_interface",
    "anthropic_content_to_openai",
    "anthropic_error_response",
    "anthropic_response_to_openai",
    "anthropic_stream_to_openai",
    "anthropic_to_openai",
    "anthropic_tool_choice_to_openai",
    "anthropic_tools_to_openai",
    "app",
    "append_jsonl",
    "attempt_interfaces",
    "attempt_to_log",
    "check_client_auth",
    "console_log",
    "console_log_attempt",
    "delay_before_attempt",
    "gateway_error_message",
    "iter_sse_json",
    "make_app",
    "normalize_anthropic_tool_call_id",
    "normalize_model",
    "openai_chunk",
    "openai_content_to_anthropic",
    "openai_error_response",
    "openai_response_to_anthropic",
    "openai_stream_to_anthropic",
    "openai_to_anthropic",
    "openai_tool_choice_to_anthropic",
    "openai_tools_to_anthropic",
    "parse_tool_arguments",
    "require_provider_key",
    "retryable",
    "sse",
    "sha16",
    "stringify_tool_result",
    "utc_now",
]


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("gateway.maas_gateway:app", host="127.0.0.1", port=int(os.environ.get("PORT", "8788")), reload=False)
