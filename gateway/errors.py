"""Error normalization for client-facing gateway responses."""

from __future__ import annotations

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from . import config
from .types import AttemptResult


def require_provider_key() -> None:
    if not config.API_KEY:
        raise HTTPException(status_code=500, detail="MAAS_API_KEY is not configured")


def check_client_auth(authorization: str | None) -> None:
    if not config.CLIENT_API_KEY:
        return
    expected = f"Bearer {config.CLIENT_API_KEY}"
    if authorization != expected:
        raise HTTPException(status_code=401, detail="invalid gateway API key")


def retryable(result: AttemptResult) -> bool:
    if result.error_type in {"EmptyStream", "FirstChunkTimeout", "StreamReadError"}:
        return True
    if result.status_code is None:
        return True
    if result.error_code == 10310:
        return True
    return result.status_code in {408, 409, 425, 429} or result.status_code >= 500


def gateway_error_message(final: AttemptResult | None, attempts: list[AttemptResult]) -> str:
    attempt_count = len(attempts)
    if not final:
        return "503 service_unavailable: MAAS gateway exhausted backend attempts"
    status = final.status_code or 502
    code = final.error_code or final.error_type or "upstream_error"
    message = final.error_message or "upstream provider unavailable"
    return f"{status} service_unavailable: MAAS gateway exhausted {attempt_count} backend attempts; last interface={final.interface}; code={code}; message={message}"


def openai_error_response(final: AttemptResult | None, attempts: list[AttemptResult]) -> JSONResponse:
    status = final.status_code if final and final.status_code and final.status_code >= 400 else 503
    if final and retryable(final):
        status = 503
    return JSONResponse(
        status_code=status,
        headers={"Retry-After": "1"} if status in {429, 503} else None,
        content={
            "error": {
                "message": gateway_error_message(final, attempts),
                "type": "server_error" if status >= 500 else "invalid_request_error",
                "code": "service_unavailable" if status >= 500 else final.error_code if final else "gateway_error",
            }
        },
    )


def anthropic_error_response(final: AttemptResult | None, attempts: list[AttemptResult]) -> JSONResponse:
    status = 529 if final and retryable(final) else final.status_code if final and final.status_code and final.status_code >= 400 else 529
    return JSONResponse(
        status_code=status,
        headers={"Retry-After": "1"} if status in {429, 503, 529} else None,
        content={
            "type": "error",
            "error": {
                "type": "overloaded_error" if status in {429, 503, 529} else "api_error",
                "message": gateway_error_message(final, attempts),
            },
        },
    )
