"""The OpenAI-compatible adapter (Gemini, Ollama, ...) against a fake chat API."""

import asyncio
import base64
import json

import httpx
import pytest

from protocol_tool.assistant import TOOLS, Assistant
from protocol_tool.config import AssistantSettings
from protocol_tool.model_status import ModelAuthError, ModelError, OpenAICompatChecker
from protocol_tool.models import ModelTurn, Session
from protocol_tool.openai_compat import OpenAICompatModel, to_openai_messages
from protocol_tool.providers import resolve
from protocol_tool.store import SessionStore

from .conftest import GOOD_PROTOCOL

GEMINI = resolve(AssistantSettings(provider="gemini"))
OLLAMA = resolve(AssistantSettings(provider="ollama"))


@pytest.fixture(autouse=True)
def gemini_key(monkeypatch, tmp_path):
    monkeypatch.setattr("protocol_tool.model_status.KEY_FILES", [tmp_path / ".env"])
    monkeypatch.setenv("GEMINI_API_KEY", "AIza" + "x" * 35)


def tiny_pdf(text: str) -> bytes:
    stream = f"BT /F1 18 Tf 20 100 Td ({text}) Tj ET".encode()
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 144] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out, offsets = b"%PDF-1.4\n", []
    for i, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + obj + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % o for o in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref)
    return out


def sse(*chunks) -> bytes:
    return b"".join(b"data: " + json.dumps(c).encode() + b"\n\n" for c in chunks) + b"data: [DONE]\n\n"


def delta(finish=None, **delta_fields):
    return {"choices": [{"index": 0, "delta": delta_fields, "finish_reason": finish}]}


class FakeChatAPI:
    """Replies to /chat/completions with scripted SSE bodies, recording requests."""

    def __init__(self, *bodies):
        self.bodies = list(bodies)
        self.requests = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content))
        status, body = self.bodies.pop(0)
        headers = {"content-type": "text/event-stream", "retry-after": "0"}
        return httpx.Response(status, content=body, headers=headers)


def collect(model, messages=None):
    async def go():
        return [e async for e in model.stream([{"type": "text", "text": "SYSTEM"}], TOOLS, messages or [{"role": "user", "content": "hi"}])]

    return asyncio.run(go())


def test_converts_the_conversation_to_openai_format():
    pdf_b64 = base64.b64encode(tiny_pdf("Add 100 uL buffer")).decode()
    messages = [
        {"role": "user", "content": [
            {"type": "document", "title": "assay.pdf", "source": {"type": "base64", "media_type": "application/pdf", "data": pdf_b64}},
            {"type": "text", "text": "<design_state>...</design_state>"},
            {"type": "text", "text": "Design this"},
        ]},
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "", "signature": "sig"},
            {"type": "text", "text": "Writing it."},
            {"type": "tool_use", "id": "c1", "name": "write_protocol", "input": {"code": "x"},
             "provider_extra": {"extra_content": {"google": {"thought_signature": "abc"}}}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "c1", "content": "Simulation PASSED"},
        ]},
        {"role": "user", "content": [{"type": "text", "text": "Thanks"}]},
    ]
    out = to_openai_messages([{"type": "text", "text": "SYSTEM"}], messages)

    assert [m["role"] for m in out] == ["system", "user", "assistant", "tool", "user"]
    assert '<document title="assay.pdf">' in out[1]["content"]
    assert "Add 100 uL buffer" in out[1]["content"]
    assert out[1]["content"].endswith("Design this")
    call = out[2]["tool_calls"][0]
    assert call["function"] == {"name": "write_protocol", "arguments": '{"code": "x"}'}
    assert call["extra_content"] == {"google": {"thought_signature": "abc"}}  # sent back unchanged
    assert out[3] == {"role": "tool", "tool_call_id": "c1", "content": "Simulation PASSED"}


def test_streams_text_and_split_tool_calls():
    arguments = json.dumps({"title": "T", "summary": "S", "code": "print(1)"})
    api = FakeChatAPI((200, sse(
        delta(content="Designing "),
        delta(content="now."),
        delta(tool_calls=[{"index": 0, "id": "call_9", "type": "function", "function": {"name": "write_protocol", "arguments": arguments[:10]},
                           "extra_content": {"google": {"thought_signature": "sig"}}}]),
        delta(tool_calls=[{"index": 0, "function": {"arguments": arguments[10:]}}]),
        delta(finish="tool_calls"),
    )))
    events = collect(OpenAICompatModel(GEMINI, httpx.MockTransport(api)))

    assert [e["text"] for e in events[:-1] if e["type"] == "text"] == ["Designing ", "now."]
    assert {"type": "tool_start", "name": "write_protocol"} in events
    turn = events[-1]
    assert isinstance(turn, ModelTurn) and turn.stop_reason == "tool_use"
    assert turn.content[0] == {"type": "text", "text": "Designing now."}
    assert turn.content[1]["input"] == {"title": "T", "summary": "S", "code": "print(1)"}
    assert turn.content[1]["provider_extra"] == {"extra_content": {"google": {"thought_signature": "sig"}}, "model": "gemini-flash-latest"}

    request = api.requests[0]
    assert request["model"] == "gemini-flash-latest" and request["stream"] is True
    assert "write_protocol" in [t["function"]["name"] for t in request["tools"]]


OVERLOADED = b'[{"error": {"code": 503, "message": "This model is currently experiencing high demand.", "status": "UNAVAILABLE"}}]'
OK = sse(delta(content="ok"), delta(finish="stop"))


