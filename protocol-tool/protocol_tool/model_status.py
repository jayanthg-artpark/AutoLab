"""Whether the server can use its AI model, so the page can say so before anyone types.

The checks cost nothing: they look at the configured key, then ask the
provider's model list whether the configured model is available.
Keys can live in the environment or in a .env file next to the project
(NAME=value lines); the file is re-read on every check, so a key pasted into
it works without restarting the server.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import anthropic
import httpx

from .providers import Provider

log = logging.getLogger(__name__)

KEY_FILES = [Path.cwd() / ".env", Path(__file__).resolve().parent.parent / ".env"]


class ModelError(Exception):
    """A model request failed; the message is shown to the scientist."""


class ModelNotFound(ModelError):
    """The configured model does not exist or is not offered to this key."""


class ModelAuthError(ModelError):
    def __init__(self, state: str, message: str = ""):
        super().__init__(message or state)
        self.state = state  # missing | invalid | no_access


@dataclass
class ModelStatus:
    ok: bool
    state: str  # ready | missing | placeholder | invalid | no_access | model_unavailable | unreachable | error
    detail: str
    checked_at: float


# --- Keys ---------------------------------------------------------------------------


def placeholder_key(name: str) -> str | None:
    """The key's value if it is obviously not a real key (e.g. copied from instructions)."""
    key = os.environ.get(name, "").strip() if name else ""
    if key and ("..." in key or "…" in key or "<" in key or len(key) < 20):
        return key
    return None


def load_key_file(name: str) -> None:
    """Takes `name` from a .env file when the environment has no usable value for it."""
    if not name or (os.environ.get(name) and not placeholder_key(name)):
        return
    for path in KEY_FILES:
        try:
            lines = path.read_text(encoding="utf-8-sig").splitlines()
        except OSError:
            continue
        for line in lines:
            key, sep, value = line.strip().partition("=")
            if sep and key.strip().removeprefix("export ").strip() == name:
                value = value.strip().strip("'\"")
                if value:
                    os.environ[name] = value
                    return


def masked_key(name: str) -> str:
    key = os.environ.get(name, "") if name else ""
    return f"{key[:7]}…{key[-4:]}" if len(key) > 16 else (key or "not set")


def auth_problem(exc: Exception) -> str | None:
    """The status state for an exception that means the model can't be used, else None."""
    if isinstance(exc, ModelAuthError):
        return exc.state
    if isinstance(exc, anthropic.AuthenticationError):
        return "invalid"
    if isinstance(exc, anthropic.PermissionDeniedError):
        return "no_access"
    # The Anthropic SDK raises a plain TypeError when it finds no credentials at all.
    if isinstance(exc, TypeError) and "authentication method" in str(exc):
        return "missing"
    return None


# --- Checkers -------------------------------------------------------------------------


class ModelChecker:
    """Caches the last result: 5 minutes when healthy, 15 seconds when not."""

    def __init__(self, provider: Provider):
        self.provider = provider
        self._status: ModelStatus | None = None

    def message(self, state: str, extra: str = "") -> str:
        p = self.provider
        env_file = f"put the line {p.key_env}=<your key> in protocol-tool\\.env, then click Check again"
        messages = {
            "missing": f"No {p.label} API key is set. {p.key_help}, {env_file}.",
            "placeholder": f'{p.key_env} is set to a placeholder ("{placeholder_key(p.key_env) or ""}"), '
                           f"not a real key. {p.key_help}, {env_file}.",
            "invalid": f"{p.label} rejected the API key. Check {p.key_env} in protocol-tool\\.env "
                       f"(the whole key, no quotes or spaces), then click Check again.",
            "no_access": f"{p.label} refused this API key (403 Forbidden). Check the key's permissions.",
            "model_unavailable": (
                f"Ollama does not have the model {p.model}. Run: ollama pull {p.model}"
                if p.name == "ollama"
                else f"{p.label} has no model called {p.model}. Change [assistant] model in config.toml."
            ),
            "unreachable": (
                f"Ollama is not running at {p.url}. {p.key_help}, start it, then click Check again."
                if p.name == "ollama"
                else f"The server cannot reach {p.label}. Check its internet connection or proxy settings."
            ),
        }
        text = messages.get(state, state)
        return f"{text} {extra}".strip()

    def key_problem(self) -> str | None:
        """'missing' or 'placeholder' when the key is unusable without asking the provider."""
        name = self.provider.key_env
        if not name:
            return None
        load_key_file(name)
        if placeholder_key(name):
            return "placeholder"
        if not os.environ.get(name) and self.provider.name != "claude":
            # (Claude can also authenticate through an `ant auth login` profile.)
            return "missing"
        return None

    def record(self, state: str, detail: str = "") -> ModelStatus:
        self._status = ModelStatus(state == "ready", state, detail or self.message(state), time.monotonic())
        return self._status

    def view(self) -> dict:
        status = self._status
        data = asdict(status) if status else {"ok": False, "state": "unknown", "detail": "Not checked yet"}
        data.pop("checked_at", None)
        p = self.provider
        return {**data, "provider": p.name, "label": p.label, "model": p.model, "free": p.free}

    async def check(self, refresh: bool = False) -> ModelStatus:
        status = self._status
        if status and not refresh and time.monotonic() - status.checked_at < (300 if status.ok else 15):
            return status
        if problem := self.key_problem():
            return self.record(problem)
        state, detail = await self._probe()
        return self.record(state, detail)

    async def _probe(self) -> tuple[str, str]:
        raise NotImplementedError

    async def log_startup(self) -> None:
        status = await self.check(refresh=True)
        p = self.provider
        if status.ok:
            log.info("%s: ready (%s, key %s)", p.label, p.model, masked_key(p.key_env))
        else:
            log.warning("%s: NOT READY - %s", p.label, status.detail)


