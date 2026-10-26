#!/usr/bin/env python3
"""Thor lab server: lets the master node move plates with Jetson Thor (Ranger Mini + Franka FR3).

Implements the lab's instrument job API (master-node/INSTRUMENT_API.md) for a transport:

  GET  /health               {"ready", "detail", "at_home", "holding_plate", "mode"}
  POST /jobs                 {"operation": "transfer", "params": {"from": {station, slot},
                              "to": {station, slot}}, "plate": {...}}  -> {"job_id"}
  GET  /jobs/{id}            {"state", "message", "result": {"steps", "log_tail", ...}}
  POST /jobs/{id}/cancel
  POST /reset                a person checked the robot: it is home and empty-handed
  GET  /jobs                 recent jobs (for people, not the master node)

Every request needs the X-API-Key from config.yaml.

A transfer is: drive to the source station, pick the plate with the arm, drive to the
destination, place it, drive home. The arm steps are flows for the existing pick-and-place
stack (perception_stack/system/run_task.py), generated from the templates in config.yaml, so
they use the same tested skills as every other arm task. Driving uses the nav team's saved
locations and their `locations go <name>` tool (Nav2), so there is one list of places.

Modes (config.yaml `mode`), from safest:
  dry_run   arm flows run with --dry-run (commands printed, nothing moves), driving is skipped
  nav_only  the base really drives to each station; the arm is not used (the plate move is
            assumed, e.g. for end-to-end tests of the lab before the arm is ready)
  arm_only  the arm really picks and places; the base does not drive (park it by hand)
  full      the base drives between stations and the arm picks and places

The robot's physical state (at home, holding a plate) is tracked and saved. If anything goes
wrong outside dry_run, or the server restarts mid-job, the state becomes UNKNOWN and /health
reports the robot as not ready until a person checks it and calls POST /reset. The master node
refuses to start runs or move plates while Thor isn't home and empty-handed.
"""

from __future__ import annotations

import argparse
import itertools
import json
import logging
import os
import secrets
import shlex
import signal
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import yaml

HERE = Path(__file__).resolve().parent
MODES = ("dry_run", "nav_only", "arm_only", "full")
DRIVING_MODES = ("nav_only", "full")   # the base really drives
ARM_MODES = ("arm_only", "full")       # the arm really moves
TERMINAL = ("succeeded", "failed", "cancelled")
LOG_LINES = 500
log = logging.getLogger("thor-server")


class ConfigError(ValueError):
    """config.yaml is missing something or contradicts itself."""


class JobRefused(ValueError):
    """A job that can't be run as given (unknown station, uncalibrated slot...)."""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- Configuration ---------------------------------------------------------------------------


