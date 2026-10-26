"""End-to-end runs through the HTTP API with simulated instruments."""

import time

from fastapi.testclient import TestClient

from master_node.devices.manual import ManualThor
from master_node.devices.sim import SimFlex, SimInstrument, SimThor
from master_node.main import create_app
from master_node.registry import Instrument

from .conftest import fast_registry, liquid_step, make_submission, make_workflow, read_step, sim_lab, wait_for_status

WASHER = Instrument(id="washer", kind="plate_washer", name="Plate washer", poll_interval_s=0.01)
QPCR = Instrument(id="qpcr", kind="qpcr", name="qPCR", poll_interval_s=0.01)


def test_full_run_moves_plate_and_stores_results(app_with):
    thor = SimThor(0.001)
    with app_with(thor=thor) as client:
        created = client.post("/runs", json=make_submission())
        assert created.status_code == 201, created.text
        assert "protocol_b64" not in created.json()["protocol"]["steps"][0]

        run = wait_for_status(client, created.json()["id"], {"succeeded", "failed"})
        assert run["status"] == "succeeded", run["error"]
        assert {s["status"] for s in run["steps"]} == {"done"}
        assert [s["label"] for s in run["steps"]] == [
            "Decode + plan", "Fetch plate to Opentrons Flex", "Opentrons Flex is clear", "Run protocol",
            "Move plate to Tecan Infinite M200 Pro", "Tecan Infinite M200 Pro is clear", "Read plate",
        ]
        assert run["has_results"]

        assert [(str(a), str(b)) for a, b in thor.transfers] == [
            ("storage/R1-S3", "flex/D2"),
            ("flex/D2", "tecan/carrier"),
        ]
        messages = [e["message"] for e in run["events"]]
        opened = messages.index("Tecan Infinite M200 Pro: drawer opened")
        assert opened < messages.index("Tecan Infinite M200 Pro: drawer closed")
        assert not any("Opentrons Flex: drawer" in m for m in messages)  # nothing to open on the Flex

        results = client.get(f"/runs/{run['id']}/results").json()
        assert results["measurements"][0]["label"] == "Absorbance 600 nm"
        assert len(results["measurements"][0]["values"]) == 96

        csv_lines = client.get(f"/runs/{run['id']}/results.csv").text.splitlines()
        assert csv_lines[0] == "run_id,plate,well,measurement,unit,value"
        assert len(csv_lines) == 97
        assert csv_lines[1].split(",")[2] == "A1"
        assert csv_lines[2].split(",")[2] == "A2"

        listed = client.get("/runs").json()
        assert [r["id"] for r in listed] == [run["id"]]


def test_workflow_across_four_instruments(app_with):
    thor, washer, qpcr = SimThor(0.001), SimInstrument(0.001), SimInstrument(0.001)
    with app_with(WASHER, QPCR, thor=thor, washer=washer, qpcr=qpcr) as client:
        workflow = make_workflow(
            liquid_step(),
            {"type": "wash", "cycles": 2, "volume_ul": 250},
            {"type": "hold", "seconds": 60, "label": "Incubate"},
            read_step(),
            {"type": "qpcr", "program": "SYBR-40"},
        )
        run_id = client.post("/runs", json=workflow).json()["id"]
        run = wait_for_status(client, run_id, {"succeeded", "failed"})
        assert run["status"] == "succeeded", run["error"]

        assert [(a.station, b.station) for a, b in thor.transfers] == [
            ("storage", "flex"), ("flex", "washer"), ("washer", "tecan"), ("tecan", "qpcr"),
        ]
        assert washer.jobs == [("wash", {"program": "", "cycles": 2, "volume_ul": 250, "soak_s": 0})]
        assert qpcr.jobs == [("qpcr", {"program": "SYBR-40", "params": {}})]
        assert [s["device"] for s in run["steps"] if s["key"] == "operation"] == ["flex", "washer", "tecan", "qpcr"]
        assert any(s["label"] == "Incubate" and s["status"] == "done" for s in run["steps"])

        labels = [m["label"] for m in client.get(f"/runs/{run_id}/results").json()["measurements"]]
        assert labels == ["Step 4 Read plate: Absorbance 450 nm", "Step 5 qPCR run: Ct"]
        assert run["outputs"]  # the washer's metadata


