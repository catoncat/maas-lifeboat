import asyncio
import json

import httpx
from fastapi.testclient import TestClient

from gateway import config
from gateway.app import make_app
from gateway.pressure import AccountPressureGate
from gateway.protocols import (
    anthropic_response_to_openai,
    anthropic_to_openai,
    openai_response_to_anthropic,
    openai_to_anthropic,
)
from gateway.sse import anthropic_stream_to_openai, openai_stream_to_anthropic
from gateway.strategy import MaasGateway, attempt_interfaces
from gateway.types import (
    AttemptResult,
    PreparedStream,
    PreparedStreamFailure,
)


def run(coro):
    return asyncio.run(coro)


def test_openai_to_anthropic_moves_system_messages():
    payload = {
        "model": "x",
        "messages": [
            {"role": "system", "content": "be terse"},
            {"role": "user", "content": "hi"},
        ],
        "max_tokens": 8,
    }
    out = openai_to_anthropic(payload)
    assert out["system"] == "be terse"
    assert out["messages"] == [{"role": "user", "content": "hi"}]
    assert out["max_tokens"] == 8


def test_anthropic_to_openai_preserves_system():
    payload = {"system": "be terse", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 8}
    out = anthropic_to_openai(payload)
    assert out["messages"][0] == {"role": "system", "content": "be terse"}
    assert out["messages"][1] == {"role": "user", "content": "hi"}


def test_response_conversions():
    openai = anthropic_response_to_openai(
        {"id": "msg1", "model": "m", "content": [{"type": "text", "text": "OK"}], "usage": {"input_tokens": 2, "output_tokens": 1}}
    )
    assert openai["choices"][0]["message"]["content"] == "OK"
    anthropic = openai_response_to_anthropic(
        {"id": "chat1", "model": "m", "choices": [{"message": {"content": "OK"}}], "usage": {"prompt_tokens": 2, "completion_tokens": 1}}
    )
    assert anthropic["content"][0]["text"] == "OK"


def test_request_conversions_preserve_thinking_controls():
    controls = {
        "options": {"enable_thinking": True},
        "thinking": {"type": "enabled", "budget_tokens": 64000},
    }

    anthropic = openai_to_anthropic(
        {
            "model": "x",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 8,
            **controls,
        }
    )
    assert anthropic["options"] == controls["options"]
    assert anthropic["thinking"] == controls["thinking"]

    openai = anthropic_to_openai(
        {
            "model": "x",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 8,
            **controls,
        }
    )
    assert openai["options"] == controls["options"]
    assert openai["thinking"] == controls["thinking"]


def test_request_conversion_enables_options_when_thinking_is_enabled():
    out = openai_to_anthropic(
        {
            "model": "x",
            "messages": [{"role": "user", "content": "hi"}],
            "thinking": {"type": "enabled", "budget_tokens": 1000},
        }
    )
    assert out["thinking"] == {"type": "enabled", "budget_tokens": 1000}
    assert out["options"] == {"enable_thinking": True}


def test_response_conversions_preserve_thinking_content():
    openai = anthropic_response_to_openai(
        {
            "id": "msg1",
            "model": "m",
            "content": [{"type": "thinking", "thinking": "think first"}, {"type": "text", "text": "OK"}],
            "usage": {"input_tokens": 2, "output_tokens": 4},
        }
    )
    message = openai["choices"][0]["message"]
    assert message["reasoning_content"] == "think first"
    assert message["content"] == "OK"

    anthropic = openai_response_to_anthropic(
        {
            "id": "chat1",
            "model": "m",
            "choices": [{"message": {"reasoning_content": "think first", "content": "OK"}}],
            "usage": {"prompt_tokens": 2, "completion_tokens": 4},
        }
    )
    assert anthropic["content"][0] == {"type": "thinking", "thinking": "think first"}
    assert anthropic["content"][1] == {"type": "text", "text": "OK"}


