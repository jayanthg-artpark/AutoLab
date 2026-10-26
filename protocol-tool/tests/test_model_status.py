"""Model connection checks (Claude, Gemini, Ollama) with fake APIs."""

import asyncio

import anthropic
import httpx
import httpx2
import pytest

from protocol_tool.assistant import Assistant
from protocol_tool.config import AssistantSettings
from protocol_tool.model_status import ClaudeChecker, OpenAICompatChecker
from protocol_tool.models import Session
from protocol_tool.providers import resolve
from protocol_tool.store import SessionStore

from .conftest import FakeModel

REAL_LOOKING_KEY = "sk-ant-api03-" + "x" * 80
CLAUDE = resolve(AssistantSettings(provider="claude"))
GEMINI = resolve(AssistantSettings(provider="gemini"))
OLLAMA = resolve(AssistantSettings(provider="ollama"))
REQUEST = httpx2.Request("GET", "https://api.anthropic.com/v1/models/claude-opus-5-5")


@pytest.fixture(autouse=True)
def no_env_file(monkeypatch, tmp_path):
    """Keep a developer's real .env out of these tests."""
    monkeypatch.setattr("protocol_tool.model_status.KEY_FILES", [tmp_path / ".env"])
    for name in ("ANTHROPIC_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path / ".env"


# --- Claude ---------------------------------------------------------------------------


def api_error(cls, status):
    return cls("rejected", response=httpx2.Response(status, request=REQUEST), body=None)


class FakeAnthropic:
    def __init__(self, exc, calls):
        self.exc, self.calls = exc, calls
        self.models = self

    async def retrieve(self, model):
        self.calls.append(model)
        if self.exc:
            raise self.exc
        return {"id": model}

    async def close(self):
        pass


def claude_checker(exc=None):
    calls = []
    return ClaudeChecker(CLAUDE, lambda: FakeAnthropic(exc, calls)), calls


@pytest.mark.parametrize(
    "exc, state, ok",
    [
        (None, "ready", True),
        (api_error(anthropic.AuthenticationError, 401), "invalid", False),
        (api_error(anthropic.PermissionDeniedError, 403), "no_access", False),
        (api_error(anthropic.NotFoundError, 404), "model_unavailable", False),
        (api_error(anthropic.RateLimitError, 429), "ready", True),
        (anthropic.APIConnectionError(request=REQUEST), "unreachable", False),
        (TypeError("Could not resolve authentication method. Expected one of api_key, auth_token"), "missing", False),
    ],
)
def test_claude_failures_get_their_own_state(monkeypatch, exc, state, ok):
    monkeypatch.setenv("ANTHROPIC_API_KEY", REAL_LOOKING_KEY)
    checker, calls = claude_checker(exc)
    status = asyncio.run(checker.check())
    assert (status.state, status.ok) == (state, ok)
    assert calls == ["claude-opus-5-5"]


@pytest.mark.parametrize("key", ["sk-ant-...", "<your key>", "sk-ant-abc"])
def test_placeholder_key_is_caught_without_calling_the_api(monkeypatch, key):
    monkeypatch.setenv("ANTHROPIC_API_KEY", key)
    checker, calls = claude_checker()
    status = asyncio.run(checker.check())
    assert status.state == "placeholder"
    assert key in status.detail and "console.anthropic.com" in status.detail
    assert calls == []


def test_results_are_cached_until_refreshed(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", REAL_LOOKING_KEY)
    checker, calls = claude_checker()

    async def go():
        await checker.check()
        await checker.check()
        await checker.check(refresh=True)

    asyncio.run(go())
    assert len(calls) == 2


def test_key_in_env_file_is_picked_up_without_restart(monkeypatch, no_env_file):
    checker, _ = claude_checker()
    no_env_file.write_text(f'ANTHROPIC_API_KEY="{REAL_LOOKING_KEY}"\n', encoding="utf-8")
    assert asyncio.run(checker.check()).state == "ready"


def test_chat_with_a_placeholder_key_explains_instead_of_calling_the_model(settings, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-...")
    store = SessionStore(settings.server.database)
    session = Session()
    store.save(session)
    model = FakeModel([])  # would raise if it were called
    assistant = Assistant(settings, store, model, ClaudeChecker(CLAUDE))

    async def go():
        return [e async for e in assistant.chat(session, "hello", [])]

    events = asyncio.run(go())
    assert model.calls == []
    error = next(e for e in events if e["type"] == "error")["message"]
    assert 'placeholder ("sk-ant-...")' in error


def test_missing_claude_credentials_get_a_clear_chat_message(settings):
    class NoCredentials:
        async def stream(self, system, tools, messages):
            raise TypeError("Could not resolve authentication method. Expected one of api_key, auth_token, or credentials")
            yield  # pragma: no cover

    store = SessionStore(settings.server.database)
    session = Session()
    store.save(session)
    assistant = Assistant(settings, store, NoCredentials(), ClaudeChecker(CLAUDE))

    async def go():
        return [e async for e in assistant.chat(session, "hello", [])]

    error = next(e for e in asyncio.run(go()) if e["type"] == "error")["message"]
    assert error.startswith("No Claude API key is set.")


# --- Gemini / Ollama (OpenAI-compatible) ---------------------------------------------------


def models_api(status=200, ids=(), raise_connect=False):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/models")
        if raise_connect:
            raise httpx.ConnectError("refused")
        return httpx.Response(status, json={"object": "list", "data": [{"id": i} for i in ids]})
    return httpx.MockTransport(handler)


def test_gemini_needs_a_key(monkeypatch):
    status = asyncio.run(OpenAICompatChecker(GEMINI, models_api(ids=["models/gemini-flash-latest"])).check())
    assert status.state == "missing"
    assert "GEMINI_API_KEY" in status.detail and "aistudio.google.com" in status.detail


@pytest.mark.parametrize(
    "transport, state",
    [
        (models_api(ids=["models/gemini-2.5-pro", "models/gemini-flash-latest"]), "ready"),
        (models_api(ids=["models/gemini-9-flash"]), "model_unavailable"),
        (models_api(status=400), "invalid"),
        (models_api(status=403), "no_access"),
        (models_api(raise_connect=True), "unreachable"),
    ],
)
def test_gemini_states(monkeypatch, transport, state):
    monkeypatch.setenv("GEMINI_API_KEY", "AIza" + "x" * 35)
    status = asyncio.run(OpenAICompatChecker(GEMINI, transport).check())
    assert status.state == state
    if state == "model_unavailable":
        assert "Available: gemini-9-flash" in status.detail


def test_ollama_needs_no_key_and_explains_missing_models():
    ready = asyncio.run(OpenAICompatChecker(OLLAMA, models_api(ids=["qwen3:30b", "llama3.2:latest"])).check())
    assert ready.state == "ready"
    missing = asyncio.run(OpenAICompatChecker(OLLAMA, models_api(ids=["llama3.2:latest"])).check())
    assert missing.state == "model_unavailable"
    assert "ollama pull qwen3:30b" in missing.detail
    down = asyncio.run(OpenAICompatChecker(OLLAMA, models_api(raise_connect=True)).check())
    assert down.state == "unreachable"
    assert "Ollama is not running" in down.detail
