# MAAS Lifeboat 策略优化计划

这个文档记录当前实验结果和下一轮研究方向。目标不是继续证明上游不稳定；这个已经足够清楚。目标是把网关策略继续往“更高最终成功率、更低后端压力、更低用户等待时间”推进。

## 当前实验快照

| 项目 | 结果 |
| --- | --- |
| 实验日期 | 2026-07-04 Asia/Shanghai |
| 原始 ledger | `logs/probe_maas.jsonl` 146 行；`logs/gateway_requests.jsonl` 15 行，均不提交 |
| 可提交聚合报告 | `docs/results/maas-probe-2026-07-04.md`；`docs/results/maas-strategy-replay-2026-07-04.md` |
| 总后端 attempts | 185 |
| 成功 / busy | 96 成功；89 个 `503/10310` |
| 上游 busy 信号 | HTTP `503` + provider code `10310` |
| 新实验脚本 | `experiments/probe_maas.py`、`experiments/analyze_maas_ledger.py`、`experiments/replay_strategies.py` |

## 目前已经回答的问题

| 问题 | 当前答案 | 证据 |
| --- | --- | --- |
| 失败外观 | 上游用 `503/10310` 暴露 busy/容量/限流类状态 | 所有失败样本几乎都是同一错误 |
| 是否本地网关 bug | 不像 | probe 直接打后端也复现同一错误 |
| OpenAI/Anthropic 是否独立 | 不独立，但有弱去相关性 | 20 个 paired 窗口：2 双成功、9 单边成功、9 双失败 |
| 转换/fallback 是否有价值 | 有，但只救单边失败窗口 | paired 中 9/20 单边失败；one-shot cross-interface success 为 11/20 |
| route/proxy 是否是解法 | 未证明 | proxy-http 小样本 16/20，但非随机且末尾 4 连 busy |
| 并发 2 是否安全 | 可用但会复现 busy burst | direct concurrency=2 样本 9/20 成功，仍是 `503/10310` |
| 7 次串行是否必需 | 不应作为默认 | 离线 replay 显示 5 次已覆盖大部分收益，7 次增加压力且仍可能全失败 |

## 2026-07-04 离线策略 replay

这一步没有发新请求，只用已有 ledger 做策略回放，目的是筛掉明显不值得线上试的方案。

| 观察 | 结果 | 策略含义 |
| --- | --- | --- |
| paired 窗口单接口 | 只用 OpenAI 为 9/20；只用 Anthropic 为 4/20 | 单接口会吃到大量短窗口 busy |
| paired 窗口串行 fallback | OpenAI -> Anthropic 为 11/20；Anthropic -> OpenAI 为 11/20 | 转换能救一部分单边失败窗口，但不能救双边 busy |
| paired 窗口并行 hedge 上界 | 11/20，平均 attempts=2.00 | 并行没有提高最终成功率，只降低成功时等待；默认不值得 |
| busy 后 <0.5s 下一次 | 0/25 成功 | 立刻重打很差，backoff/cooldown 有必要 |
| busy 后 0.5-2s 下一次 | 13/18 成功 | 短冷却值得进入下一轮实测 |
| EWMA paired replay | budget=1 差于固定 OpenAI；budget=2 最多打平固定 OpenAI-first | 当前不默认上线，最多保留功能开关 |
| balanced trace 当前 x5 | 45/55 | 这只是近似 replay，不能单独决定最终顺序 |
| balanced trace 当前 x7 | 49/55 | 7 次多救一部分，但 p95 wall time 明显变长 |

当前最强信号不是“换接口就好了”，而是：**不要让同一个账号在首包前同时发起多个接入/重试窗口；busy 后不要立即打下一枪；转换只作为弱去相关 fallback。**

paired probe 的 9/20 both-fail 不是单账号最终成功率的绝对天花板。它只表示“同一瞬间协议转换救不了”；冷却、串行重试和客户端 retry 等的是下一个时间窗口，所以仍可能提高最终成功率。

