"""SSE helpers and streaming protocol converters."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, AsyncIterator

import httpx

from . import config


def sse(data: dict[str, Any] | str, event: str | None = None) -> bytes:
    prefix = f"event: {event}\n" if event else ""
    payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    return f"{prefix}data: {payload}\n\n".encode("utf-8")


def openai_chunk(chunk_id: str, model: str, delta: dict[str, Any], finish_reason: str | None = None) -> bytes:
    return sse(
        {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
        }
    )


def reasoning_delta_text(delta: dict[str, Any]) -> str:
    value = delta.get("reasoning_content")
    if value is None:
        value = delta.get("reasoning")
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("content", "text", "reasoning_content", "summary"):
            if value.get(key):
                return str(value[key])
    return str(value)


async def iter_sse_json(response: httpx.Response) -> AsyncIterator[tuple[str | None, dict[str, Any]]]:
    buffer = ""
    async for text in response.aiter_text():
        if not text:
            continue
        buffer += text.replace("\r\n", "\n").replace("\r", "\n")
        while "\n\n" in buffer:
            raw, buffer = buffer.split("\n\n", 1)
            event_name: str | None = None
            data_lines: list[str] = []
            for line in raw.split("\n"):
                if line.startswith("event:"):
                    event_name = line[6:].strip()
                elif line.startswith("data:"):
                    data_lines.append(line[5:].lstrip())
            if not data_lines:
                continue
            data = "\n".join(data_lines)
            if data == "[DONE]":
                continue
            try:
                yield event_name, json.loads(data)
            except json.JSONDecodeError:
                continue


async def anthropic_stream_to_openai(response: httpx.Response) -> AsyncIterator[bytes]:
    chunk_id = f"chatcmpl-{uuid.uuid4().hex}"
    model = config.MODEL
    role_sent = False
    final_sent = False
    saw_tool = False
    tool_index_by_content_index: dict[int, int] = {}

    async for _, event in iter_sse_json(response):
        event_type = event.get("type")
        if event_type == "message_start":
            message = event.get("message") or {}
            chunk_id = message.get("id") or chunk_id
            model = message.get("model") or model
            role_sent = True
            yield openai_chunk(chunk_id, model, {"role": "assistant"})
            continue

        if not role_sent:
            role_sent = True
            yield openai_chunk(chunk_id, model, {"role": "assistant"})

        if event_type == "content_block_start":
            content_index = int(event.get("index", 0))
            block = event.get("content_block") or {}
            block_type = block.get("type")
            if block_type == "text":
                text = block.get("text") or ""
                if text:
                    yield openai_chunk(chunk_id, model, {"content": text})
            elif block_type == "thinking":
                thinking = block.get("thinking") or block.get("text") or ""
                if thinking:
                    yield openai_chunk(chunk_id, model, {"reasoning_content": thinking})
            elif block_type == "tool_use":
                saw_tool = True
                tool_index = len(tool_index_by_content_index)
                tool_index_by_content_index[content_index] = tool_index
                name = block.get("name", "")
                arguments = json.dumps(block.get("input") or {}, ensure_ascii=False) if block.get("input") else ""
                yield openai_chunk(
                    chunk_id,
                    model,
                    {
                        "tool_calls": [
                            {
                                "index": tool_index,
                                "id": block.get("id", f"call_{uuid.uuid4().hex}"),
                                "type": "function",
                                "function": {"name": name, "arguments": arguments},
                            }
                        ]
                    },
                )
            continue

        if event_type == "content_block_delta":
            content_index = int(event.get("index", 0))
            delta = event.get("delta") or {}
            delta_type = delta.get("type")
            if delta_type == "text_delta":
                text = delta.get("text") or ""
                if text:
                    yield openai_chunk(chunk_id, model, {"content": text})
            elif delta_type == "thinking_delta":
                thinking = delta.get("thinking") or delta.get("text") or ""
                if thinking:
                    yield openai_chunk(chunk_id, model, {"reasoning_content": thinking})
            elif delta_type == "input_json_delta":
                saw_tool = True
                tool_index = tool_index_by_content_index.get(content_index, content_index)
                partial_json = delta.get("partial_json") or ""
                if partial_json:
                    yield openai_chunk(
                        chunk_id,
                        model,
                        {"tool_calls": [{"index": tool_index, "function": {"arguments": partial_json}}]},
                    )
            continue

        if event_type == "message_delta":
            delta = event.get("delta") or {}
            stop_reason = delta.get("stop_reason")
            if stop_reason:
                final_sent = True
                finish_reason = "tool_calls" if stop_reason == "tool_use" or saw_tool else "length" if stop_reason == "max_tokens" else "stop"
                yield openai_chunk(chunk_id, model, {}, finish_reason)
            continue

        if event_type == "message_stop":
            if not final_sent:
                final_sent = True
                yield openai_chunk(chunk_id, model, {}, "tool_calls" if saw_tool else "stop")

    if not final_sent:
        yield openai_chunk(chunk_id, model, {}, "tool_calls" if saw_tool else "stop")
    yield b"data: [DONE]\n\n"


async def openai_stream_to_anthropic(response: httpx.Response) -> AsyncIterator[bytes]:
    message_id = f"msg_{uuid.uuid4().hex}"
    model = config.MODEL
    started = False
    next_block_index = 0
    text_block_index: int | None = None
    thinking_block_index: int | None = None
    tool_blocks: dict[int, int] = {}
    open_blocks: set[int] = set()
    stop_reason = "end_turn"

    def message_start() -> bytes:
        return sse(
            {
                "type": "message_start",
                "message": {
                    "id": message_id,
                    "type": "message",
                    "role": "assistant",
                    "model": model,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            },
            event="message_start",
        )

    async for _, event in iter_sse_json(response):
        if event.get("id"):
            message_id = str(event["id"])
        if event.get("model"):
            model = str(event["model"])
        if not started:
            started = True
            yield message_start()

        choice = (event.get("choices") or [{}])[0]
        delta = choice.get("delta") or {}
        reasoning = reasoning_delta_text(delta)
        if reasoning:
            if thinking_block_index is None:
                thinking_block_index = next_block_index
                next_block_index += 1
                open_blocks.add(thinking_block_index)
                yield sse(
                    {"type": "content_block_start", "index": thinking_block_index, "content_block": {"type": "thinking", "thinking": ""}},
                    event="content_block_start",
                )
            yield sse(
                {"type": "content_block_delta", "index": thinking_block_index, "delta": {"type": "thinking_delta", "thinking": reasoning}},
                event="content_block_delta",
            )

        content = delta.get("content")
        if content:
            if text_block_index is None:
                text_block_index = next_block_index
                next_block_index += 1
                open_blocks.add(text_block_index)
                yield sse({"type": "content_block_start", "index": text_block_index, "content_block": {"type": "text", "text": ""}}, event="content_block_start")
            yield sse({"type": "content_block_delta", "index": text_block_index, "delta": {"type": "text_delta", "text": content}}, event="content_block_delta")

        for call in delta.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            source_index = int(call.get("index", 0))
            fn = call.get("function") or {}
            if source_index not in tool_blocks:
                block_index = next_block_index
                tool_blocks[source_index] = block_index
                next_block_index += 1
                open_blocks.add(block_index)
                yield sse(
                    {
                        "type": "content_block_start",
                        "index": block_index,
                        "content_block": {
                            "type": "tool_use",
                            "id": call.get("id", f"toolu_{uuid.uuid4().hex}"),
                            "name": fn.get("name", ""),
                            "input": {},
                        },
                    },
                    event="content_block_start",
                )
            arguments = fn.get("arguments")
            if arguments:
                yield sse(
                    {"type": "content_block_delta", "index": tool_blocks[source_index], "delta": {"type": "input_json_delta", "partial_json": arguments}},
                    event="content_block_delta",
                )

        finish_reason = choice.get("finish_reason")
        if finish_reason:
            if finish_reason == "tool_calls":
                stop_reason = "tool_use"
            elif finish_reason == "length":
                stop_reason = "max_tokens"
            else:
                stop_reason = "end_turn"

    if not started:
        started = True
        yield message_start()
    for block_index in sorted(open_blocks):
        yield sse({"type": "content_block_stop", "index": block_index}, event="content_block_stop")
    yield sse(
        {"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None}, "usage": {"output_tokens": 0}},
        event="message_delta",
    )
    yield sse({"type": "message_stop"}, event="message_stop")
