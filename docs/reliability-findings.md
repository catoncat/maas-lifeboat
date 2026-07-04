# 可靠性实验记录

MAAS Lifeboat 不是对上游可用性的证明。它是一个本地缓冲层：在单账号条件下减少瞬时 busy 对用户的影响，并把每次尝试记录下来，方便复算和继续改策略。

## 实验范围

| 项目 | 取值 |
| --- | --- |
| 实验日期 | 2026-07-04 Asia/Shanghai |
| 账号 | 单账号 |
| 模型 | `astron-code-latest` / GLM 5.2 观测配置 |
| 主要请求 | 短 prompt、低 `max_tokens`、以非 streaming 为主 |
| 并发边界 | 默认不超过 2 |
| 代理边界 | 只用 request-level proxy，不修改系统代理 |
| 原始日志 | `logs/` 下 JSONL，本地保留，不提交 |
| 可提交报告 | [`docs/results/maas-probe-2026-07-04.md`](results/maas-probe-2026-07-04.md) |

这些实验是温和试探，不是压力测试。目标是判断错误形态、接口相关性、重试收益和下一步策略，而不是打爆 provider。

## 总体结果

| 观察项 | 结果 |
| --- | --- |
| 总后端 attempts | 185 |
| 成功 | 96 |
| 失败 | 89 |
| 主错误 | HTTP `503` + provider code `10310` |
| 是否看到鉴权/参数错误 | 没有 |
| 是否看到独立的标准限流形态 | 没有；压力统一表现为 `503/10310` |

当前最可靠的判断是：失败主要来自上游 busy。样本不能区分“账号级调度池饱和”和“provider 全局饱和”，也不能把 proxy/IP 效果证明成稳定机制。

## 直接探测摘要

| 条件 | 成功 | `503/10310` | 解释 |
| --- | ---: | ---: | --- |
| direct，并发 1，OpenAI | 23/50 | 27/50 | 单接口成功率明显波动 |
| direct，并发 1，Anthropic | 27/50 | 23/50 | 与 OpenAI 同量级，也会 busy |
| direct，并发 2，两个接口 | 9/20 | 11/20 | 并发 2 已能复现 busy burst |
| HTTP proxy route，并发 1，两个接口 | 16/20 | 4/20 | 短窗口较好，但末尾 4 连 busy，不能证明换路由解决 |
| direct streaming，并发 1，两个接口 | 6/6 | 0/6 | 短 stream 成功窗口正常，首包约 0.53-0.99s |

## 同窗口 paired probe

| 同一窗口结果 | 数量 |
| --- | ---: |
| OpenAI 成功 / Anthropic 成功 | 2 |
| OpenAI 成功 / Anthropic 失败 | 7 |
| OpenAI 失败 / Anthropic 成功 | 2 |
| OpenAI 失败 / Anthropic 失败 | 9 |

结论：两个兼容入口不是独立 provider。它们有弱去相关性，所以 fallback 能救一部分单边失败窗口；但 9/20 个窗口两边同时 busy，说明共享同一段上游压力。

这就是为什么还保留协议转换：不是因为“Anthropic 接口更好”，而是因为“同一时刻有时只有一边失败”。转换是弱 fallback，不是高可用双活。

## PI 真实使用证据

用户提供的 PI terminal 片段显示：

| 观察项 | 结果 |
| --- | --- |
| 可见 request starts | 17 |
| 可解析 request ends | 15 |
| 成功 / 全失败 | 14 成功，1 个所有 attempts 都 busy |
| 可解析后端 attempts | 36 |
| 后端 busy / 成功 | 22 个 `503/10310`，14 个成功 |
| 成功请求耗时 | 部分 stream 端到端 35-53s |

这说明“某次请求能成功”不能证明网关已经可直接使用。真实 PI 使用里，一个客户端请求可能隐藏多次后端 busy，也可能在成功接入后长时间生成或被客户端缓冲。

## 对最初问题的回答

### 失败机制弄清楚了吗？

弄清楚了一部分：可观测失败几乎都是上游 busy，形态是 HTTP `503` + provider code `10310`。这不是本地网关自己造出来的错误，直连 provider 也能复现。

没有完全弄清的是 provider 内部原因。单账号样本无法证明它究竟是账号级、模型池级、区域级还是 provider 全局拥塞。我们不把不可验证的内部机制写成结论。

### 是否是 OpenAI 接口坏、Anthropic 接口好？

