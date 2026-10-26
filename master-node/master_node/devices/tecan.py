"""Tecan Infinite M200 Pro through the lab's reader API (runs on the Tecan PC, port 8000).

  GET  /health              -> {"ready": bool}
  POST /drawer/open         -> returns once the drawer is out
  POST /drawer/close        -> returns once the drawer is in
  POST /read/absorbance     <- {"wavelength": int, "wells": [...] | null, "flashes": int}
  POST /read/fluorescence   <- {"excitation_wavelength": int, "emission_wavelength": int,
                                "wells": [...] | null, "flashes": int, "gain": int}
  POST /read/luminescence   <- {"wells": [...] | null, "meas_integration_us": int}
                            -> {"data": [...], "metadata": {...}}

Reads are synchronous: the request returns when the reader has finished, so
start_read runs it in the background and wait_read waits for it. Absorbance at
several wavelengths is one request per wavelength.

The API has no keys, so only the master node should be able to reach it: run the
master node on the Tecan PC (base_url http://127.0.0.1:8000), or firewall the
Tecan PC so port 8000 accepts only the master node PC.
"""

from __future__ import annotations

import asyncio
import itertools
import re
from typing import Any

import httpx

from ..registry import Instrument
from .base import DeviceError, ProgressCallback
from .http import HttpDevice, key_header

WELL = re.compile(r"^[A-P]\d{1,2}$")


class TecanInfiniteHttp(HttpDevice):
    name = "Tecan"

    def __init__(self, settings: Instrument, transport: httpx.AsyncBaseTransport | None = None):
        super().__init__(
            settings.url, settings.request_timeout_s, headers=key_header(settings.api_key), transport=transport
        )
        self._settings = settings
        self._reads: dict[str, asyncio.Task[dict[str, Any]]] = {}
        self._ids = itertools.count(1)
        # One request at a time: the reader can lose data or wedge when its traffic overlaps.
        self._lock = asyncio.Lock()
        self._busy = ""

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        async with self._lock:
            self._busy = "reading" if path.startswith("/read/") else "moving the drawer" if path.startswith("/drawer/") else "checking"
            try:
                return await super()._request(method, path, **kwargs)
            finally:
                self._busy = ""

    async def check_ready(self) -> str | None:
        if self._busy in ("reading", "moving the drawer"):
            # The status bar polls every few seconds; leave the reader alone mid-operation.
            # Anything else (another status check) is waited for, so a check always really checks.
            return f"busy {self._busy}"
        status = await self._request("GET", "/health")
        if not (status or {}).get("ready"):
            raise DeviceError(f"Tecan is not ready: {status}")

    # --- Instrument interface ----------------------------------------------------------

    async def open_access(self) -> str | None:
        await self.open_tray()
        return "drawer opened"

    async def close_access(self) -> str | None:
        await self.close_tray()
        return "drawer closed"

    async def start(self, operation: str, params: dict[str, Any], plate: dict[str, Any]) -> str:
        if operation != "read":
            raise DeviceError(f"The Tecan reader cannot do '{operation}'")
        return await self.start_read(params, plate.get("barcode", ""), plate.get("labware", ""))

    async def wait(self, handle: str, on_progress: ProgressCallback | None = None) -> dict[str, Any]:
        return await self.wait_read(handle, on_progress)

    async def cancel(self, handle: str) -> None:
        await self.abort(handle)

    # --- Reader API ------------------------------------------------------------------------

    async def open_tray(self) -> None:
        await self._request("POST", "/drawer/open")

    async def close_tray(self) -> None:
        await self._request("POST", "/drawer/close")

    async def start_read(self, read: dict[str, Any], plate_id: str, labware: str) -> str:
        if read.get("method"):
            raise DeviceError("This reader runs reads from settings, not saved Tecan methods")
        read_id = f"read-{next(self._ids)}"
        self._reads[read_id] = asyncio.create_task(self._read(read))
        return read_id

    async def wait_read(self, read_id: str, on_progress: ProgressCallback | None = None) -> dict[str, Any]:
        if on_progress:
            on_progress("reading")
        return await self._reads.pop(read_id)

    async def abort(self, read_id: str) -> None:
        # The API cannot interrupt a read in progress; stop waiting for it.
        task = self._reads.pop(read_id, None)
        if task:
            task.cancel()

    async def _read(self, read: dict[str, Any]) -> dict[str, Any]:
        wells = read.get("wells") or None
        flashes = read.get("flashes", 25)
        if read["mode"] == "absorbance":
            calls = [
                (f"Absorbance {nm} nm", "OD", "/read/absorbance", {"wavelength": nm, "wells": wells, "flashes": flashes})
                for nm in read["wavelengths_nm"]
            ]
        elif read["mode"] == "fluorescence":
            body = {
                "excitation_wavelength": read["excitation_nm"], "emission_wavelength": read["emission_nm"],
                "wells": wells, "flashes": flashes,
            }
            if read.get("gain"):
                body["gain"] = read["gain"]
            label = f"Fluorescence Ex {read['excitation_nm']} / Em {read['emission_nm']} nm"
            calls = [(label, "RFU", "/read/fluorescence", body)]
        else:
            body = {"wells": wells, "meas_integration_us": int(read.get("integration_ms", 1000)) * 1000}
            calls = [("Luminescence", "RLU", "/read/luminescence", body)]

        measurements, metadata = [], {"reader": "Tecan Infinite M200 Pro"}
        for label, unit, path, body in calls:
            # A read can take minutes; allow up to the device's operation timeout.
            response = await self._request("POST", path, json=body, timeout=self._settings.timeout_s)
            if not isinstance(response, dict) or "data" not in response:
                raise DeviceError(f"Tecan: {path} returned no data: {str(response)[:200]}")
            measurements.append({"label": label, "unit": unit, "values": readings_by_well(response["data"], wells)})
            metadata.update(response.get("metadata") or {})
        return {"measurements": measurements, "metadata": metadata}