def test_openai_to_anthropic_preserves_tools_and_tool_history():
    payload = {
        "model": "x",
        "messages": [
            {"role": "system", "content": "be precise"},
            {"role": "user", "content": "list files"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call-1|very/long+provider=id",
                        "type": "function",
                        "function": {"name": "bash", "arguments": "{\"cmd\":\"ls\"}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call-1|very/long+provider=id", "content": "README.md"},
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "bash",
                    "description": "Run shell",
                    "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]},
                },
            }
        ],
        "tool_choice": {"type": "function", "function": {"name": "bash"}},
        "max_tokens": 32,
    }

    out = openai_to_anthropic(payload)

    assert out["system"] == "be precise"
    assert out["tools"] == [
        {
            "name": "bash",
            "description": "Run shell",
            "input_schema": {"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]},
        }
    ]
    assert out["tool_choice"] == {"type": "tool", "name": "bash"}
    tool_use = out["messages"][1]["content"][0]
    assert tool_use == {"type": "tool_use", "id": "call-1", "name": "bash", "input": {"cmd": "ls"}}
    assert out["messages"][2] == {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "call-1", "content": "README.md"}]}


def test_anthropic_to_openai_preserves_tools_and_tool_history():
    payload = {
        "system": "be precise",
        "messages": [
            {"role": "user", "content": "list files"},
            {"role": "assistant", "content": [{"type": "tool_use", "id": "toolu_1", "name": "bash", "input": {"cmd": "ls"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "README.md"}]},
        ],
        "tools": [{"name": "bash", "description": "Run shell", "input_schema": {"type": "object", "properties": {"cmd": {"type": "string"}}}}],
        "tool_choice": {"type": "tool", "name": "bash"},
        "max_tokens": 32,
    }

    out = anthropic_to_openai(payload)

    assert out["tools"] == [
        {
            "type": "function",
            "function": {"name": "bash", "description": "Run shell", "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}}},
        }
    ]
    assert out["tool_choice"] == {"type": "function", "function": {"name": "bash"}}
    assert out["messages"][1]["role"] == "user"
    assert out["messages"][2] == {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": "toolu_1", "type": "function", "function": {"name": "bash", "arguments": "{\"cmd\": \"ls\"}"}}],
    }
    assert out["messages"][3] == {"role": "tool", "tool_call_id": "toolu_1", "content": "README.md"}


def test_response_conversions_preserve_tool_calls():
    openai = anthropic_response_to_openai(
        {
            "id": "msg1",
            "model": "m",
            "content": [{"type": "tool_use", "id": "toolu_1", "name": "bash", "input": {"cmd": "pwd"}}],
            "usage": {"input_tokens": 2, "output_tokens": 1},
        }
    )
    assert openai["choices"][0]["finish_reason"] == "tool_calls"
    assert openai["choices"][0]["message"]["tool_calls"][0]["function"] == {"name": "bash", "arguments": "{\"cmd\": \"pwd\"}"}

    anthropic = openai_response_to_anthropic(
        {
            "id": "chat1",
            "model": "m",
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "bash", "arguments": "{\"cmd\":\"pwd\"}"}}],
                    }
                }
            ],
            "usage": {"prompt_tokens": 2, "completion_tokens": 1},
        }
    )
    assert anthropic["stop_reason"] == "tool_use"
    assert anthropic["content"][0] == {"type": "tool_use", "id": "call_1", "name": "bash", "input": {"cmd": "pwd"}}


def test_anthropic_stream_to_openai_preserves_tool_calls():
    response = httpx.Response(
        200,
        content=(
            b'event: message_start\n'
            b'data: {"type":"message_start","message":{"id":"msg1","model":"m","content":[]}}\n\n'
            b'event: content_block_start\n'
            b'data: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"toolu_1","name":"bash","input":{"cmd":"pwd"}}}\n\n'
            b'event: message_delta\n'
            b'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"}}\n\n'
            b'event: message_stop\n'
            b'data: {"type":"message_stop"}\n\n'
        ),
    )
    chunks = run(_collect(anthropic_stream_to_openai(response)))
    body = b"".join(chunks).decode()
    events = []
    for part in body.split("\n\n"):
        if part.startswith("data: {"):
            events.append(json.loads(part.removeprefix("data: ")))
    tool_delta = events[1]["choices"][0]["delta"]["tool_calls"][0]
    assert tool_delta["id"] == "toolu_1"
    assert tool_delta["function"]["name"] == "bash"
    assert json.loads(tool_delta["function"]["arguments"]) == {"cmd": "pwd"}
    assert events[-1]["choices"][0]["finish_reason"] == "tool_calls"
    assert "data: [DONE]" in body


