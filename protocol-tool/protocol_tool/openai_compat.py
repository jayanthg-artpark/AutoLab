"""Models behind an OpenAI-compatible chat API: Gemini's free tier, Ollama, and others.

The chat loop keeps the conversation in Anthropic's message format (content
blocks, tool_use / tool_result). This adapter converts it to OpenAI chat
format for each request and converts the streamed reply back, so the loop,
the tools and the saved sessions are the same whichever model is used.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import logging
import os
import uuid
from typing import Any

import httpx

from .model_status import ModelAuthError, ModelError, ModelNotFound, load_key_file
from .models import ModelTurn
from .providers import Provider

log = logging.getLogger(__name__)

MAX_PDF_CHARS = 150_000
RATE_LIMIT_RETRIES = 3
_pdf_cache: dict[str, str] = {}


class Overloaded(ModelError):
    """The server is busy or briefly failing (5xx); another model may answer."""


class RateLimited(ModelError):
    """This model's quota is used up for now (free tiers limit each model separately)."""


class OpenAICompatModel:
    """Tries the configured model first, then the fallbacks, and stays with whichever answers."""

    def __init__(self, provider: Provider, transport: httpx.AsyncBaseTransport | None = None):
        self._provider = provider
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(600, connect=10), transport=transport)
        self._models = list(dict.fromkeys([provider.model, *provider.fallback_models]))
        self._active = 0

    @property
    def active_model(self) -> str:
        return self._models[self._active]

    async def stream(self, system, tools, messages):
        p = self._provider
        order = self._models[self._active:] + self._models[:self._active]
        last_error: ModelError | None = None
        for position, model in enumerate(order):
            is_last = position == len(order) - 1
            body = {
                "model": model,
                "messages": to_openai_messages(system, messages, model),
                "stream": True,
            }
            if tools:  # some servers reject an empty tool list
                body["tools"] = [to_openai_tool(t) for t in tools]
            chunks = self._post(body, retry_rate_limit=is_last)
            try:
                first = await anext(chunks, None)  # sends the request; raises on HTTP errors
            except (Overloaded, RateLimited, ModelNotFound) as exc:
                if not is_last:
                    log.warning("%s model %s unavailable (%s); trying %s", p.label, model, exc, order[position + 1])
                last_error = exc
                continue
            if model != self.active_model:
                log.info("%s: now using %s", p.label, model)
                self._active = self._models.index(model)
            async for event in self._read(first, chunks, model):
                yield event
            return
        raise last_error or ModelError(f"No {p.label} model answered")

    async def _read(self, first, chunks, model: str):
        """Turns the streamed chunks into UI events and a final ModelTurn."""
        p = self._provider
        text = ""
        calls: dict[int, dict[str, Any]] = {}
        finish = None

        async def all_chunks():
            if first is not None:
                yield first
                async for chunk in chunks:
                    yield chunk

        async for chunk in all_chunks():
            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}
                if delta.get("content"):
                    text += delta["content"]
                    yield {"type": "text", "text": delta["content"]}
                for position, part in enumerate(delta.get("tool_calls") or []):
                    index = part.get("index", position)
                    call = calls.setdefault(index, {"id": "", "name": "", "arguments": "", "extra": {}})
                    function = part.get("function") or {}
                    if part.get("id"):
                        call["id"] = part["id"]
                    if function.get("name") and not call["name"]:
                        call["name"] = function["name"]
                        yield {"type": "tool_start", "name": call["name"]}
                    call["arguments"] += function.get("arguments") or ""
                    # Provider-specific fields (e.g. Gemini's thought signatures) must go back unchanged.
                    call["extra"].update({k: v for k, v in part.items() if k not in ("index", "id", "type", "function")})
                finish = choice.get("finish_reason") or finish

        content: list[dict[str, Any]] = [{"type": "text", "text": text}] if text else []
        for index in sorted(calls):
            call = calls[index]
            try:
                arguments = json.loads(call["arguments"] or "{}")
            except json.JSONDecodeError as exc:
                if finish == "length":
                    arguments = {}  # cut off; the loop answers it as truncated and never runs it
                else:
                    raise ValueError(f"{p.label} sent tool arguments that are not valid JSON") from exc
            block = {"type": "tool_use", "id": call["id"] or f"call_{uuid.uuid4().hex[:12]}", "name": call["name"], "input": arguments}
            if call["extra"]:
                # Only the model that made them can check these, so remember which one it was.
                block["provider_extra"] = {**call["extra"], "model": model}
            content.append(block)

        stop = {"length": "max_tokens", "content_filter": "refusal"}.get(finish, "end_turn")
        if calls and stop == "end_turn":
            stop = "tool_use"
        yield ModelTurn(content=content, stop_reason=stop)

    async def _post(self, body: dict[str, Any], retry_rate_limit: bool):
        """Streams parsed chunks. Retries a busy server once; waits out rate limits only if asked."""
        p = self._provider
        load_key_file(p.key_env)
        headers = {"Authorization": f"Bearer {os.environ[p.key_env]}"} if p.key_env and os.environ.get(p.key_env) else {}
        if p.key_env and not headers:
            raise ModelAuthError("missing")
        retries = {"overloaded": 1, "rate_limited": RATE_LIMIT_RETRIES if retry_rate_limit else 0}
        attempt = 0
        while True:
            try:
                async with self._client.stream("POST", f"{p.url}/chat/completions", json=body, headers=headers) as response:
                    if response.is_error:
                        await response.aread()
                        error = _http_error(p, body["model"], response)
                        kind = {Overloaded: "overloaded", RateLimited: "rate_limited"}.get(type(error))
                        if kind and retries[kind] > 0:
                            retries[kind] -= 1
                            wait = _retry_after(response, attempt)
                            attempt += 1
                            log.info("%s %s answered %s; retrying in %.0f s", p.label, body["model"], response.status_code, wait)
                            await asyncio.sleep(wait)
                            continue
                        raise error
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            return
                        chunk = json.loads(data)
                        if "error" in chunk:
                            raise ModelError(f"{p.label} error: {chunk['error'].get('message', chunk['error'])}")
                        yield chunk
                    return
            except httpx.ConnectError as exc:
                where = "Ollama is not running" if p.name == "ollama" else f"Cannot reach {p.label}"
                raise ModelError(f"{where} ({p.url}).") from exc
            except httpx.HTTPError as exc:
                raise ModelError(f"Connection to {p.label} failed: {exc!r}") from exc

    async def aclose(self) -> None:
        await self._client.aclose()