def test_plate_stays_put_between_steps_on_the_same_instrument(app_with):
    thor = SimThor(0.001)
    with app_with(thor=thor) as client:
        run_id = client.post("/runs", json=make_workflow(read_step(), read_step(wavelengths_nm=[600]))).json()["id"]
        run = wait_for_status(client, run_id, {"succeeded", "failed"})
        assert run["status"] == "succeeded", run["error"]
        assert len(thor.transfers) == 1


def test_flex_failure_stops_the_run_before_moving_the_plate(app_with):
    thor = SimThor(0.001)
    with app_with(thor=thor, flex=SimFlex(0.001, fail_runs=True)) as client:
        run_id = client.post("/runs", json=make_submission()).json()["id"]
        run = wait_for_status(client, run_id, {"succeeded", "failed"})

        assert run["status"] == "failed"
        statuses = [(s["label"], s["status"]) for s in run["steps"]]
        assert ("Run protocol", "failed") in statuses
        assert statuses[-1] == ("Read plate", "skipped")
        assert len(thor.transfers) == 1  # the plate stays on the Flex
        assert "no tip detected" in run["error"]


def test_abort_stops_the_flex(app_with):
    flex = SimFlex(0.05)  # ~1 s run, long enough to abort mid-way
    with app_with(flex=flex) as client:
        run_id = client.post("/runs", json=make_submission()).json()["id"]

        deadline = time.monotonic() + 5
        while client.get("/runs").json()[0]["current_step"] != "Run protocol":
            assert time.monotonic() < deadline, "never reached 'Run protocol'"
            time.sleep(0.01)
        time.sleep(0.2)  # past the upload, into the run

        assert client.post(f"/runs/{run_id}/abort").status_code == 202
        run = wait_for_status(client, run_id, {"aborted"})

        assert flex.stopped == ["flex-1"]
        statuses = {s["label"]: s["status"] for s in run["steps"]}
        assert statuses["Run protocol"] == "failed"
        assert statuses["Read plate"] == "skipped"
        assert client.post(f"/runs/{run_id}/abort").status_code == 409


def test_runs_execute_one_at_a_time_in_order(app_with):
    with app_with() as client:
        first = client.post("/runs", json=make_submission(name="first")).json()["id"]
        second = client.post("/runs", json=make_submission(name="second")).json()["id"]
        a = wait_for_status(client, first, {"succeeded"})
        b = wait_for_status(client, second, {"succeeded"})
        assert a["steps"][-1]["finished_at"] <= b["steps"][1]["started_at"]


def test_abort_while_queued(app_with):
    with app_with(flex=SimFlex(0.05)) as client:
        client.post("/runs", json=make_submission(name="first"))
        second = client.post("/runs", json=make_submission(name="second")).json()["id"]
        assert client.post(f"/runs/{second}/abort").status_code == 202
        run = client.get(f"/runs/{second}").json()
        assert run["status"] == "aborted"
        assert {s["status"] for s in run["steps"][1:]} == {"skipped"}


def test_reads_only_the_selected_wells_in_each_mode(app_with):
    with app_with() as client:
        submission = make_submission(
            read={"mode": "fluorescence", "excitation_nm": 485, "emission_nm": 535, "wells": ["b2", "A1", "A1"]}
        )
        run_id = client.post("/runs", json=submission).json()["id"]
        assert client.get(f"/runs/{run_id}").json()["protocol"]["steps"][1]["settings"]["wells"] == ["A1", "B2"]
        wait_for_status(client, run_id, {"succeeded"})

        measurement = client.get(f"/runs/{run_id}/results").json()["measurements"][0]
        assert measurement["label"] == "Fluorescence Ex 485 / Em 535 nm"
        assert measurement["unit"] == "RFU"
        assert sorted(measurement["values"]) == ["A1", "B2"]


