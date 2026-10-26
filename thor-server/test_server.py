"""Tests for server.py with a fake arm stack (nothing real runs). Run: python3 -m unittest -v test_server"""

import json
import os
import sys
import tempfile
import textwrap
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
import server  # noqa: E402

KEY = "test-key"

# Stands in for perception_stack/system/run_task.py: records its arguments, and fails or hangs
# when marker files in its folder say so.
FAKE_RUN_TASK = textwrap.dedent("""\
    import json, os, sys, time
    here = os.path.dirname(os.path.abspath(__file__))
    flow = sys.argv[1]
    with open(os.path.join(here, "calls.jsonl"), "a") as f:
        f.write(json.dumps({"argv": sys.argv[1:], "flow": open(flow).read()}) + "\\n")
    kind = "pick" if flow.endswith("pick.yaml") else "place"
    print(f"[ORCH] fake {kind} {'dry run' if '--dry-run' in sys.argv else 'REAL'}", flush=True)
    if os.path.exists(os.path.join(here, f"slow_{kind}")):
        time.sleep(30)
    if os.path.exists(os.path.join(here, f"fail_{kind}")):
        print("vs_pick: plate not detected", flush=True)
        sys.exit(1)
""")


# Stands in for `ros2 run ranger_obstacle_avoidance locations go <name>`: records the drive, and
# fails or hangs when marker files say so. Ctrl+C (SIGINT) ends it like the real tool's cancel.
FAKE_GO = textwrap.dedent("""\
    import json, os, sys, time
    here = os.path.dirname(os.path.abspath(__file__))
    name = sys.argv[1]
    with open(os.path.join(here, "drives.jsonl"), "a") as f:
        f.write(json.dumps(name) + "\\n")
    print(f"going to {name}", flush=True)
    try:
        if os.path.exists(os.path.join(here, f"slow_drive_{name}")):
            time.sleep(30)
    except KeyboardInterrupt:
        print("cancelling", flush=True)
        sys.exit(1)
    if os.path.exists(os.path.join(here, f"fail_drive_{name}")):
        print(f"{name}: failed (nav2 aborted)", flush=True)
        sys.exit(1)
    print(f"{name}: arrived", flush=True)
""")

LOCATIONS = "home: {x: -0.163, y: 0.138, yaw_deg: -0.1}\nopentrons: {x: 1.863, y: 3.182, yaw_deg: 0.6}\n" \
            "tecan_spectro: {x: 3.411, y: 3.293, yaw_deg: 0.5}\n"


