"""The HTTP drivers against mocked device APIs."""

import asyncio
import json

import httpx
import pytest

from master_node.devices.base import DeviceError, Location
from master_node.devices.flex import FlexHttp
from master_node.devices.job_api import JobApiInstrument, JobApiTransport
from master_node.registry import Instrument


def flex_settings() -> Instrument:
    return Instrument(id="flex", kind="liquid_handler", driver="opentrons_flex", url="http://flex:31950", poll_interval_s=0)


def tecan_settings(base_url: str) -> Instrument:
    return Instrument(id="tecan", kind="plate_reader", driver="tecan_infinite", url=base_url)


class FakeFlex:
    """Just enough of the Opentrons robot-server API for one run."""

    def __init__(self, analysis_result="ok", final_status="succeeded"):
        self.analysis_result = analysis_result
        self.final_status = final_status
        self.calls: list[tuple[str, str]] = []
        self.run_polls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["Opentrons-Version"] == "*"
        method, path = request.method, request.url.path
        self.calls.append((method, path))

        if (method, path) == ("POST", "/protocols"):
            assert b'filename="proto.py"' in request.content
            return httpx.Response(201, json={"data": {"id": "p1", "analysisSummaries": [{"id": "a1", "status": "pending"}]}})
        if (method, path) == ("GET", "/protocols/p1/analyses/a1"):
            errors = [] if self.analysis_result == "ok" else [{"detail": "Labware not found"}]
            return httpx.Response(200, json={"data": {"id": "a1", "status": "completed", "result": self.analysis_result, "errors": errors}})
        if (method, path) == ("POST", "/runs"):
            assert json.loads(request.content) == {"data": {"protocolId": "p1", "runTimeParameterValues": {"volume_ul": 50}}}
            return httpx.Response(201, json={"data": {"id": "r1", "status": "idle"}})
        if (method, path) == ("POST", "/runs/r1/actions"):
            return httpx.Response(201, json={"data": {"actionType": json.loads(request.content)["data"]["actionType"]}})
        if (method, path) == ("GET", "/runs/r1"):
            self.run_polls += 1
            status = "running" if self.run_polls < 3 else self.final_status
            errors = [{"detail": "Pipette collision"}] if status == "failed" else []
            return httpx.Response(200, json={"data": {"id": "r1", "status": status, "errors": errors}})
        if (method, path) == ("PATCH", "/runs/r1"):
            assert json.loads(request.content) == {"data": {"current": False}}
            return httpx.Response(200, json={"data": {"id": "r1", "current": False}})
        return httpx.Response(404, json={"errorId": "notFound"})


def run_flex(fake: FakeFlex) -> list[str]:
    async def go():
        flex = FlexHttp(flex_settings(), transport=httpx.MockTransport(fake))
        progress: list[str] = []
        try:
            run_id = await flex.start_run("proto.py", b"def run(ctx): pass", {"volume_ul": 50})
            await flex.wait_run(run_id, progress.append)
        finally:
            await flex.aclose()
        return progress

    return asyncio.run(go())


def test_flex_uploads_analyses_plays_and_dismisses():
    fake = FakeFlex()
    assert run_flex(fake) == ["running", "succeeded"]
    assert fake.calls == [
        ("POST", "/protocols"),
        ("GET", "/protocols/p1/analyses/a1"),
        ("POST", "/runs"),
        ("POST", "/runs/r1/actions"),
        ("GET", "/runs/r1"),
        ("GET", "/runs/r1"),
        ("GET", "/runs/r1"),
        ("PATCH", "/runs/r1"),
    ]


def test_flex_failed_run_reports_robot_errors():
    fake = FakeFlex(final_status="failed")
    with pytest.raises(DeviceError, match="r1 failed: Pipette collision"):
        run_flex(fake)
    assert fake.calls[-1] == ("PATCH", "/runs/r1")  # still dismissed


