"""Shared HTTP plumbing for the device drivers."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable, TypeVar

import httpx

from .base import DeviceError, ProgressCallback

T = TypeVar("T")


class HttpDevice:
    name = "device"

    def __init__(
        self,
        base_url: str,
        request_timeout_s: float,
        headers: dict[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        if not base_url:
            raise ValueError(f"{self.name}: base_url is required in http mode")
        self._client = httpx.AsyncClient(
            base_url=base_url, timeout=request_timeout_s, headers=headers, transport=transport
        )

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Sends a request and returns the decoded JSON body (None if empty)."""
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise DeviceError(f"{self.name}: {method} {path} failed: {exc!r}") from exc
        if response.is_error:
            raise DeviceError(
                f"{self.name}: {method} {path} returned {response.status_code}: "
                f"{response.text[:300]}"
            )
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as exc:
            raise DeviceError(f"{self.name}: {method} {path} returned non-JSON body") from exc

    async def aclose(self) -> None:
        await self._client.aclose()


def key_header(api_key: str) -> dict[str, str]:
    return {"X-API-Key": api_key} if api_key else {}


async def poll(
    fetch: Callable[[], Awaitable[T]],
    state_of: Callable[[T], str],
    terminal: set[str],
    *,
    interval_s: float,
    timeout_s: float,
    what: str,
    on_progress: ProgressCallback | None = None,
) -> T:
    """Calls fetch() until state_of(result) is terminal, reporting state changes."""
    deadline = time.monotonic() + timeout_s
    last_state = None
    while True:
        value = await fetch()
        state = state_of(value)
        if state != last_state:
            if on_progress:
                on_progress(state)
            last_state = state
        if state in terminal:
            return value
        if time.monotonic() >= deadline:
            raise DeviceError(
                f"Timed out after {timeout_s:.0f}s waiting for {what} (last state: {state})"
            )
        await asyncio.sleep(interval_s)