def _retry_after(response: httpx.Response, attempt: int) -> float:
    try:
        return min(float(response.headers["retry-after"]), 60)
    except (KeyError, ValueError):
        return min(2 * 2**attempt, 30)


def _http_error(p: Provider, model: str, response: httpx.Response) -> ModelError:
    try:
        error = response.json()
        error = error[0] if isinstance(error, list) else error
        message = (error.get("error") or {}).get("message") or response.text
    except (ValueError, AttributeError):
        message = response.text
    message = str(message)[:400]
    # Gemini answers a bad key with 400 "Please pass a valid API key" (or API_KEY_INVALID).
    if response.status_code == 401 or (response.status_code == 400 and "api key" in message.lower()):
        return ModelAuthError("invalid", message)
    if response.status_code == 403:
        return ModelAuthError("no_access", message)
    if response.status_code == 429:
        return RateLimited(f"{p.label}'s free-tier limit was reached. Wait a minute and send the message again. ({message})")
    if response.status_code in (500, 502, 503, 504):
        return Overloaded(f"{p.label} is overloaded right now ({response.status_code}). "
                          f"Send the message again in a minute. ({message})")
    if response.status_code == 404:
        return ModelNotFound(f"{p.label} cannot use the model {model}: {message}")
    return ModelError(f"{p.label} error {response.status_code}: {message}")


# --- Format conversion -------------------------------------------------------------------

# Accepted by Gemini in place of a thought signature it can't check (history from another model).
GEMINI_PLACEHOLDER_SIGNATURE = "skip_thought_signature_validator"


def to_openai_tool(tool: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {"name": tool["name"], "description": tool.get("description", ""), "parameters": tool["input_schema"]},
    }