def test_flex_rejected_analysis_never_creates_a_run():
    fake = FakeFlex(analysis_result="not-ok")
    with pytest.raises(DeviceError, match="Labware not found"):
        run_flex(fake)
    assert ("POST", "/runs") not in fake.calls


# --- Job API (washer, qPCR, Thor and later instrument servers) ----------------------------


class FakeJobServer:
    def __init__(self, final_state="succeeded", result=None, health=None):
        self.final_state = final_state
        self.result = result or {}
        self.health = health or {"ready": True, "detail": "idle"}
        self.calls: list[tuple[str, str, dict | None]] = []
        self.polls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert request.headers["X-API-Key"] == "master-only"
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, request.url.path, body))
        path = request.url.path
        if path == "/health":
            return httpx.Response(200, json=self.health)
        if path == "/jobs":
            return httpx.Response(201, json={"job_id": "j7"})
        if path == "/jobs/j7":
            self.polls += 1
            state = "running" if self.polls < 2 else self.final_state
            message = "lid jammed" if state == "failed" else ""
            return httpx.Response(200, json={"state": state, "message": message, "result": self.result})
        return httpx.Response(200, json={"ok": True})


def job_settings(**options) -> Instrument:
    return Instrument(id="washer", kind="plate_washer", name="Washer", driver="job_api", url="http://washer",
                      poll_interval_s=0, api_key="master-only", options=options)


def test_job_api_instrument_runs_a_job_and_returns_its_result():
    fake = FakeJobServer(result={"measurements": [{"label": "Ct", "unit": "cycles", "values": {"A1": 21.5}}]})

    async def go():
        washer = JobApiInstrument(job_settings(access=True), transport=httpx.MockTransport(fake))
        try:
            assert await washer.check_ready() == "idle"
            await washer.open_access()
            await washer.close_access()
            job = await washer.start("wash", {"cycles": 3}, {"barcode": "PL1", "labware": "plate", "slot": "carrier"})
            progress: list[str] = []
            return job, await washer.wait(job, progress.append), progress
        finally:
            await washer.aclose()

    job, result, progress = asyncio.run(go())
    assert job == "j7"
    assert progress == ["running", "succeeded"]
    assert result["measurements"][0]["values"] == {"A1": 21.5}
    assert [(m, p) for m, p, _ in fake.calls][:4] == [
        ("GET", "/health"), ("POST", "/access/open"), ("POST", "/access/close"), ("POST", "/jobs"),
    ]
    assert fake.calls[3][2] == {"operation": "wash", "params": {"cycles": 3},
                                "plate": {"barcode": "PL1", "labware": "plate", "slot": "carrier"}}


def test_job_api_failed_job_and_not_ready_raise():
    async def go(fake, check=False):
        washer = JobApiInstrument(job_settings(), transport=httpx.MockTransport(fake))
        try:
            if check:
                await washer.check_ready()
            else:
                await washer.wait(await washer.start("wash", {}, {}))
        finally:
            await washer.aclose()

    with pytest.raises(DeviceError, match="Washer job j7 failed: lid jammed"):
        asyncio.run(go(FakeJobServer(final_state="failed")))
    with pytest.raises(DeviceError, match="Washer is not ready: door open"):
        asyncio.run(go(FakeJobServer(health={"ready": False, "detail": "door open"}), check=True))


def test_job_api_sends_a_protocol_file_as_base64():
    fake = FakeJobServer()

    async def go():
        handler = JobApiInstrument(job_settings(), transport=httpx.MockTransport(fake))
        try:
            await handler.start("liquid_handling", {"protocol_filename": "p.py", "source": b"def run(p): pass",
                                                    "runtime_parameters": {}}, {})
        finally:
            await handler.aclose()

    asyncio.run(go())
    assert fake.calls[0][2]["params"] == {"protocol_filename": "p.py", "runtime_parameters": {},
                                          "protocol_b64": "ZGVmIHJ1bihwKTogcGFzcw=="}