def test_anthropic_stream_to_openai_preserves_thinking_delta():
    response = httpx.Response(
        200,
        content=(
            b'event: message_start\n'
            b'data: {"type":"message_start","message":{"id":"msg1","model":"m","content":[]}}\n\n'
            b'event: content_block_start\n'
            b'data: {"type":"content_block_start","index":0,"content_block":{"type":"thinking","thinking":""}}\n\n'
            b'event: content_block_delta\n'
            b'data: {"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"think "}}\n\n'
            b'event: content_block_delta\n'
            b'data: {"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"first"}}\n\n'
            b'event: content_block_start\n'
            b'data: {"type":"content_block_start","index":1,"content_block":{"type":"text","text":""}}\n\n'
            b'event: content_block_delta\n'
            b'data: {"type":"content_block_delta","index":1,"delta":{"type":"text_delta","text":"OK"}}\n\n'
            b'event: message_delta\n'
            b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n\n'
            b'event: message_stop\n'
            b'data: {"type":"message_stop"}\n\n'
        ),
    )
    chunks = run(_collect(anthropic_stream_to_openai(response)))
    body = b"".join(chunks).decode()
    events = [json.loads(part.removeprefix("data: ")) for part in body.split("\n\n") if part.startswith("data: {")]
    deltas = [event["choices"][0]["delta"] for event in events]

    assert {"reasoning_content": "think "} in deltas
    assert {"reasoning_content": "first"} in deltas
    assert {"content": "OK"} in deltas
    assert "data: [DONE]" in body


def test_openai_stream_to_anthropic_preserves_reasoning_content_delta():
    response = httpx.Response(
        200,
        content=(
            b'data: {"id":"chat1","model":"m","choices":[{"delta":{"role":"assistant"}}]}\n\n'
            b'data: {"id":"chat1","model":"m","choices":[{"delta":{"reasoning_content":"think "}}]}\n\n'
            b'data: {"id":"chat1","model":"m","choices":[{"delta":{"reasoning_content":"first"}}]}\n\n'
            b'data: {"id":"chat1","model":"m","choices":[{"delta":{"content":"OK"}}]}\n\n'
            b'data: {"id":"chat1","model":"m","choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
            b"data: [DONE]\n\n"
        ),
    )
    chunks = run(_collect(openai_stream_to_anthropic(response)))
    body = b"".join(chunks).decode()

    assert '"type": "thinking"' in body
    assert '"type": "thinking_delta", "thinking": "think "' in body
    assert '"type": "thinking_delta", "thinking": "first"' in body
    assert '"type": "text_delta", "text": "OK"' in body
    assert "message_stop" in body


async def _collect(chunks):
    out = []
    async for chunk in chunks:
        out.append(chunk)
    return out


def test_strategy_retries_same_then_alternate(monkeypatch):
    monkeypatch.setattr(config, "API_KEY", "test-key")
    monkeypatch.setattr(config, "SAME_RETRY_DELAY_S", 0)
    monkeypatch.setattr(config, "ALT_RETRY_DELAY_S", 0)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if len(calls) < 3:
            return httpx.Response(503, json={"error": {"code": 10310, "message": "busy", "type": "server_error"}})
        return httpx.Response(200, json={"content": [{"type": "text", "text": "OK"}], "id": "msg1", "model": "m", "usage": {"input_tokens": 1, "output_tokens": 1}})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://example.test") as client:
            gateway = MaasGateway(client)
            final, attempts = await gateway.run_strategy("openai", {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 8})
            return final, attempts

    final, attempts = run(scenario())
    assert final.ok
    assert [a.interface for a in attempts] == ["openai", "openai", "anthropic"]
    assert calls == ["/v2/chat/completions", "/v2/chat/completions", "/anthropic/v1/messages"]


