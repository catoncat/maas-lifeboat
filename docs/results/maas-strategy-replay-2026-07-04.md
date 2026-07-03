# MAAS strategy replay

Offline replay against recorded ledgers. Scope: single-account only, one model, no new provider requests.

## Same-window paired strategy replay

- Samples: 20 same-window direct paired probes.

| strategy | success | mean attempts | all-busy | backend latency | wall/first-success time |
| --- | --- | --- | --- | --- | --- |
| OpenAI only | 9/20 (45.0%, 95% CI 25.8%-65.8%) | 1.00 | 11/20 | median 0.400s, p95 1.529s | median 0.400s, p95 1.529s |
| Anthropic only | 4/20 (20.0%, 95% CI 8.1%-41.6%) | 1.00 | 16/20 | median 0.174s, p95 2.132s | median 0.174s, p95 2.132s |
| OpenAI -> Anthropic | 11/20 (55.0%, 95% CI 34.2%-74.2%) | 1.55 | 9/20 | median 0.795s, p95 2.325s | median 1.510s, p95 3.525s |
| Anthropic -> OpenAI | 11/20 (55.0%, 95% CI 34.2%-74.2%) | 1.80 | 9/20 | median 0.874s, p95 2.175s | median 1.957s, p95 2.859s |
| Parallel both (upper bound) | 11/20 (55.0%, 95% CI 34.2%-74.2%) | 2.00 | 9/20 | median 1.180s, p95 2.500s | median 0.790s, p95 2.132s |

## Online EWMA interface-order replay

- Samples: 20 paired windows, replayed online: each decision only sees earlier windows.

- EWMA changes the first interface only. With fallback budget=2, final success can only improve if first-attempt ordering reduces wasted attempts; it cannot rescue same-window both-fail.

| strategy | first picks | success | mean attempts | all-busy | backend latency | wall/first-success time |
| --- | --- | --- | --- | --- | --- | --- |
| Fixed OpenAI first, budget=1 | openai=20, anthropic=0 | 9/20 (45.0%, 95% CI 25.8%-65.8%) | 1.00 | 11/20 | median 0.400s, p95 1.529s | median 0.400s, p95 1.529s |
| Fixed Anthropic first, budget=1 | openai=0, anthropic=20 | 4/20 (20.0%, 95% CI 8.1%-41.6%) | 1.00 | 16/20 | median 0.174s, p95 2.132s | median 0.174s, p95 2.132s |
| Fixed OpenAI first, fallback budget=2 | openai=20, anthropic=0 | 11/20 (55.0%, 95% CI 34.2%-74.2%) | 1.55 | 9/20 | median 0.795s, p95 2.325s | median 1.510s, p95 3.525s |
| Fixed Anthropic first, fallback budget=2 | openai=0, anthropic=20 | 11/20 (55.0%, 95% CI 34.2%-74.2%) | 1.80 | 9/20 | median 0.874s, p95 2.175s | median 1.957s, p95 2.859s |
| EWMA alpha=0.2, budget=1 | openai=13, anthropic=7 | 8/20 (40.0%, 95% CI 21.9%-61.3%) | 1.00 | 12/20 | median 0.400s, p95 2.132s | median 0.400s, p95 2.132s |
| EWMA alpha=0.2, budget=2 | openai=17, anthropic=3 | 11/20 (55.0%, 95% CI 34.2%-74.2%) | 1.60 | 9/20 | median 0.795s, p95 2.175s | median 1.541s, p95 2.845s |
| EWMA alpha=0.35, budget=1 | openai=13, anthropic=7 | 8/20 (40.0%, 95% CI 21.9%-61.3%) | 1.00 | 12/20 | median 0.400s, p95 2.132s | median 0.400s, p95 2.132s |
| EWMA alpha=0.35, budget=2 | openai=17, anthropic=3 | 11/20 (55.0%, 95% CI 34.2%-74.2%) | 1.60 | 9/20 | median 0.795s, p95 2.175s | median 1.541s, p95 2.845s |
| EWMA alpha=0.5, budget=1 | openai=9, anthropic=11 | 6/20 (30.0%, 95% CI 14.5%-51.9%) | 1.00 | 14/20 | median 0.277s, p95 2.132s | median 0.277s, p95 2.132s |
| EWMA alpha=0.5, budget=2 | openai=18, anthropic=2 | 11/20 (55.0%, 95% CI 34.2%-74.2%) | 1.55 | 9/20 | median 0.795s, p95 2.175s | median 1.531s, p95 2.845s |
| EWMA alpha=0.8, budget=1 | openai=9, anthropic=11 | 6/20 (30.0%, 95% CI 14.5%-51.9%) | 1.00 | 14/20 | median 0.277s, p95 2.132s | median 0.277s, p95 2.132s |
| EWMA alpha=0.8, budget=2 | openai=18, anthropic=2 | 11/20 (55.0%, 95% CI 34.2%-74.2%) | 1.55 | 9/20 | median 0.795s, p95 2.175s | median 1.531s, p95 2.845s |

