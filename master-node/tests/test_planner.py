import base64

import pytest

from master_node.models import ProtocolSubmission, StepStatus
from master_node.planner import PlanError, plan_run
from master_node.registry import Instrument, Registry, default_registry

from .conftest import PROTOCOL, liquid_step, make_submission, make_workflow, read_step


def plan(submission, registry=None):
    return plan_run(ProtocolSubmission(**submission), registry or default_registry())


def test_plan_follows_the_sequence_diagram():
    steps, warnings = plan(make_submission())

    assert [s.label for s in steps] == [
        "Decode + plan",
        "Fetch plate to Opentrons Flex", "Opentrons Flex is clear", "Run protocol",
        "Move plate to Tecan Infinite M200 Pro", "Tecan Infinite M200 Pro is clear", "Read plate",
    ]
    assert [s.id for s in steps][:3] == ["1-decode_plan", "2-transfer", "3-clear"]
    assert steps[0].status is StepStatus.done
    assert all(s.status is StepStatus.pending for s in steps[1:])
    assert steps[1].params == {
        "from": {"station": "storage", "slot": "R1-S3"},
        "to": {"station": "flex", "slot": "D2"},
        "access": ["flex"],
    }
    assert steps[4].params["from"] == {"station": "flex", "slot": "D2"}
    assert steps[4].params["to"] == {"station": "tecan", "slot": "carrier"}
    assert steps[4].params["access"] == ["flex", "tecan"]
    assert warnings == []


def test_protocol_deck_slot_overrides_default():
    submission = make_submission(source=PROTOCOL.replace(b'"D2"', b'"C3"'))
    submission["liquid_handling"]["deck_slot"] = "C3"
    steps, warnings = plan(submission)
    assert steps[1].params["to"] == {"station": "flex", "slot": "C3"}
    assert warnings == []


def test_warns_when_protocol_never_uses_the_plate_slot():
    submission = make_submission()
    submission["liquid_handling"]["deck_slot"] = "B1"
    _, warnings = plan(submission)
    assert any("slot B1" in w for w in warnings)


def test_rejects_slots_the_transport_cannot_reach():
    for slot in ("A1", "A2", "A3", "D3"):
        submission = make_submission(source=PROTOCOL.replace(b'"D2"', f'"{slot}"'.encode()))
        submission["liquid_handling"]["deck_slot"] = slot
        with pytest.raises(PlanError, match=f"Jetson Thor cannot place the plate in Opentrons Flex slot {slot}"):
            plan(submission)


def test_rejects_bad_base64():
    submission = make_submission()
    submission["liquid_handling"]["protocol_b64"] = "not base64!!"
    with pytest.raises(PlanError, match="base64"):
        plan(submission)


def test_rejects_protocol_without_run_function():
    submission = make_submission(source=b"print('hello')\n")
    with pytest.raises(PlanError, match="run\\(protocol\\)"):
        plan(submission)


def test_rejects_non_utf8_protocol():
    submission = make_submission()
    submission["liquid_handling"]["protocol_b64"] = base64.b64encode(b"\xff\xfe def run(").decode()
    with pytest.raises(PlanError, match="UTF-8"):
        plan(submission)


def test_picks_the_first_instrument_whose_limits_allow_the_step():
    registry = default_registry()
    registry.get("tecan").limits = {"modes": ["absorbance"]}
    spark = Instrument(id="spark", kind="plate_reader", name="Spark")
    registry = Registry(registry.instruments + [spark])

    steps, _ = plan(make_workflow(read_step()), registry)
    assert steps[-1].device == "tecan"
    steps, _ = plan(make_workflow(read_step(mode="luminescence")), registry)
    assert steps[-1].device == "spark"


def test_wash_limits():
    washer = Instrument(id="washer", kind="plate_washer",
                        limits={"programs": ["ELISA-3x"], "max_volume_ul": 350, "max_cycles": 5})
    registry = Registry(default_registry().instruments + [washer])
    plan(make_workflow({"type": "wash", "program": "ELISA-3x"}), registry)
    with pytest.raises(PlanError, match="program 'Other' is not one of ELISA-3x"):
        plan(make_workflow({"type": "wash", "program": "Other"}), registry)
    with pytest.raises(PlanError, match="volume 400 uL outside 0-350 uL"):
        plan(make_workflow({"type": "wash", "volume_ul": 400}), registry)
    with pytest.raises(PlanError, match="cycles 6 outside 1-5"):
        plan(make_workflow({"type": "wash", "cycles": 6}), registry)


def test_labware_limit():
    registry = default_registry()
    registry.get("tecan").limits = {"labware": ["nest_96_wellplate_200ul_flat"]}
    with pytest.raises(PlanError, match="plate corning_96_wellplate_360ul_flat is not one of"):
        plan(make_workflow(read_step()), registry)


def test_holds_need_no_instrument_and_no_move():
    steps, _ = plan(make_workflow(liquid_step(), {"type": "hold", "seconds": 600}))
    assert [s.key for s in steps] == ["decode_plan", "transfer", "clear", "operation", "hold"]
    assert steps[-1].device == "master"
