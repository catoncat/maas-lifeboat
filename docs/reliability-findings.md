# Reliability findings

MAAS Lifeboat is a mitigation for an intermittently busy upstream provider, not a proof that the provider is highly available.

## Latest controlled probe

Date: 2026-07-04 Asia/Shanghai (2026-07-03 UTC)

Test shape:

- Short prompt: ask for a tiny `OK` reply.
- Low `max_tokens`.
- Mostly non-streaming requests, because the first question was provider acceptance/rejection rather than long generation quality.
- Request-level routing only. No system proxy settings were changed.
- Default concurrency no higher than 2.
- Raw ledgers were written under `logs/` and are intentionally git-ignored.
- Shareable aggregate: [`docs/results/maas-probe-2026-07-04.md`](results/maas-probe-2026-07-04.md).

Probe scripts:

```bash
python3 experiments/probe_maas.py --interfaces openai --repeat 20 --rate-interval 0.75 --concurrency 1 --route-label direct
python3 experiments/probe_maas.py --interfaces anthropic --repeat 20 --rate-interval 0.75 --concurrency 1 --route-label direct
python3 experiments/probe_maas.py --interfaces both --pattern paired --repeat 20 --rate-interval 0.35 --concurrency 1 --route-label direct
python3 experiments/probe_maas.py --interfaces both --repeat 10 --rate-interval 0.75 --concurrency 1 --route-label proxy-http --proxy-env MAAS_PROXY_URL
python3 experiments/probe_maas.py --interfaces both --repeat 10 --rate-interval 0.2 --concurrency 2 --route-label direct
python3 experiments/probe_maas.py --interfaces both --repeat 3 --stream --rate-interval 1.0 --concurrency 1 --route-label direct
```

## Results summary

Backend attempt totals across the new probe plus the local PI gateway ledger:

- 185 backend attempts.
- 96 succeeded.
- 89 failed as HTTP `503` with provider code `10310`.
- No distinct credential, schema, quota, or hard rate-limit error appeared in this sample.

Direct non-streaming probe only:

| Condition | Success | `503/10310` | Notes |
| --- | ---: | ---: | --- |
| Direct, concurrency 1, OpenAI | 23/50 | 27/50 | Includes baseline, paired, and follow-up windows. |
| Direct, concurrency 1, Anthropic | 27/50 | 23/50 | Strong time-window variation; one 20/20 baseline was followed by a 4/20 paired window. |
| Direct, concurrency 2, both interfaces | 9/20 | 11/20 | Same error shape, no new rate-limit error. |
| HTTP proxy route, concurrency 1, both interfaces | 16/20 | 4/20 | Better in that short window, but the group ended with four consecutive `503/10310`; this is not proof that proxy/IP fixes the issue. |
| Direct streaming, concurrency 1, both interfaces | 6/6 | 0/6 | First chunk latency was 0.53-0.99s for successful short streams. |

Same-window paired direct probe:

| Outcome | Count |
| --- | ---: |
| OpenAI ok / Anthropic ok | 2 |
| OpenAI ok / Anthropic fail | 7 |
| OpenAI fail / Anthropic ok | 2 |
| OpenAI fail / Anthropic fail | 9 |

Interpretation: the two interface faces are not independent. Cross-interface fallback can help when only one face fails, but nearly half of the paired windows had both faces fail. The failure is bursty and time-correlated.

## PI gateway evidence

The PI terminal attachment showed real streaming client behavior:

- 17 request starts were visible in the pasted terminal segment.
- 15 request ends were visible and parseable: 14 successes and 1 all-attempt failure.
- 36 backend attempts were parseable: 22 failed as `503/10310`, 14 succeeded.
- One request exhausted the 7-attempt sequence with only `503/10310`.
- Several later requests succeeded after 2-5 attempts.
- Some successful streams took 35-53s end-to-end even though backend first accepted the request in roughly 1-6s. That points to long stream duration and/or client-side consumption/buffering after acceptance, not just retry delay.

This is why a single successful retry cannot justify a "ready" conclusion.

## Answers to the reliability questions

### 1. What is the likely failure mechanism?

Best current explanation: short-term provider capacity or account/model scheduling saturation exposed as `503/10310`. The evidence is:

- The dominant error is a provider busy code, not auth failure, invalid model, malformed request, or context limit.
- Failures occur in bursts and can clear seconds later.
- Both OpenAI-compatible and Anthropic-compatible faces can fail with the same provider code.
- A single route can show both long success streaks and immediate all-busy streaks.

