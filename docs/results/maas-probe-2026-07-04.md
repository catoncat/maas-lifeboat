# MAAS probe 聚合报告

- 后端 attempts：185

| run | 接口 | 路线 | stream | 并发 | 成功 | 10310 | 耗时 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 20260704-baseline-anthropic-direct-n20 | anthropic | direct | 否 | 1 | 20/20 (100.0%，95% 置信区间 83.9%-100.0%) | 0 | 中位数 0.776s，p95 1.073s |
| 20260704-baseline-openai-direct-n20 | openai | direct | 否 | 1 | 10/20 (50.0%，95% 置信区间 29.9%-70.1%) | 10 | 中位数 0.628s，p95 1.728s |
| 20260704-paired-direct-20pairs | anthropic | direct | 否 | 1 | 4/20 (20.0%，95% 置信区间 8.1%-41.6%) | 16 | 中位数 0.174s，p95 2.132s |
| 20260704-paired-direct-20pairs | openai | direct | 否 | 1 | 9/20 (45.0%，95% 置信区间 25.8%-65.8%) | 11 | 中位数 0.400s，p95 1.529s |
| 20260704-ramp-direct-conc2-both-n10 | anthropic | direct | 否 | 2 | 6/10 (60.0%，95% 置信区间 31.3%-83.2%) | 4 | 中位数 0.853s，p95 3.141s |
| 20260704-ramp-direct-conc2-both-n10 | openai | direct | 否 | 2 | 3/10 (30.0%，95% 置信区间 10.8%-60.3%) | 7 | 中位数 0.283s，p95 1.277s |
| 20260704-route-direct-followup-both-n10 | anthropic | direct | 否 | 1 | 3/10 (30.0%，95% 置信区间 10.8%-60.3%) | 7 | 中位数 0.222s，p95 1.047s |
| 20260704-route-direct-followup-both-n10 | openai | direct | 否 | 1 | 4/10 (40.0%，95% 置信区间 16.8%-68.7%) | 6 | 中位数 0.435s，p95 2.309s |
| 20260704-route-proxy-http-both-n10 | anthropic | proxy-http | 否 | 1 | 8/10 (80.0%，95% 置信区间 49.0%-94.3%) | 2 | 中位数 1.067s，p95 2.063s |
| 20260704-route-proxy-http-both-n10 | openai | proxy-http | 否 | 1 | 8/10 (80.0%，95% 置信区间 49.0%-94.3%) | 2 | 中位数 1.092s，p95 1.889s |
| 20260704-stream-direct-both-n3 | anthropic | direct | 是 | 1 | 3/3 (100.0%，95% 置信区间 43.8%-100.0%) | 0 | 中位数 0.746s，p95 0.809s |
| 20260704-stream-direct-both-n3 | openai | direct | 是 | 1 | 3/3 (100.0%，95% 置信区间 43.8%-100.0%) | 0 | 中位数 1.057s，p95 1.262s |
| gateway-ledger | anthropic | gateway-configured-route | 是 | - | 5/10 (50.0%，95% 置信区间 23.7%-76.3%) | 5 | 中位数 0.744s，p95 5.116s |
| gateway-ledger | openai | gateway-configured-route | 是 | - | 10/29 (34.5%，95% 置信区间 19.9%-52.7%) | 19 | 中位数 0.800s，p95 4.249s |

## 同窗口 paired 接口结果

- 完整 paired 窗口：20

- 同窗口只要允许跨接口尝试一次，成功窗口为：11/20 (55.0%，95% 置信区间 34.2%-74.2%)

| paired 结果 | 数量 |
| --- | --- |
| OpenAI 成功 / Anthropic 成功 | 2 |
| OpenAI 成功 / Anthropic 失败 | 7 |
| OpenAI 失败 / Anthropic 成功 | 2 |
| OpenAI 失败 / Anthropic 失败 | 9 |

## 离线串行重试预算模拟

| 串行预算 | 估算成功率 | 平均 attempts | 累计后端耗时 |
| --- | --- | --- | --- |
| 1 | 75/140 (53.6%，95% 置信区间 45.3%-61.6%) | 1.00 | 中位数 0.677s，p95 2.026s |
| 2 | 97/139 (69.8%，95% 置信区间 61.7%-76.8%) | 1.47 | 中位数 0.900s，p95 2.149s |
| 3 | 107/138 (77.5%，95% 置信区间 69.9%-83.7%) | 1.77 | 中位数 1.044s，p95 2.309s |
| 5 | 120/136 (88.2%，95% 置信区间 81.7%-92.6%) | 2.15 | 中位数 1.175s，p95 2.500s |
| 7 | 128/134 (95.5%，95% 置信区间 90.6%-97.9%) | 2.37 | 中位数 1.314s，p95 2.713s |

## Streaming 首包观测

| run | 接口 | 路线 | stream 成功 | 首包耗时 |
| --- | --- | --- | --- | --- |
| 20260704-stream-direct-both-n3 | anthropic | direct | 3/3 (100.0%，95% 置信区间 43.8%-100.0%) | 中位数 0.614s，p95 0.682s |
| 20260704-stream-direct-both-n3 | openai | direct | 3/3 (100.0%，95% 置信区间 43.8%-100.0%) | 中位数 0.896s，p95 0.985s |
| gateway-ledger | anthropic | gateway-configured-route | 5/10 (50.0%，95% 置信区间 23.7%-76.3%) | - |
| gateway-ledger | openai | gateway-configured-route | 10/29 (34.5%，95% 置信区间 19.9%-52.7%) | - |

## Gateway pressure 观测

- 15 行 gateway 请求里没有结构化 pressure 字段。

## 状态码和错误分布

| HTTP status | provider_code | 结束类型 | 数量 |
| --- | --- | --- | --- |
| 200 | None | 成功 | 96 |
| 503 | 10310 | HTTP错误 | 89 |
