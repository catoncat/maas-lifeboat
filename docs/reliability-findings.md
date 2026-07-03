# Reliability findings

This project started as a small empirical investigation of a flaky MAAS coding provider.

## Observed upstream behavior

- Dominant failure: HTTP `503` with provider code `10310`.
- Message: `The system is busy, please try again later.`
- Both OpenAI-compatible and Anthropic-compatible endpoints can fail with the same busy signal.
- In the local request ledger, single endpoint success was roughly 58-62%.

Sample from local experiments:

| Surface | Requests | Success rate |
| --- | ---: | ---: |
| OpenAI chat | 53 | 62.3% |
| Anthropic messages | 50 | 58.0% |
| HTTP proxy route | 104 | 60.6% |

These numbers are small-sample and provider-specific. They should not be treated as a universal benchmark.

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

## Important correction

The early conclusion that the gateway was "ready" was overconfident. The experiment showed that retry/fallback helped; it did not prove near-100% availability under PI agents' real workload.