不是。两个入口都能成功，也都能在相邻窗口甚至同一窗口 busy。当前证据只支持“两个入口弱去相关”，不支持“某个协议天然稳定”。

### 是否能测出固定 rate limit？

没有测出稳定 RPM/TPM 阈值。provider 没有用清晰的限流 envelope 暴露边界，而是把压力统一返回成 `503/10310`。

因此项目文档只写“本账号经验边界”：首包前的接入/重试窗口默认本地 queue 为 1，温和 probe 并发不超过 2，busy 后不要立即重打。它不是官方或稳定 rate limit。

### proxy/IP 是否有效？

没有证据证明它是主修复点。HTTP proxy route 在一个短窗口里 16/20 成功，但该组最后出现 4 连 busy；实验也没有随机化路由顺序，所以无法排除自然恢复窗口。

因此 gateway 保留 request-level proxy 配置，但不把 route/IP 随机化作为默认策略。

### 7 次串行重试合理吗？

7 次能提高最终成功概率，但不适合作为默认起点。离线 replay 显示 5 次已经吃到大部分收益，7 次会继续增加等待和后端压力。

| 串行预算 | 估算成功率 | 平均 attempts |
| --- | ---: | ---: |
| 1 | 75/140 (53.6%) | 1.00 |
| 2 | 97/139 (69.8%) | 1.47 |
| 3 | 107/138 (77.5%) | 1.77 |
| 5 | 120/136 (88.2%) | 2.15 |
| 7 | 128/134 (95.5%) | 2.37 |

这个 replay 是方向性证据，不是线上保证。当前默认采用 5 次：

```text
native -> native -> alternate -> native -> alternate
```

高价值调用可以手动设 `MAAS_MAX_BACKEND_ATTEMPTS=7`，但要接受更高延迟和更多后端 attempts。

### PI 偶尔“卡很久然后一次性吐出”是 gateway 流式错了吗？

现有证据不能简单归因于 gateway 流式错误。短 stream 直连探测首包正常；PI 真实片段里，部分成功请求是在已经接入成功后端之后，端到端 stream 持续 35-53s。

可能原因包括 provider 生成慢、工具调用/长上下文导致客户端侧展示延迟、PI 自己缓冲 chunk，或者长 stream 占住本地/上游容量。gateway 已经加了“首包前不提交客户端响应”的保护，但要区分 PI 缓冲和 provider 生成，还需要客户端侧 chunk 时间戳。

## 当前默认策略

| 策略 | 状态 | 原因 |
| --- | --- | --- |
| 单账号接入 queue | 默认启用 | 保护首包前的接入/重试窗口；stream 首包后释放，不锁完整长回复 |
| 5 次基础串行重试 | 默认启用 | 在成功率和后端压力之间更平衡 |
| all-busy 内部救援轮 | 默认启用 | 基础 5 次全部 `503/10310` 时，短等后额外试 2 次，避免过早把 503 抛给 PI |
| 跨接口 fallback | 默认启用 | 能救单边失败窗口 |
| all-busy cooldown | 默认启用 | busy 后立刻重打效果很差 |
| 标准可重试错误 | 默认启用 | 给 PI/Codex 这类客户端一个请求级 retry 信号 |
| aggressive hedging | 不默认 | 固定消耗 2 次后端 attempts，未提高 paired 最终成功率 |
| EWMA 接口排序 | 不默认 | replay 没有稳定超过固定顺序 |
| route/IP 随机化 | 不默认 | 单账号下证据价值低，容易把自然恢复误判成路由收益 |

## 运维建议

1. 默认 `MAAS_MAX_INFLIGHT_REQUESTS=1`，只串行化首包前的接入/重试窗口；stream 首包后释放 queue。
2. 默认 `MAAS_MAX_BACKEND_ATTEMPTS=5`，并保留 `MAAS_ALL_BUSY_RECOVERY_ATTEMPTS=2` 作为只在失败边缘触发的救援轮。
3. 保持 `MAAS_ENABLE_CROSS_INTERFACE_FALLBACK=1`。
4. 保持 `MAAS_BUSY_COOLDOWN_S=1.0`，all-busy 后短暂停顿。
5. 看 `logs/gateway_requests.jsonl`，不要只看客户端是否成功。
6. 如果最终仍失败，保留 `Retry-After`，让上层客户端做下一轮请求级 retry。

当前项目的价值不是“保证 100% 成功”，而是把随机失败变成可观察、可重试、可复算、可继续优化的系统行为。