def readings_by_well(data: list[Any], wells: list[str] | None) -> dict[str, float | None]:
    """The reader's `data` as {"A1": value, ...}, accepting the layouts readers commonly use:
    a list of {"well": ..., "value": ...} records, an 8x12 (or 16x24) grid of rows, or a flat
    list in the order of the requested wells (or A1..H12 for a full plate)."""
    if not isinstance(data, list):
        raise DeviceError(f"Tecan returned data in an unknown format: {str(data)[:200]}")
    values: dict[str, float | None]
    if data and all(isinstance(item, dict) for item in data):
        values = {}
        for item in data:
            well = next((item[k] for k in ("well", "Well", "position", "name", "id") if k in item), None)
            value = next((item[k] for k in item if k.lower() in ("value", "od", "rfu", "rlu", "signal", "reading", "result")), None)
            if well is None:
                raise DeviceError(f"Tecan returned a reading without a well name: {str(item)[:200]}")
            values[str(well).upper()] = _number(value)
    elif data and all(isinstance(row, list) for row in data):
        values = {f"{chr(65 + r)}{c + 1}": _number(v) for r, row in enumerate(data) for c, v in enumerate(row)}
    elif wells and len(data) == len(wells):
        values = {well: _number(v) for well, v in zip(wells, data)}
    elif len(data) in (96, 384):
        cols = 12 if len(data) == 96 else 24
        values = {f"{chr(65 + i // cols)}{i % cols + 1}": _number(v) for i, v in enumerate(data)}
    else:
        raise DeviceError(f"Tecan returned {len(data)} readings in an unknown layout: {str(data)[:300]}")
    if wells:
        values = {well: values.get(well) for well in wells}
    return {well: v for well, v in values.items() if WELL.match(well)}


def _number(value: Any) -> float | None:
    """A reading as a number; overflow markers such as "OVER" become None."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
