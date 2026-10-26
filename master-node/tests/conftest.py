from __future__ import annotations

import base64
import time
from typing import Any

import pytest
from fastapi.testclient import TestClient

from master_node.config import Settings
from master_node.devices import Lab
from master_node.devices.sim import SimFlex, SimInstrument, SimTecan, SimThor
from master_node.main import create_app
from master_node.registry import Instrument, Registry, default_registry

PROTOCOL = b'''
from opentrons import protocol_api

requirements = {"robotType": "Flex", "apiLevel": "2.20"}

def run(protocol: protocol_api.ProtocolContext):
    plate = protocol.load_labware("corning_96_wellplate_360ul_flat", "D2")
'''


ALL_WELLS = [f"{row}{col}" for row in "ABCDEFGH" for col in range(1, 13)]


def make_submission(source: bytes = PROTOCOL, **overrides: Any) -> dict[str, Any]:
    """The older fixed shape (one protocol, one read), which the master node still accepts."""
    submission = {
        "name": "OD600 growth check",
        "scientist": "Test Scientist",
        "plate": {"barcode": "PL-0001", "storage_slot": "R1-S3"},
        "liquid_handling": {
            "protocol_filename": "growth.py",
            "protocol_b64": base64.b64encode(source).decode(),
        },
        "read": {"mode": "absorbance", "wavelengths_nm": [600], "wells": ALL_WELLS},
    }
    submission.update(overrides)
    return submission


def make_workflow(*steps: dict[str, Any], **overrides: Any) -> dict[str, Any]:
    submission = {
        "name": "Workflow",
        "scientist": "Test Scientist",
        "plate": {"barcode": "PL-0002", "storage_slot": "R1-S4"},
        "steps": list(steps),
    }
    submission.update(overrides)
    return submission


def liquid_step(source: bytes = PROTOCOL, **overrides: Any) -> dict[str, Any]:
    return {"type": "liquid_handling", "protocol_filename": "p.py",
            "protocol_b64": base64.b64encode(source).decode(), **overrides}


def read_step(**settings: Any) -> dict[str, Any]:
    return {"type": "read", "settings": {"mode": "absorbance", "wavelengths_nm": [450], "wells": ["A1", "A2"], **settings}}


def fast_registry(*extra: Instrument) -> Registry:
    registry = default_registry()
    for inst in registry.instruments:
        inst.poll_interval_s = 0.01
        inst.clear_timeout_s = 1.0
    return Registry(registry.instruments + list(extra))


def sim_lab(registry: Registry, **devices: Any) -> Lab:
    """Simulated drivers for the registry, with any given by id swapped in."""
    instruments = {}
    for inst in registry.instruments:
        if inst.kind == "transport":
            continue
        default = {"liquid_handler": SimFlex, "plate_reader": SimTecan}.get(inst.kind, SimInstrument)
        instruments[inst.id] = devices.get(inst.id) or default(0.001)
    return Lab(transport=devices.get("thor") or SimThor(0.001), instruments=instruments)


@pytest.fixture
def settings(tmp_path) -> Settings:
    s = Settings()
    s.server.database = str(tmp_path / "runs.db")
    s.server.instruments = str(tmp_path / "no-instruments.toml")
    s.sim.time_scale = 0.001
    return s


@pytest.fixture
def app_with(settings):
    """create_app with a fast registry and simulated lab; give drivers by instrument id."""

    def build(*extra: Instrument, **devices: Any):
        registry = fast_registry(*extra)
        return TestClient(create_app(settings, sim_lab(registry, **devices), registry, hold_scale=0.001))

    return build


def wait_for_status(client: TestClient, run_id: str, statuses: set[str], timeout_s: float = 10) -> dict:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        run = client.get(f"/runs/{run_id}").json()
        if run["status"] in statuses:
            return run
        time.sleep(0.02)
    raise AssertionError(f"Run {run_id} stuck in {run['status']}: {run['events']}")