def load_config(path: str | Path) -> dict[str, Any]:
    path = Path(path).expanduser()
    cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    server = cfg.setdefault("server", {})
    server.setdefault("host", "0.0.0.0")
    server.setdefault("port", 8030)
    if not str(server.get("api_key") or "").strip():
        raise ConfigError(f"{path}: server.api_key is empty; the master node's key must be set")
    if cfg.get("mode") not in MODES:
        raise ConfigError(f"{path}: mode must be one of {', '.join(MODES)}, got {cfg.get('mode')!r}")
    for key in ("perception_stack", "ros_setup", "workspace_setup"):
        if not cfg.get(key):
            raise ConfigError(f"{path}: {key} is required")
        cfg[key] = str(Path(str(cfg[key])).expanduser())
    cfg.setdefault("run_task", str(Path(cfg["perception_stack"]) / "system" / "run_task.py"))
    cfg["run_task"] = str(Path(cfg["run_task"]).expanduser())
    nav = cfg.setdefault("nav", {}) or {}
    cfg["nav"] = nav
    nav.setdefault("setup", "~/jayanth/ranger_mini/install/setup.bash")
    nav.setdefault("env_script", "~/jayanth/ranger_mini/scripts/dds_env.sh")
    nav.setdefault("locations_file", "~/jayanth/ranger_mini/maps/locations.yaml")
    nav.setdefault("go_command", "ros2 run ranger_obstacle_avoidance locations go")
    nav.setdefault("timeout_s", 600)
    for key in ("setup", "env_script", "locations_file"):
        nav[key] = str(Path(str(nav[key])).expanduser())
    cfg.setdefault("arm_timeout_s", 1800)
    cfg.setdefault("step_pause_s", 0.5)
    cfg.setdefault("data_dir", str(HERE))
    cfg["data_dir"] = str(Path(cfg["data_dir"]).expanduser())
    plate = cfg.get("plate") or {}
    if not plate.get("object") or not isinstance(plate.get("class_id"), int):
        raise ConfigError(f"{path}: plate.object and plate.class_id (RT-DETR class) are required")
    stations = cfg.get("stations") or {}
    if not stations:
        raise ConfigError(f"{path}: no stations")
    for name, st in stations.items():
        st = stations[name] = st or {}
        st.setdefault("slots", {})
        st.setdefault("location", None)  # a name in the nav team's locations.yaml
    if cfg.get("home_station") not in stations:
        raise ConfigError(f"{path}: home_station must be one of the stations")
    flows = cfg.get("flows") or {}
    for name in ("pick", "place"):
        if not isinstance(flows.get(name), list) or not flows[name]:
            raise ConfigError(f"{path}: flows.{name} must be a non-empty list of flow steps")
    return cfg


def substitute(value: Any, values: dict[str, Any]) -> Any:
    """Replaces whole-string "$name" tokens (same rule as the arm stack's own $item templating).
    "${PERCEPTION_STACK_ROOT}/..." is left alone for the arm stack to expand."""
    if isinstance(value, str) and value.startswith("$") and value[1:] in values:
        return values[value[1:]]
    if isinstance(value, list):
        return [substitute(v, values) for v in value]
    if isinstance(value, dict):
        return {k: substitute(v, values) for k, v in value.items()}
    return value


# --- Physical state ---------------------------------------------------------------------------


