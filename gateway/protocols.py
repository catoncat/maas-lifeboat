"""OpenAI/Anthropic payload and response conversion."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from . import config


def normalize_model(payload: dict[str, Any]) -> dict[str, Any]:
    out = dict(payload)
    out["model"] = config.MODEL
    return out


def parse_tool_arguments(raw: Any) -> Any:
    if isinstance(raw, str):
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"arguments": raw}
    return raw if raw is not None else {}


def normalize_anthropic_tool_call_id(value: Any) -> str:
    raw = str(value or f"toolu_{uuid.uuid4().hex}")
    if "|" in raw:
        raw = raw.split("|", 1)[0]
    normalized = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in raw)
    return (normalized or f"toolu_{uuid.uuid4().hex}")[:64]


def stringify_tool_result(content: Any) -> str:
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False)


def openai_content_to_anthropic(content: Any) -> Any:
    if not isinstance(content, list):
        return content if content is not None else ""
    blocks: list[dict[str, Any]] = []
    for item in content:
        if not isinstance(item, dict):
            blocks.append({"type": "text", "text": str(item)})
            continue
        kind = item.get("type")
        if kind == "text":
            blocks.append({"type": "text", "text": item.get("text", "")})
        elif kind in {"image_url", "input_image"}:
            image_url = item.get("image_url") or {}
            url = image_url.get("url") if isinstance(image_url, dict) else image_url
            blocks.append({"type": "image", "source": {"type": "url", "url": url}})
        else:
            blocks.append(item)
    return blocks


def anthropic_content_to_openai(content: Any) -> Any:
    if not isinstance(content, list):
        return content if content is not None else ""
    blocks: list[dict[str, Any]] = []
    for item in content:
        if not isinstance(item, dict):
            blocks.append({"type": "text", "text": str(item)})
            continue
        kind = item.get("type")
        if kind == "text":
            blocks.append({"type": "text", "text": item.get("text", "")})
        elif kind == "image":
            source = item.get("source") or {}
            if isinstance(source, dict) and source.get("type") == "url":
                blocks.append({"type": "image_url", "image_url": {"url": source.get("url", "")}})
            else:
                blocks.append(item)
        else:
            blocks.append(item)
    return blocks


def openai_tools_to_anthropic(tools: Any) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    if not isinstance(tools, list):
        return converted
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function") if tool.get("type") == "function" else tool
        if not isinstance(fn, dict) or not fn.get("name"):
            continue
        converted.append(
            {
                "name": fn["name"],
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
            }
        )
    return converted


def anthropic_tools_to_openai(tools: Any) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    if not isinstance(tools, list):
        return converted
    for tool in tools:
        if not isinstance(tool, dict) or not tool.get("name"):
            continue
        converted.append(
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
                },
            }
        )
    return converted


def openai_tool_choice_to_anthropic(choice: Any) -> Any:
    if choice in (None, "auto"):
        return "auto" if choice == "auto" else None
    if choice == "none":
        return {"type": "none"}
    if choice == "required":
        return {"type": "any"}
    if isinstance(choice, dict):
        fn = choice.get("function") or {}
        if choice.get("type") == "function" and isinstance(fn, dict) and fn.get("name"):
            return {"type": "tool", "name": fn["name"]}
    return None


def anthropic_tool_choice_to_openai(choice: Any) -> Any:
    if choice in (None, "auto"):
        return "auto" if choice == "auto" else None
    if isinstance(choice, dict):
        kind = choice.get("type")
        if kind == "none":
            return "none"
        if kind == "any":
            return "required"
        if kind == "tool" and choice.get("name"):
            return {"type": "function", "function": {"name": choice["name"]}}
    return None


def openai_to_anthropic(openai_payload: dict[str, Any]) -> dict[str, Any]:
    messages = openai_payload.get("messages") or []
    system_parts: list[str] = []
    anthropic_messages: list[dict[str, Any]] = []
    tool_call_id_map: dict[str, str] = {}
    pending_tool_results: list[dict[str, Any]] = []

    def flush_tool_results() -> None:
        nonlocal pending_tool_results
        if pending_tool_results:
            anthropic_messages.append({"role": "user", "content": pending_tool_results})
            pending_tool_results = []

    for message in messages:
        role = message.get("role")
        content = message.get("content", "")
        if role in {"developer", "system"}:
            flush_tool_results()
            if isinstance(content, str):
                system_parts.append(content)
            else:
                system_parts.append(json.dumps(content, ensure_ascii=False))
            continue
        if role == "tool":
            original_id = str(message.get("tool_call_id", ""))
            tool_use_id = tool_call_id_map.get(original_id) or normalize_anthropic_tool_call_id(original_id)
            pending_tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": stringify_tool_result(content),
                }
            )
            continue
        flush_tool_results()
        if role not in {"user", "assistant"}:
            role = "user"
        converted_content = openai_content_to_anthropic(content)
        if role == "assistant" and message.get("tool_calls"):
            blocks: list[dict[str, Any]] = []
            if isinstance(converted_content, str) and converted_content:
                blocks.append({"type": "text", "text": converted_content})
            elif isinstance(converted_content, list):
                blocks.extend(converted_content)
            for call in message.get("tool_calls") or []:
                if not isinstance(call, dict):
                    continue
                fn = call.get("function") or {}
                original_id = str(call.get("id", ""))
                normalized_id = normalize_anthropic_tool_call_id(original_id)
                if original_id:
                    tool_call_id_map[original_id] = normalized_id
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": normalized_id,
                        "name": fn.get("name", ""),
                        "input": parse_tool_arguments(fn.get("arguments", "{}")),
                    }
                )
            converted_content = blocks
        anthropic_messages.append({"role": role, "content": converted_content})
    flush_tool_results()
    converted: dict[str, Any] = {
        "model": config.MODEL,
        "messages": anthropic_messages or [{"role": "user", "content": ""}],
        "max_tokens": openai_payload.get("max_tokens") or openai_payload.get("max_completion_tokens") or 1024,
    }
    if "temperature" in openai_payload:
        converted["temperature"] = openai_payload["temperature"]
    if "stream" in openai_payload:
        converted["stream"] = openai_payload["stream"]
    if system_parts:
        converted["system"] = "\n\n".join(system_parts)
    tools = openai_tools_to_anthropic(openai_payload.get("tools"))
    if tools:
        converted["tools"] = tools
    tool_choice = openai_tool_choice_to_anthropic(openai_payload.get("tool_choice"))
    if tool_choice is not None:
        converted["tool_choice"] = tool_choice
    return converted


def anthropic_to_openai(anthropic_payload: dict[str, Any]) -> dict[str, Any]:
    messages = anthropic_payload.get("messages") or []
    openai_messages: list[dict[str, Any]] = []
    if anthropic_payload.get("system"):
        openai_messages.append({"role": "system", "content": anthropic_payload["system"]})
    for message in messages:
        role = message.get("role", "user")
        if role not in {"user", "assistant", "system"}:
            role = "user"
        content = message.get("content", "")
        if isinstance(content, list):
            text_blocks: list[dict[str, Any]] = []
            tool_calls: list[dict[str, Any]] = []
            for item in content:
                if not isinstance(item, dict):
                    text_blocks.append({"type": "text", "text": str(item)})
                    continue
                if item.get("type") == "tool_use":
                    tool_calls.append(
                        {
                            "id": item.get("id", f"call_{uuid.uuid4().hex}"),
                            "type": "function",
                            "function": {"name": item.get("name", ""), "arguments": json.dumps(item.get("input") or {}, ensure_ascii=False)},
                        }
                    )
                elif item.get("type") == "tool_result":
                    openai_messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": item.get("tool_use_id", ""),
                            "content": stringify_tool_result(item.get("content", "")),
                        }
                    )
                else:
                    text_blocks.append(item)
            if tool_calls:
                text_content = ""
                for block in text_blocks:
                    if isinstance(block, dict) and block.get("type") == "text":
                        text_content += str(block.get("text", ""))
                openai_messages.append({"role": "assistant", "content": text_content or None, "tool_calls": tool_calls})
                continue
            content = anthropic_content_to_openai(text_blocks)
        openai_messages.append({"role": role, "content": content})
    converted: dict[str, Any] = {
        "model": config.MODEL,
        "messages": openai_messages or [{"role": "user", "content": ""}],
        "max_tokens": anthropic_payload.get("max_tokens", 1024),
    }
    if "temperature" in anthropic_payload:
        converted["temperature"] = anthropic_payload["temperature"]
    if "stream" in anthropic_payload:
        converted["stream"] = anthropic_payload["stream"]
    tools = anthropic_tools_to_openai(anthropic_payload.get("tools"))
    if tools:
        converted["tools"] = tools
    tool_choice = anthropic_tool_choice_to_openai(anthropic_payload.get("tool_choice"))
    if tool_choice is not None:
        converted["tool_choice"] = tool_choice
    return converted


def anthropic_response_to_openai(obj: dict[str, Any]) -> dict[str, Any]:
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for item in obj.get("content", []):
        if isinstance(item, dict) and item.get("type") == "text":
            text_parts.append(str(item.get("text", "")))
        elif isinstance(item, dict) and item.get("type") == "tool_use":
            tool_calls.append(
                {
                    "id": item.get("id", f"call_{uuid.uuid4().hex}"),
                    "type": "function",
                    "function": {"name": item.get("name", ""), "arguments": json.dumps(item.get("input") or {}, ensure_ascii=False)},
                }
            )
    usage = obj.get("usage") or {}
    message: dict[str, Any] = {"role": "assistant", "content": "".join(text_parts) if text_parts else None}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": obj.get("id", f"chatcmpl-{uuid.uuid4().hex}"),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": obj.get("model", config.MODEL),
        "choices": [
            {
                "index": 0,
                "finish_reason": "tool_calls" if tool_calls else "stop",
                "message": message,
            }
        ],
        "usage": {
            "prompt_tokens": usage.get("input_tokens", 0),
            "completion_tokens": usage.get("output_tokens", 0),
            "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
        },
    }


def openai_response_to_anthropic(obj: dict[str, Any]) -> dict[str, Any]:
    choices = obj.get("choices") or []
    content_blocks: list[dict[str, Any]] = []
    if choices:
        message = choices[0].get("message") or {}
        content = message.get("content") or ""
        if content:
            content_blocks.append({"type": "text", "text": content})
        for call in message.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            fn = call.get("function") or {}
            content_blocks.append(
                {
                    "type": "tool_use",
                    "id": call.get("id", f"toolu_{uuid.uuid4().hex}"),
                    "name": fn.get("name", ""),
                    "input": parse_tool_arguments(fn.get("arguments", "{}")),
                }
            )
    usage = obj.get("usage") or {}
    return {
        "id": obj.get("id", f"msg_{uuid.uuid4().hex}"),
        "type": "message",
        "role": "assistant",
        "model": obj.get("model", config.MODEL),
        "content": content_blocks or [{"type": "text", "text": ""}],
        "stop_reason": "tool_use" if any(block.get("type") == "tool_use" for block in content_blocks) else "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }
