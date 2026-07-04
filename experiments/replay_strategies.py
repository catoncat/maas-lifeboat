#!/usr/bin/env python3
"""基于已记录的 MAAS attempt ledger 离线回放 gateway 策略。

这个脚本不调用 provider。它的作用是在消耗新请求前，用同一段
busy/success 轨迹先筛掉明显不值得上线实测的策略。
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
    return f"{successes}/{total} ({pct(successes / total)}，95% 置信区间 {pct(lo)}-{pct(hi)})"


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
    """返回 direct、并发 1、且同一 run 里同时采样两个协议入口的轨迹。

    单接口 baseline 适合看原始成功率，但会让接口顺序 replay 偏掉：
    例如一个合成的 OpenAI-only 策略可能跨过整段 Anthropic-only 样本。
    所以策略回放只使用同一 run 内两个入口都有采样的轨迹。
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


def paired_windows(rows: list[dict[str, Any]]) -> list[dict[Interface, dict[str, Any]]]:
    pairs: dict[tuple[str, str], dict[Interface, dict[str, Any]]] = defaultdict(dict)
    pair_times: dict[tuple[str, str], datetime] = {}
    for row in direct_nonstream_probe(rows, concurrency=1):
        pair_id = row.get("pair_id")
        interface = row.get("interface")
        if pair_id and interface in {"openai", "anthropic"}:
            key = (str(row.get("run_id")), str(pair_id))
            pairs[key][interface] = row  # type: ignore[index]
            ts = parse_ts(row.get("timestamp"))
            if ts is not None:
                pair_times[key] = min(pair_times.get(key, ts), ts)
    ordered = sorted(pairs.items(), key=lambda item: (pair_times.get(item[0]) or datetime.min, item[0]))
    return [pair for _, pair in ordered if {"openai", "anthropic"} <= set(pair)]


def paired_strategy_section(rows: list[dict[str, Any]]) -> str:
    complete = paired_windows(rows)
    if not complete:
        return "- 没有完整 paired 窗口。"

    strategies: list[tuple[str, Any]] = [
        ("只用 OpenAI", lambda p: sequence_result(["openai"], p)),
        ("只用 Anthropic", lambda p: sequence_result(["anthropic"], p)),
        ("OpenAI -> Anthropic", lambda p: sequence_result(["openai", "anthropic"], p)),
        ("Anthropic -> OpenAI", lambda p: sequence_result(["anthropic", "openai"], p)),
        ("两个入口并行（上界）", hedge_result),
    ]
    table_rows = []
    for name, fn in strategies:
        results = [fn(pair) for pair in complete]
        table_rows.append([name, *summarize_results(results)])

    return "\n\n".join(
        [
            f"- 样本：{len(complete)} 个同窗口 direct paired probe。",
            md_table(
                ["策略", "成功", "平均 attempts", "all-busy", "后端耗时", "墙钟/首个成功耗时"],
                table_rows,
            ),
        ]
    )


def update_score(score: float, ok: bool, alpha: float) -> float:
    return alpha * (1.0 if ok else 0.0) + (1.0 - alpha) * score


def ewma_paired_replay(pairs: list[dict[Interface, dict[str, Any]]], *, alpha: float, budget: int) -> tuple[list[ReplayResult], Counter[str]]:
    scores: dict[Interface, float] = {"openai": 0.5, "anthropic": 0.5}
    results: list[ReplayResult] = []
    picks: Counter[str] = Counter()
    for pair in pairs:
        first: Interface = "openai" if scores["openai"] >= scores["anthropic"] else "anthropic"
        sequence = [first]
        if budget > 1:
            sequence.append("anthropic" if first == "openai" else "openai")
        result = sequence_result(sequence, pair)
        results.append(result)
        picks[first] += 1
        for attempt_index, interface in enumerate(sequence):
            row = pair[interface]
            scores[interface] = update_score(scores[interface], bool(row.get("ok")), alpha)
            if row.get("ok"):
                break
    return results, picks