def test_invalid_read_settings_are_rejected(app_with):
    bad_reads = [
        ({"mode": "absorbance", "wavelengths_nm": [], "wells": ["A1"]}, "at least one wavelength"),
        ({"mode": "absorbance", "wavelengths_nm": [150], "wells": ["A1"]}, "230-1000 nm"),
        ({"mode": "fluorescence", "excitation_nm": 485, "wells": ["A1"]}, "excitation_nm and emission_nm"),
        ({"mode": "fluorescence", "excitation_nm": 535, "emission_nm": 485, "wells": ["A1"]}, "longer than excitation"),
        ({"mode": "luminescence", "wells": ["Z9"]}, "not well names"),
        ({"mode": "luminescence", "wells": []}, "at least 1"),
    ]
    with app_with() as client:
        for read, message in bad_reads:
            response = client.post("/runs", json=make_submission(read=read))
            assert response.status_code == 422, read
            assert message in response.text, (read, response.text)


def test_invalid_protocol_is_rejected_at_submission(app_with):
    with app_with() as client:
        response = client.post("/runs", json=make_submission(source=b"x = 1\n"))
        assert response.status_code == 422
        assert "run(protocol)" in response.json()["detail"]
        assert client.get("/runs").json() == []


def test_steps_with_no_instrument_are_rejected(app_with):
    with app_with() as client:
        response = client.post("/runs", json=make_workflow({"type": "wash"}))
        assert response.status_code == 422
        assert "needs a plate washer, and the registry has none" in response.json()["detail"]


def test_safety_limits_reject_steps_and_plan_previews_without_queuing(settings):
    tecan_absorbance_only = fast_registry()
    tecan_absorbance_only.get("tecan").limits = {"modes": ["absorbance"], "wavelength_nm": [300, 800]}
    tecan_absorbance_only.get("tecan").api_key = "secret"
    with TestClient(create_app(settings, sim_lab(tecan_absorbance_only), tecan_absorbance_only)) as client:
        fluorescence = make_workflow(read_step(mode="fluorescence", excitation_nm=485, emission_nm=535))
        response = client.post("/plan", json=fluorescence)
        assert response.status_code == 422
        assert "fluorescence is not available (only absorbance)" in response.json()["detail"]

        too_short = client.post("/runs", json=make_workflow(read_step(wavelengths_nm=[260])))
        assert too_short.status_code == 422
        assert "wavelength 260 nm outside 300-800 nm" in too_short.json()["detail"]

        plan = client.post("/plan", json=make_workflow(liquid_step(), read_step()))
        assert plan.status_code == 200, plan.text
        assert [s["device"] for s in plan.json()["steps"]] == ["master", "thor", "thor", "flex", "thor", "thor", "tecan"]
        assert client.get("/runs").json() == []  # nothing queued

        listed = client.get("/instruments").json()["instruments"]
        assert [i["id"] for i in listed] == ["thor", "flex", "tecan"]
        assert all("api_key" not in i for i in listed)


def test_named_instrument_must_exist_and_be_the_right_kind(app_with):
    with app_with() as client:
        missing = client.post("/plan", json=make_workflow({**read_step(), "instrument": "spark"}))
        assert "no instrument 'spark'" in missing.json()["detail"]
        wrong = client.post("/plan", json=make_workflow({**read_step(), "instrument": "flex"}))
        assert "Opentrons Flex is a liquid handler, not a plate reader" in wrong.json()["detail"]


def test_preflight_blocks_runs_while_thor_is_holding_a_plate_until_a_person_resets_it(app_with):
    thor = SimThor(0.001)
    thor._holding = True
    with app_with(thor=thor) as client:
        run_id = client.post("/runs", json=make_submission()).json()["id"]
        run = wait_for_status(client, run_id, {"failed"})
        assert run["error"].startswith("Preflight check failed")
        assert thor.transfers == []

        reset = client.post("/transport/reset")
        assert reset.json() == {"at_home": True, "holding_plate": False, "detail": "simulated"}
        run_id = client.post("/runs", json=make_submission()).json()["id"]
        wait_for_status(client, run_id, {"succeeded"})