def test_job_api_without_access_option_never_touches_the_drawer():
    fake = FakeJobServer()

    async def go():
        washer = JobApiInstrument(job_settings(), transport=httpx.MockTransport(fake))
        try:
            await washer.open_access()
            await washer.close_access()
        finally:
            await washer.aclose()

    asyncio.run(go())
    assert fake.calls == []


def test_job_api_transport_moves_the_plate():
    fake = FakeJobServer(health={"ready": True, "at_home": True, "holding_plate": False, "detail": "parked"})

    async def go():
        thor = JobApiTransport(
            Instrument(id="thor", kind="transport", name="Thor", driver="job_api", url="http://thor",
                       poll_interval_s=0, api_key="master-only"),
            transport=httpx.MockTransport(fake),
        )
        try:
            status = await thor.status()
            task_id = await thor.start_transfer(Location("storage", "S1"), Location("flex", "D2"), "PL1")
            await thor.wait_transfer(task_id)
            return status
        finally:
            await thor.aclose()

    status = asyncio.run(go())
    assert status.clear and status.message == "parked"
    assert fake.calls[1][2] == {"operation": "transfer",
                                "params": {"from": {"station": "storage", "slot": "S1"}, "to": {"station": "flex", "slot": "D2"}},
                                "plate": {"barcode": "PL1"}}


def test_job_api_transport_reset_marks_thor_home_and_empty_handed():
    fake = FakeJobServer(health={"ready": False, "at_home": False, "holding_plate": True, "detail": "job cancelled"})

    def server(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/reset":
            fake.health = {"ready": True, "at_home": True, "holding_plate": False, "detail": "home, empty-handed"}
        return fake(request)

    async def go():
        thor = JobApiTransport(
            Instrument(id="thor", kind="transport", name="Thor", driver="job_api", url="http://thor",
                       api_key="master-only"),
            transport=httpx.MockTransport(server),
        )
        try:
            return await thor.reset()
        finally:
            await thor.aclose()

    status = asyncio.run(go())
    assert status.clear and status.message == "home, empty-handed"
    assert [call[:2] for call in fake.calls] == [("POST", "/reset"), ("GET", "/health")]


def test_unreachable_device_is_a_device_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    async def go():
        thor = JobApiTransport(
            Instrument(id="thor", kind="transport", name="Thor", driver="job_api", url="http://thor"),
            transport=httpx.MockTransport(handler),
        )
        try:
            await thor.status()
        finally:
            await thor.aclose()

    with pytest.raises(DeviceError, match="Thor: GET /health failed"):
        asyncio.run(go())


# --- Tecan Infinite M200 Pro API ------------------------------------------------------------

from master_node.devices.tecan import TecanInfiniteHttp, readings_by_well


class FakeInfinite:
    def __init__(self, data):
        self.data = data
        self.calls: list[tuple[str, dict | None]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.url.path, body))
        if request.url.path == "/health":
            return httpx.Response(200, json={"ready": True})
        if request.url.path.startswith("/drawer/"):
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(200, json={"data": self.data, "metadata": {"temperature_c": 25.1}})


def read_with(fake, read):
    async def go():
        tecan = TecanInfiniteHttp(tecan_settings("http://tecan"), transport=httpx.MockTransport(fake))
        try:
            await tecan.check_ready()
            await tecan.open_tray()
            await tecan.close_tray()
            read_id = await tecan.start_read(read, "PL-1", "corning_96_wellplate_360ul_flat")
            return await tecan.wait_read(read_id)
        finally:
            await tecan.aclose()

    return asyncio.run(go())


def test_tecan_absorbance_reads_each_wavelength():
    fake = FakeInfinite([{"well": "A1", "value": 0.5}, {"well": "A2", "value": "OVER"}])
    result = read_with(fake, {"mode": "absorbance", "wavelengths_nm": [450, 600], "wells": ["A1", "A2"], "flashes": 10})

    assert [c[0] for c in fake.calls] == ["/health", "/drawer/open", "/drawer/close", "/read/absorbance", "/read/absorbance"]
    assert fake.calls[3][1] == {"wavelength": 450, "wells": ["A1", "A2"], "flashes": 10}
    assert fake.calls[4][1]["wavelength"] == 600
    assert [m["label"] for m in result["measurements"]] == ["Absorbance 450 nm", "Absorbance 600 nm"]
    assert result["measurements"][0]["values"] == {"A1": 0.5, "A2": None}  # overflow -> no value
    assert result["metadata"]["temperature_c"] == 25.1


