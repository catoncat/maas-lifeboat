# Gateway dogfood aggregate

Scope: local gateway, single account, OpenAI-compatible streaming client path. Raw ledgers are not committed.

## Overview

| metric | value |
| --- | --- |
| run_id | 20260704-gateway-dogfood-queue-c1-clientc2-n4 |
| client requests | 4 |
| client success | 4/4 (100.0%) |
| client statuses | 200:4 |
| client latency | median 1.676s, p95 2.603s |
| client first chunk | median 1.562s, p95 2.541s |
| backend requests | 4 |
| backend success | 4/4 (100.0%) |
| backend attempts | 4 |
| backend attempt statuses | 200:4 |
| backend request latency | median 1.669s, p95 2.595s |
| backend attempt latency | median 0.800s, p95 1.566s |
| attempts per success | 1.00 |

## Pressure observations

| metric | value |
| --- | --- |
| queue wait | median 0.802s, p95 1.728s |
| queue waited requests | 3/4 |
| mean queue wait | 0.833s |
| cooldown wait | median 0.000s, p95 0.000s |
| cooldown set | 0/4 |
| retry-after | - |

## Backend request rows

| # | ok | attempts | queue_wait_s | cooldown_wait_s | cooldown_set_s | elapsed_s | attempt_statuses |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | True | 1 | 0.0 | 0.0 | None | 1.729 | 200 |
| 2 | True | 1 | 1.728 | 0.0 | None | 2.595 | 200 |
| 3 | True | 1 | 0.864 | 0.0 | None | 1.608 | 200 |
| 4 | True | 1 | 0.741 | 0.0 | None | 1.596 | 200 |
