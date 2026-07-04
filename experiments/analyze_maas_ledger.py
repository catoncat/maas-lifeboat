#!/usr/bin/env python3
"""把 probe/gateway JSONL ledger 汇总成可提交的中文 Markdown 报告。"""

from __future__ import annotations

import argparse
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median
from typing import Any


def load_rows(paths: list[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for path in paths:
        with path.open(encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    row = json.loads(line)
                    row["_source"] = str(path)
                    rows.append(row)
    return rows


def normalise(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in rows:
        if "attempts" not in row:
            out.append(row)
            continue
        for attempt_no, attempt in enumerate(row.get("attempts", []), 1):
            out.append(
                {
                    "timestamp": row.get("ts") or row.get("timestamp"),
                    "run_id": row.get("run_id") or "gateway-ledger",
                    "request_id": row.get("request_id"),
                    "attempt_no": attempt_no,
                    "sequence_no": None,
                    "pair_id": None,
                    "interface": attempt.get("interface"),
                    "route_label": row.get("route_label") or "gateway-configured-route",
                    "stream": row.get("stream", False),
                    "concurrency": row.get("concurrency"),
                    "status": attempt.get("status_code"),
                    "ok": attempt.get("ok"),
                    "provider_error_code": attempt.get("error_code"),
                    "provider_error_type": attempt.get("error_type"),
                    "provider_error_message": attempt.get("error_message"),
                    "latency_s": attempt.get("elapsed_s"),
                    "bytes": None,
                    "first_chunk_arrived": None,
                    "first_chunk_latency_s": None,
                    "finish_class": "ok" if attempt.get("ok") else "http_error",
                    "error_class": attempt.get("error_type"),
                    "payload_hash": row.get("payload_sha256_16"),
                    "response_id": attempt.get("response_id"),
                    "_source": row.get("_source"),
                }
            )
    return out


def wilson(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    if total == 0:
        return 0.0, 0.0
    phat = successes / total
    denom = 1 + z * z / total
    centre = phat + z * z / (2 * total)
    margin = z * math.sqrt((phat * (1 - phat) + z * z / (4 * total)) / total)
    return (centre - margin) / denom, (centre + margin) / denom


def pct(value: float) -> str:
    return f"{100 * value:.1f}%"


def yes_no(value: Any) -> str:
    if value is True:
        return "是"
    if value is False:
        return "否"
    return "-"


def finish_label(value: Any) -> str:
    labels = {"ok": "成功", "http_error": "HTTP错误", "exception": "异常"}
    return labels.get(str(value), str(value))


def rate_cell(successes: int, total: int) -> str:
    lo, hi = wilson(successes, total)
    return f"{successes}/{total} ({pct(successes / total if total else 0)}，95% 置信区间 {pct(lo)}-{pct(hi)})"


def md_table(headers: list[str], rows: list[list[Any]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(str(cell) for cell in row) + " |")
    return "\n".join(lines)


def quantiles(values: list[float]) -> str:
    if not values:
        return "-"
    ordered = sorted(values)
    p95 = ordered[min(len(ordered) - 1, int(math.ceil(len(ordered) * 0.95)) - 1)]
    return f"中位数 {median(ordered):.3f}s，p95 {p95:.3f}s"


def summarize_attempts(attempts: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    lines.append(f"- 后端 attempts：{len(attempts)}")
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in attempts:
        key = (row.get("run_id"), row.get("interface"), row.get("route_label"), row.get("stream"), row.get("concurrency"))
        groups[key].append(row)
    table_rows: list[list[Any]] = []
    for key, vals in sorted(groups.items(), key=lambda item: str(item[0])):
        successes = sum(1 for v in vals if v.get("ok"))
        busy = sum(1 for v in vals if str(v.get("provider_error_code")) == "10310")
        latencies = [float(v["latency_s"]) for v in vals if isinstance(v.get("latency_s"), (int, float))]
        table_rows.append([key[0], key[1], key[2], yes_no(key[3]), key[4] or "-", rate_cell(successes, len(vals)), busy, quantiles(latencies)])
    lines.append(md_table(["run", "接口", "路线", "stream", "并发", "成功", "10310", "耗时"], table_rows))
    return "\n\n".join(lines)


def summarize_pairs(attempts: list[dict[str, Any]]) -> str:
    pairs: dict[tuple[str, str], dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in attempts:
        pair_id = row.get("pair_id")
        run_id = row.get("run_id")
        interface = row.get("interface")
        if pair_id and run_id and interface:
            pairs[(run_id, pair_id)][interface] = row
    complete = [pair for pair in pairs.values() if {"openai", "anthropic"} <= set(pair)]
    if not complete:
        return "- 没有完整的同窗口 paired 样本。"
    counts = Counter((bool(pair["openai"].get("ok")), bool(pair["anthropic"].get("ok"))) for pair in complete)
    rows = [
        ["OpenAI 成功 / Anthropic 成功", counts[(True, True)]],
        ["OpenAI 成功 / Anthropic 失败", counts[(True, False)]],
        ["OpenAI 失败 / Anthropic 成功", counts[(False, True)]],
        ["OpenAI 失败 / Anthropic 失败", counts[(False, False)]],
    ]
    both_fail = counts[(False, False)]
    one_or_more_ok = len(complete) - both_fail
    return "\n\n".join(
        [
            f"- 完整 paired 窗口：{len(complete)}",
            f"- 同窗口只要允许跨接口尝试一次，成功窗口为：{rate_cell(one_or_more_ok, len(complete))}",
            md_table(["paired 结果", "数量"], rows),
        ]
    )


def summarize_strategy(attempts: list[dict[str, Any]]) -> str:
    eligible = [a for a in attempts if a.get("attempt_no") == 1 and not a.get("stream")]
    if len(eligible) < 5:
        eligible = [a for a in attempts if a.get("attempt_no") == 1]
    if len(eligible) < 5:
        return "- 独立单次 attempt 样本不足，无法做离线重试预算模拟。"
    rows: list[list[Any]] = []
    for budget in (1, 2, 3, 5, 7):
        successes = 0
        latencies: list[float] = []
        costs: list[int] = []
        for start in range(len(eligible)):
            window = eligible[start : start + budget]
            if len(window) < budget:
                continue
            cost = 0
            latency = 0.0
            ok = False
            for item in window:
                cost += 1
                latency += float(item.get("latency_s") or 0)
                if item.get("ok"):
                    ok = True
                    break
            successes += 1 if ok else 0
            latencies.append(latency)
            costs.append(cost)
        total = len(latencies)
        rows.append([budget, rate_cell(successes, total), f"{sum(costs) / total:.2f}" if total else "-", quantiles(latencies)])
    return md_table(["串行预算", "估算成功率", "平均 attempts", "累计后端耗时"], rows)


def summarize_streams(attempts: list[dict[str, Any]]) -> str:
    streams = [row for row in attempts if row.get("stream")]
    if not streams:
        return "- 没有 streaming 样本。"
    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in streams:
        groups[(row.get("run_id"), row.get("interface"), row.get("route_label"))].append(row)
    rows: list[list[Any]] = []
    for key, vals in sorted(groups.items(), key=lambda item: str(item[0])):
        first_chunks = [float(v["first_chunk_latency_s"]) for v in vals if isinstance(v.get("first_chunk_latency_s"), (int, float))]
        successes = sum(1 for v in vals if v.get("ok"))
        rows.append([key[0], key[1], key[2], rate_cell(successes, len(vals)), quantiles(first_chunks)])
    return md_table(["run", "接口", "路线", "stream 成功", "首包耗时"], rows)


def pressure_value(row: dict[str, Any], key: str) -> Any:
    pressure = row.get("pressure")
    return pressure.get(key) if isinstance(pressure, dict) else None


def summarize_pressure(rows: list[dict[str, Any]]) -> str:
    gateway_rows = [row for row in rows if isinstance(row.get("attempts"), list)]
    with_pressure = [row for row in gateway_rows if isinstance(row.get("pressure"), dict)]
    if not gateway_rows:
        return "- 没有 gateway 请求行。"
    if not with_pressure:
        return f"- {len(gateway_rows)} 行 gateway 请求里没有结构化 pressure 字段。"

    groups: dict[tuple[Any, Any], list[dict[str, Any]]] = defaultdict(list)
    for row in with_pressure:
        groups[(row.get("surface"), row.get("stream", False))].append(row)

    table_rows: list[list[Any]] = []
    for key, vals in sorted(groups.items(), key=lambda item: str(item[0])):
        queue_waits = [float(v) for row in vals if isinstance((v := pressure_value(row, "queue_wait_s")), (int, float))]
        cooldown_waits = [float(v) for row in vals if isinstance((v := pressure_value(row, "cooldown_wait_s")), (int, float))]
        total_waits = [float(v) for row in vals if isinstance((v := pressure_value(row, "total_wait_s")), (int, float))]
        cooldown_sets = [float(v) for row in vals if isinstance((v := pressure_value(row, "busy_cooldown_set_s")), (int, float))]
        retry_afters = [float(v) for row in vals if isinstance((v := pressure_value(row, "retry_after_s")), (int, float))]
        table_rows.append(
            [
                key[0],
                key[1],
                len(vals),
                quantiles(queue_waits),
                quantiles(cooldown_waits),
                quantiles(total_waits),
                f"{len(cooldown_sets)}/{len(vals)}",
                quantiles(retry_afters),
            ]
        )

    return "\n\n".join(
        [
            f"- 带 pressure 字段的 gateway 请求：{len(with_pressure)}/{len(gateway_rows)}",
            md_table(
                ["入口", "stream", "请求数", "排队等待", "cooldown 等待", "总等待", "设置 cooldown", "Retry-After"],
                table_rows,
            ),
        ]
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ledgers", nargs="+", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    attempts = normalise(load_rows(args.ledgers))
    raw_rows = load_rows(args.ledgers)

    by_status = Counter((row.get("status"), str(row.get("provider_error_code")), row.get("finish_class")) for row in attempts)
    content = "\n\n".join(
        [
            "# MAAS probe 聚合报告",
            summarize_attempts(attempts),
            "## 同窗口 paired 接口结果",
            summarize_pairs(attempts),
            "## 离线串行重试预算模拟",
            summarize_strategy(attempts),
            "## Streaming 首包观测",
            summarize_streams(attempts),
            "## Gateway pressure 观测",
            summarize_pressure(raw_rows),
            "## 状态码和错误分布",
            md_table(["HTTP status", "provider_code", "结束类型", "数量"], [[a, b, finish_label(c), n] for (a, b, c), n in by_status.most_common()]),
        ]
    )
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content + "\n", encoding="utf-8")
    else:
        print(content)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