def test_transport_reset_is_refused_while_a_run_is_running(app_with):
    with app_with(flex=SimFlex(0.05)) as client:
        run_id = client.post("/runs", json=make_submission()).json()["id"]
        deadline = time.monotonic() + 5
        while client.get("/runs").json()[0]["status"] != "running":
            assert time.monotonic() < deadline, "never started"
            time.sleep(0.01)

        response = client.post("/transport/reset")
        assert response.status_code == 409
        assert run_id in response.json()["detail"]
        wait_for_status(client, run_id, {"succeeded"})


def test_devices_lists_every_registered_instrument(app_with):
    with app_with(WASHER) as client:
        devices = client.get("/devices").json()
        assert list(devices) == ["thor", "flex", "tecan", "washer"]
        assert devices["washer"] == {"ok": True, "detail": "ready", "mode": "sim", "name": "Plate washer", "kind": "plate_washer"}


def test_api_key_is_enforced(settings, app_with):
    settings.server.api_key = "s3cret"
    with app_with() as client:
        assert client.get("/health").status_code == 200
        assert client.get("/runs").status_code == 401
        assert client.get("/runs", headers={"X-API-Key": "wrong"}).status_code == 401
        assert client.get("/runs", headers={"X-API-Key": "s3cret"}).status_code == 200


def test_restart_marks_running_runs_interrupted_and_requeues_queued(settings, app_with):
    from master_node.models import ProtocolSubmission, Run, RunStatus, Step, StepStatus
    from master_node.planner import plan_run
    from master_node.store import RunStore

    store = RunStore(settings.server.database)
    for run_id, status in [("mid-flight", RunStatus.running), ("waiting", RunStatus.queued)]:
        submission = ProtocolSubmission(**make_submission())
        steps, _ = plan_run(submission, fast_registry())
        run = Run(id=run_id, status=status, protocol=submission, steps=steps)
        if status is RunStatus.running:
            run.steps[1].status = StepStatus.running
        store.save(run)
    # Queued by an older master node, with its fixed step names.
    old = Run(id="old", status=RunStatus.queued, protocol=ProtocolSubmission(**make_submission()),
              steps=[Step(key="fetch_plate", label="Fetch plate", device="thor")])
    store.save(old)
    store.close()

    with app_with() as client:
        interrupted = client.get("/runs/mid-flight").json()
        assert interrupted["status"] == "interrupted"
        assert interrupted["steps"][1]["status"] == "failed"
        assert wait_for_status(client, "waiting", {"succeeded"})["status"] == "succeeded"
        assert client.get("/runs/old").json()["error"] == "Planned by an older version of the master node; send it again"


def test_manual_thor_waits_for_a_person_at_each_move(settings):
    registry = fast_registry()
    registry.get("thor").driver = "manual"
    lab = sim_lab(registry, thor=ManualThor(timeout_s=10))
    with TestClient(create_app(settings, lab, registry)) as client:
        run_id = client.post("/runs", json=make_submission()).json()["id"]
        moves = []
        for expected_to in ("flex", "tecan"):
            deadline = time.monotonic() + 5
            while not (move := client.get(f"/runs/{run_id}").json()["context"].get("manual_move")):
                assert time.monotonic() < deadline, "run never asked for the plate to be moved"
                time.sleep(0.02)
            moves.append((move["from"]["station"], move["to"]["station"]))
            assert move["to"]["station"] == expected_to
            time.sleep(0.1)
            assert client.get(f"/runs/{run_id}").json()["status"] == "running"  # still waiting
            assert client.post(f"/runs/{run_id}/plate-moved").status_code == 202
        run = wait_for_status(client, run_id, {"succeeded", "failed"})
        assert run["status"] == "succeeded", run["error"]
        assert moves == [("storage", "flex"), ("flex", "tecan")]
        assert run["steps"][1]["detail"] == "Plate placed at flex/D2 by hand"
        assert client.post(f"/runs/{run_id}/plate-moved").status_code == 409
        assert client.get("/devices").json()["thor"]["detail"] == "manual: a person moves the plate"
