from __future__ import annotations

from ..registry import Instrument as InstrumentEntry, Registry
from .base import DeviceError, Instrument, Lab, Location, ThorStatus, Transport
from .flex import FlexHttp
from .job_api import JobApiInstrument, JobApiTransport
from .manual import ManualThor
from .sim import SimFlex, SimInstrument, SimTecan, SimThor
from .tecan import TecanInfiniteHttp

__all__ = ["DeviceError", "Instrument", "Lab", "Location", "ThorStatus", "Transport", "build_lab"]


def build_lab(registry: Registry, time_scale: float = 1.0) -> Lab:
    """A driver for every instrument in the registry."""
    transport = registry.transport
    return Lab(
        transport=_transport(transport, time_scale) if transport else None,
        instruments={i.id: _instrument(i, time_scale) for i in registry.instruments if i.kind != "transport"},
    )


def _transport(entry: InstrumentEntry, time_scale: float) -> Transport:
    if entry.driver == "manual":
        return ManualThor(entry.timeout_s)
    if entry.driver == "sim":
        return SimThor(time_scale)
    return JobApiTransport(entry)


def _instrument(entry: InstrumentEntry, time_scale: float) -> Instrument:
    if entry.driver == "opentrons_flex":
        return FlexHttp(entry)
    if entry.driver == "tecan_infinite":
        return TecanInfiniteHttp(entry)
    if entry.driver == "job_api":
        return JobApiInstrument(entry)
    # Simulated: the Flex and Tecan simulators behave like those instruments; anything else is generic.
    if entry.kind == "liquid_handler":
        return SimFlex(time_scale)
    if entry.kind == "plate_reader":
        return SimTecan(time_scale)
    return SimInstrument(time_scale)