def test_strategy_fallback_carries_thinking_controls_to_alternate_interface(monkeypatch):
    monkeypatch.setattr(config, "API_KEY", "test-key")
    monkeypatch.setattr(config, "SAME_RETRY_DELAY_S", 0)
    monkeypatch.setattr(config, "ALT_RETRY_DELAY_S", 0)
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        if len(bodies) < 3:
            return httpx.Response(503, json={"error": {"code": 10310, "message": "busy", "type": "server_error"}})
        return httpx.Response(200, json={"content": [{"type": "text", "text": "OK"}], "id": "msg1", "model": "m", "usage": {"input_tokens": 1, "output_tokens": 1}})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://example.test") as client:
            gateway = MaasGateway(client)
            return await gateway.run_strategy(
                "openai",
                {
                    "messages": [{"role": "user", "content": "hi"}],
                    "max_tokens": 8,
                    "options": {"enable_thinking": True},
                    "thinking": {"type": "enabled", "budget_tokens": 64000},
                },
            )

    final, attempts = run(scenario())
    assert final.ok
    assert [a.interface for a in attempts] == ["openai", "openai", "anthropic"]
    assert bodies[2]["options"] == {"enable_thinking": True}
    assert bodies[2]["thinking"] == {"type": "enabled", "budget_tokens": 64000}


def test_default_attempt_plan_is_warm_five_step_fallback(monkeypatch):
    monkeypatch.setattr(config, "MAX_BACKEND_ATTEMPTS", 5)
    monkeypatch.setattr(config, "CROSS_INTERFACE_FALLBACK", True)

    assert attempt_interfaces("openai") == ["openai", "openai", "anthropic", "openai", "anthropic"]
    assert attempt_interfaces("anthropic") == ["anthropic", "anthropic", "openai", "anthropic", "openai"]


def test_strategy_uses_all_busy_recovery_before_returning_503(monkeypatch):
    monkeypatch.setattr(config, "API_KEY", "test-key")
    monkeypatch.setattr(config, "MAX_BACKEND_ATTEMPTS", 5)
    monkeypatch.setattr(config, "ALL_BUSY_RECOVERY_ATTEMPTS", 2)
    monkeypatch.setattr(config, "ALL_BUSY_RECOVERY_DELAY_S", 0)
    monkeypatch.setattr(config, "SAME_RETRY_DELAY_S", 0)
    monkeypatch.setattr(config, "ALT_RETRY_DELAY_S", 0)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if len(calls) <= 5:
            return httpx.Response(503, json={"error": {"code": 10310, "message": "busy", "type": "server_error"}})
        return httpx.Response(200, json={"content": [{"type": "text", "text": "OK"}], "id": "msg1", "model": "m", "usage": {"input_tokens": 1, "output_tokens": 1}})

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://example.test") as client:
            gateway = MaasGateway(client)
            final, attempts = await gateway.run_strategy("openai", {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 8})
            return final, attempts

    final, attempts = run(scenario())
    assert final.ok
    assert len(attempts) == 6
    assert [a.interface for a in attempts] == ["openai", "openai", "anthropic", "openai", "anthropic", "openai"]


def test_account_pressure_gate_serializes_requests():
    async def scenario():
        gate = AccountPressureGate(1)
        first = await gate.acquire("req-1", "openai")
        second_task = asyncio.create_task(gate.acquire("req-2", "openai"))
        await asyncio.sleep(0)
        assert not second_task.done()
        first.release()
        second = await asyncio.wait_for(second_task, timeout=0.5)
        assert second.inflight_limit == 1
        assert second.queue_wait_s >= 0
        assert second.cooldown_wait_s == 0
        second.release()

    run(scenario())


def test_account_pressure_gate_cools_down_after_all_busy():
    async def scenario():
        gate = AccountPressureGate(1, busy_cooldown_s=0.02)
        cooldown_set_s = gate.observe_attempts(
            "req-1",
            "openai",
            [AttemptResult("openai", False, 503, 0.1, error_code=10310), AttemptResult("anthropic", False, 503, 0.1, error_code=10310)],
        )
        assert cooldown_set_s == 0.02
        permit = await gate.acquire("req-2", "openai")
        try:
            assert permit.waited_s >= 0.01
            assert permit.cooldown_wait_s >= 0.01
        finally:
            permit.release()

    run(scenario())