## Trace replay of retry orders

- Trace: 60 direct, non-stream, concurrency=1 attempts from runs that sampled both OpenAI and Anthropic. Near-end starts without enough future samples are excluded per strategy.

- This is still an approximate replay; use it to compare strategy shapes, not to declare a precise rate limit.

| strategy | attempt order | starts | success | mean attempts | all-busy | backend latency | wall time |
| --- | --- | --- | --- | --- | --- | --- | --- |
| OpenAI only x5 | openai -> openai -> openai -> openai -> openai | 55 | 49/55 (89.1%, 95% CI 78.2%-94.9%) | 2.35 | 6/55 | median 1.491s, p95 2.527s | median 2.572s, p95 8.841s |
| Anthropic only x5 | anthropic -> anthropic -> anthropic -> anthropic -> anthropic | 56 | 52/56 (92.9%, 95% CI 83.0%-97.2%) | 2.93 | 4/56 | median 1.512s, p95 2.629s | median 3.348s, p95 8.389s |
| Current default shape x5 | openai -> openai -> anthropic -> openai -> anthropic | 55 | 45/55 (81.8%, 95% CI 69.7%-89.8%) | 2.38 | 10/55 | median 1.266s, p95 2.693s | median 2.572s, p95 10.319s |
| Strict OpenAI/Anthropic alternation x5 | openai -> anthropic -> openai -> anthropic -> openai | 55 | 43/55 (78.2%, 95% CI 65.6%-87.1%) | 2.56 | 12/55 | median 1.351s, p95 2.500s | median 3.525s, p95 10.686s |
| Anthropic-first shape x5 | anthropic -> anthropic -> openai -> anthropic -> openai | 56 | 44/56 (78.6%, 95% CI 66.2%-87.3%) | 2.89 | 12/56 | median 1.463s, p95 2.358s | median 4.063s, p95 10.060s |
| Current default shape x7 | openai -> openai -> anthropic -> openai -> anthropic -> openai -> anthropic | 55 | 49/55 (89.1%, 95% CI 78.2%-94.9%) | 2.71 | 6/55 | median 1.664s, p95 2.852s | median 2.572s, p95 16.652s |

## Cooldown signal after `503/10310`

The probe timestamp is written after each response, so this is a coarse signal rather than a causal cooldown proof.

| gap after busy | next samples | next attempt success |
| --- | --- | --- |
| <0.5s | 25 | 0/25 (0.0%, 95% CI 0.0%-13.3%) |
| 0.5-1s | 11 | 7/11 (63.6%, 95% CI 35.4%-84.8%) |
| 1-2s | 7 | 6/7 (85.7%, 95% CI 48.7%-97.4%) |
| 2-4s | 4 | 4/4 (100.0%, 95% CI 51.0%-100.0%) |
| >=4s | 2 | 1/2 (50.0%, 95% CI 9.5%-90.5%) |

| busy streak length | count |
| --- | --- |
| 1 | 11 |
| 2 | 1 |
| 3 | 3 |
| 4 | 2 |
| 8 | 1 |
| 12 | 1 |

## Concurrency signal

| concurrency | interface | success | 10310 | latency |
| --- | --- | --- | --- | --- |
| 1 | anthropic | 27/50 (54.0%, 95% CI 40.4%-67.0%) | 23 | median 0.601s, p95 1.222s |
| 1 | openai | 23/50 (46.0%, 95% CI 33.0%-59.6%) | 27 | median 0.468s, p95 2.026s |
| 2 | anthropic | 6/10 (60.0%, 95% CI 31.3%-83.2%) | 4 | median 0.853s, p95 3.141s |
| 2 | openai | 3/10 (30.0%, 95% CI 10.8%-60.3%) | 7 | median 0.283s, p95 1.277s |

## Interpretation

- Cross-interface fallback is useful, but only for windows where one compatible face succeeds and the other fails.
- Same-window both-fail is not a final single-account success ceiling. It only means protocol conversion cannot help at that instant; cooldown, serial retry, and client retry wait for a later window.
- EWMA ordering is useful only if it lowers first-attempt waste on future windows. Keep it behind replay/feature-flag evidence because these two protocol faces are weakly decorrelated, not independent providers.
- Always-on parallel hedging is a latency upper-bound strategy, not the default: it consumes two backend attempts per user request.
- The next implementation target should be single-account pressure control: global queue, adaptive face ordering, and short cooldown after repeated `503/10310`.
- Route/IP randomization and precise RPM/TPM fitting are lower-priority for this single-account project because the provider exposes pressure as the same `503/10310` signal.
- These replay numbers are not causal rate-limit measurements. They are a cheap filter for which strategies deserve the next gentle live probe.