class ServerTest(unittest.TestCase):
    mode = "dry_run"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "ros.bash").write_text("")
        (root / "run_task.py").write_text(FAKE_RUN_TASK)
        (root / "go.py").write_text(FAKE_GO)
        (root / "locations.yaml").write_text(LOCATIONS)
        self.root = root
        cfg = yaml.safe_load((Path(server.__file__).parent / "config.yaml").read_text())
        cfg.update(mode=self.mode, perception_stack=str(root), ros_setup=str(root / "ros.bash"),
                   workspace_setup=str(root / "ros.bash"), run_task=str(root / "run_task.py"),
                   data_dir=str(root / "data"), step_pause_s=0)
        cfg["nav"].update(setup=str(root / "ros.bash"), env_script=str(root / "ros.bash"),
                          locations_file=str(root / "locations.yaml"), go_command=f"python3 {root / 'go.py'}",
                          timeout_s=20)
        cfg["server"].update(host="127.0.0.1", port=0, api_key=KEY)
        self.cfg_path = root / "config.yaml"
        self.cfg_path.write_text(yaml.safe_dump(cfg))
        self.start()

    def start(self):
        self.httpd = server.serve(server.load_config(self.cfg_path))
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def tearDown(self):
        self.stop()
        self.tmp.cleanup()

    def call(self, method, path, body=None, key=KEY):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", data=data, method=method,
                                     headers={"X-API-Key": key, "Content-Type": "application/json"} if key else {})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def transfer(self, src=("flex", "D2"), dst=("tecan", "carrier")):
        return self.call("POST", "/jobs", {"operation": "transfer",
                                           "params": {"from": {"station": src[0], "slot": src[1]},
                                                      "to": {"station": dst[0], "slot": dst[1]}},
                                           "plate": {"barcode": "PL-1"}})

    def wait(self, job_id, states=server.TERMINAL, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            _, job = self.call("GET", f"/jobs/{job_id}")
            if job["state"] in states:
                return job
            time.sleep(0.05)
        self.fail(f"job {job_id} stuck in {job['state']}")

    def calls(self):
        path = self.root / "calls.jsonl"
        return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []

    def drives(self):
        path = self.root / "drives.jsonl"
        return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []


class DryRun(ServerTest):
    def test_key_is_required(self):
        self.assertEqual(self.call("GET", "/health", key=None)[0], 401)
        self.assertEqual(self.call("GET", "/health", key="wrong")[0], 401)
        status, health = self.call("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(health, {"ready": True, "detail": "dry_run: home, empty-handed", "at_home": True,
                                  "holding_plate": False, "mode": "dry_run"})

    def test_transfer_generates_flows_and_runs_them_dry(self):
        status, body = self.transfer()
        self.assertEqual(status, 201)
        job = self.wait(body["job_id"])
        self.assertEqual(job["state"], "succeeded", job["message"])
        steps = job["result"]["steps"]
        self.assertEqual([s["label"] for s in steps], [
            "Drive to flex", "Pick plate at flex/D2", "Drive to tecan", "Place plate at tecan/carrier", "Drive home (home)"])
        self.assertEqual({s["state"] for s in steps}, {"done"})
        self.assertTrue(steps[0]["detail"].startswith("not driving (mode dry_run)"))

        pick, place = self.calls()
        self.assertEqual(pick["argv"][1:], ["--dry-run"])
        pick_flow, place_flow = yaml.safe_load(pick["flow"]), yaml.safe_load(place["flow"])
        self.assertEqual(pick_flow["objects"], {"well_plate": 1})
        self.assertEqual(pick_flow["flow"][1]["args"], {"class_ids": [1], "names": ["well_plate"]})
        self.assertEqual(pick_flow["flow"][2]["steps"][0]["args"], {"object": "well_plate"})
        self.assertEqual(place_flow["flow"][0]["args"]["target_frame"], "tecan_tray")
        self.assertEqual(place_flow["flow"][0]["args"]["demo_data"], "${PERCEPTION_STACK_ROOT}/assets/objects")
        # The robot didn't move, so its state is unchanged.
        self.assertTrue(self.call("GET", "/health")[1]["ready"])

    def test_uncalibrated_slots_are_a_warning_in_dry_run(self):
        job = self.wait(self.transfer(src=("storage", "R1-S3"), dst=("flex", "C2"))[1]["job_id"])
        self.assertEqual(job["state"], "succeeded")
        self.assertIn("slot 'R1-S3' at 'storage' isn't calibrated", job["result"]["warnings"][0])

    def test_bad_requests(self):
        self.assertEqual(self.transfer(src=("fridge", "A1"))[0], 422)
        bad = self.call("POST", "/jobs", {"operation": "wash", "params": {}})
        self.assertEqual(bad[0], 422)
        self.assertIn("only does 'transfer'", bad[1]["detail"])
        self.assertEqual(self.call("GET", "/jobs/nope")[0], 404)

    def test_one_job_at_a_time_and_cancel(self):
        (self.root / "slow_pick").write_text("")
        first = self.transfer()[1]["job_id"]
        self.wait(first, states=("running",))
        time.sleep(0.3)
        busy = self.transfer()
        self.assertEqual(busy[0], 409)
        self.assertFalse(self.call("GET", "/health")[1]["ready"])
        started = time.monotonic()
        self.call("POST", f"/jobs/{first}/cancel")
        job = self.wait(first)
        self.assertEqual(job["state"], "cancelled")
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual([s["state"] for s in job["result"]["steps"]][1:3], ["cancelled", "skipped"])
        self.assertTrue(self.call("GET", "/health")[1]["ready"])  # dry run: nothing physical happened


class ArmOnly(ServerTest):
    mode = "arm_only"

    def test_uncalibrated_slots_are_refused(self):
        status, body = self.transfer(src=("storage", "R1-S3"))
        self.assertEqual(status, 422)
        self.assertIn("isn't calibrated", body["detail"])

    def test_success_runs_for_real_and_ends_home_empty_handed(self):
        job = self.wait(self.transfer()[1]["job_id"])
        self.assertEqual(job["state"], "succeeded", job["message"])
        self.assertNotIn("--dry-run", self.calls()[0]["argv"])
        self.assertIn("the base must already be parked here", job["result"]["steps"][0]["detail"])
        self.assertEqual(self.call("GET", "/health")[1]["detail"], "arm_only: home, empty-handed")

    def test_failure_leaves_the_state_unknown_until_reset(self):
        (self.root / "fail_place").write_text("")
        job = self.wait(self.transfer()[1]["job_id"])
        self.assertEqual(job["state"], "failed")
        self.assertIn("Place plate at tecan/carrier failed (exit 1): vs_pick: plate not detected", job["message"])
        health = self.call("GET", "/health")[1]
        self.assertFalse(health["ready"])
        self.assertFalse(health["at_home"])
        self.assertTrue(health["holding_plate"])  # it picked; the place failed
        self.assertIn("check where the robot and plate are", health["detail"])
        refused = self.transfer()
        self.assertEqual(refused[0], 422)
        self.assertTrue(self.call("POST", "/reset")[1]["ready"])
        self.assertEqual(self.transfer()[0], 201)

    def test_restart_mid_job_leaves_the_state_unknown(self):
        (self.root / "slow_pick").write_text("")
        self.wait(self.transfer()[1]["job_id"], states=("running",))
        time.sleep(0.3)
        self.stop()
        self.start()  # a new server process reading the same state.json
        health = self.call("GET", "/health")[1]
        self.assertFalse(health["ready"])
        self.assertIn("The server stopped during job", health["detail"])