def test_pressure_gate_non_busy_returns_none():
    gate = AccountPressureGate(1, busy_cooldown_s=0.5)
    # A successful attempt should not trigger any arm.
    cooldown_set_s = gate.observe_attempts(
        "req-1",
        "openai",
        [AttemptResult("openai", True, 200, 0.1)],
    )
    assert cooldown_set_s is None


def test_stream_retries_before_first_chunk(monkeypatch):
    monkeypatch.setattr(config, "API_KEY", "test-key")
    monkeypatch.setattr(config, "SAME_RETRY_DELAY_S", 0)
    monkeypatch.setattr(config, "ALT_RETRY_DELAY_S", 0)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if len(calls) == 1:
            return httpx.Response(503, json={"error": {"code": 10310, "message": "busy", "type": "server_error"}})
        return httpx.Response(200, content=b'data: {"choices":[{"delta":{"content":"OK"}}]}\n\ndata: [DONE]\n\n')

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://example.test") as client:
            gateway = MaasGateway(client)
            prepared = await gateway.prepare_stream_strategy("openai", {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 8})
            assert not isinstance(prepared, PreparedStreamFailure)
            chunks = await _collect(prepared.chunks)
            return b"".join(chunks), prepared.attempts

    body, attempts = run(scenario())
    assert b"OK" in body
    assert [a.interface for a in attempts] == ["openai", "openai"]
    assert [a.ok for a in attempts] == [False, True]


def test_stream_empty_200_retries_before_commit(monkeypatch):
    monkeypatch.setattr(config, "API_KEY", "test-key")
    monkeypatch.setattr(config, "SAME_RETRY_DELAY_S", 0)
    monkeypatch.setattr(config, "ALT_RETRY_DELAY_S", 0)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if len(calls) == 1:
            return httpx.Response(200, content=b"")
        return httpx.Response(200, content=b'data: {"choices":[{"delta":{"content":"OK"}}]}\n\ndata: [DONE]\n\n')

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://example.test") as client:
            gateway = MaasGateway(client)
            prepared = await gateway.prepare_stream_strategy("openai", {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 8})
            assert not isinstance(prepared, PreparedStreamFailure)
            chunks = await _collect(prepared.chunks)
            return b"".join(chunks), prepared.attempts

    body, attempts = run(scenario())
    assert b"OK" in body
    assert [a.error_type for a in attempts] == ["EmptyStream", None]
    assert [a.ok for a in attempts] == [False, True]


def test_stream_alternate_fallback_converts_anthropic_sse(monkeypatch):
    monkeypatch.setattr(config, "API_KEY", "test-key")
    monkeypatch.setattr(config, "SAME_RETRY_DELAY_S", 0)
    monkeypatch.setattr(config, "ALT_RETRY_DELAY_S", 0)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if len(calls) < 3:
            return httpx.Response(503, json={"error": {"code": 10310, "message": "busy", "type": "server_error"}})
        return httpx.Response(
            200,
            content=(
                b'event: message_start\n'
                b'data: {"type":"message_start","message":{"id":"msg1","model":"m","content":[]}}\n\n'
                b'event: content_block_start\n'
                b'data: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
                b'event: content_block_delta\n'
                b'data: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"OK"}}\n\n'
                b'event: message_delta\n'
                b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"}}\n\n'
                b'event: message_stop\n'
                b'data: {"type":"message_stop"}\n\n'
            ),
        )

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://example.test") as client:
            gateway = MaasGateway(client)
            prepared = await gateway.prepare_stream_strategy("openai", {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 8})
            assert not isinstance(prepared, PreparedStreamFailure)
            chunks = await _collect(prepared.chunks)
            return b"".join(chunks), prepared.attempts

    body, attempts = run(scenario())
    assert b"OK" in body
    assert b"[DONE]" in body
    assert [a.interface for a in attempts] == ["openai", "openai", "anthropic"]
    assert calls == ["/v2/chat/completions", "/v2/chat/completions", "/anthropic/v1/messages"]


