#!/usr/bin/env python3
"""Replay gateway strategies against recorded MAAS attempt ledgers.

This is an offline experiment. It does not call the provider. The goal is to
compare strategy shapes on the same observed busy/success trace before spending
more real requests.
"""

from __future__ import annotations

import argparse
import bisect
import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from statistics import mean, median
from typing import Any, Literal


Interface = Literal["openai", "anthropic"]


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
                    "first_chunk_latency_s": None,
                    "finish_class": "ok" if attempt.get("ok") else "http_error",
                    "error_class": attempt.get("error_type"),
                    "payload_hash": row.get("payload_sha256_16"),
                    "response_id": attempt.get("response_id"),
                    "_source": row.get("_source"),
                }
            )
    return out


def parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def pct(value: float) -> str:
    return f"{100 * value:.1f}%"


def wilson(successes: int, total: int, z: float = 1.96) -> tuple[float, float]:
    if total == 0:
        return 0.0, 0.0
    phat = successes / total
    denom = 1 + z * z / total
    centre = phat + z * z / (2 * total)
    margin = z * math.sqrt((phat * (1 - phat) + z * z / (4 * total)) / total)
    return (centre - margin) / denom, (centre + margin) / denom


def rate_cell(successes: int, total: int) -> str:
    if total == 0:
        return "0/0"
    lo, hi = wilson(successes, total)
    return f"{successes}/{total} ({pct(successes / total)}, 95% CI {pct(lo)}-{pct(hi)})"


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


def latency(row: dict[str, Any]) -> float:
    value = row.get("latency_s")
    return float(value) if isinstance(value, (int, float)) else 0.0


def is_probe(row: dict[str, Any]) -> bool:
    return row.get("run_id") != "gateway-ledger"


def direct_nonstream_probe(rows: list[dict[str, Any]], *, concurrency: int | None = None) -> list[dict[str, Any]]:
    selected = [
        row
        for row in rows
        if is_probe(row)
        and not row.get("stream")
        and row.get("route_label") == "direct"
        and row.get("interface") in {"openai", "anthropic"}
        and isinstance(row.get("latency_s"), (int, float))
    ]
    if concurrency is not None:
        selected = [row for row in selected if row.get("concurrency") == concurrency]
    return sorted(selected, key=lambda row: (parse_ts(row.get("timestamp")) or datetime.min, row.get("sequence_no") or 0))


