# 模型 fallback 探测记录（2026-07-04）

目的：验证同一账号、同一 provider 下，模型维度是否会影响 `503/10310` busy 命中率，并判断是否值得把备用模型作为救援层。

## 方法

- 绕过 gateway，直接请求上游 OpenAI-compatible 和 Anthropic-compatible 两个入口。
- 不使用并发压测；每个模型各跑 6 次轻量请求。
- 只用于判断“备用模型能否作为救援候选”，不把这个小样本写成稳定 SLA。

## 结果

| 模型 | OpenAI 入口 | Anthropic 入口 | 合计 | 主要失败 |
| --- | ---: | ---: | ---: | --- |
| `xopdeepseekv4pro` | 3/3 成功 | 3/3 成功 | 6/6 成功 | 无 |
| `astron-code-latest` | 1/3 成功 | 1/3 成功 | 2/6 成功 | `503/10310 system busy` |

另有一次 `astron-code-latest` 探测中出现 request-level proxy 传输中断，未计入上表。

## 判断

`xopdeepseekv4pro` 在这个窗口明显更稳，可以作为同账号下的模型 fallback。但它不是 `astron-code-latest` 的等价副本：

- 上下文窗口按保守值 `128000` 处理。
- fallback 输出上限按 `32768` 处理。
- 主模型 500k 上下文任务超出 fallback 安全估算时，不切模型。
- 实测备用模型对 64k thinking budget 返回授权失败，因此 fallback 默认移除 thinking 控制。
- 工具调用字段不移除，仍按 OpenAI/Anthropic 转换逻辑透传。

## 当前策略

默认不开启模型 fallback。确认要优先提高 PI 使用体感时，可配置：

```bash
MAAS_MODEL_FALLBACKS=xopdeepseekv4pro
MAAS_MODEL_FALLBACK_ATTEMPTS=3
MAAS_MODEL_FALLBACK_CONTEXT_WINDOW=128000
MAAS_MODEL_FALLBACK_MAX_TOKENS=32768
MAAS_MODEL_FALLBACK_CONTEXT_SAFETY_TOKENS=4096
MAAS_MODEL_FALLBACK_STRIP_THINKING=1
```

触发条件：主模型基础 attempts 全部为 `503/10310`，且当前请求估算上下文没有超过 fallback 窗口。进入模型 fallback 时不额外等待同模型 all-busy recovery delay；如果因为上下文门禁跳过模型 fallback，才回到普通同模型 recovery 逻辑。
