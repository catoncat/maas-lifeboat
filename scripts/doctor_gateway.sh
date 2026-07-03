#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="$ROOT_DIR"
ENV_FILE="$APP_DIR/.env.local"
PI_MODELS="${PI_MODELS:-$HOME/.pi/agent/models.json}"

python3 - <<PY
import hashlib
import json
import pathlib
import re
import sys

env_file = pathlib.Path("$ENV_FILE")
pi_models = pathlib.Path("$PI_MODELS")

def sha16(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:16]

def read_env(path: pathlib.Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'").strip('"')
    return values

def resolve_pi_key(value: str) -> str:
    if value.startswith("!cat "):
        p = pathlib.Path(value[5:].strip())
        return p.read_text().strip() if p.exists() else ""
    return value

errors: list[str] = []
warnings: list[str] = []
env = read_env(env_file)

provider_key = env.get("MAAS_API_KEY", "")
gateway_key = env.get("MAAS_GATEWAY_API_KEY", "")

print(f"env_file: {env_file} exists={env_file.exists()}")
print(f"MAAS_API_KEY: present={bool(provider_key)} len={len(provider_key)} sha16={sha16(provider_key) if provider_key else ''} has_colon={':' in provider_key}")
print(f"MAAS_GATEWAY_API_KEY: present={bool(gateway_key)} len={len(gateway_key)} sha16={sha16(gateway_key) if gateway_key else ''}")

if not provider_key:
    errors.append("MAAS_API_KEY is missing")
elif len(provider_key) < 40 or ":" not in provider_key:
    errors.append("MAAS_API_KEY does not look like the XFyun provider id:secret key")

if not gateway_key:
    warnings.append("MAAS_GATEWAY_API_KEY is empty; gateway will accept any client bearer token")

if provider_key and gateway_key and provider_key == gateway_key:
    errors.append("MAAS_API_KEY and MAAS_GATEWAY_API_KEY are identical; upstream provider key and local client key must be different")

if pi_models.exists():
    data = json.loads(pi_models.read_text())
    xunfei = data.get("providers", {}).get("xunfei")
    print(f"pi_models: {pi_models} xunfei_present={bool(xunfei)}")
    if xunfei:
        print(f"xunfei.baseUrl={xunfei.get('baseUrl')}")
        print(f"xunfei.api={xunfei.get('api')}")
        pi_key = resolve_pi_key(str(xunfei.get("apiKey", "")))
        print(f"xunfei.apiKey: present={bool(pi_key)} len={len(pi_key)} sha16={sha16(pi_key) if pi_key else ''}")
        if xunfei.get("baseUrl") != "http://127.0.0.1:18788/v1":
            warnings.append("PI xunfei.baseUrl is not the local gateway URL")
        if xunfei.get("api") != "openai-completions":
            warnings.append("PI xunfei.api is not openai-completions")
        if gateway_key and pi_key and gateway_key != pi_key:
            errors.append("PI xunfei.apiKey does not match MAAS_GATEWAY_API_KEY")
else:
    warnings.append(f"PI models file not found: {pi_models}")

if errors:
    print("ERRORS:")
    for item in errors:
        print(f"- {item}")
if warnings:
    print("WARNINGS:")
    for item in warnings:
        print(f"- {item}")

sys.exit(1 if errors else 0)
PY

echo
echo "Port check:"
lsof -nP -iTCP:18788 -sTCP:LISTEN 2>/dev/null || echo "No process is listening on 127.0.0.1:18788"
