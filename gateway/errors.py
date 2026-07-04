"""Error normalization for client-facing gateway responses."""

from __future__ import annotations

from fastapi import HTTPException
from fastapi.responses import JSONResponse

from . import config
from .types import AttemptResult


def require_provider_key() -> None:
    if not config.API_KEY:
        raise HTTPException(status_code=500, detail="MAAS_API_KEY is not configured")


def check_client_auth(authorization: str | None, x_api_key: str | None = None) -> None:
    if not config.CLIENT_API_KEY:
        return
    expected = f"Bearer {config.CLIENT_API_KEY}"
    if authorization != expected and x_api_key != config.CLIENT_API_KEY:
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


def all_busy(attempts: list[AttemptResult]) -> bool:
    return bool(attempts) and all(not attempt.ok and str(attempt.error_code) == "10310" for attempt in attempts)


def retry_after_seconds(status: int, attempts: list[AttemptResult]) -> int | None:
    if status not in {429, 503, 529}:
        return None
    return config.ALL_BUSY_RETRY_AFTER_S if all_busy(attempts) else 1


def retry_after_header(status: int, attempts: list[AttemptResult]) -> dict[str, str] | None:
    retry_after = retry_after_seconds(status, attempts)
    return {"Retry-After": str(retry_after)} if retry_after is not None else None


def openai_error_status(final: AttemptResult | None) -> int:
    status = final.status_code if final and final.status_code and final.status_code >= 400 else 503
    if final and retryable(final):
        return 503
    return status


def openai_error_response(final: AttemptResult | None, attempts: list[AttemptResult]) -> JSONResponse:
    status = openai_error_status(final)
    return JSONResponse(
        status_code=status,
        headers=retry_after_header(status, attempts),
        content={
            "error": {
                "message": gateway_error_message(final, attempts),
                "type": "server_error" if status >= 500 else "invalid_request_error",
                "code": "service_unavailable" if status >= 500 else final.error_code if final else "gateway_error",
            }
        },
    )


def anthropic_error_status(final: AttemptResult | None) -> int:
    return 529 if final and retryable(final) else final.status_code if final and final.status_code and final.status_code >= 400 else 529


def anthropic_error_response(final: AttemptResult | None, attempts: list[AttemptResult]) -> JSONResponse:
    status = anthropic_error_status(final)
    return JSONResponse(
        status_code=status,
        headers=retry_after_header(status, attempts),
        content={
            "type": "error",
            "error": {
                "type": "overloaded_error" if status in {429, 503, 529} else "api_error",
                "message": gateway_error_message(final, attempts),
            },
        },
    )
