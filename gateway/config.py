"""Runtime configuration for the MAAS gateway."""

from __future__ import annotations

import os
from pathlib import Path


BASE_URL = os.environ.get("MAAS_BASE_URL", "https://maas-coding-api.cn-huabei-1.xf-yun.com").rstrip("/")
MODEL = os.environ.get("MAAS_MODEL", "astron-code-latest")
MODEL_CONTEXT_WINDOW = int(os.environ.get("MAAS_CONTEXT_WINDOW", "500000"))
MODEL_MAX_TOKENS = int(os.environ.get("MAAS_MAX_TOKENS", "131072"))
MODEL_FALLBACKS = [model.strip() for model in os.environ.get("MAAS_MODEL_FALLBACKS", "").split(",") if model.strip()]
MODEL_FALLBACK_ATTEMPTS = max(0, int(os.environ.get("MAAS_MODEL_FALLBACK_ATTEMPTS", "0")))
MODEL_FALLBACK_CONTEXT_WINDOW = max(1, int(os.environ.get("MAAS_MODEL_FALLBACK_CONTEXT_WINDOW", "128000")))
MODEL_FALLBACK_MAX_TOKENS = max(1, int(os.environ.get("MAAS_MODEL_FALLBACK_MAX_TOKENS", "32768")))
MODEL_FALLBACK_CONTEXT_SAFETY_TOKENS = max(0, int(os.environ.get("MAAS_MODEL_FALLBACK_CONTEXT_SAFETY_TOKENS", "4096")))
MODEL_FALLBACK_STRIP_THINKING = os.environ.get("MAAS_MODEL_FALLBACK_STRIP_THINKING", "1") == "1"
LEDGER = Path(os.environ.get("MAAS_GATEWAY_LOG", "logs/gateway_requests.jsonl"))
API_KEY = os.environ.get("MAAS_API_KEY", "")
CLIENT_API_KEY = os.environ.get("MAAS_GATEWAY_API_KEY")
PROXY_URL = os.environ.get("MAAS_PROXY_URL")
SAME_RETRY_DELAY_S = float(os.environ.get("MAAS_SAME_RETRY_DELAY_S", "0.8"))
ALT_RETRY_DELAY_S = float(os.environ.get("MAAS_ALT_RETRY_DELAY_S", "1.2"))
RETRY_BACKOFF_MULTIPLIER = float(os.environ.get("MAAS_RETRY_BACKOFF_MULTIPLIER", "1.5"))
MAX_RETRY_DELAY_S = float(os.environ.get("MAAS_MAX_RETRY_DELAY_S", "3.0"))
RETRY_JITTER_S = float(os.environ.get("MAAS_RETRY_JITTER_S", "0.25"))
BUSY_COOLDOWN_S = float(os.environ.get("MAAS_BUSY_COOLDOWN_S", "1.0"))
ALL_BUSY_RETRY_AFTER_S = max(1, int(float(os.environ.get("MAAS_ALL_BUSY_RETRY_AFTER_S", "3"))))
ALL_BUSY_RECOVERY_ATTEMPTS = max(0, int(os.environ.get("MAAS_ALL_BUSY_RECOVERY_ATTEMPTS", "2")))
ALL_BUSY_RECOVERY_DELAY_S = max(0.0, float(os.environ.get("MAAS_ALL_BUSY_RECOVERY_DELAY_S", "3.0")))
TIMEOUT_S = float(os.environ.get("MAAS_TIMEOUT_S", "45"))
STREAM_FIRST_CHUNK_TIMEOUT_S = float(os.environ.get("MAAS_STREAM_FIRST_CHUNK_TIMEOUT_S", "20"))
CROSS_INTERFACE_FALLBACK = os.environ.get("MAAS_ENABLE_CROSS_INTERFACE_FALLBACK", "1") == "1"
MAX_BACKEND_ATTEMPTS = max(1, int(os.environ.get("MAAS_MAX_BACKEND_ATTEMPTS", "5")))
MAX_INFLIGHT_REQUESTS = max(1, int(os.environ.get("MAAS_MAX_INFLIGHT_REQUESTS", "1")))