def to_openai_messages(
    system: list[dict[str, Any]], messages: list[dict[str, Any]], model: str | None = None
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = [{"role": "system", "content": "\n\n".join(b["text"] for b in system)}]
    for message in messages:
        content = message["content"]
        if isinstance(content, str):
            content = [{"type": "text", "text": content}]
        if message["role"] == "assistant":
            text = "".join(b.get("text", "") for b in content if b.get("type") == "text")
            calls = [
                {
                    "id": b["id"], "type": "function",
                    "function": {"name": b["name"], "arguments": json.dumps(b.get("input") or {})},
                    **_extras_for(b, model),
                }
                for b in content if b.get("type") == "tool_use"
            ]
            if calls and model and model.startswith("gemini") and not any("extra_content" in c for c in calls):
                # Calls made by another model (a fallback after a quota error, or a session that switched
                # provider) carry no signature this Gemini model accepts, and Gemini rejects function calls
                # without one. Google's documented placeholder goes where Gemini puts signatures: the first call.
                calls[0]["extra_content"] = {"google": {"thought_signature": GEMINI_PLACEHOLDER_SIGNATURE}}
            if text or calls:
                entry: dict[str, Any] = {"role": "assistant", "content": text or None}
                if calls:
                    entry["tool_calls"] = calls
                out.append(entry)
            continue

        # Tool results first: they must directly follow the assistant's tool calls.
        for block in content:
            if block.get("type") == "tool_result":
                result = block.get("content", "")
                if isinstance(result, list):
                    result = "\n".join(b.get("text", "") for b in result if isinstance(b, dict))
                if block.get("is_error"):
                    result = f"Error: {result}"
                out.append({"role": "tool", "tool_call_id": block["tool_use_id"], "content": result})

        parts = [p for p in (_user_part(b) for b in content) if p]
        if not parts:
            continue
        if all(p["type"] == "text" for p in parts):
            text = "\n\n".join(p["text"] for p in parts)
            previous = out[-1]
            if previous["role"] == "user" and isinstance(previous["content"], str):
                previous["content"] += "\n\n" + text  # keep user/assistant turns alternating
            else:
                out.append({"role": "user", "content": text})
        else:
            out.append({"role": "user", "content": parts})
    return out


def _extras_for(block: dict[str, Any], model: str | None) -> dict[str, Any]:
    """A tool call's provider fields, sent back only to the model that produced them."""
    extra = dict(block.get("provider_extra") or {})
    made_by = extra.pop("model", None)
    return extra if made_by in (None, model) or model is None else {}


def _user_part(block: dict[str, Any]) -> dict[str, Any] | None:
    kind = block.get("type")
    if kind == "text":
        return {"type": "text", "text": block["text"]}
    if kind == "image":
        source = block["source"]
        return {"type": "image_url", "image_url": {"url": f"data:{source['media_type']};base64,{source['data']}"}}
    if kind == "document":
        source = block["source"]
        title = block.get("title") or "attached document"
        if source.get("type") == "text":
            body = source["data"]
        elif source.get("media_type") == "application/pdf":
            body = pdf_text(source["data"])
        else:
            return None
        return {"type": "text", "text": f'<document title="{title}">\n{body}\n</document>'}
    return None


def pdf_text(data_b64: str) -> str:
    """The text of a base64 PDF, cached because the whole conversation is resent each turn."""
    digest = hashlib.sha1(data_b64.encode()).hexdigest()
    if digest in _pdf_cache:
        return _pdf_cache[digest]
    try:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(base64.b64decode(data_b64)))
        pages = [f"[page {i}]\n{(page.extract_text() or '').strip()}" for i, page in enumerate(reader.pages, 1)]
        text = "\n\n".join(pages).strip()
    except Exception as exc:  # a broken PDF should not break the chat
        log.warning("Could not read PDF: %s", exc)
        text = "(This PDF could not be read.)"
    if not text.replace("[page", "").strip(" \n]0123456789"):
        text = "(This PDF has no extractable text; it may be a scan. Ask the scientist to paste the key steps.)"
    if len(text) > MAX_PDF_CHARS:
        text = text[:MAX_PDF_CHARS] + "\n(…the rest of the document was cut off)"
    if len(_pdf_cache) > 32:
        _pdf_cache.clear()
    _pdf_cache[digest] = text
    return text
