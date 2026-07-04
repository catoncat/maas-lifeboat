# MAAS Lifeboat

中文 | [English](README.en.md)

MAAS Lifeboat 是一层本地救生网关，放在你的客户端和 MAAS coding provider 之间。

它暴露 OpenAI/Anthropic-compatible API，内部做单账号排队、串行重试、跨接口 fallback、短冷却和结构化日志，用来缓解上游频繁返回 `503` / `10310 system busy` 的问题。

它不承诺把上游变成 100% 可用。它解决的是更现实的问题：**减少瞬时 busy 对用户的打断，同时把每一次尝试记录成可复算证据。**

| 适合 | 不适合 |
| --- | --- |
| PI agents、Codex、OpenWebUI、LangChain 等 OpenAI-compatible 客户端 | 想绕过账号级或 provider-wide 容量饱和 |
| 只有一个 MAAS 账号，但希望并行对话别互相打爆 | 多账号调度、供应商套利、批量压测 |
| 希望保留每次请求/重试的 ledger，用实验继续优化策略 | 希望无日志、无状态、完全透明的裸转发 |

## 这个仓库解决什么

| 问题 | 做法 |
| --- | --- |
| MAAS 经常直接返回 `503/10310` | 在首包前串行重试，不急着把失败暴露给客户端 |
| PI 并行对话互相挤占单账号窗口 | 本地 queue 保护首包前的接入/重试阶段；stream 首包到达后释放 queue |
| OpenAI/Anthropic 两个入口偶尔一边可用 | 失败后跨接口 fallback，但不把它们当独立 provider |
| all-busy 后立刻重打浪费请求 | 设置短 cooldown，并向客户端返回 `Retry-After` |
| 不知道到底失败在哪 | JSONL ledger 记录每次 attempt、排队、cooldown 和错误码 |
| 不能影响其他应用 | 只使用 request-level proxy，不改系统代理 |

## 当前实验结论

| 结论 | 证据 | 策略影响 |
| --- | --- | --- |
| 主失败是上游 busy | 185 个后端 attempts 中 89 个为 HTTP `503` + code `10310` | 只对 busy/transport/首包前失败重试 |
| 不是本地网关单独造成 | 直连 OpenAI/Anthropic 兼容入口也能复现同样错误 | 保留后端直连 probe 和 gateway ledger |
| 两个接口不是独立 provider | paired probe 20 对里 9 对两边同时 busy | fallback 有用，但不能当高可用双活 |
| 两个接口有弱去相关 | paired probe 20 对里 9 对只有一边失败 | 保留协议转换和跨接口 fallback |
| 固定 rate limit 没测出来 | 压力统一暴露为 `503/10310`，没有稳定 envelope | 文档只写经验边界，不写伪精确 RPM/TPM |
| 并行 hedging 不适合默认 | paired replay 没提高最终成功率，只固定消耗 2 次 attempts | 默认用串行重试，不无脑并发 |
| 本地 queue 值得保留，但不能锁完整 stream | dogfood 中 3/4 请求排队，4/4 成功，pressure ledger 可用 | 默认只串行化首包前窗口，不串行化整段长回复 |

最重要的边界：**paired probe 的 9/20 both-fail 不是最终成功率天花板。** 它只说明“同一瞬间协议转换救不了”。串行重试、短冷却和客户端 retry 等的是后面的时间窗口，仍然可能成功。

## 功能清单

| 能力 | 说明 |
| --- | --- |
| OpenAI-compatible API | `POST /v1/chat/completions`、`GET /v1/models` |
| Anthropic-compatible API | `POST /anthropic/v1/messages` |
| 单账号接入 queue | 默认 `MAAS_MAX_INFLIGHT_REQUESTS=1`；stream 首包前排队，首包后释放，不锁完整长回复 |
| 串行重试 | 默认 5 次，带 backoff 和 jitter |
| 跨接口 fallback | OpenAI 客户端失败时可尝试 Anthropic 入口，反向也支持 |
| Streaming 保护 | 上游 `200` 但迟迟没有首个 chunk 时，不急着把响应提交给客户端 |
| 工具调用转换 | 支持 OpenAI `tool_calls` 和 Anthropic `tool_use` 的双向转换 |
| 思考参数转换 | 跨接口 fallback 时保留 `options.enable_thinking`、`thinking`；`thinking` block 和 `reasoning_content` 支持双向转换 |
| 可复算日志 | JSONL ledger 记录每次 attempt、queue/cooldown、Retry-After，不记录完整 prompt 或 key |
| request-level proxy | 可配置代理，但不修改系统代理，不影响其他应用 |

## 推荐默认配置