def test_busy_or_rate_limited_models_fall_back_and_stick():
    api = FakeChatAPI(
        (429, b'{"error": {"message": "quota"}}'),  # main model: out of quota -> next model at once
        (503, OVERLOADED),                          # first fallback: busy -> one retry
        (200, OK),
        (200, OK),                                  # next request starts with the model that answered
    )
    model = OpenAICompatModel(GEMINI, httpx.MockTransport(api))
    assert collect(model)[-1].content == [{"type": "text", "text": "ok"}]
    assert [r["model"] for r in api.requests] == ["gemini-flash-latest", "gemini-3.5-flash", "gemini-3.5-flash"]
    assert model.active_model == "gemini-3.5-flash"
    collect(model)
    assert api.requests[-1]["model"] == "gemini-3.5-flash"


def test_all_models_busy_gives_a_clear_error():
    api = FakeChatAPI(*[(503, OVERLOADED)] * 6)  # three models, one retry each
    with pytest.raises(ModelError, match="overloaded right now"):
        collect(OpenAICompatModel(GEMINI, httpx.MockTransport(api)))
    assert len(api.requests) == 6


def test_thought_signatures_only_go_back_to_the_model_that_made_them():
    messages = [
        {"role": "user", "content": "go"},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "c1", "name": "write_protocol", "input": {},
                                           "provider_extra": {"extra_content": {"sig": "s"}, "model": "gemini-3.8-flash"}}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "ok"}]},
    ]
    same = to_openai_messages([{"type": "text", "text": "S"}], messages, "gemini-3.8-flash")[2]["tool_calls"][0]
    other = to_openai_messages([{"type": "text", "text": "S"}], messages, "gemini-3.5-flash")[2]["tool_calls"][0]
    assert same["extra_content"] == {"sig": "s"} and "model" not in same
    # Another Gemini model can't check that signature and rejects calls without one: placeholder instead.
    assert other["extra_content"] == {"google": {"thought_signature": "skip_thought_signature_validator"}}
    assert "extra_content" not in to_openai_messages([{"type": "text", "text": "S"}], messages, "qwen3:30b")[2]["tool_calls"][0]

    # Parallel calls: Gemini signs only the first; the others are sent back as they came.
    messages[1]["content"].append({"type": "tool_use", "id": "c2", "name": "search_knowledge", "input": {}})
    messages[2]["content"].append({"type": "tool_result", "tool_use_id": "c2", "content": "ok"})
    first, second = to_openai_messages([{"type": "text", "text": "S"}], messages, "gemini-3.8-flash")[2]["tool_calls"]
    assert first["extra_content"] == {"sig": "s"} and "extra_content" not in second


def test_retired_model_is_reported_in_the_banner(settings):
    body = (b'{"error": {"code": 404, "message": "This model models/gemini-2.5-flash is no longer available to new users. '
            b'Please update your code to use models/gemini-3.8-flash.", "status": "NOT_FOUND"}}')
    api = FakeChatAPI((404, body), (404, body), (404, body))  # main model and both fallbacks
    store = SessionStore(settings.server.database)
    session = Session()
    store.save(session)
    checker = OpenAICompatChecker(GEMINI)
    assistant = Assistant(settings, store, OpenAICompatModel(GEMINI, httpx.MockTransport(api)), checker)

    async def go():
        return [e async for e in assistant.chat(session, "hello", [])]

    error = next(e for e in asyncio.run(go()) if e["type"] == "error")["message"]
    assert "gemini-3.8-flash" in error and "config.toml" in error
    assert checker.view()["state"] == "model_unavailable"


@pytest.mark.parametrize("status, body", [
    (401, b'{"error": {"message": "bad key"}}'),
    # What Gemini actually returns for a bad key (checked against the live API).
    (400, b'{"error": {"code": 400, "message": "Please pass a valid API key", "status": "INVALID_ARGUMENT"}}'),
    (400, b'[{"error": {"code": 400, "message": "API key not valid. Please pass a valid API key.", "status": "INVALID_ARGUMENT"}}]'),
])
def test_rejected_keys_are_auth_errors(status, body):
    api = FakeChatAPI((status, body))
    with pytest.raises(ModelAuthError) as err:
        collect(OpenAICompatModel(GEMINI, httpx.MockTransport(api)))
    assert err.value.state == "invalid"


def test_ollama_not_running_is_explained():
    def refuse(request):
        raise httpx.ConnectError("refused")

    with pytest.raises(ModelError, match="Ollama is not running"):
        collect(OpenAICompatModel(OLLAMA, httpx.MockTransport(refuse)))


def test_a_full_design_turn_through_the_adapter(settings):
    """Gemini writes a protocol, the real simulator passes it, Gemini sums up."""
    arguments = json.dumps({"title": "Dilution series", "summary": "Two-fold series.", "plate_slot": "D2", "code": GOOD_PROTOCOL})
    api = FakeChatAPI(
        (200, sse(delta(tool_calls=[{"index": 0, "id": "c1", "type": "function", "function": {"name": "write_protocol", "arguments": arguments}}]),
                  delta(finish="stop"))),  # Gemini reports "stop" even when it calls tools
        (200, sse(delta(content="The protocol passes simulation."), delta(finish="stop"))),
    )
    settings.assistant.provider = "gemini"
    store = SessionStore(settings.server.database)
    session = Session()
    store.save(session)
    checker = OpenAICompatChecker(GEMINI)
    assistant = Assistant(settings, store, OpenAICompatModel(GEMINI, httpx.MockTransport(api)), checker)

    async def go():
        return [e async for e in assistant.chat(session, "Make a dilution series", [])]

    events = asyncio.run(go())
    assert session.design.protocol("main").simulation.ok and session.design.title == "Dilution series"
    assert events[-1]["entry"]["text"] == "The protocol passes simulation."
    second = api.requests[1]["messages"]
    assert second[-1]["role"] == "tool" and "Simulation PASSED" in second[-1]["content"]
