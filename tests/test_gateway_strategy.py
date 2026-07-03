import asyncio
import json

import httpx
from fastapi.testclient import TestClient

from gateway import config
from gateway.app import make_app
from gateway.protocols import (
    anthropic_response_to_openai,
    anthropic_to_openai,
    openai_response_to_anthropic,
    openai_to_anthropic,
)
from gateway.sse import anthropic_stream_to_openai
from gateway.strategy import MaasGateway
from gateway.types import (
    AttemptResult,
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
    body = response.json()
    assert body["error"]["type"] == "server_error"
    assert body["error"]["code"] == "service_unavailable"
    assert "503 service_unavailable" in body["error"]["message"]
    assert "The system is busy" in body["error"]["message"]
