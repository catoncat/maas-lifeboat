# MAAS Lifeboat

![MAAS Lifeboat four-panel banner](assets/readme-banner/maas-gateway-four-panel-comic.png)

Local OpenAI/Anthropic-compatible lifeboat gateway for flaky MAAS coding providers.

The gateway is meant for tools such as PI agents, Cursor, OpenWebUI, LangChain, and other OpenAI-compatible clients that need a local endpoint while an upstream provider intermittently returns `503` / `10310 system busy`.

It does not promise magic availability. If failures are caused by account-level quota, provider-wide saturation, or invalid credentials, the gateway can only surface cleaner retryable errors.

## Reliability readout

Current evidence points to upstream capacity/saturation, not a local client bug:

- The dominant failure is HTTP `503` with provider code `10310` and message `The system is busy, please try again later.`
- The same busy signal appears through both OpenAI-compatible and Anthropic-compatible surfaces.
- In PI-agent streaming logs from July 4, 2026 local time, first attempts succeeded only 7/16 times. A 7-attempt gateway request still failed once, and the immediate client retry then succeeded.
- In a later 15-request local gateway ledger, all 15 requests eventually succeeded, but they required 39 backend attempts. First attempts succeeded only 6/15 times.
- Earlier single-surface probes were also noisy: OpenAI chat 33/53, Anthropic messages 29/50, and one HTTP-proxy route 63/104.

Interpretation:

- Retry and cross-interface fallback make the provider more usable, but they do not make it near-100% reliable.
- The two interfaces are not independent enough to be treated as separate providers; both can return the same busy error in the same request window.
- No hard `429` rate-limit response has been observed in these samples. The practical limit looks like transient account/model/provider capacity, made worse by overlapping long streaming requests.
- There is not enough controlled evidence that changing route/IP/代理端口 fixes the failure. Treat proxy changes as a routing variable to measure, not as the primary cure.

Recommended operating posture:

- Keep concurrency low. `1` in-flight generation per account is safest; `2` can work but increases busy bursts.
- Prefer gentle serial retries with short jittered delay over aggressive hedging.
- Keep `MAAS_MAX_BACKEND_ATTEMPTS=7` only if you accept extra latency/cost. It improves real PI usage but can still fail.
- Let clients retry final `503`/`529` errors. A failed 7-attempt request may succeed immediately on the next client-level retry.

See [docs/reliability-findings.md](docs/reliability-findings.md) for the evidence and caveats.

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
