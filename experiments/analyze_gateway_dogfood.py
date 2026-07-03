#!/usr/bin/env python3
"""Aggregate local gateway dogfood client/backend ledgers."""

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


def quantiles(values: list[float]) -> str:
    if not values:
        return "-"
    ordered = sorted(values)
    p95 = ordered[min(len(ordered) - 1, int(math.ceil(len(ordered) * 0.95)) - 1)]
    return f"median {median(ordered):.3f}s, p95 {p95:.3f}s"


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
        ["metric", "value"],
        [
            ["run_id", args.run_id],
            ["client requests", len(client_rows)],
            ["client success", f"{client_successes}/{len(client_rows)} ({pct(client_successes / len(client_rows)) if client_rows else '0.0%'})"],
            ["client statuses", ", ".join(f"{k}:{v}" for k, v in sorted(status_counts.items(), key=lambda item: str(item[0])))],
            ["client latency", quantiles(client_latencies)],
            ["client first chunk", quantiles(first_chunks)],
            ["backend requests", len(backend_rows)],
            ["backend success", f"{backend_successes}/{len(backend_rows)} ({pct(backend_successes / len(backend_rows)) if backend_rows else '0.0%'})"],
            ["backend attempts", len(backend_attempts)],
            ["backend attempt statuses", ", ".join(f"{k}:{v}" for k, v in sorted(attempt_status_counts.items(), key=lambda item: str(item[0])))],
            ["backend request latency", quantiles(backend_latencies)],
            ["backend attempt latency", quantiles(attempt_latencies)],
            ["attempts per success", f"{len(backend_attempts) / client_successes:.2f}" if client_successes else "-"],
        ],
    )

    pressure = md_table(
        ["metric", "value"],
        [
            ["queue wait", quantiles(queue_waits)],
            ["queue waited requests", f"{sum(1 for v in queue_waits if v > 0)}/{len(queue_waits)}"],
            ["mean queue wait", f"{mean(queue_waits):.3f}s" if queue_waits else "-"],
            ["cooldown wait", quantiles(cooldown_waits)],
            ["cooldown set", f"{len(cooldown_sets)}/{len(backend_rows)}"],
            ["retry-after", quantiles(retry_afters)],
        ],
    )

    rows = []
    for index, row in enumerate(backend_rows, 1):
        attempts = row.get("attempts", [])
        rows.append(
            [
                index,
                row.get("ok"),
                len(attempts),
                pressure_value(row, "queue_wait_s"),
                pressure_value(row, "cooldown_wait_s"),
                pressure_value(row, "busy_cooldown_set_s"),
                row.get("elapsed_s"),
                ",".join(str(attempt.get("status_code")) for attempt in attempts),
            ]
        )

    content = "\n\n".join(
        [
            "# Gateway dogfood aggregate",
            "Scope: local gateway, single account, OpenAI-compatible streaming client path. Raw ledgers are not committed.",
            "## Overview",
            overview,
            "## Pressure observations",
            pressure,
            "## Backend request rows",
            md_table(["#", "ok", "attempts", "queue_wait_s", "cooldown_wait_s", "cooldown_set_s", "elapsed_s", "attempt_statuses"], rows),
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