class ClaudeChecker(ModelChecker):
    def __init__(self, provider: Provider, client_factory=anthropic.AsyncAnthropic):
        super().__init__(provider)
        self._client_factory = client_factory

    async def _probe(self) -> tuple[str, str]:
        model = self.provider.model
        try:
            client = self._client_factory()
            try:
                await client.models.retrieve(model)
            finally:
                await client.close()
        except anthropic.NotFoundError:
            return "model_unavailable", ""
        except anthropic.RateLimitError:
            return "ready", f"{model} is available (currently rate-limited)"
        except anthropic.APIConnectionError:
            return "unreachable", ""
        except anthropic.APIStatusError as exc:
            if state := auth_problem(exc):
                return state, ""
            return "error", f"Claude API error {exc.status_code}: {exc.message}"
        except TypeError as exc:
            if state := auth_problem(exc):
                return state, ""
            raise
        return "ready", f"{model} is available"


class OpenAICompatChecker(ModelChecker):
    """Gemini, Ollama and other OpenAI-compatible servers: GET /models."""

    def __init__(self, provider: Provider, transport: httpx.AsyncBaseTransport | None = None):
        super().__init__(provider)
        self._transport = transport

    async def _probe(self) -> tuple[str, str]:
        p = self.provider
        headers = {"Authorization": f"Bearer {os.environ[p.key_env]}"} if p.key_env and os.environ.get(p.key_env) else {}
        try:
            async with httpx.AsyncClient(timeout=10, transport=self._transport) as client:
                response = await client.get(f"{p.url}/models", headers=headers)
        except httpx.HTTPError:
            return "unreachable", ""
        if response.status_code in (400, 401):
            # Gemini answers an invalid key with 400 API_KEY_INVALID.
            return "invalid", ""
        if response.status_code == 403:
            return "no_access", ""
        if response.status_code == 429:
            return "ready", f"{p.model} (currently rate-limited)"
        if response.is_error:
            return "error", f"{p.label} answered {response.status_code}: {response.text[:200]}"
        try:
            ids = [str(m.get("id", "")) for m in response.json().get("data", [])]
        except (ValueError, AttributeError):
            return "ready", f"{p.model} via {p.url}"
        if not model_listed(p.model, ids):
            options = [i.removeprefix("models/") for i in ids if p.name != "gemini" or "gemini" in i][:8]
            hint = f"Available: {', '.join(options)}." if options else ""
            return "model_unavailable", self.message("model_unavailable", hint)
        return "ready", f"{p.model} is available"


def model_listed(model: str, ids: list[str]) -> bool:
    names = {i.removeprefix("models/") for i in ids}
    return model in names or (":" not in model and f"{model}:latest" in names)


def build_checker(provider: Provider) -> ModelChecker:
    return ClaudeChecker(provider) if provider.name == "claude" else OpenAICompatChecker(provider)