class NavOnly(ServerTest):
    mode = "nav_only"

    def test_drives_to_each_station_and_home_without_the_arm(self):
        job = self.wait(self.transfer()[1]["job_id"])
        self.assertEqual(job["state"], "succeeded", job["message"])
        self.assertEqual(self.drives(), ["opentrons", "tecan_spectro", "home"])
        self.assertEqual(self.calls(), [])  # the arm stack was never run
        details = [s["detail"] for s in job["result"]["steps"]]
        self.assertEqual(details[0], "arrived at 'opentrons'")
        self.assertEqual(details[1], "arm not used (mode nav_only): the plate move is assumed")
        self.assertEqual(self.call("GET", "/health")[1]["detail"], "nav_only: home, empty-handed")

    def test_storage_is_the_home_position_for_now_and_slots_dont_matter(self):
        job = self.wait(self.transfer(src=("storage", "BENCH"), dst=("flex", "C3"))[1]["job_id"])
        self.assertEqual(job["state"], "succeeded", job["message"])
        self.assertEqual(self.drives(), ["home", "opentrons", "home"])
        self.assertEqual(job["result"]["warnings"], [])

    def test_a_failed_drive_leaves_the_robot_state_unknown(self):
        (self.root / "fail_drive_tecan_spectro").write_text("")
        job = self.wait(self.transfer()[1]["job_id"])
        self.assertEqual(job["state"], "failed")
        self.assertIn("Drive to tecan failed (exit 1): tecan_spectro: failed (nav2 aborted)", job["message"])
        health = self.call("GET", "/health")[1]
        self.assertFalse(health["ready"])
        self.assertFalse(health["at_home"])
        self.assertIn("during 'Drive to tecan'", health["detail"])
        self.assertTrue(self.call("POST", "/reset")[1]["ready"])

    def test_locations_must_be_saved(self):
        (self.root / "locations.yaml").write_text("home: {x: 0, y: 0, yaw_deg: 0}\n")
        status, body = self.transfer()
        self.assertEqual(status, 422)
        self.assertIn("location 'opentrons' (station 'flex') isn't in", body["detail"])

    def test_cancel_stops_the_drive(self):
        (self.root / "slow_drive_opentrons").write_text("")
        job_id = self.transfer()[1]["job_id"]
        self.wait(job_id, states=("running",))
        time.sleep(0.5)
        self.call("POST", f"/jobs/{job_id}/cancel")
        job = self.wait(job_id)
        self.assertEqual(job["state"], "cancelled")
        self.assertEqual(self.drives(), ["opentrons"])
        self.assertFalse(self.call("GET", "/health")[1]["ready"])  # somewhere on the floor: check it


class FailureReason(unittest.TestCase):
    def test_the_cause_beats_the_summary(self):
        log = ["[BT]   [ACTION] place_seat",
               "[ORCH] place_seat raised: place_seat: no `slot_depth_offset` entry for target_frame \"tecan_tray\"",
               "[BT]   Sequence \"x\" -> FAILURE at \"place_seat\"",
               "[ORCH] Flow \"thor_place_1\" FAILED.", ""]
        self.assertTrue(server.failure_reason(log).startswith("[ORCH] place_seat raised:"))
        self.assertEqual(server.failure_reason(["just this"]), "just this")


class Config(unittest.TestCase):
    def test_shipped_config_loads(self):
        cfg = server.load_config(Path(server.__file__).parent / "config.yaml")
        self.assertEqual(cfg["mode"], "nav_only")
        self.assertEqual(cfg["stations"]["tecan"], {"location": "tecan_spectro", "slots": {"carrier": "tecan_tray"}})
        self.assertTrue(cfg["nav"]["locations_file"].endswith("ranger_mini/maps/locations.yaml"))

    def test_mistakes_are_refused(self):
        with tempfile.TemporaryDirectory() as d:
            base = yaml.safe_load((Path(server.__file__).parent / "config.yaml").read_text())
            for change, message in [({"mode": "fast"}, "mode must be"),
                                    ({"server": {"api_key": ""}}, "api_key is empty"),
                                    ({"home_station": "garage"}, "home_station"),
                                    ({"flows": {"pick": []}}, "flows.pick")]:
                path = Path(d) / "c.yaml"
                path.write_text(yaml.safe_dump({**base, **change}))
                with self.assertRaisesRegex(server.ConfigError, message):
                    server.load_config(path)


if __name__ == "__main__":
    unittest.main()