```bash
MAAS_MAX_INFLIGHT_REQUESTS=1
MAAS_MAX_BACKEND_ATTEMPTS=5
MAAS_ENABLE_CROSS_INTERFACE_FALLBACK=1
MAAS_SAME_RETRY_DELAY_S=0.8
MAAS_ALT_RETRY_DELAY_S=1.2
MAAS_RETRY_BACKOFF_MULTIPLIER=1.5
MAAS_MAX_RETRY_DELAY_S=3.0
MAAS_RETRY_JITTER_S=0.25
MAAS_BUSY_COOLDOWN_S=1.0
MAAS_ALL_BUSY_RETRY_AFTER_S=3
MAAS_ALL_BUSY_RECOVERY_ATTEMPTS=2
MAAS_ALL_BUSY_RECOVERY_DELAY_S=3.0
MAAS_STREAM_FIRST_CHUNK_TIMEOUT_S=20
```

| 配置 | 为什么 |
| --- | --- |
| `MAAS_MAX_INFLIGHT_REQUESTS=1` | 单账号下保护首包前的接入/重试窗口。stream 首包后释放 queue，不会把整段长回复串行化。 |
| `MAAS_MAX_BACKEND_ATTEMPTS=5` | 5 次已经覆盖大部分短暂 busy 窗口；7 次更慢、更重，只适合高价值调用。 |
| `MAAS_ALL_BUSY_RECOVERY_ATTEMPTS=2` | 只在基础 5 次全部是 `503/10310` 时触发，等一小段时间后再救援 2 次，减少 PI 直接看到 503 的概率。 |
| `MAAS_BUSY_COOLDOWN_S=1.0` | 如果一个请求所有 attempts 都是 `503/10310`，下一位请求先短暂停一下。 |
| `MAAS_ALL_BUSY_RETRY_AFTER_S=3` | all-busy 后给客户端更明确的请求级 retry 信号。 |

如果你更看重并行体感，可以把 `MAAS_MAX_INFLIGHT_REQUESTS` 设为 `2`。不要直接理解成“完整回复并发数”：对 streaming 路径，它控制的是首包前的接入/重试阶段。

## 安装

```bash
python3 -m pip install -e ".[test]"
cp .env.example .env.local
chmod 600 .env.local
$EDITOR .env.local
```

最少需要填：

```bash
MAAS_API_KEY='<upstream provider key>'
MAAS_GATEWAY_API_KEY='local-client-key'
MAAS_PROXY_URL=''
```

`MAAS_API_KEY` 是上游 provider key。`MAAS_GATEWAY_API_KEY` 是本地客户端访问网关的 key。不要把两者设成一样。

如需 SOCKS proxy 支持：

```bash
python3 -m pip install -e ".[socks]"
```

## 启动

```bash
scripts/start_gateway.sh
```

OpenAI-compatible 客户端配置：

```text
OPENAI_BASE_URL=http://127.0.0.1:18788/v1
OPENAI_API_KEY=<MAAS_GATEWAY_API_KEY>
```

PI agents provider 示例：

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

默认模型元数据按当前观测的 GLM 5.2 / Astron Code 设置：500k context window，131072 max output tokens。若 provider 调整限制，可用 `MAAS_CONTEXT_WINDOW` 和 `MAAS_MAX_TOKENS` 覆盖。

如果客户端支持请求覆盖，可以为 `astron-code-latest` 加上思考参数。gateway 会在 OpenAI/Anthropic fallback 转换时保留这些字段：

```json
{
  "options": {
    "enable_thinking": true
  },
  "thinking": {
    "type": "enabled",
    "budget_tokens": 64000
  }
}
```

非流式响应里，Anthropic `thinking` block 会映射为 OpenAI-compatible `message.reasoning_content`；反向也会从 `reasoning_content` 映射回 Anthropic `thinking` block。流式响应里，对应映射为 `delta.reasoning_content` 和 Anthropic `thinking_delta`。

## macOS 用户服务

只创建当前用户的 LaunchAgent，监听 `127.0.0.1`，不修改系统代理。

```bash
scripts/install_launchagent.sh
```

卸载：

```bash
scripts/uninstall_launchagent.sh
```

## 观察运行状态

```bash
scripts/doctor_gateway.sh
tail -f logs/gateway_requests.jsonl
```

console 会显示每次请求、排队、attempt 和释放：

```text
[maas-gateway] request start id=... surface=openai stream=true ...
[maas-gateway] queue acquired id=... surface=openai limit=1 waited=0.0s
[maas-gateway] attempt id=... n=1/7 interface=openai ok=false status=503 code=10310 ...
[maas-gateway] attempt id=... n=3/7 interface=anthropic ok=true status=200 ...
[maas-gateway] queue release id=... surface=openai limit=1
[maas-gateway] request end id=... ok=true attempts=3 ...
```

对 streaming 请求，`queue release` 会在首个有效 chunk 已经拿到后发生；后续长回复继续 streaming，但不会继续占住 queue。

如果基础 5 次全部是 `503/10310`，会先进入内部救援轮，而不是马上把 503 抛给 PI：

```text
[maas-gateway] all-busy recovery wait id=... sleep=3.0s
[maas-gateway] attempt id=... n=6/7 interface=openai ok=true status=200 ...
```

如果一个请求所有后端 attempts 都是 `503/10310`，会额外看到：

```text
[maas-gateway] cooldown set id=... surface=openai sleep=1.0s reason=all_attempts_10310
```

