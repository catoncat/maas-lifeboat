# MAAS probe aggregate

- Backend attempts: 185

| run | interface | route | stream | conc | success | 10310 | latency |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 20260704-baseline-anthropic-direct-n20 | anthropic | direct | False | 1 | 20/20 (100.0%, 95% CI 83.9%-100.0%) | 0 | median 0.776s, p95 1.073s |
| 20260704-baseline-openai-direct-n20 | openai | direct | False | 1 | 10/20 (50.0%, 95% CI 29.9%-70.1%) | 10 | median 0.628s, p95 1.728s |
| 20260704-paired-direct-20pairs | anthropic | direct | False | 1 | 4/20 (20.0%, 95% CI 8.1%-41.6%) | 16 | median 0.174s, p95 2.132s |
| 20260704-paired-direct-20pairs | openai | direct | False | 1 | 9/20 (45.0%, 95% CI 25.8%-65.8%) | 11 | median 0.400s, p95 1.529s |
| 20260704-ramp-direct-conc2-both-n10 | anthropic | direct | False | 2 | 6/10 (60.0%, 95% CI 31.3%-83.2%) | 4 | median 0.853s, p95 3.141s |
| 20260704-ramp-direct-conc2-both-n10 | openai | direct | False | 2 | 3/10 (30.0%, 95% CI 10.8%-60.3%) | 7 | median 0.283s, p95 1.277s |
| 20260704-route-direct-followup-both-n10 | anthropic | direct | False | 1 | 3/10 (30.0%, 95% CI 10.8%-60.3%) | 7 | median 0.222s, p95 1.047s |
| 20260704-route-direct-followup-both-n10 | openai | direct | False | 1 | 4/10 (40.0%, 95% CI 16.8%-68.7%) | 6 | median 0.435s, p95 2.309s |
| 20260704-route-proxy-http-both-n10 | anthropic | proxy-http | False | 1 | 8/10 (80.0%, 95% CI 49.0%-94.3%) | 2 | median 1.067s, p95 2.063s |
| 20260704-route-proxy-http-both-n10 | openai | proxy-http | False | 1 | 8/10 (80.0%, 95% CI 49.0%-94.3%) | 2 | median 1.092s, p95 1.889s |
| 20260704-stream-direct-both-n3 | anthropic | direct | True | 1 | 3/3 (100.0%, 95% CI 43.8%-100.0%) | 0 | median 0.746s, p95 0.809s |
| 20260704-stream-direct-both-n3 | openai | direct | True | 1 | 3/3 (100.0%, 95% CI 43.8%-100.0%) | 0 | median 1.057s, p95 1.262s |
| gateway-ledger | anthropic | gateway-configured-route | True | - | 5/10 (50.0%, 95% CI 23.7%-76.3%) | 5 | median 0.744s, p95 5.116s |
| gateway-ledger | openai | gateway-configured-route | True | - | 10/29 (34.5%, 95% CI 19.9%-52.7%) | 19 | median 0.800s, p95 4.249s |

## Same-window paired interface outcomes

- Complete pairs: 20

- Cross-interface one-shot success in paired windows: 11/20 (55.0%, 95% CI 34.2%-74.2%)

| paired outcome | count |
| --- | --- |
| OpenAI ok / Anthropic ok | 2 |
| OpenAI ok / Anthropic fail | 7 |
| OpenAI fail / Anthropic ok | 2 |
| OpenAI fail / Anthropic fail | 9 |

## Offline serial retry budget simulation

| serial budget | estimated success | mean attempts | summed backend latency |
| --- | --- | --- | --- |
| 1 | 75/140 (53.6%, 95% CI 45.3%-61.6%) | 1.00 | median 0.677s, p95 2.026s |
| 2 | 97/139 (69.8%, 95% CI 61.7%-76.8%) | 1.47 | median 0.900s, p95 2.149s |
| 3 | 107/138 (77.5%, 95% CI 69.9%-83.7%) | 1.77 | median 1.044s, p95 2.309s |
| 5 | 120/136 (88.2%, 95% CI 81.7%-92.6%) | 2.15 | median 1.175s, p95 2.500s |
| 7 | 128/134 (95.5%, 95% CI 90.6%-97.9%) | 2.37 | median 1.314s, p95 2.713s |

## Streaming first-chunk observations

| run | interface | route | stream success | first chunk latency |
| --- | --- | --- | --- | --- |
| 20260704-stream-direct-both-n3 | anthropic | direct | 3/3 (100.0%, 95% CI 43.8%-100.0%) | median 0.614s, p95 0.682s |
| 20260704-stream-direct-both-n3 | openai | direct | 3/3 (100.0%, 95% CI 43.8%-100.0%) | median 0.896s, p95 0.985s |
| gateway-ledger | anthropic | gateway-configured-route | 5/10 (50.0%, 95% CI 23.7%-76.3%) | - |
| gateway-ledger | openai | gateway-configured-route | 10/29 (34.5%, 95% CI 19.9%-52.7%) | - |

## Gateway pressure observations

- No structured pressure fields in 15 gateway request rows.

## Status/error distribution

| status | provider_code | finish | count |
| --- | --- | --- | --- |
| 200 | None | ok | 96 |
| 503 | 10310 | http_error | 89 |