def ewma_section(rows: list[dict[str, Any]]) -> str:
    complete = paired_windows(rows)
    if not complete:
        return "- 没有完整 paired 窗口。"

    table_rows: list[list[Any]] = []
    baselines = [
        ("固定 OpenAI 优先，预算=1", lambda: ([sequence_result(["openai"], pair) for pair in complete], Counter({"openai": len(complete)}))),
        ("固定 Anthropic 优先，预算=1", lambda: ([sequence_result(["anthropic"], pair) for pair in complete], Counter({"anthropic": len(complete)}))),
        ("固定 OpenAI 优先，fallback 预算=2", lambda: ([sequence_result(["openai", "anthropic"], pair) for pair in complete], Counter({"openai": len(complete)}))),
        ("固定 Anthropic 优先，fallback 预算=2", lambda: ([sequence_result(["anthropic", "openai"], pair) for pair in complete], Counter({"anthropic": len(complete)}))),
    ]
    for label, fn in baselines:
        results, picks = fn()
        table_rows.append([label, f"openai={picks['openai']}, anthropic={picks['anthropic']}", *summarize_results(results)])

    for alpha in (0.2, 0.35, 0.5, 0.8):
        for budget in (1, 2):
            results, picks = ewma_paired_replay(complete, alpha=alpha, budget=budget)
            table_rows.append([f"EWMA alpha={alpha:g}, budget={budget}", f"openai={picks['openai']}, anthropic={picks['anthropic']}", *summarize_results(results)])

    return "\n\n".join(
        [
            f"- 样本：{len(complete)} 个 paired 窗口，按在线方式回放：每次决策只能看到更早的窗口。",
            "- EWMA 只改变第一个接口。fallback 预算=2 时，它只能通过减少首选接口浪费来降低成本；同窗口两边都 busy 时，它救不了。",
            md_table(
                ["策略", "首选次数", "成功", "平均 attempts", "all-busy", "后端耗时", "墙钟/首个成功耗时"],
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
        ("openai-only", "只用 OpenAI x5", 5),
        ("anthropic-only", "只用 Anthropic x5", 5),
        ("native-openai", "当前默认形状 x5", 5),
        ("strict-openai-alt", "严格 OpenAI/Anthropic 交替 x5", 5),
        ("anthropic-first", "Anthropic 优先形状 x5", 5),
        ("native-openai", "当前默认形状 x7", 7),
    ]
    table_rows: list[list[Any]] = []
    for key, label, budget in strategies:
        sequence = make_sequence(key, budget)
        results = [result for start in range(len(trace)) if (result := trace_replay(trace, sequence, start)) is not None]
        table_rows.append([label, " -> ".join(sequence), len(results), *summarize_results(results)])

    return "\n\n".join(
        [
            f"- 轨迹：{len(trace)} 个 direct、非 stream、并发 1 的 attempts；只取同一 run 内同时采样 OpenAI 和 Anthropic 的样本。每个策略都会排除尾部未来样本不足的起点。",
            "- 这仍然只是近似 replay。它适合比较策略形状，不适合宣布精确 rate limit。",
            md_table(
                ["策略", "attempt 顺序", "起点数", "成功", "平均 attempts", "all-busy", "后端耗时", "墙钟耗时"],
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
            "probe 时间戳是在每个响应结束后写入的，所以这里是粗粒度信号，不是严格的 cooldown 因果证明。",
            md_table(["busy 后间隔", "后续样本数", "下一次 attempt 成功"], rows_out),
            md_table(["连续 busy 长度", "数量"], streak_rows),
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
    return md_table(["并发", "接口", "成功", "10310", "耗时"], table_rows)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ledgers", nargs="+", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    rows = normalise(load_rows(args.ledgers))
    content = "\n\n".join(
        [
            "# MAAS 策略 replay 报告",
            "这是基于已记录 ledger 的离线回放。范围：单账号、单模型、不发新 provider 请求。",
            "## 同窗口 paired 策略 replay",
            paired_strategy_section(rows),
            "## 在线 EWMA 接口顺序 replay",
            ewma_section(rows),
            "## 重试顺序 trace replay",
            trace_strategy_section(rows),
            "## `503/10310` 后的 cooldown 信号",
            cooldown_section(rows),
            "## 并发信号",
            concurrency_section(rows),
            "## 解读",
            "\n".join(
                [
                    "- 跨接口 fallback 有价值，但只救“一个兼容入口成功、另一个失败”的窗口。",
                    "- 同窗口 both-fail 不是单账号最终成功率天花板。它只说明协议转换在那个瞬间救不了；cooldown、串行重试和客户端 retry 等的是后面的窗口。",
                    "- EWMA 排序只有在能降低未来首选接口浪费时才有用。两个协议入口只是弱去相关，不是独立 provider，所以它应继续留在 replay/feature flag 后面。",
                    "- 常开并行 hedging 更像尾延迟上界策略，不适合默认：每个用户请求固定消耗两个后端 attempts。",
                    "- 下一步主线应是单账号压力控制：全局 queue、短 cooldown、再谨慎评估接口排序。",
                    "- route/IP 随机化和精确 RPM/TPM 拟合不是当前主线。provider 把压力统一暴露为 `503/10310`，单账号下很难分清路由收益和自然恢复窗口。",
                    "- 这些 replay 数字不是因果 rate-limit 测量，只是用来筛选哪些策略值得做下一轮温和实测。",
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
