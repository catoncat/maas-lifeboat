# MAAS Lifeboat

[中文](README.md) | English

MAAS Lifeboat is a local OpenAI/Anthropic-compatible gateway for a flaky MAAS coding provider that often returns `503` / `10310 system busy`.

It logs every backend attempt, retries gently, falls back between compatible OpenAI and Anthropic surfaces, keeps streaming usable where possible, and returns clean retryable client errors when the upstream provider is still overloaded.

It is not a magic availability layer. If the upstream account, model pool, or provider capacity is saturated, the gateway can reduce user-visible failures but cannot create capacity.

## At a glance

| Area | Value |
| --- | --- |
| Client protocols | OpenAI `/v1/chat/completions`; Anthropic `/anthropic/v1/messages` |
| Backend protocols | MAAS OpenAI-compatible and Anthropic-compatible endpoints |
| Strategy | low-concurrency serial retries, cross-interface fallback, first-chunk streaming guard |
| Observability | JSONL ledger plus console attempt logs |
| Proxy policy | request-level proxy only; never changes system proxy settings |
| Primary clients | PI agents, Cursor, OpenWebUI, LangChain, and similar OpenAI-compatible tools |

## Reliability readout

| Question | Current answer |
| --- | --- |
| Dominant failure | HTTP `503`, provider code `10310`, `The system is busy, please try again later.` |
| Local gateway bug? | Unlikely. Both backend surfaces return the same busy signal. |
| Independent provider failover? | No. OpenAI and Anthropic surfaces are useful fallback routes but not independent providers. |
| Clear rate limit? | No clean `429` or fixed RPM threshold observed; behavior looks like transient capacity saturation. |
| Route/proxy cure? | Not proven. Treat route/IP/proxy as a variable to measure, not the main fix. |
| Default strategy | Gentle serial recovery with final retryable client errors. |

## Key data

| Source | Sample | First-attempt success | Final gateway success | Backend attempts | Observation |
| --- | ---: | ---: | ---: | ---: | --- |
| PI-agent console excerpt, 2026-07-04 | 15 completed streaming requests | 7/16 | 14/15 | 36 | One request exhausted all 7 attempts, then the immediate client retry succeeded |
| Local gateway JSONL ledger | 15 streaming requests | 6/15 | 15/15 | 39 | 9/15 needed at least one backend retry; latency range 1.604s-16.937s |
| Earlier OpenAI single-surface probe | 53 requests | not separated | 33/53 = 62.3% | 53 | noisy single-surface success |
| Earlier Anthropic single-surface probe | 50 requests | not separated | 29/50 = 58.0% | 50 | same order of reliability as OpenAI |
| Earlier HTTP proxy-route probe | 104 requests | not separated | 63/104 = 60.6% | 104 | no obvious route magic bullet |
| 2026-07-04 gentle probe | 140 independent non-streaming requests | 75/140 = 53.6% | offline 5-attempt budget about 88.2%; 7-attempt budget about 95.5% | 140 | `503/10310` was bursty and correlated across OpenAI/Anthropic faces |

See [docs/reliability-findings.md](docs/reliability-findings.md) for more detail.

## Recommended posture

| Setting | Recommendation |
| --- | --- |
| Concurrency | `1` in-flight generation per account is safest; `2` can work but increases busy bursts |
| Backend attempts | default `MAAS_MAX_BACKEND_ATTEMPTS=5`; use `7` only for high-value calls if you accept extra latency/cost |
| Attempt order | `native -> native -> alternate -> native -> alternate` |
| Retry style | serial retry with mild backoff/jitter; avoid always-on aggressive hedging |
| Final failure | return OpenAI `503` or Anthropic `529` so clients can retry the whole request |

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
MAAS_MAX_BACKEND_ATTEMPTS=5
MAAS_ENABLE_CROSS_INTERFACE_FALLBACK=1
MAAS_SAME_RETRY_DELAY_S=0.8
MAAS_ALT_RETRY_DELAY_S=1.2
MAAS_RETRY_BACKOFF_MULTIPLIER=1.5
MAAS_MAX_RETRY_DELAY_S=3.0
MAAS_RETRY_JITTER_S=0.25
MAAS_STREAM_FIRST_CHUNK_TIMEOUT_S=20
```

Run:

```bash
scripts/start_gateway.sh
```

OpenAI-compatible clients:

```text
OPENAI_BASE_URL=http://127.0.0.1:18788/v1
OPENAI_API_KEY=<MAAS_GATEWAY_API_KEY>
```

## Diagnostics

```bash
scripts/doctor_gateway.sh
tail -f logs/gateway_requests.jsonl
```

The ledger records payload hashes and attempt metadata, not full prompts or API keys.

Provider probe:

```bash
python3 experiments/probe_maas.py --interfaces both --pattern paired --repeat 20 --rate-interval 0.35 --concurrency 1 --route-label direct
python3 experiments/analyze_maas_ledger.py logs/probe_maas.jsonl logs/gateway_requests.jsonl --output docs/results/maas-probe-2026-07-04.md
```

## Tests

```bash
python3 -m pytest -q
python3 -m compileall -q gateway
```