def balanced_direct_trace(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return direct conc=1 runs that sampled both protocol faces.

    Single-face baseline blocks are useful for raw success rates, but they bias
    interface-order replay because a synthetic OpenAI-only policy can jump over
    an Anthropic-only block. Strategy replay should use traces where both faces
    were sampled in the same run.
    """

    direct = direct_nonstream_probe(rows, concurrency=1)
    by_run: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in direct:
        by_run[str(row.get("run_id"))].append(row)
    balanced: list[dict[str, Any]] = []
    for run_rows in by_run.values():
        interfaces = {row.get("interface") for row in run_rows}
        if {"openai", "anthropic"} <= interfaces:
            balanced.extend(run_rows)
    return sorted(balanced, key=lambda row: (parse_ts(row.get("timestamp")) or datetime.min, row.get("sequence_no") or 0))


def delay_for(attempt_index: int, current: Interface, previous: Interface) -> float:
    if attempt_index == 0:
        return 0.0
    base = 0.8 if current == previous else 1.2
    return min(3.0, base * (1.5 ** max(0, attempt_index - 1)))


@dataclass(frozen=True)
class ReplayResult:
    ok: bool
    attempts: int
    backend_latency_s: float
    wall_s: float
    all_busy: bool


def sequence_result(sequence: list[Interface], pair: dict[Interface, dict[str, Any]]) -> ReplayResult:
    backend = 0.0
    wall = 0.0
    busy = True
    previous = sequence[0]
    for index, interface in enumerate(sequence):
        row = pair[interface]
        wait = delay_for(index, interface, previous)
        backend += latency(row)
        wall += wait + latency(row)
        if row.get("ok"):
            return ReplayResult(True, index + 1, backend, wall, False)
        if str(row.get("provider_error_code")) != "10310":
            busy = False
        previous = interface
    return ReplayResult(False, len(sequence), backend, wall, busy)


def hedge_result(pair: dict[Interface, dict[str, Any]]) -> ReplayResult:
    attempts = [pair["openai"], pair["anthropic"]]
    successes = [latency(row) for row in attempts if row.get("ok")]
    backend = sum(latency(row) for row in attempts)
    if successes:
        return ReplayResult(True, 2, backend, min(successes), False)
    all_busy = all(str(row.get("provider_error_code")) == "10310" for row in attempts)
    return ReplayResult(False, 2, backend, max(latency(row) for row in attempts), all_busy)


def summarize_results(results: list[ReplayResult]) -> list[Any]:
    successes = sum(1 for result in results if result.ok)
    all_busy = sum(1 for result in results if result.all_busy)
    attempts = [result.attempts for result in results]
    backend = [result.backend_latency_s for result in results]
    wall = [result.wall_s for result in results]
    return [
        rate_cell(successes, len(results)),
        f"{mean(attempts):.2f}" if attempts else "-",
        f"{all_busy}/{len(results)}",
        quantiles(backend),
        quantiles(wall),
    ]


def paired_strategy_section(rows: list[dict[str, Any]]) -> str:
    pairs: dict[tuple[str, str], dict[Interface, dict[str, Any]]] = defaultdict(dict)
    for row in direct_nonstream_probe(rows, concurrency=1):
        pair_id = row.get("pair_id")
        interface = row.get("interface")
        if pair_id and interface in {"openai", "anthropic"}:
            pairs[(str(row.get("run_id")), str(pair_id))][interface] = row  # type: ignore[index]

    complete = [pair for pair in pairs.values() if {"openai", "anthropic"} <= set(pair)]
    if not complete:
        return "- No complete paired windows."

    strategies: list[tuple[str, Any]] = [
        ("OpenAI only", lambda p: sequence_result(["openai"], p)),
        ("Anthropic only", lambda p: sequence_result(["anthropic"], p)),
        ("OpenAI -> Anthropic", lambda p: sequence_result(["openai", "anthropic"], p)),
        ("Anthropic -> OpenAI", lambda p: sequence_result(["anthropic", "openai"], p)),
        ("Parallel both (upper bound)", hedge_result),
    ]
    table_rows = []
    for name, fn in strategies:
        results = [fn(pair) for pair in complete]
        table_rows.append([name, *summarize_results(results)])

    return "\n\n".join(
        [
            f"- Samples: {len(complete)} same-window direct paired probes.",
            md_table(
                ["strategy", "success", "mean attempts", "all-busy", "backend latency", "wall/first-success time"],
                table_rows,
            ),
        ]
    )


def make_sequence(name: str, budget: int) -> list[Interface]:
    if name == "openai-only":
        return ["openai"] * budget
    if name == "anthropic-only":
        return ["anthropic"] * budget
    if name == "native-openai":
        base: list[Interface] = ["openai", "openai", "anthropic", "openai", "anthropic", "openai", "anthropic"]
        return base[:budget]
    if name == "strict-openai-alt":
        return ["openai" if i % 2 == 0 else "anthropic" for i in range(budget)]
    if name == "anthropic-first":
        base = ["anthropic", "anthropic", "openai", "anthropic", "openai", "anthropic", "openai"]
        return base[:budget]
    raise ValueError(name)


def trace_replay(rows: list[dict[str, Any]], sequence: list[Interface], start: int) -> ReplayResult | None:
    indexed = list(enumerate(rows))
    positions: dict[Interface, list[int]] = {"openai": [], "anthropic": []}
    row_by_index: dict[int, dict[str, Any]] = {}
    for idx, row in indexed:
        interface = row.get("interface")
        if interface in positions:
            positions[interface].append(idx)  # type: ignore[index]
            row_by_index[idx] = row

    cursor = start
    backend = 0.0
    wall = 0.0
    previous = sequence[0]
    busy = True
    used = 0
    for attempt_index, interface in enumerate(sequence):
        candidates = positions[interface]
        pos = bisect.bisect_left(candidates, cursor)
        if pos >= len(candidates):
            return None
        row_index = candidates[pos]
        row = row_by_index[row_index]
        wait = delay_for(attempt_index, interface, previous)
        backend += latency(row)
        wall += wait + latency(row)
        used += 1
        cursor = row_index + 1
        if row.get("ok"):
            return ReplayResult(True, used, backend, wall, False)
        if str(row.get("provider_error_code")) != "10310":
            busy = False
        previous = interface
    return ReplayResult(False, used, backend, wall, busy)


def trace_strategy_section(rows: list[dict[str, Any]]) -> str:
    trace = balanced_direct_trace(rows)
    strategies = [
        ("openai-only", "OpenAI only x5", 5),
        ("anthropic-only", "Anthropic only x5", 5),
        ("native-openai", "Current default shape x5", 5),
        ("strict-openai-alt", "Strict OpenAI/Anthropic alternation x5", 5),
        ("anthropic-first", "Anthropic-first shape x5", 5),
        ("native-openai", "Current default shape x7", 7),
    ]
    table_rows: list[list[Any]] = []
    for key, label, budget in strategies:
        sequence = make_sequence(key, budget)
        results = [result for start in range(len(trace)) if (result := trace_replay(trace, sequence, start)) is not None]
        table_rows.append([label, " -> ".join(sequence), len(results), *summarize_results(results)])

    return "\n\n".join(
        [
            f"- Trace: {len(trace)} direct, non-stream, concurrency=1 attempts from runs that sampled both OpenAI and Anthropic. Near-end starts without enough future samples are excluded per strategy.",
            "- This is still an approximate replay; use it to compare strategy shapes, not to declare a precise rate limit.",
            md_table(
                ["strategy", "attempt order", "starts", "success", "mean attempts", "all-busy", "backend latency", "wall time"],
                table_rows,
            ),
        ]
    )


def cooldown_section(rows: list[dict[str, Any]]) -> str:
    trace = direct_nonstream_probe(rows, concurrency=1)
    with_ts = [(parse_ts(row.get("timestamp")), row) for row in trace]
    with_ts = [(ts, row) for ts, row in with_ts if ts is not None]
    table: dict[str, list[bool]] = defaultdict(list)
    buckets = [
        (0.5, "<0.5s"),
        (1.0, "0.5-1s"),
        (2.0, "1-2s"),
        (4.0, "2-4s"),
        (999999.0, ">=4s"),
    ]
    for (prev_ts, prev), (next_ts, nxt) in zip(with_ts, with_ts[1:]):
        if str(prev.get("provider_error_code")) != "10310":
            continue
        assert prev_ts is not None and next_ts is not None
        gap = max(0.0, (next_ts - prev_ts).total_seconds())
        label = next(label for limit, label in buckets if gap < limit)
        table[label].append(bool(nxt.get("ok")))

    rows_out = []
    for _, label in buckets:
        vals = table.get(label, [])
        rows_out.append([label, len(vals), rate_cell(sum(vals), len(vals)) if vals else "-"])

    streaks: Counter[int] = Counter()
    current = 0
    for row in trace:
        if str(row.get("provider_error_code")) == "10310":
            current += 1
            continue
        if current:
            streaks[current] += 1
            current = 0
    if current:
        streaks[current] += 1
    streak_rows = [[length, count] for length, count in sorted(streaks.items())]

    return "\n\n".join(
        [
            "The probe timestamp is written after each response, so this is a coarse signal rather than a causal cooldown proof.",
            md_table(["gap after busy", "next samples", "next attempt success"], rows_out),
            md_table(["busy streak length", "count"], streak_rows),
        ]
    )


def concurrency_section(rows: list[dict[str, Any]]) -> str:
    direct = direct_nonstream_probe(rows)
    groups: dict[tuple[Any, Any], list[dict[str, Any]]] = defaultdict(list)
    for row in direct:
        groups[(row.get("concurrency"), row.get("interface"))].append(row)
    table_rows = []
    for key, vals in sorted(groups.items(), key=lambda item: str(item[0])):
        successes = sum(1 for row in vals if row.get("ok"))
        busy = sum(1 for row in vals if str(row.get("provider_error_code")) == "10310")
        table_rows.append([key[0], key[1], rate_cell(successes, len(vals)), busy, quantiles([latency(row) for row in vals])])
    return md_table(["concurrency", "interface", "success", "10310", "latency"], table_rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ledgers", nargs="+", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    rows = normalise(load_rows(args.ledgers))
    content = "\n\n".join(
        [
            "# MAAS strategy replay",
            "Offline replay against recorded ledgers. Scope: single-account only, one model, no new provider requests.",
            "## Same-window paired strategy replay",
            paired_strategy_section(rows),
            "## Trace replay of retry orders",
            trace_strategy_section(rows),
            "## Cooldown signal after `503/10310`",
            cooldown_section(rows),
            "## Concurrency signal",
            concurrency_section(rows),
            "## Interpretation",
            "\n".join(
                [
                    "- Cross-interface fallback is useful, but only for windows where one compatible face succeeds and the other fails.",
                    "- Same-window both-fail is not a final single-account success ceiling. It only means protocol conversion cannot help at that instant; cooldown, serial retry, and client retry wait for a later window.",
                    "- Always-on parallel hedging is a latency upper-bound strategy, not the default: it consumes two backend attempts per user request.",
                    "- The next implementation target should be single-account pressure control: global queue, adaptive face ordering, and short cooldown after repeated `503/10310`.",
                    "- Route/IP randomization and precise RPM/TPM fitting are lower-priority for this single-account project because the provider exposes pressure as the same `503/10310` signal.",
                    "- These replay numbers are not causal rate-limit measurements. They are a cheap filter for which strategies deserve the next gentle live probe.",
                ]
            ),
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
