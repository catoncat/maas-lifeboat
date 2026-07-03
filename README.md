# MAAS Lifeboat

![MAAS Lifeboat four-panel banner](assets/readme-banner/maas-gateway-four-panel-comic.png)

中文 | [English](README.en.md)

一个本地 OpenAI/Anthropic 兼容网关，用来把一个经常返回 `503` / `10310 system busy` 的 MAAS coding provider 变得更可用。

它做三件事：**记录每次后端请求、温和串行重试、在 OpenAI/Anthropic 两个兼容接口之间 fallback**。如果上游最终还是忙，它会返回更标准的可重试错误，让 PI agents、Cursor、OpenWebUI、LangChain 等客户端有机会自动重试。

它不做一件事：**不承诺把上游 50%-60% 的真实容量变成 100%**。如果问题来自账号、模型池或 provider 容量，网关只能降低失败体感，不能凭空创造容量。

## 这个仓库是干嘛的？

| 项目 | 内容 |
| --- | --- |
| 输入协议 | OpenAI-compatible `/v1/chat/completions`；Anthropic-compatible `/anthropic/v1/messages` |
| 后端协议 | MAAS OpenAI-compatible endpoint；MAAS Anthropic-compatible endpoint |
| 核心策略 | 低并发串行重试、跨接口 fallback、stream 首 chunk 保护、标准化可重试错误 |
| 观测能力 | JSONL ledger 记录每次 attempt；console 打印每次重试接口、状态码、错误码、耗时 |
| 不影响系统 | 只支持 request-level proxy，不修改系统代理 |
| 主要使用场景 | PI agents / Cursor / OpenWebUI / LangChain 这类 OpenAI-compatible 客户端 |

## 当前实验结论

| 问题 | 当前结论 | 证据强度 |
| --- | --- | --- |
| 主要失败是什么？ | 几乎都是 HTTP `503` + provider code `10310` + `The system is busy, please try again later.` | 强 |
| 是本地 gateway bug 吗？ | 不像。两个后端接口都会返回同一个 busy 信号。 | 中-强 |
| 是 OpenAI 接口坏、Anthropic 接口好吗？ | 不是。两个接口都能成功，也都能在同一窗口返回 busy。 | 中 |
| 两个接口能当独立 provider 吗？ | 不能。它们更像同一上游容量池的两个兼容入口。 | 中 |
| 改 route/IP/proxy 一定有用吗？ | 目前没有足够证据。只能当实验变量，不是主解法。 | 弱-中 |
| 有明确 rate limit 吗？ | 没观察到干净的 `429` 或固定 RPM 阈值；更像瞬时容量/并发占用导致的 `503`。 | 中 |
| 为什么 PI 并行时会卡？ | 长 streaming 请求会占用窗口；HTTP `200` 不等于客户端马上看到有效 chunk。 | 中 |
| 7 次串行是否万能？ | 不是。真实日志里出现过 7/7 全 busy；5 次默认更温和，7 次只适合高价值请求。 | 强 |

## 关键数据

| 数据来源 | 样本 | 首次 attempt 成功 | 网关最终成功 | 后端 attempts | 关键现象 |
| --- | ---: | ---: | ---: | ---: | --- |
| PI-agent console excerpt，2026-07-04 | 15 个有 `request end` 的 streaming 请求 | 7/16 | 14/15 | 36 | 1 个请求 7/7 全部 `503/10310`，随后客户端重试成功 |
| 本地 gateway JSONL ledger | 15 个 streaming 请求 | 6/15 | 15/15 | 39 | 9/15 需要至少一次后端重试；延迟 1.604s-16.937s |
| 早期 OpenAI 单接口探测 | 53 次请求 | 未单独统计 | 33/53 = 62.3% | 53 | 单接口成功率明显不稳定 |
| 早期 Anthropic 单接口探测 | 50 次请求 | 未单独统计 | 29/50 = 58.0% | 50 | 与 OpenAI 接口同量级 |
| 早期 HTTP proxy route 探测 | 104 次请求 | 未单独统计 | 63/104 = 60.6% | 104 | 没看到“换路由就稳定”的证据 |
| 2026-07-04 温和 probe | 140 个非流式独立请求 | 75/140 = 53.6% | 离线 5 次预算约 88.2%，7 次预算约 95.5% | 140 | `503/10310` 呈 burst；同一窗口 OpenAI/Anthropic 强相关 |

这些样本还不够大，不能当通用 benchmark。它们能支持的判断是：**失败机制不像单纯客户端错误，也不像某一个协议面完全坏掉；更像上游容量在短时间内波动。**

详细证据见 [docs/reliability-findings.md](docs/reliability-findings.md)。

## 推荐运行策略

