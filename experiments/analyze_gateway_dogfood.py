#!/usr/bin/env python3
"""汇总本地 gateway dogfood 的客户端和后端 ledger。"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from statistics import mean, median
from typing import Any


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def pct(value: float) -> str:
    return f"{100 * value:.1f}%"


def cell(value: Any) -> Any:
    if value is True:
        return "是"
    if value is False:
        return "否"
    if value is None:
        return "-"
    return value


def quantiles(values: list[float]) -> str:
    if not values:
        return "-"
    ordered = sorted(values)
    p95 = ordered[min(len(ordered) - 1, int(math.ceil(len(ordered) * 0.95)) - 1)]
    return f"中位数 {median(ordered):.3f}s，p95 {p95:.3f}s"


def md_table(headers: list[str], rows: list[list[Any]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
    for row in rows:
        lines.append("| " + " | ".join(str(cell) for cell in row) + " |")
    return "\n".join(lines)


def pressure_value(row: dict[str, Any], key: str) -> Any:
    pressure = row.get("pressure")
    return pressure.get(key) if isinstance(pressure, dict) else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-ledger", required=True, type=Path)
    parser.add_argument("--backend-ledger", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    client_rows = [row for row in load_jsonl(args.client_ledger) if row.get("run_id") == args.run_id]
    backend_rows = load_jsonl(args.backend_ledger)
    backend_attempts = [attempt for row in backend_rows for attempt in row.get("attempts", [])]

    client_successes = sum(1 for row in client_rows if row.get("ok"))
    backend_successes = sum(1 for row in backend_rows if row.get("ok"))
    first_chunks = [float(v) for row in client_rows if isinstance((v := row.get("first_chunk_latency_s")), (int, float))]
    client_latencies = [float(v) for row in client_rows if isinstance((v := row.get("latency_s")), (int, float))]
    backend_latencies = [float(v) for row in backend_rows if isinstance((v := row.get("elapsed_s")), (int, float))]
    attempt_latencies = [float(v) for attempt in backend_attempts if isinstance((v := attempt.get("elapsed_s")), (int, float))]
    queue_waits = [float(v) for row in backend_rows if isinstance((v := pressure_value(row, "queue_wait_s")), (int, float))]
    cooldown_waits = [float(v) for row in backend_rows if isinstance((v := pressure_value(row, "cooldown_wait_s")), (int, float))]
    cooldown_sets = [float(v) for row in backend_rows if isinstance((v := pressure_value(row, "busy_cooldown_set_s")), (int, float))]
    retry_afters = [float(v) for row in backend_rows if isinstance((v := pressure_value(row, "retry_after_s")), (int, float))]

    status_counts: dict[Any, int] = {}
    for row in client_rows:
        status_counts[row.get("status")] = status_counts.get(row.get("status"), 0) + 1
    attempt_status_counts: dict[Any, int] = {}
    for attempt in backend_attempts:
        status = attempt.get("status_code")
        attempt_status_counts[status] = attempt_status_counts.get(status, 0) + 1

    overview = md_table(
        ["指标", "值"],
        [
            ["run_id", args.run_id],
            ["客户端请求数", len(client_rows)],
            ["客户端成功", f"{client_successes}/{len(client_rows)} ({pct(client_successes / len(client_rows)) if client_rows else '0.0%'})"],
            ["客户端状态码", ", ".join(f"{k}:{v}" for k, v in sorted(status_counts.items(), key=lambda item: str(item[0])))],
            ["客户端总耗时", quantiles(client_latencies)],
            ["客户端首包耗时", quantiles(first_chunks)],
            ["gateway 请求数", len(backend_rows)],
            ["gateway 成功", f"{backend_successes}/{len(backend_rows)} ({pct(backend_successes / len(backend_rows)) if backend_rows else '0.0%'})"],
            ["后端 attempts", len(backend_attempts)],
            ["后端 attempt 状态码", ", ".join(f"{k}:{v}" for k, v in sorted(attempt_status_counts.items(), key=lambda item: str(item[0])))],
            ["gateway 请求耗时", quantiles(backend_latencies)],
            ["后端 attempt 耗时", quantiles(attempt_latencies)],
            ["每次成功消耗 attempts", f"{len(backend_attempts) / client_successes:.2f}" if client_successes else "-"],
        ],
    )

    pressure = md_table(
        ["指标", "值"],
        [
            ["排队等待", quantiles(queue_waits)],
            ["发生排队的请求", f"{sum(1 for v in queue_waits if v > 0)}/{len(queue_waits)}"],
            ["平均排队等待", f"{mean(queue_waits):.3f}s" if queue_waits else "-"],
            ["cooldown 等待", quantiles(cooldown_waits)],
            ["设置 cooldown", f"{len(cooldown_sets)}/{len(backend_rows)}"],
            ["Retry-After", quantiles(retry_afters)],
        ],
    )

    rows = []
    for index, row in enumerate(backend_rows, 1):
        attempts = row.get("attempts", [])
        rows.append(
            [
                index,
                cell(row.get("ok")),
                len(attempts),
                cell(pressure_value(row, "queue_wait_s")),
                cell(pressure_value(row, "cooldown_wait_s")),
                cell(pressure_value(row, "busy_cooldown_set_s")),
                cell(row.get("elapsed_s")),
                ",".join(str(attempt.get("status_code")) for attempt in attempts),
            ]
        )

    content = "\n\n".join(
        [
            "# Gateway dogfood 聚合报告",
            "范围：本地 gateway、单账号、OpenAI-compatible streaming 客户端路径。原始 ledger 不提交。",
            "## 总览",
            overview,
            "## 排队和冷却观测",
            pressure,
            "## Gateway 请求明细",
            md_table(["#", "成功", "attempts", "排队等待(s)", "cooldown等待(s)", "设置cooldown(s)", "总耗时(s)", "attempt状态码"], rows),
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