def test_tecan_fluorescence_and_luminescence_bodies():
    fake = FakeInfinite([[1.0, 2.0], [3.0, 4.0]])
    result = read_with(fake, {"mode": "fluorescence", "excitation_nm": 485, "emission_nm": 528, "gain": 80,
                              "wells": ["A1", "B2"], "flashes": 25})
    assert fake.calls[-1] == ("/read/fluorescence", {"excitation_wavelength": 485, "emission_wavelength": 528,
                                                     "wells": ["A1", "B2"], "flashes": 25, "gain": 80})
    assert result["measurements"][0]["values"] == {"A1": 1.0, "B2": 4.0}

    fake = FakeInfinite([7.0, 8.0])
    result = read_with(fake, {"mode": "luminescence", "integration_ms": 500, "wells": ["C3", "C4"]})
    assert fake.calls[-1] == ("/read/luminescence", {"wells": ["C3", "C4"], "meas_integration_us": 500_000})
    assert result["measurements"][0]["values"] == {"C3": 7.0, "C4": 8.0}


def test_tecan_full_plate_flat_list_and_unknown_layouts():
    flat = list(range(96))
    values = readings_by_well(flat, None)
    assert values["A1"] == 0 and values["A12"] == 11 and values["H12"] == 95
    with pytest.raises(DeviceError, match="unknown layout"):
        readings_by_well([1, 2, 3], ["A1"])


def test_tecan_rejects_saved_methods_and_reports_not_ready():
    async def go(handler, read=None):
        tecan = TecanInfiniteHttp(tecan_settings("http://tecan"), transport=httpx.MockTransport(handler))
        try:
            if read:
                await tecan.start_read(read, "", "")
            else:
                await tecan.check_ready()
        finally:
            await tecan.aclose()

    with pytest.raises(DeviceError, match="not saved Tecan methods"):
        asyncio.run(go(FakeInfinite([]), {"mode": "absorbance", "method": "MyMethod", "wells": ["A1"]}))
    with pytest.raises(DeviceError, match="not ready"):
        asyncio.run(go(lambda r: httpx.Response(200, json={"ready": False})))


# --- Tecan Infinite M200 Pro API ------------------------------------------------------------

from master_node.devices.tecan import TecanInfiniteHttp, readings_by_well


class FakeTecan:
    def __init__(self, data):
        self.data = data
        self.calls: list[tuple[str, dict | None]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.url.path, body))
        if request.url.path == "/health":
            return httpx.Response(200, json={"ready": True})
        if request.url.path.startswith("/read/"):
            return httpx.Response(200, json={"data": self.data, "metadata": {"temperature_c": 25.1}})
        return httpx.Response(200, json={"ok": True})


def run_tecan(fake: FakeTecan, read: dict):
    async def go():
        tecan = TecanInfiniteHttp(tecan_settings("http://tecan:8000"), transport=httpx.MockTransport(fake))
        try:
            await tecan.check_ready()
            await tecan.open_tray()
            await tecan.close_tray()
            read_id = await tecan.start_read(read, "PL-1", "corning_96_wellplate_360ul_flat")
            return await tecan.wait_read(read_id)
        finally:
            await tecan.aclose()

    return asyncio.run(go())


