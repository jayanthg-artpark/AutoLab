"""Client for the master node's API: submits runs and relays run status to the browser."""

from __future__ import annotations

from typing import Any

import httpx

from .config import MasterSettings


class MasterUnavailable(Exception):
    """The master node could not be reached."""


class MasterError(Exception):
    def __init__(self, status_code: int, detail: Any):
        super().__init__(str(detail))
        self.status_code = status_code
        self.detail = detail


class MasterClient:
    def __init__(self, settings: MasterSettings, transport: httpx.AsyncBaseTransport | None = None):
        headers = {"X-API-Key": settings.api_key} if settings.api_key else {}
        self._client = httpx.AsyncClient(
            base_url=settings.url, headers=headers, timeout=settings.timeout_s, transport=transport
        )

    async def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Returns the master node's response as-is, raising only if it is unreachable."""
        try:
            return await self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise MasterUnavailable(f"Cannot reach the master node at {self._client.base_url}: {exc!r}") from exc

    async def submit_run(self, payload: dict[str, Any]) -> dict[str, Any]:
        response = await self.request("POST", "/runs", json=payload)
        if response.is_error:
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text
            raise MasterError(response.status_code, detail)
        return response.json()

    async def aclose(self) -> None:
        await self._client.aclose()
