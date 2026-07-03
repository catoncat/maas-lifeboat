# 可靠性实验结论

本文记录 MAAS Lifeboat 目前能被证据支持的可靠性结论。原始 ledger 留在本地 `logs/`，不提交；公开文档只保留聚合数字和不含敏感端点的 route 描述。

## 一句话结论

当前行为最像 **上游账号/模型池/provider 容量瞬时饱和**，不像本地 gateway bug，也不像某一个协议面彻底坏掉。

网关能通过串行重试、跨接口 fallback、stream 首包保护和可重试错误显著改善可用性；但如果上游容量池正在 busy，它不能保证接近 100% 成功。

## 关键数据

| 数据来源 | 样本 | 首次 attempt 成功 | 最终请求成功 | 后端 attempts | 关键现象 |
| --- | ---: | ---: | ---: | ---: | --- |
| PI-agent console excerpt，2026-07-04 | 15 个有 `request end` 的 streaming 请求 | 7/16 | 14/15 | 36 | 1 个请求 7/7 全部 `503/10310`，随后客户端重试成功 |
| 本地 gateway JSONL ledger | 15 个 streaming 请求 | 6/15 | 15/15 | 39 | 9/15 需要至少一次后端重试；延迟 1.604s-16.937s |
| 早期 OpenAI 单接口探测 | 53 次请求 | 未单独统计 | 33/53 = 62.3% | 53 | 单接口成功率明显不稳定 |
| 早期 Anthropic 单接口探测 | 50 次请求 | 未单独统计 | 29/50 = 58.0% | 50 | 与 OpenAI 接口同量级 |
| 早期 HTTP proxy route 探测 | 104 次请求 | 未单独统计 | 63/104 = 60.6% | 104 | 没看到“换路由就稳定”的证据 |

这些样本不够大，不能当通用 benchmark。它们能支持的是机制判断：失败不是一个简单的本地 bug，也不是某一个协议面永久不可用。

## 结论表

| 问题 | 当前判断 | 证据 |
| --- | --- | --- |
| 主要错误 | HTTP `503` + provider code `10310` + `The system is busy, please try again later.` | 多份 gateway/PI 日志一致 |
| OpenAI vs Anthropic | 两者都可能成功，也都可能 busy | PI excerpt 中两个接口在同一请求窗口都返回 `10310` |
| 跨接口 fallback | 有帮助，但不是独立 provider failover | 本地 ledger 中 Anthropic fallback 救回多次请求；但也出现 Anthropic busy |
| 7 次串行 | 有实际收益，但不是成功保证 | PI excerpt 中出现 7/7 全 busy；本地 ledger 中 15/15 成功但用了 39 attempts |
| route/proxy | 暂无强证据证明能稳定修复 | 早期 HTTP proxy route 成功率仍约 60.6% |
| rate limit | 未见干净 `429` 或固定 RPM 阈值 | 失败形态是 `503` busy，不是 rate-limit envelope |
| streaming 卡顿 | HTTP `200` 和客户端可见输出不是同一事件 | 日志中部分请求很快拿到 `200`，但总 stream 持续数十秒 |

## 证据边界

| 还不能证明的事 | 当前处理 |
| --- | --- |
| 到底是账号级、模型池级还是 provider 全局拥塞 | 只能判断更像上游容量问题；不能精确归因 |
| route/IP/proxy 是否能提升成功率 | 需要 paired route test；当前不把 proxy 当主解法 |
| 精确 rate limit / 并发阈值 | 先按保守策略运行：账号级 1 最稳，2 可用但风险上升 |
| delayed hedging 是否值得 | 暂不默认开启；未来如实现必须 delayed + budgeted |
| 大 token / 长上下文场景 | 当前结论主要来自短请求和真实 PI streaming 样本 |

## PI-agent streaming readback

### 终端 console 样本

附件中的 PI/gateway console log 展示了一个关键失败模式：

```text
openai 503 -> openai 503 -> anthropic 503 -> openai 503 -> anthropic 503 -> openai 503 -> anthropic 503
```

这说明两个协议面不能被当作完全独立的 provider。更重要的是：该请求失败后，客户端立即重试同一对话，下一次 first attempt 成功。这就是为什么 gateway 最终失败时必须返回标准可重试错误，而不是把流里塞一个非标准 error chunk。

### 本地 gateway ledger 样本

后续本地 JSONL ledger 的 15 个 streaming 请求全部最终成功，但需要 39 次后端 attempts。这个样本比 console excerpt 健康，但仍然显示 first-attempt reliability 很差：只有 6/15 第一次成功。

## 策略演进

最早策略是：

```text
native -> same-interface retry -> alternate-interface fallback
```

这个策略在 12 次小样本里看起来可用，但真实 PI usage 很快暴露了三次都 busy 的情况：

```text
openai 503 -> openai 503 -> anthropic 503
```

当前默认改为更保守的 7 次串行：

```text
native -> native -> alternate -> native -> alternate -> native -> alternate
```

这个策略会增加延迟和后端 attempt 数，但在真实 PI 使用中能救回明显更多请求。

## 推荐 gateway 策略

| 策略项 | 推荐 |
| --- | --- |
| 重试类型 | 只重试 timeout、连接失败、首包前 empty stream、HTTP `408/409/425/429`、HTTP `5xx`、provider code `10310` |
| 不重试类型 | 认证失败、参数错误和其他确定性 `4xx` |
| attempt 顺序 | `native -> native -> alternate -> native -> alternate -> native -> alternate` |
| delay | 短 delay + jitter，避免所有请求同步重试 |
| hedging | 默认不开；如果未来做，只能 delayed hedging + budget |
| streaming | 首个客户端可见 chunk 前允许换路；发出 chunk 后不做无损 failover |
| 最终失败 | OpenAI surface 返回 `503`，Anthropic surface 返回 `529`，方便客户端请求级重试 |

## 后续实验不应放大的风险

后续实验应该继续温和、小样本、可复现：

| 实验 | 目的 | 边界 |
| --- | --- | --- |
| OpenAI/Anthropic paired probe | 量化两个协议面的相关性 | 并发不超过 1，短 prompt |
| direct/proxy route probe | 判断 route 是否显著影响成功率 | 不公开 proxy endpoint，不做大流量比较 |
| concurrency ramp | 找账号级安全并发 | 主要测 1 和 2；3 只做短暂上界 |
| 离线策略回放 | 比较 1/2/3/5/7 attempts 和 fallback 顺序 | 优先用已有 ledger，不靠大量真实请求 |

## 重要修正

早期“ready”的判断过于乐观。实验只证明 retry/fallback 有帮助；没有证明 provider 在 PI agents 真实工作负载下能接近 100% 可用。
