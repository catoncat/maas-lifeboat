# MAAS Lifeboat

Local OpenAI/Anthropic-compatible lifeboat gateway for flaky MAAS coding providers.

The gateway is meant for tools such as PI agents, Cursor, OpenWebUI, LangChain, and other OpenAI-compatible clients that need a local endpoint while an upstream provider intermittently returns `503` / `10310 system busy`.

It does not promise magic availability. If failures are caused by account-level quota, provider-wide saturation, or invalid credentials, the gateway can only surface cleaner retryable errors.

## Features

- OpenAI-compatible `POST /v1/chat/completions`
- Anthropic-compatible `POST /anthropic/v1/messages`
- Local `/v1/models`
- Serial retries with cross-interface fallback; no aggressive concurrent hedging
- Streaming preserved across fallback paths before the first client chunk is committed
- First-chunk timeout guard for streams that hang after HTTP `200`
- Tool-call conversion between OpenAI `tool_calls` and Anthropic `tool_use`
- Client-facing retryable errors (`503` for OpenAI, `529` for Anthropic) when all backend attempts fail
- JSONL request ledger plus readable console attempt logs
- Request-level proxy support; system proxy settings are never modified

## Setup

```bash
python3 -m pip install -e ".[test]"
cp .env.example .env.local
chmod 600 .env.local
$EDITOR .env.local
```

Important variables:

```bash
MAAS_API_KEY='<upstream provider key>'
MAAS_GATEWAY_API_KEY='local-client-key'
MAAS_PROXY_URL=''
MAAS_MAX_BACKEND_ATTEMPTS=7
MAAS_ENABLE_CROSS_INTERFACE_FALLBACK=1
MAAS_STREAM_FIRST_CHUNK_TIMEOUT_S=20
```

`MAAS_API_KEY` is the upstream provider key. `MAAS_GATEWAY_API_KEY` is the local key your client sends to this gateway. Do not use the same value for both.

If you need a SOCKS proxy, install the optional dependency:

```bash
python3 -m pip install -e ".[socks]"
```

## Run

```bash
scripts/start_gateway.sh
```

OpenAI-compatible clients:

```text
OPENAI_BASE_URL=http://127.0.0.1:18788/v1
OPENAI_API_KEY=<MAAS_GATEWAY_API_KEY>
```

PI agents provider example:

```json
{
  "baseUrl": "http://127.0.0.1:18788/v1",
  "api": "openai-completions",
  "apiKey": "!cat /path/to/local-client-key",
  "models": [
    {
      "id": "astron-code-latest",
      "name": "Astron Code Latest",
      "contextWindow": 500000,
      "maxTokens": 131072
    }
  ]
}
```

The default model metadata matches the observed GLM 5.2/Astron Code setup: 500k context window and 131072 max output tokens. Override with `MAAS_CONTEXT_WINDOW` and `MAAS_MAX_TOKENS` if your provider changes those limits.

## macOS user service

This creates a user LaunchAgent only. It listens on `127.0.0.1` and does not change system proxy settings.

```bash
scripts/install_launchagent.sh
```

Uninstall:

```bash
scripts/uninstall_launchagent.sh
```

## Diagnostics

```bash
scripts/doctor_gateway.sh
tail -f logs/gateway_requests.jsonl
```

The server console prints each attempt:

```text
[maas-gateway] request start id=... surface=openai stream=true ...
[maas-gateway] attempt id=... n=1/7 interface=openai ok=false status=503 code=10310 ...
[maas-gateway] attempt id=... n=3/7 interface=anthropic ok=true status=200 ...
[maas-gateway] request end id=... ok=true attempts=3 ...
```

The ledger records payload hashes and attempt metadata, not request bodies or API keys.

## Project layout

```text
gateway/app.py          FastAPI routes
gateway/strategy.py     retry/fallback strategy and backend calls
gateway/protocols.py    OpenAI/Anthropic payload and response conversion
gateway/sse.py          SSE parsing and streaming conversion
gateway/errors.py       retry classification and client error envelopes
gateway/config.py       environment-backed settings
gateway/logging.py      console logs and JSONL ledger helpers
```

`gateway/maas_gateway.py` is only a thin entrypoint for `uvicorn gateway.maas_gateway:app`.

## Tests

```bash
python3 -m pytest -q
python3 -m compileall -q gateway
```

## Current limits

- If an upstream stream fails after the gateway has already sent chunks to the client, it cannot switch to a different completion without corrupting the stream.
- Streaming conversion covers text and tool-call deltas; complex multimodal streaming is not covered.
- Success rate depends on upstream capacity. The gateway improves transient failure handling but cannot fix account-level or provider-wide saturation.
