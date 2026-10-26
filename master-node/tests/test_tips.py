"""Tip racks carried over between runs."""

import pytest

from master_node.devices.sim import SimFlex
from master_node.models import TipUse
from master_node.tips import TipProblem, TipTracker, with_tip_state

from .conftest import liquid_step, make_submission, make_workflow, wait_for_status

TIPS_200 = "opentrons_flex_96_tiprack_200ul"


def use(pickups, channels=1, racks=(("C1", TIPS_200),)):
    return TipUse(pipette="left pipette", channels=channels, pickups=pickups,
                  racks=[{"slot": s, "load_name": n} for s, n in racks])


@pytest.fixture
def tracker(tmp_path):
    t = TipTracker(str(tmp_path / "tips.db"))
    yield t
    t.close()


def test_single_channel_runs_continue_where_the_last_stopped(tracker):
    before, after = tracker.plan([use(40)])
    assert before == {"C1": 0} and after == {"C1": (TIPS_200, 40)}
    tracker.commit(after)
    before, after = tracker.plan([use(50)])
    assert before == {"C1": 40} and after == {"C1": (TIPS_200, 90)}
    tracker.commit(after)
    with pytest.raises(TipProblem, match=r"needs 10 tips \(C1 6 left\)"):
        tracker.plan([use(10)])


def test_eight_channel_skips_a_partly_used_column(tracker):
    tracker.commit({"C1": (TIPS_200, 3)})
    _, after = tracker.plan([use(2, channels=8)])
    assert after == {"C1": (TIPS_200, 24)}  # columns 2 and 3; rest of column 1 written off


def test_spills_into_a_fresh_second_rack_but_not_a_partly_used_one(tracker):
    racks = (("C1", TIPS_200), ("C2", TIPS_200))
    tracker.commit({"C1": (TIPS_200, 90)})
    _, after = tracker.plan([use(10, racks=racks)])
    assert after == {"C1": (TIPS_200, 96), "C2": (TIPS_200, 4)}
    tracker.commit({"C2": (TIPS_200, 30)})
    with pytest.raises(TipProblem, match="C2 too, but that rack is partly used"):
        tracker.plan([use(10, racks=racks)])


def test_a_different_tip_type_in_a_recorded_slot_is_flagged(tracker):
    tracker.commit({"C1": ("opentrons_flex_96_tiprack_1000ul", 10)})
    with pytest.raises(TipProblem, match="records say C1 holds opentrons_flex_96_tiprack_1000ul"):
        tracker.plan([use(5)])


def test_fresh_and_removed_racks(tracker):
    tracker.commit({"C1": (TIPS_200, 70)})
    assert tracker.mark_fresh("C1").used == 0
    assert tracker.mark_fresh("B2") is None  # unknown slot without a tip type
    assert tracker.mark_fresh("B2", TIPS_200).load_name == TIPS_200
    assert tracker.remove("B2") and not tracker.remove("B2")
    assert [r.view()["remaining"] for r in tracker.racks()] == [96]


def test_wrapper_renames_run_and_carries_the_counts():
    code = with_tip_state(b"def run(protocol):\n    pass\n", {"C1": 40}).decode()
    assert code.count("def run(") == 1 and "def _lab_original_run(protocol):" in code
    assert "_LAB_TIPS_USED = {'C1': 40}" in code


class RecordingFlex(SimFlex):
    def __init__(self):
        super().__init__(0.001)
        self.sources = []

    async def start_run(self, filename, source, runtime_parameters):
        self.sources.append(source.decode())
        return await super().start_run(filename, source, runtime_parameters)


TIP_USE_40 = [{
    "pipette": "flex_1channel_1000", "channels": 1, "pickups": 40,
    "racks": [{"slot": "C1", "load_name": TIPS_200}],
}]


def test_runs_track_tips_and_refuse_when_short(app_with):
    flex = RecordingFlex()
    submission = make_submission()
    submission["liquid_handling"]["tips"] = TIP_USE_40
    with app_with(flex=flex) as client:
        for expected_start in (0, 40):
            run_id = client.post("/runs", json=submission).json()["id"]
            assert wait_for_status(client, run_id, {"succeeded", "failed"})["status"] == "succeeded"
            assert f"_LAB_TIPS_USED = {{'C1': {expected_start}}}" in flex.sources[-1]
        racks = client.get("/tips").json()["racks"]
        assert racks == [{**racks[0], "slot": "C1", "used": 80, "remaining": 16, "next_tip": "A11"}]

        run_id = client.post("/runs", json=submission).json()["id"]
        run = wait_for_status(client, run_id, {"succeeded", "failed"})
        assert run["status"] == "failed" and "Not enough tips" in run["error"]
        assert len(flex.sources) == 2  # the Flex was never started

        assert client.post("/tips/C1/fresh").json()["remaining"] == 96
        run_id = client.post("/runs", json=submission).json()["id"]
        assert wait_for_status(client, run_id, {"succeeded", "failed"})["status"] == "succeeded"


def test_two_protocols_in_one_run_share_the_racks(app_with):
    flex = RecordingFlex()
    with app_with(flex=flex) as client:
        two = make_workflow(liquid_step(tips=TIP_USE_40), liquid_step(tips=TIP_USE_40))
        run = wait_for_status(client, client.post("/runs", json=two).json()["id"], {"succeeded", "failed"})
        assert run["status"] == "succeeded", run["error"]
        assert ["_LAB_TIPS_USED = {'C1': 0}" in flex.sources[0], "_LAB_TIPS_USED = {'C1': 40}" in flex.sources[1]] == [True, True]

        # 80 used; a run needing 2 x 40 more is refused before anything moves.
        run = wait_for_status(client, client.post("/runs", json=two).json()["id"], {"succeeded", "failed"})
        assert run["status"] == "failed" and "Not enough tips" in run["error"]
        assert len(flex.sources) == 2