ledger 不记录完整 prompt 或 API key，只记录 hash 和元数据。关键字段：

| 字段 | 含义 |
| --- | --- |
| `attempts[]` | 每次后端 attempt 的接口、状态码、错误码、耗时 |
| `request_start_ts` | 请求进入 gateway 的时间，用于分析 queue/cooldown 对后续请求的影响 |
| `pressure.inflight_limit` | 本地 queue 上限 |
| `pressure.queue_scope` | queue 保护范围；stream 成功时通常是 `first_chunk` |
| `pressure.queue_wait_s` | 等待 queue permit 的时间 |
| `pressure.cooldown_wait_s` | all-busy 后被 cooldown 挡住的时间 |
| `pressure.busy_cooldown_set_s` | 本请求是否设置了后续 cooldown |
| `pressure.retry_after_s` | 最终失败时返回给客户端的 `Retry-After` 秒数 |

## 实验数据摘要

| 数据来源 | 样本 | 关键结果 | 说明 |
| --- | ---: | --- | --- |
| 早期 OpenAI 单接口探测 | 53 | 33/53 成功 | 单接口成功率明显波动 |
| 早期 Anthropic 单接口探测 | 50 | 29/50 成功 | 与 OpenAI 同量级 |
| 早期 HTTP proxy route 探测 | 104 | 63/104 成功 | 没看到“换路由就稳定”的证据 |
| 2026-07-04 温和 probe | 140 | 首次 attempt 75/140；离线 5 次预算约 88.2% | `503/10310` 呈 burst；两个入口强相关 |
| paired direct probe | 20 对 | 11/20 至少一边成功，9/20 两边都 busy | fallback 有价值，但不是双 provider 高可用 |
| gateway dogfood | 4 | 4/4 成功；3/4 发生本地排队 | 证明 queue 和 pressure ledger 生效；后续代码已改为 stream 首包后释放 queue |

详细材料：

- [可靠性实验记录](docs/reliability-findings.md)
- [MAAS probe 聚合报告](docs/results/maas-probe-2026-07-04.md)
- [策略 replay 报告](docs/results/maas-strategy-replay-2026-07-04.md)
- [gateway dogfood 报告](docs/results/gateway-dogfood-2026-07-04.md)
- [策略优化计划](docs/strategy-optimization-plan.md)

## 已试过但暂不默认的策略

| 策略 | 结论 |
| --- | --- |
| aggressive hedging | 不默认。paired replay 里最终成功率没有提高，只是固定消耗 2 次后端 attempts。 |
| EWMA 接口排序 | 不默认。在线 replay 没有提高最终成功率，最多打平固定 OpenAI-first。 |
| route/IP 随机化 | 暂不作为主线。单账号下很难和自然恢复窗口分开。 |
| 精确 RPM/TPM 拟合 | 暂不写成结论。provider 用 `503/10310` 统一表达压力，没有标准限流 envelope。 |

## 实验命令

Provider probe：

```bash
python3 experiments/probe_maas.py --interfaces both --pattern paired --repeat 20 --rate-interval 0.35 --concurrency 1 --route-label direct
python3 experiments/analyze_maas_ledger.py logs/probe_maas.jsonl logs/gateway_requests.jsonl --output docs/results/maas-probe-YYYY-MM-DD.md
```

网关 dogfood：

```bash
python3 experiments/probe_gateway.py --base-url http://127.0.0.1:18788/v1 --repeat 4 --concurrency 2 --stream --ledger logs/gateway_dogfood_client.jsonl
python3 experiments/analyze_gateway_dogfood.py --client-ledger logs/gateway_dogfood_client.jsonl --backend-ledger logs/gateway_requests.jsonl --run-id <run-id> --output docs/results/gateway-dogfood-YYYY-MM-DD.md
```

`logs/` 下的原始 JSONL ledger 默认不提交。对外分享前只使用聚合结果，并确认没有 key、完整 prompt、proxy URL 或个人路径。

## 项目结构

```text
gateway/app.py          FastAPI 路由
gateway/strategy.py     重试、fallback 和后端调用
gateway/protocols.py    OpenAI/Anthropic 请求与响应转换
gateway/sse.py          SSE 解析和 streaming 转换
gateway/errors.py       可重试错误分类和客户端错误响应
gateway/pressure.py     单账号 queue 和 cooldown
gateway/config.py       环境变量配置
gateway/logging.py      console 日志和 JSONL ledger
```

`gateway/maas_gateway.py` 只是 `uvicorn gateway.maas_gateway:app` 的轻入口。

## 测试

```bash
python3 -m pytest -q
python3 -m compileall -q gateway experiments
```

## 当前限制

- 上游 stream 已经给客户端发出 chunk 后，不能无损切换到另一个 completion。
- streaming 转换覆盖 text、tool-call 和 thinking delta；复杂 multimodal streaming 未覆盖。
- 成功率最终受上游容量限制。gateway 能改善瞬时失败处理，但不能修复账号级或 provider-wide 饱和。
- route/proxy 只作为 request-level 实验变量；当前证据不能证明换 IP 会稳定修复 `503/10310`。