## 范围说明

这个项目只优化**单账号、单模型、单本地 gateway**下的可用性。多账号不是目标，也不是默认假设；所有策略都按“不能增加账号、不能改系统代理、不能影响其他应用”来设计。

## 还没有弄清楚的问题

| 未解问题 | 为什么当前账号难以直接证明 | 下一步可做 |
| --- | --- | --- |
| 账号级限制 vs provider 全局拥塞 | 只有一个账号，且项目不走多账号路线 | 只测“本账号经验可用边界”，不把不可验证的上游内部机制写成定论 |
| 模型池限制 vs route/IP | 当前只有一个主模型，route 样本非随机 | 暂不作为主力；只有 queue/cooldown dogfood 后仍明显不够，再做小块随机化 |
| 精确 RPM/TPM | 上游不返回标准限流 envelope，而是统一 `503/10310` | 只记录经验边界，不把拟合阈值写成稳定 rate limit |
| PI 卡顿来自哪里 | 网关只有服务端 attempt/stream 时间，缺少 PI 客户端 chunk 时间戳 | 增加客户端可见 chunk 时间戳或 PI 侧日志 |

## 优化目标

不要只看“最终成功率”。下一轮策略比较用下面这些指标：

| 指标 | 含义 |
| --- | --- |
| final_success_rate | 客户端最终拿到成功响应的比例 |
| attempts_per_success | 每个成功请求消耗多少后端 attempts |
| all_busy_rate | 所有预算 attempts 都是 `503/10310` 的比例 |
| p50/p95_time_to_first_chunk | 用户多久看到首个有效输出 |
| p50/p95_time_to_final | 请求完成时间 |
| provider_pressure | 同一时间 in-flight attempts 和 streams 的数量 |
| retry_after_success | final error 后客户端下一轮 retry 成功率 |

## 新增观测字段

后续 PI dogfood 不再只看 console 文本。网关 ledger 会写入 `pressure` 对象：

| 字段 | 用途 |
| --- | --- |
| `pressure.inflight_limit` | 当时的本地单账号并发上限 |
| `pressure.queue_scope` | queue 保护范围；stream 成功时应为 `first_chunk` |
| `pressure.queue_wait_s` | 用户请求在本地 queue 等了多久 |
| `pressure.cooldown_wait_s` | 请求是否被 all-busy cooldown 延迟 |
| `pressure.busy_cooldown_set_s` | 当前请求是否触发后续 cooldown |
| `pressure.retry_after_s` | 最终失败时给客户端的 retry 建议 |

下一轮 dogfood 要同时看最终成功率、attempts per success、`queue_wait_s` 和 `cooldown_wait_s`，否则只看“客户端有没有报错”会低估隐藏成本。

## 2026-07-04 网关 dogfood

这一轮只验证本地 queue/cooldown 记录链路，不把它当成功率结论。

| 项目 | 结果 |
| --- | --- |
| 设置 | 独立端口临时网关；OpenAI-compatible streaming；客户端并发 2；`MAAS_MAX_INFLIGHT_REQUESTS=1` |
| 请求数 | 4 |
| 客户端成功 | 4/4 |
| 后端 attempts | 4，全部 HTTP 200 |
| queue 结果 | 3/4 请求发生排队；排队等待中位数 0.802s，p95 1.728s |
| cooldown 结果 | 0/4 触发；本窗口没有 `503/10310` |
| 结论 | queue 机制和结构化 pressure ledger 可用；但这个样本处在健康窗口，不能证明最终成功率提升 |

聚合报告见 `docs/results/gateway-dogfood-2026-07-04.md`。

## 下一轮策略候选