Uncertainty: the sample cannot separate global provider saturation from account/model pool saturation. It also cannot rule out route/IP effects because the proxy sample was small and not randomized.

### 2. Are OpenAI and Anthropic endpoint failures independent?

No. They are partially independent at best. In 20 same-window direct pairs, 9 pairs had both faces fail, 7 had only Anthropic fail, 2 had only OpenAI fail, and 2 had both succeed.

One-shot cross-interface fallback would have succeeded in 11/20 paired windows. It improves odds over a single face in mixed windows, but it does not bypass shared provider capacity during both-fail bursts.

### 3. Does route/IP/proxy change success rate?

Maybe, but not proven. The HTTP proxy route sample got 16/20 successes while adjacent direct windows were worse. However:

- The proxy group ended with four consecutive `503/10310`.
- The experiment was not randomized across routes.
- The provider state changed minute by minute.
- Confidence intervals are wide at N=20.

Conclusion: request-level route can be kept as an operational variable, but it should not be presented as a fix. Do not spend the main effort comparing proxy implementations or ports until larger randomized route samples are worth the cost.

### 4. Is there an observable rate limit or concurrency threshold?

No explicit rate-limit error appeared. The provider returned the same `503/10310` at concurrency 1 and 2.

Current safe operational envelope:

- Start rate: about 1 request/sec or slower for routine probing.
- Client concurrency: keep new backend request concurrency at 1-2.
- Long streaming calls: treat each accepted stream as occupying provider/client capacity until the stream ends. A stream that took 50s end-to-end can overlap with many later attempts even if its first backend acceptance was fast.

Short concurrency 3 probes were intentionally skipped because concurrency 2 already reproduced the same busy bursts and no new threshold signal was needed.

### 5. Is 7-attempt serial retry reasonable?

7 attempts can improve chance of success, but it should not be the default conclusion.

Offline simulation over independent single-attempt samples estimated:

| Serial budget | Estimated success | Mean attempts |
| --- | ---: | ---: |
| 1 | 75/140 (53.6%) | 1.00 |
| 2 | 97/139 (69.8%) | 1.47 |
| 3 | 107/138 (77.5%) | 1.77 |
| 5 | 120/136 (88.2%) | 2.15 |
| 7 | 128/134 (95.5%) | 2.37 |

This simulation is optimistic because it replays nearby real attempts as if they were retry slots. It is still useful directionally: more attempts help, but shared busy bursts mean 7 can still fully fail.

The gateway default is now:

```text
native -> native -> alternate -> native -> alternate
```

with mild backoff and jitter. Use `MAAS_MAX_BACKEND_ATTEMPTS=7` only for high-value interactive calls where extra latency and provider load are acceptable.

Recommended gateway algorithm:

- Retry only retryable provider busy/transport/first-chunk failures.
- Keep cross-interface fallback enabled.
- Use serial retries by default; avoid aggressive concurrent hedging.
- Add jitter so multiple local clients do not align retries.
- For streams, do not commit the client response until the first provider chunk arrives.
- If using hedging, use delayed hedging only after a first-attempt timeout budget and keep global in-flight attempts capped.

### 6. Why did PI sometimes "read files for a long time, then dump output"?

Current evidence separates two phases:

- Backend acceptance/first chunk for successful short streams can be under 1s.
- Real PI streams in the attachment sometimes took 35-53s end-to-end after the gateway had already returned HTTP `200` to the client.

So the observed pause is not explained solely by retrying before the first chunk. Plausible contributors are:

- Provider generation duration for tool-heavy or long-context replies.
- PI client buffering until it has a larger displayable chunk or until a tool/read phase completes.
- Long streams occupying local/provider concurrency while later requests retry.

The small direct streaming probe did not reproduce a 200-with-no-first-chunk hang. The gateway's first-chunk timeout remains useful protection, but this experiment cannot prove PI buffering versus provider generation for full real workloads without timestamped client-side chunk logs.

## Operational recommendation

Run MAAS Lifeboat as a conservative fallback gateway:

- Default 5 backend attempts.
- Same-interface warm retry, then alternating cross-interface fallback.
- Backoff + jitter between attempts.
- Keep request-level proxy support as an optional route variable, not a promised availability improvement.
- Keep raw JSONL ledgers local and ignored.
- Watch request-level attempts, not just client-visible success, because a successful request may hide multiple backend `503/10310` attempts.
