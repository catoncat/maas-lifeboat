# Reliability findings

This project started as a small empirical investigation of a flaky MAAS coding provider.

## Executive conclusion

The provider behavior is best explained as transient upstream account/model/provider capacity saturation, not a broken gateway implementation and not a clearly isolated IP problem.

The strongest evidence is the shape of failures:

- Errors are overwhelmingly HTTP `503`, provider code `10310`, message `The system is busy, please try again later.`
- Both OpenAI-compatible and Anthropic-compatible endpoints show the same busy signal.
- A gateway request can exhaust seven serial attempts across both interfaces, while the next client-level retry of the same conversation can succeed immediately.
- No sample so far has shown a clean `429` rate-limit envelope or a deterministic "after N requests per minute" cutoff.

This means the gateway can improve perceived usability with retries, fallback, first-chunk safeguards, and retryable client errors. It cannot guarantee near-100% success if the upstream account/model pool is saturated.

## Observed upstream behavior

- Dominant failure: HTTP `503` with provider code `10310`.
- Message: `The system is busy, please try again later.`
- Both OpenAI-compatible and Anthropic-compatible endpoints can fail with the same busy signal.
- No hard `429` rate-limit response has been observed in the reviewed samples.
- In the local request ledger, single endpoint success was roughly 58-62%.

Sample from local experiments:

| Surface | Requests | Success rate |
| --- | ---: | ---: |
| OpenAI chat | 53 | 62.3% |
| Anthropic messages | 50 | 58.0% |
| HTTP proxy route | 104 | 60.6% |

These numbers are small-sample and provider-specific. They should not be treated as a universal benchmark.

## PI-agent streaming readback

The most useful real workload evidence came from PI-agent streaming traffic on July 4, 2026 local time.

### Terminal console sample

From the attached PI/gateway console log:

| Metric | Value |
| --- | ---: |
| Gateway requests with completed `request end` lines | 15 |
| Completed gateway requests that succeeded | 14/15 |
| Backend attempts recorded in that excerpt | 36 |
| First attempts that succeeded | 7/16 |
| OpenAI backend attempts | 29, with 12 success and 17 busy failures |
| Anthropic backend attempts | 7, with 2 success and 5 busy failures |
| Exhausted gateway request | 1 request failed after 7/7 attempts |

The exhausted request followed this pattern:

```text
openai 503 -> openai 503 -> anthropic 503 -> openai 503 -> anthropic 503 -> openai 503 -> anthropic 503
```

The client then retried the same conversation and got a first-attempt success. That is a key correction to the early design assumption: backend retries help, but the final error still needs to be a clean retryable client error.

### Local gateway ledger sample

From the local JSONL gateway ledger written after the retry logic was expanded:

| Metric | Value |
| --- | ---: |
| Gateway streaming requests | 15 |
| Requests eventually succeeded | 15/15 |
| Backend attempts required | 39 |
| Requests that succeeded on first attempt | 6/15 |
| Requests needing more than one backend attempt | 9/15 |
| OpenAI attempts | 29, with 10 success and 19 busy failures |
| Anthropic attempts | 10, with 5 success and 5 busy failures |
| Request latency range | 1.604s to 16.937s |

This sample is biased toward a healthier window than the terminal excerpt, but it still shows the same mechanism: first-attempt reliability is poor, and fallback/retry buys success by spending extra attempts and latency.

## What the current evidence does and does not prove

### Account/model/provider capacity vs. route/IP

The same `503`/`10310` busy signal appears on both protocol surfaces and across retry attempts. That points more strongly to upstream account/model/provider capacity than to a local gateway bug.

The evidence is not strong enough to prove whether the limit is account-specific, model-pool-specific, or provider-wide. It is also not strong enough to prove that route/IP has no effect. The current route/IP evidence only says there is no obvious proxy magic bullet in the small samples collected so far.

### Interface independence

OpenAI and Anthropic surfaces are useful fallback routes, but they are not independent providers. In the PI excerpt, both surfaces returned the same busy code inside one exhausted request. Treat cross-interface fallback as a way to sample another compatible queue, not as a mathematically independent failover.

### Rate limit

No clean hard rate-limit threshold has been measured. The reviewed failures are `503` busy responses rather than `429` rate-limit responses.

Operationally, overlapping long streaming generations appear risky: the terminal sample includes simultaneous PI requests where one request succeeds and another burns through retries. Until a controlled ramp says otherwise, assume:

- safest: 1 in-flight generation per account
- acceptable but risky: 2 in-flight generations
- avoid: aggressive hedging or unbounded parallel conversations against the same account

This is a conservative operating bound, not a provider-published limit.

### Streaming stalls and buffering

The logs show that "backend accepted the stream" and "client-visible request finished" are different events. Some requests got HTTP `200` within a few seconds but kept streaming for tens of seconds. That can look like a stall from the client side if the model, provider, or client buffers output.

The gateway now waits for a real first client-visible chunk before committing a streaming response. If upstream returns HTTP `200` but no usable stream data before `MAAS_STREAM_FIRST_CHUNK_TIMEOUT_S`, the gateway can treat that attempt as retryable. After chunks have been sent to the client, seamless failover is no longer safe.

## Strategy evolution

Initial strategy:

```text
native -> same-interface retry -> alternate-interface fallback
```

That strategy looked good in a 12-trial sample, but later real PI-agent usage exposed all-three-attempt failures:

```text
openai 503 -> openai 503 -> anthropic 503
```

The current default is a warmer serial plan:

```text
native -> native -> alternate -> native -> alternate -> native -> alternate
```

This keeps concurrency low while giving transient provider saturation more chances to clear.

For streaming calls, the gateway now waits for a real first client-visible chunk before committing the HTTP response to the caller. If the provider returns HTTP `200` but produces no stream data before `MAAS_STREAM_FIRST_CHUNK_TIMEOUT_S`, that attempt is treated as retryable and the gateway can try the next interface.

## Recommended gateway strategy

Use low-concurrency serial recovery as the default:

```text
native -> native -> alternate -> native -> alternate -> native -> alternate
```

Recommended details:

- retry only retryable failures: timeout, connection errors, empty stream before first chunk, HTTP `408/409/425/429`, HTTP `5xx`, and provider code `10310`
- do not retry authentication or parameter `4xx`
- use short delay plus jitter between attempts, rather than firing attempts concurrently
- keep request-level proxy support, but do not change system proxy settings
- return OpenAI-style `503` or Anthropic-style `529` when all backend attempts fail so clients such as PI can retry the whole request
- do not enable always-on hedging by default; if hedging is added later, make it delayed and budgeted

Seven attempts is a pragmatic current default, not a proven optimum. The evidence says it can turn many failures into successes, but it also increases latency and can still fail during saturated windows.

## Important correction

The early conclusion that the gateway was "ready" was overconfident. The experiment showed that retry/fallback helped; it did not prove near-100% availability under PI agents' real workload.

## Remaining experiments

The next useful work is controlled and modest:

- paired OpenAI/Anthropic probes in the same time window to quantify correlation
- small direct-vs-proxy route probes without publishing proxy endpoints
- short concurrency ramp at 1 and 2 in-flight requests, with 3 only as a brief upper-bound check
- offline replay of real attempt sequences to compare 1/2/3/5/7 attempts, cross-interface order, and delayed hedging budgets

Raw ledgers should stay local and ignored. Published findings should include sample size, date, route labels without sensitive endpoints, and confidence caveats.