| 策略 | 假设 | 风险 | 实验方法 |
| --- | --- | --- | --- |
| 自适应接口排序 | 最近成功率更高的接口应该先试 | 过拟合短窗口噪声 | EWMA replay 已完成；当前不默认上线 |
| 首包前接入队列 | 限制首包前 in-flight 接入/重试窗口能减少 busy burst | 首包前可能排队，但长 stream 不应锁住队列 | 已改为 stream 首包后释放 queue，下一步 dogfood PI 并行对话 |
| burst 冷却/熔断 | 两个接口连续 busy 后短暂停止能减少浪费 | 过早冷却会错过恢复窗口 | 已加入 all-busy 后 `MAAS_BUSY_COOLDOWN_S`；下一步 dogfood 0/1/2/4s |
| all-busy 内部救援轮 | 基础 5 次全 busy 后，等 3s 再试 2 次可减少 PI 可见 503 | 更慢，且失败窗口很长时仍会失败 | 已加入 `MAAS_ALL_BUSY_RECOVERY_ATTEMPTS=2` |
| 分层 attempts | 普通请求 5 次，高价值请求 7 次 | 用户需要知道何时高价值 | 通过 env/header 配置策略档位 |
| delayed hedging | 首包慢时补发能降低尾延迟 | 并发压力变高 | 只对首包慢启用，设置全局 hedging budget |
| 客户端 retry 协同 | 最终 `503/529` 后等待短 jitter 再 retry 可能比继续打满 attempts 更好 | 依赖客户端行为 | all-busy 时返回 `MAAS_ALL_BUSY_RETRY_AFTER_S`，测 PI 是否按预期 retry |

## 下一轮实验顺序

| 阶段 | 动作 | 请求强度 |
| --- | --- | --- |
| 1. 离线 replay | 已加入 paired/trace/cooldown replay | 0 真实请求 |
| 2. 网关 queue 原型 | 已加本地首包前 single-account cap，不改系统代理 | 0 或少量 dogfood |
| 3. PI dogfood | 用真实 PI 并行对话测试 queue/cooldown 的体感 | 首包前 queue 默认 1；stream 首包后释放；cooldown 默认 1s |
| 4. EWMA replay | 已完成；结果不足以默认上线 | 0 真实请求 |
| 5. route block（可选） | direct/proxy 小块随机化，只在主策略仍不够时做 | 并发 1，短 prompt |
| 6. 决策 | 根据 attempts_per_success 和 p95 首包时间调整默认策略 | 先文档，后代码 |

## 当前推荐

先不要做 aggressive hedging。当前最可能提升可用性的方向是：

1. **首包前接入队列**：已作为默认保守策略加入，减少 PI 并行对话同时冲击接入/重试窗口；stream 首包后释放 queue，避免长回复阻塞其他对话。
2. **all-busy 内部救援轮**：已加入基础 5 次全 busy 后等待再试 2 次，目标是减少 PI 直接看到 `503`。
3. **both-fail 短冷却**：已加入 all-busy 后 `MAAS_BUSY_COOLDOWN_S=1.0`，下一轮用 PI dogfood 调整。
4. **EWMA 接口排序暂不默认**：replay 没有显示稳定优势；如果实现，只应放在功能开关后面继续 dogfood。
5. **客户端 retry 协同**：最终错误不伪装成功；all-busy 时 `Retry-After` 默认 3 秒，让 PI 做下一轮请求级 retry。

这些都可以先用现有 ledger 做离线评估，再做小流量实测。

## 已放弃：cooldown 随机对照开关

曾考虑过在 all-busy 后随机跳过一半 cooldown，用随机分组对比验证因果收益。这个方案现在不进入代码：

| 原因 | 判断 |
| --- | --- |
| 它会故意降低一半 busy 事件后的保护 | 不适合作为 PI 日常策略 |
| 当前目标是提高可用性，不是在线牺牲流量做实验 | 不把用户真实任务当空白对照组 |
| 已有 ledger 已能支持离线观察 gap-after-busy | 继续保留 `request_start_ts`，但不保留随机分组 |

最终采用固定策略：只要一个请求所有后端 attempts 都是 `503/10310`，就设置短 cooldown；如果基础 attempts 全 busy，再进入内部救援轮。