| 策略项 | 推荐值 | 原因 |
| --- | --- | --- |
| 并发 | 账号级 `1` 最稳；`2` 可用但更容易 busy | 并行 streaming 会占用上游容量窗口 |
| 默认后端 attempts | `5` | 比 7 次更温和；仍能覆盖多数短暂 busy 窗口 |
| 高价值 attempts | `7` | PI 实测中能救回一些请求，但仍可能 7/7 全 busy |
| 尝试顺序 | `native -> native -> alternate -> native -> alternate` | 先给原生接口一次同接口重试，再采样另一个兼容入口 |
| 重试方式 | 串行 + backoff + jitter | 避免 aggressive hedging 把上游打得更忙 |
| 最终失败 | 返回 OpenAI `503` 或 Anthropic `529` | 让 PI 这类客户端能继续做请求级重试 |
| proxy | 只做 request-level proxy | 不修改系统代理，不影响其他应用 |

## 证据边界

| 还没证明的事 | 当前处理 |
| --- | --- |
| 账号级限制、模型池限制、provider 全局拥塞三者怎么区分 | 只能说更像上游容量问题；不能精确归因 |
| route/IP/proxy 是否能稳定提高成功率 | 需要更严格 paired test；README 不把 proxy 当主解法 |
| 精确 RPM / TPM / 并发阈值 | 还没有 `429` 或固定阈值证据；先保守限制并发 |
| delayed hedging 是否值得 | 暂不默认开启；如果实现，必须加预算和延迟触发 |
| 大 token / 长上下文请求表现 | 当前实验主要是低成本短请求和真实 PI streaming 日志 |

## 功能

- OpenAI-compatible `POST /v1/chat/completions`
- Anthropic-compatible `POST /anthropic/v1/messages`
- Local `/v1/models`
- 串行重试 + 跨接口 fallback + 温和 backoff/jitter
- OpenAI 客户端路径下 fallback 仍保持 streaming
- stream 首 chunk timeout：上游 HTTP `200` 但迟迟没有有效首包时，可以换下一次 attempt
- OpenAI `tool_calls` 与 Anthropic `tool_use` 双向转换
- 最终失败返回更标准的可重试错误
- JSONL 请求账本 + console attempt 日志
- request-level proxy，不修改系统代理

## 安装

```bash
python3 -m pip install -e ".[test]"
cp .env.example .env.local
chmod 600 .env.local
$EDITOR .env.local
```

关键环境变量：

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

`MAAS_API_KEY` 是上游 provider key。`MAAS_GATEWAY_API_KEY` 是本地客户端访问这个 gateway 的 key。不要把这两个值设成一样。

如果需要 SOCKS proxy 支持：

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

## macOS 用户服务

只创建当前用户的 LaunchAgent，监听 `127.0.0.1`，不修改系统代理。

```bash
scripts/install_launchagent.sh
```

卸载：

```bash
scripts/uninstall_launchagent.sh
```

## 诊断

```bash
scripts/doctor_gateway.sh
tail -f logs/gateway_requests.jsonl
```

服务 console 会打印每次 attempt：

```text
[maas-gateway] request start id=... surface=openai stream=true ...
[maas-gateway] attempt id=... n=1/5 interface=openai ok=false status=503 code=10310 ...
[maas-gateway] attempt id=... n=3/5 interface=anthropic ok=true status=200 ...
[maas-gateway] request end id=... ok=true attempts=3 ...
```

ledger 只记录 payload hash 和 attempt metadata，不记录完整 prompt 或 API key。

Provider probe：

```bash
python3 experiments/probe_maas.py --interfaces both --pattern paired --repeat 20 --rate-interval 0.35 --concurrency 1 --route-label direct
python3 experiments/analyze_maas_ledger.py logs/probe_maas.jsonl logs/gateway_requests.jsonl --output docs/results/maas-probe-2026-07-04.md
```

`logs/` 下的原始 JSONL ledger 默认不提交。对外分享前只使用聚合结果，并确认没有 key、完整 prompt、proxy URL 或个人路径。

## 项目结构

```text
gateway/app.py          FastAPI routes
gateway/strategy.py     retry/fallback strategy and backend calls
gateway/protocols.py    OpenAI/Anthropic payload and response conversion
gateway/sse.py          SSE parsing and streaming conversion
gateway/errors.py       retry classification and client error envelopes
gateway/config.py       environment-backed settings
gateway/logging.py      console logs and JSONL ledger helpers
```

`gateway/maas_gateway.py` 只是 `uvicorn gateway.maas_gateway:app` 的轻入口。

## 测试

```bash
python3 -m pytest -q
python3 -m compileall -q gateway
```

## 当前限制

- 上游 stream 已经给客户端发出 chunk 后，不能无损切换到另一个 completion。
- streaming 转换覆盖 text 和 tool-call delta；复杂 multimodal streaming 未覆盖。
- 成功率最终受上游容量限制。gateway 能改善瞬时失败处理，但不能修复账号级或 provider-wide 饱和。
- route/proxy 可以作为 request-level 实验变量，但当前证据不能证明换 IP 会稳定修复 `503/10310`。