def test_tecan_absorbance_is_one_read_per_wavelength():
    fake = FakeTecan([{"well": "A1", "value": 0.5}, {"well": "A2", "value": "OVER"}])
    read = {"mode": "absorbance", "wavelengths_nm": [450, 600], "wells": ["A1", "A2"], "flashes": 10}
    result = run_tecan(fake, read)
    assert [path for path, _ in fake.calls] == [
        "/health", "/drawer/open", "/drawer/close", "/read/absorbance", "/read/absorbance",
    ]
    assert fake.calls[3][1] == {"wavelength": 450, "wells": ["A1", "A2"], "flashes": 10}
    assert [m["label"] for m in result["measurements"]] == ["Absorbance 450 nm", "Absorbance 600 nm"]
    assert result["measurements"][0]["values"] == {"A1": 0.5, "A2": None}  # overflow -> no value
    assert result["metadata"]["temperature_c"] == 25.1


def test_tecan_fluorescence_and_luminescence_requests():
    fake = FakeTecan([[1.0] * 12] * 8)
    run_tecan(fake, {"mode": "fluorescence", "excitation_nm": 485, "emission_nm": 528, "gain": 80,
                     "wells": None, "flashes": 25})
    assert fake.calls[-1] == ("/read/fluorescence", {"excitation_wavelength": 485, "emission_wavelength": 528,
                                                     "wells": None, "flashes": 25, "gain": 80})
    fake = FakeTecan(list(range(96)))
    result = run_tecan(fake, {"mode": "luminescence", "integration_ms": 500, "wells": None, "flashes": 25})
    assert fake.calls[-1] == ("/read/luminescence", {"wells": None, "meas_integration_us": 500000})
    assert result["measurements"][0]["values"]["H12"] == 95.0


@pytest.mark.parametrize("data, wells, expected", [
    ([[0.1, 0.2], [0.3, 0.4]], ["A2", "B1"], {"A2": 0.2, "B1": 0.3}),          # grid of rows
    ([0.7, 0.8], ["C3", "C4"], {"C3": 0.7, "C4": 0.8}),                         # flat, in requested order
    ([{"Well": "b2", "OD": "1.5"}], None, {"B2": 1.5}),                          # records
])
def test_tecan_data_layouts(data, wells, expected):
    assert readings_by_well(data, wells) == expected


def test_tecan_saved_methods_are_refused():
    with pytest.raises(DeviceError, match="saved Tecan methods"):
        run_tecan(FakeTecan([]), {"mode": "absorbance", "wavelengths_nm": [600], "wells": None, "method": "OD600"})


def test_tecan_is_not_polled_while_reading():
    seen: list[str] = []
    release = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/read/absorbance":
            await release.wait()
            return httpx.Response(200, json={"data": [[0.5]], "metadata": {}})
        return httpx.Response(200, json={"ready": True})

    async def go():
        tecan = TecanInfiniteHttp(tecan_settings("http://tecan"), transport=httpx.MockTransport(handler))
        try:
            read_id = await tecan.start_read({"mode": "absorbance", "wavelengths_nm": [600], "wells": ["D1"]}, "", "")
            await asyncio.sleep(0.05)
            assert await tecan.check_ready() == "busy reading"
            release.set()
            result = await tecan.wait_read(read_id)
            assert await tecan.check_ready() is None
            return result
        finally:
            await tecan.aclose()

    asyncio.run(go())
    assert seen == ["/read/absorbance", "/health"]


def test_a_status_check_waits_for_one_in_flight_and_still_fails_when_unreachable():
    calls = 0
    release = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        await release.wait()
        raise httpx.ConnectError("All connection attempts failed")

    async def go():
        tecan = TecanInfiniteHttp(tecan_settings("http://tecan"), transport=httpx.MockTransport(handler))
        try:
            first = asyncio.create_task(tecan.check_ready())
            await asyncio.sleep(0.05)
            second = asyncio.create_task(tecan.check_ready())  # e.g. the pre-run check
            await asyncio.sleep(0.05)
            assert not second.done()
            release.set()
            for task in (first, second):
                with pytest.raises(DeviceError, match="ConnectError"):
                    await task
        finally:
            await tecan.aclose()

    asyncio.run(go())
    assert calls == 2