def test_anthropic_surface_fallback_converts_openai_sse(monkeypatch):
    monkeypatch.setattr(config, "API_KEY", "test-key")
    monkeypatch.setattr(config, "SAME_RETRY_DELAY_S", 0)
    monkeypatch.setattr(config, "ALT_RETRY_DELAY_S", 0)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if len(calls) < 3:
            return httpx.Response(503, json={"error": {"code": 10310, "message": "busy", "type": "server_error"}})
        return httpx.Response(
            200,
            content=(
                b'data: {"id":"chat1","model":"m","choices":[{"delta":{"role":"assistant"}}]}\n\n'
                b'data: {"id":"chat1","model":"m","choices":[{"delta":{"content":"OK"}}]}\n\n'
                b'data: {"id":"chat1","model":"m","choices":[{"delta":{},"finish_reason":"stop"}]}\n\n'
                b"data: [DONE]\n\n"
            ),
        )

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://example.test") as client:
            gateway = MaasGateway(client)
            prepared = await gateway.prepare_stream_strategy("anthropic", {"messages": [{"role": "user", "content": "hi"}], "max_tokens": 8})
            assert not isinstance(prepared, PreparedStreamFailure)
            chunks = await _collect(prepared.chunks)
            return b"".join(chunks).decode(), prepared.attempts

    body, attempts = run(scenario())
    assert "event: message_start" in body
    assert "content_block_delta" in body
    assert "OK" in body
    assert "message_stop" in body
    assert [a.interface for a in attempts] == ["anthropic", "anthropic", "openai"]


def test_stream_alternate_fallback_converts_anthropic_tool_use_sse(monkeypatch):
    monkeypatch.setattr(config, "API_KEY", "test-key")
    monkeypatch.setattr(config, "SAME_RETRY_DELAY_S", 0)
    monkeypatch.setattr(config, "ALT_RETRY_DELAY_S", 0)
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if len(calls) < 3:
            return httpx.Response(503, json={"error": {"code": 10310, "message": "busy", "type": "server_error"}})
        return httpx.Response(
            200,
            content=(
                b'event: message_start\n'
                b'data: {"type":"message_start","message":{"id":"msg1","model":"m","content":[]}}\n\n'
                b'event: content_block_start\n'
                b'data: {"type":"content_block_start","index":0,"content_block":{"type":"tool_use","id":"toolu_1","name":"bash","input":{}}}\n\n'
                b'event: content_block_delta\n'
                b'data: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"{\\\\\\"cmd\\\\\\": "}}\n\n'
                b'event: content_block_delta\n'
                b'data: {"type":"content_block_delta","index":0,"delta":{"type":"input_json_delta","partial_json":"\\\\\\"pwd\\\\\\"}"}}\n\n'
                b'event: message_delta\n'
                b'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"}}\n\n'
                b'event: message_stop\n'
                b'data: {"type":"message_stop"}\n\n'
            ),
        )

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://example.test") as client:
            gateway = MaasGateway(client)
            prepared = await gateway.prepare_stream_strategy(
                "openai",
                {
                    "messages": [{"role": "user", "content": "pwd"}],
                    "tools": [{"type": "function", "function": {"name": "bash", "parameters": {"type": "object"}}}],
                    "max_tokens": 8,
                },
            )
            assert not isinstance(prepared, PreparedStreamFailure)
            chunks = await _collect(prepared.chunks)
            return b"".join(chunks).decode(), prepared.attempts

    body, attempts = run(scenario())
    assert '"tool_calls"' in body
    assert '"id": "toolu_1"' in body
    assert '"name": "bash"' in body
    assert '"finish_reason": "tool_calls"' in body
    assert "data: [DONE]" in body
    assert [a.interface for a in attempts] == ["openai", "openai", "anthropic"]