class RobotState:
    """Where the robot is, as far as this server knows. Saved so a restart can't forget a plate."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.data = {"at_home": True, "holding_plate": False, "known": True, "note": "", "active_job": None}
        if path.exists():
            try:
                self.data.update(json.loads(path.read_text(encoding="utf-8")))
            except ValueError:
                self.data.update(known=False, note="state.json was unreadable; check the robot and POST /reset")
        if self.data.get("active_job"):
            self.data.update(known=False, at_home=False,
                             note=f"The server stopped during job {self.data['active_job']}; check the robot "
                                  "(where is it, is it holding a plate?) and POST /reset")
            self.data["active_job"] = None
        self.save()

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def update(self, **changes: Any) -> None:
        with self.lock:
            self.data.update(changes)
            self.save()

    def view(self) -> dict[str, Any]:
        with self.lock:
            return dict(self.data)


# --- Jobs ----------------------------------------------------------------------------------


@dataclass
class Job:
    id: str
    params: dict[str, Any]
    plate: dict[str, Any]
    steps: list[dict[str, Any]]
    mode: str
    warnings: list[str]
    state: str = "queued"
    message: str = ""
    created: str = field(default_factory=now)
    finished: str = ""
    log: deque = field(default_factory=lambda: deque(maxlen=LOG_LINES))
    proc: subprocess.Popen | None = None
    cancel_requested: bool = False

    def view(self, log_lines: int = 40) -> dict[str, Any]:
        return {
            "job_id": self.id, "state": self.state, "message": self.message, "mode": self.mode,
            "created": self.created, "finished": self.finished,
            "result": {
                "from": self.params.get("from"), "to": self.params.get("to"), "mode": self.mode,
                "steps": [{k: v for k, v in s.items() if k != "flow_steps"} for s in self.steps],
                "warnings": self.warnings, "log_tail": list(self.log)[-log_lines:],
            },
        }


class Thor:
    """Runs transfer jobs one at a time and keeps the robot state."""

    def __init__(self, cfg: dict[str, Any]):
        self.cfg = cfg
        self.data_dir = Path(cfg["data_dir"])
        (self.data_dir / "jobs").mkdir(parents=True, exist_ok=True)
        self.state = RobotState(self.data_dir / "state.json")
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()
        self.current: Job | None = None
        self.ids = itertools.count(1)

    # -- API ------------------------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        st = self.state.view()
        busy = self.current
        if busy:
            ready, detail = False, f"busy with job {busy.id}: {busy.message}"
        elif not st["known"]:
            ready, detail = False, st["note"] or "robot state unknown; check it and POST /reset"
        else:
            ready = True
            detail = f"{self.cfg['mode']}: " + ("home, empty-handed" if st["at_home"] and not st["holding_plate"]
                                                 else f"at_home={st['at_home']} holding_plate={st['holding_plate']}")
        return {"ready": ready, "detail": detail, "at_home": bool(st["at_home"] and st["known"]),
                "holding_plate": bool(st["holding_plate"] or not st["known"]), "mode": self.cfg["mode"]}

    def submit(self, body: dict[str, Any]) -> Job:
        if body.get("operation") != "transfer":
            raise JobRefused(f"Thor only does 'transfer', not {body.get('operation')!r}")
        params = body.get("params") or {}
        steps, warnings = self.plan(params)
        with self.lock:
            if self.current is not None:
                raise JobBusy(f"busy with job {self.current.id}")
            st = self.state.view()
            if not st["known"]:
                raise JobRefused(st["note"] or "robot state unknown; check it and POST /reset")
            job = Job(id=f"thor-{time.strftime('%Y%m%d-%H%M%S')}-{next(self.ids)}", params=params,
                      plate=body.get("plate") or {}, steps=steps, mode=self.cfg["mode"], warnings=warnings)
            self.jobs[job.id] = job
            self.current = job
        self.state.update(active_job=job.id if self.cfg["mode"] != "dry_run" else None)
        threading.Thread(target=self._run, args=(job,), name=job.id, daemon=True).start()
        return job

    def cancel(self, job_id: str) -> Job:
        job = self.jobs[job_id]
        if job.state in TERMINAL:
            return job
        job.cancel_requested = True
        self._stop_process(job)
        return job

    def reset(self) -> dict[str, Any]:
        if self.current is not None:
            raise JobBusy(f"busy with job {self.current.id}")
        self.state.update(at_home=True, holding_plate=False, known=True, note="", active_job=None)
        log.info("State reset by a person: home, empty-handed")
        return self.health()

    # -- Planning -----------------------------------------------------------------------------

    def plan(self, params: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
        stations = self.cfg["stations"]
        mode = self.cfg["mode"]
        warnings: list[str] = []
        ends = []
        for key in ("from", "to"):
            loc = params.get(key)
            if not isinstance(loc, dict) or not loc.get("station") or not loc.get("slot"):
                raise JobRefused(f"params.{key} must be {{station, slot}}")
            station, slot = str(loc["station"]), str(loc["slot"])
            if station not in stations:
                raise JobRefused(f"Thor doesn't know station '{station}' (known: {', '.join(stations)})")
            frame = stations[station]["slots"].get(slot)
            if frame is None and mode != "nav_only":  # nav_only doesn't use the arm
                known = ", ".join(stations[station]["slots"]) or "none"
                problem = f"slot '{slot}' at '{station}' isn't calibrated for the arm (calibrated: {known})"
                if mode in ARM_MODES:
                    raise JobRefused(problem)
                warnings.append(problem + "; dry run continues with a placeholder frame")
                frame = f"UNCALIBRATED_{station}_{slot}"
            ends.append((station, slot, frame))
        (src_station, src_slot, src_frame), (dst_station, dst_slot, dst_frame) = ends
        home = self.cfg["home_station"]
        if mode in DRIVING_MODES:
            saved = self._saved_locations()
            for station in dict.fromkeys((src_station, dst_station, home)):
                name = stations[station]["location"]
                if not name:
                    raise JobRefused(f"station '{station}' has no location to drive to; save one with "
                                     f"'ros2 run ranger_obstacle_avoidance locations save <name>' and set "
                                     f"stations.{station}.location in config.yaml")
                if name not in saved:
                    raise JobRefused(f"location '{name}' (station '{station}') isn't in "
                                     f"{self.cfg['nav']['locations_file']} (saved: {', '.join(saved) or 'none'})")
        steps = [
            {"kind": "navigate", "station": src_station, "label": f"Drive to {src_station}"},
            {"kind": "arm", "flow": "pick", "frame": src_frame, "label": f"Pick plate at {src_station}/{src_slot}"},
            {"kind": "navigate", "station": dst_station, "label": f"Drive to {dst_station}"},
            {"kind": "arm", "flow": "place", "frame": dst_frame, "label": f"Place plate at {dst_station}/{dst_slot}"},
            {"kind": "navigate", "station": home, "label": f"Drive home ({home})"},
        ]
        for s in steps:
            s["state"] = "pending"
        return steps, warnings

    def _saved_locations(self) -> dict[str, Any]:
        """The nav team's named places (re-read every job, so newly saved ones work at once)."""
        path = Path(self.cfg["nav"]["locations_file"])
        try:
            return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except FileNotFoundError:
            raise JobRefused(f"no saved locations file at {path}") from None

    # -- Execution ------------------------------------------------------------------------------

    def _run(self, job: Job) -> None:
        mode = self.cfg["mode"]
        job.state = "running"
        self._note(job, f"Started ({mode}): {job.params['from']} -> {job.params['to']}")
        for w in job.warnings:
            self._note(job, f"WARNING: {w}")
        try:
            for step in job.steps:
                if job.cancel_requested:
                    raise Cancelled()
                step["state"] = "running"
                step["started"] = now()
                job.message = step["label"]
                self._note(job, f"--- {step['label']}")
                if step["kind"] == "navigate":
                    detail = self._navigate(job, step)
                else:
                    detail = self._arm(job, step)
                step["state"], step["detail"], step["finished"] = "done", detail, now()
                self._note(job, f"{step['label']}: {detail}")
                time.sleep(float(self.cfg["step_pause_s"]))
            job.state, job.message = "succeeded", "Plate placed; Thor is home and empty-handed"
            if mode != "dry_run":
                self.state.update(at_home=True, holding_plate=False, known=True, note="", active_job=None)
        except Cancelled:
            self._end_failed(job, "cancelled", "Cancelled")
        except Exception as exc:  # noqa: BLE001 - every failure must end the job, never the server
            log.exception("Job %s failed", job.id)
            self._end_failed(job, "failed", str(exc))
        finally:
            job.finished = now()
            self._save_job(job)
            with self.lock:
                self.current = None
            log.info("Job %s %s: %s", job.id, job.state, job.message)

    def _end_failed(self, job: Job, state: str, message: str) -> None:
        during = next((s["label"] for s in job.steps if s["state"] == "running"), "the job")
        for step in job.steps:
            if step["state"] == "running":
                step["state"], step["finished"], step["detail"] = state, now(), message
            elif step["state"] == "pending":
                step["state"] = "skipped"
        job.state, job.message = state, message
        self._note(job, f"{state.upper()}: {message}")
        if self.cfg["mode"] != "dry_run":
            # Somewhere between stations, maybe holding the plate: a person must look.
            self.state.update(known=False, at_home=False, active_job=None,
                              note=f"Job {job.id} {state} during '{during}'; check where the robot and plate "
                                   "are, make it safe, then POST /reset")

    def _navigate(self, job: Job, step: dict[str, Any]) -> str:
        mode = self.cfg["mode"]
        if mode not in DRIVING_MODES:
            return f"not driving (mode {mode})" + ("" if mode == "dry_run" else ": the base must already be parked here")
        nav = self.cfg["nav"]
        name = self.cfg["stations"][step["station"]]["location"]
        self.state.update(at_home=False)
        # SIGINT on timeout or cancel makes the locations tool cancel the Nav2 goal.
        self._run_process(job, f"{nav['go_command']} {shlex.quote(name)}", timeout=float(nav["timeout_s"]),
                          what=step["label"], setup=[nav["setup"], nav["env_script"]])
        if step["station"] == self.cfg["home_station"]:
            self.state.update(at_home=True)
        return f"arrived at '{name}'"

    def _arm(self, job: Job, step: dict[str, Any]) -> str:
        if self.cfg["mode"] == "nav_only":
            return "arm not used (mode nav_only): the plate move is assumed"
        dry = self.cfg["mode"] == "dry_run"
        plate = self.cfg["plate"]
        values = {"object": plate["object"], "class_id": plate["class_id"], "frame": step["frame"]}
        flow = {
            "name": f"thor_{step['flow']}_{job.id.replace('-', '_')}",
            "objects": {plate["object"]: plate["class_id"]},
            "flow": substitute(self.cfg["flows"][step["flow"]], values),
        }
        path = self.data_dir / "jobs" / job.id / f"{step['flow']}.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(flow, sort_keys=False), encoding="utf-8")
        step["flow_file"] = str(path)
        if not dry and step["flow"] == "pick":
            self.state.update(at_home=False)
        command = f"python3 {shlex.quote(self.cfg['run_task'])} {shlex.quote(str(path))}" + (" --dry-run" if dry else "")
        self._run_process(job, command, timeout=float(self.cfg["arm_timeout_s"]), what=step["label"])
        if not dry:
            self.state.update(holding_plate=step["flow"] == "pick")
        return ("dry run: commands printed, nothing moved" if dry else "done") + f" ({path.name})"

    def _run_process(self, job: Job, command: str, timeout: float, what: str, setup: list[str] | None = None) -> None:
        """Runs a command in the sourced ROS environment, streaming its output into the job log.
        `setup` replaces the arm workspace's setup script (e.g. the nav workspace's)."""
        scripts = [self.cfg["ros_setup"]] + (setup if setup is not None else [self.cfg["workspace_setup"]])
        shell = " && ".join(f"source {shlex.quote(s)}" for s in scripts) + f" && exec {command}"
        self._note(job, f"$ {command}")
        proc = subprocess.Popen(["bash", "-c", shell], cwd=self.cfg["perception_stack"], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, errors="replace", start_new_session=True)
        job.proc = proc
        timer = threading.Timer(timeout, lambda: (self._note(job, f"TIMEOUT after {timeout:.0f}s"), self._stop_process(job)))
        timer.start()
        try:
            for line in proc.stdout:
                job.log.append(line.rstrip())
            code = proc.wait()
        finally:
            timer.cancel()
            proc.stdout.close()
            job.proc = None
        if job.cancel_requested:
            raise Cancelled()
        if code != 0:
            raise RuntimeError(f"{what} failed (exit {code}): {failure_reason(job.log)[:400]}")

    @staticmethod
    def _stop_process(job: Job) -> None:
        proc = job.proc
        if proc and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGINT)  # like Ctrl+C: lets the arm/nav scripts stop cleanly
                for _ in range(50):
                    if proc.poll() is not None:
                        return
                    time.sleep(0.1)
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    def _note(self, job: Job, text: str) -> None:
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {text}"
        job.log.append(line)
        log.info("%s %s", job.id, text)

    def _save_job(self, job: Job) -> None:
        folder = self.data_dir / "jobs" / job.id
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "job.json").write_text(json.dumps(job.view(log_lines=LOG_LINES), indent=1), encoding="utf-8")


REASON_MARKERS = ("raised:", "error", "exception", "not available", "not detected", "timed out", "abort")


def failure_reason(lines) -> str:
    """The line of a failed run that says why: the arm stack prints the real cause (e.g.
    "place_seat raised: ...") before its generic 'Flow ... FAILED' summary."""
    lines = [l.strip() for l in lines if l.strip()]
    for line in reversed(lines):
        lower = line.lower()
        if any(m in lower for m in REASON_MARKERS) and "flow \"" not in lower:
            return line
    return lines[-1] if lines else "no output"


class Cancelled(Exception):
    pass


class JobBusy(Exception):
    pass


# --- HTTP ----------------------------------------------------------------------------------


def make_handler(thor: Thor):
    api_key = str(thor.cfg["server"]["api_key"]).encode()

    class Handler(BaseHTTPRequestHandler):
        server_version = "ThorLabServer/1.0"

        def log_message(self, fmt, *args):  # route through logging
            log.debug("%s %s", self.address_string(), fmt % args)

        def _send(self, status: int, body: Any) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _authorised(self) -> bool:
            given = (self.headers.get("X-API-Key") or "").encode()
            if secrets.compare_digest(given, api_key):
                return True
            self._send(401, {"detail": "Missing or wrong X-API-Key"})
            return False

        def _body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("body must be a JSON object")
            return data

        def do_GET(self):
            if not self._authorised():
                return
            parts = [p for p in self.path.split("?")[0].split("/") if p]
            if parts == ["health"]:
                return self._send(200, thor.health())
            if parts == ["jobs"]:
                recent = sorted(thor.jobs.values(), key=lambda j: j.created, reverse=True)[:20]
                return self._send(200, {"jobs": [{"job_id": j.id, "state": j.state, "message": j.message,
                                                  "created": j.created} for j in recent]})
            if len(parts) == 2 and parts[0] == "jobs":
                job = thor.jobs.get(parts[1])
                return self._send(200, job.view()) if job else self._send(404, {"detail": f"No job {parts[1]}"})
            self._send(404, {"detail": "Not found"})

        def do_POST(self):
            if not self._authorised():
                return
            parts = [p for p in self.path.split("?")[0].split("/") if p]
            try:
                if parts == ["jobs"]:
                    job = thor.submit(self._body())
                    return self._send(201, {"job_id": job.id})
                if len(parts) == 3 and parts[0] == "jobs" and parts[2] == "cancel":
                    if parts[1] not in thor.jobs:
                        return self._send(404, {"detail": f"No job {parts[1]}"})
                    return self._send(200, thor.cancel(parts[1]).view())
                if parts == ["reset"]:
                    return self._send(200, thor.reset())
            except JobBusy as exc:
                return self._send(409, {"detail": str(exc)})
            except (JobRefused, ValueError) as exc:
                return self._send(422, {"detail": str(exc)})
            self._send(404, {"detail": "Not found"})

    return Handler


def serve(cfg: dict[str, Any]) -> ThreadingHTTPServer:
    thor = Thor(cfg)
    httpd = ThreadingHTTPServer((cfg["server"]["host"], int(cfg["server"]["port"])), make_handler(thor))
    httpd.thor = thor  # for tests
    return httpd


def main() -> None:
    parser = argparse.ArgumentParser(description="Thor lab server (job API for the master node)")
    parser.add_argument("--config", default=str(HERE / "config.yaml"))
    args = parser.parse_args()
    cfg = load_config(args.config)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    httpd = serve(cfg)
    log.info("Thor lab server on %s:%s, mode %s, stations %s", cfg["server"]["host"], cfg["server"]["port"],
             cfg["mode"], ", ".join(cfg["stations"]))
    signal.signal(signal.SIGTERM, lambda *_: threading.Thread(target=httpd.shutdown).start())
    httpd.serve_forever()


if __name__ == "__main__":
    main()