def test_stream_route_returns_retryable_http_error_before_first_chunk(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "API_KEY", "provider-key:secret")
    monkeypatch.setattr(config, "CLIENT_API_KEY", "client-key")
    monkeypatch.setattr(config, "LEDGER", tmp_path / "gateway_requests.jsonl")
    monkeypatch.setattr(config, "BUSY_COOLDOWN_S", 1.0)
    monkeypatch.setattr(config, "MAX_INFLIGHT_REQUESTS", 1)
    monkeypatch.setattr(config, "ALL_BUSY_RETRY_AFTER_S", 3)

    async def fail_prepare(self, native, payload, request_id=None):
        attempts = [
            AttemptResult("openai", False, 503, 0.1, error_code=10310, error_type="server_error", error_message="The system is busy"),
            AttemptResult("anthropic", False, 503, 0.1, error_code=10310, error_type="api_error", error_message="The system is busy"),
        ]
        return PreparedStreamFailure(final=attempts[-1], attempts=attempts, total_attempts=2)

    monkeypatch.setattr(MaasGateway, "prepare_stream_strategy", fail_prepare)
    app = make_app()

    with TestClient(app) as client:
        response = client.post(
            "/v1/chat/completions",
            headers={"Authorization": "Bearer client-key"},
            json={"model": "astron-code-latest", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
        )

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "3"
    body = response.json()
    assert body["error"]["type"] == "server_error"
    assert body["error"]["code"] == "service_unavailable"
    assert "503 service_unavailable" in body["error"]["message"]
    assert "The system is busy" in body["error"]["message"]

    rows = [json.loads(line) for line in config.LEDGER.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["request_start_ts"]
    pressure = rows[0]["pressure"]
    assert pressure["inflight_limit"] == 1
    assert pressure["busy_cooldown_set_s"] == 1.0
    assert pressure["retry_after_s"] == 3
    assert pressure["queue_wait_s"] >= 0
    assert pressure["cooldown_wait_s"] == 0


def test_stream_route_releases_queue_after_first_chunk(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "API_KEY", "provider-key:secret")
    monkeypatch.setattr(config, "CLIENT_API_KEY", "client-key")
    monkeypatch.setattr(config, "LEDGER", tmp_path / "gateway_requests.jsonl")
    monkeypatch.setattr(config, "BUSY_COOLDOWN_S", 0.0)
    monkeypatch.setattr(config, "MAX_INFLIGHT_REQUESTS", 1)

    async def scenario():
        first_prepare_done = asyncio.Event()
        second_prepare_started = asyncio.Event()
        calls = 0

        async def fake_prepare(self, native, payload, request_id=None):
            nonlocal calls
            calls += 1
            call_no = calls
            if call_no == 1:
                first_prepare_done.set()
            if call_no == 2:
                second_prepare_started.set()

            async def chunks():
                yield b'data: {"choices":[{"delta":{"content":"OK"}}]}\n\n'
                if call_no == 1:
                    await second_prepare_started.wait()
                yield b"data: [DONE]\n\n"

            return PreparedStream(
                interface="openai",
                chunks=chunks(),
                attempts=[AttemptResult("openai", True, 200, 0.01)],
                total_attempts=1,
            )

        monkeypatch.setattr(MaasGateway, "prepare_stream_strategy", fake_prepare)
        app = make_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            first = asyncio.create_task(
                client.post(
                    "/v1/chat/completions",
                    headers={"Authorization": "Bearer client-key"},
                    json={"model": "astron-code-latest", "stream": True, "messages": [{"role": "user", "content": "one"}]},
                )
            )
            await asyncio.wait_for(first_prepare_done.wait(), timeout=0.5)
            second = asyncio.create_task(
                client.post(
                    "/v1/chat/completions",
                    headers={"Authorization": "Bearer client-key"},
                    json={"model": "astron-code-latest", "stream": True, "messages": [{"role": "user", "content": "two"}]},
                )
            )
            await asyncio.wait_for(second_prepare_started.wait(), timeout=0.5)
            first_response, second_response = await asyncio.gather(first, second)

        assert first_response.status_code == 200
        assert second_response.status_code == 200
        assert calls == 2
        rows = [json.loads(line) for line in config.LEDGER.read_text(encoding="utf-8").splitlines()]
        assert len(rows) == 2
        assert all(row["request_start_ts"] for row in rows)
        assert {row["pressure"]["queue_scope"] for row in rows} == {"first_chunk"}

    run(scenario())
