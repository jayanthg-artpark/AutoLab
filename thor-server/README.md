# Thor lab server

Runs on the Jetson Thor (Ranger Mini base + Franka FR3 arm), in `~/lab_thor_server`. It is the
transport instrument for the lab's master node: the master node asks it to move a plate from one
station to another, using the job API in `master-node/INSTRUMENT_API.md`.

A transfer is: drive to the source station → pick the plate with the arm → drive to the
destination → place it → drive home. Driving uses the nav team's saved places and their tool
(`ros2 run ranger_obstacle_avoidance locations go <name>`, Nav2), so there is one list of places
(`~/jayanth/ranger_mini/maps/locations.yaml`). The arm steps are flows for the existing
pick-and-place stack (`~/perception_stack`, run through its `system/run_task.py`), generated from
templates in `config.yaml`.

## Modes (`mode` in config.yaml)

| Mode | Base | Arm | Use |
|---|---|---|---|
| `dry_run` | not driven | flows run with `--dry-run`: commands printed, **nothing moves** | testing the link from the master node |
| `nav_only` | **drives** to each station and home | not used: the plate move is assumed | end-to-end lab tests before the arm is ready |
| `arm_only` | not driven: park it at the station by hand | really picks and places | first arm tests, one station at a time |
| `full` | drives | really picks and places | the real thing |

Outside `dry_run`, a failed, cancelled or interrupted job leaves the robot state **unknown**:
`/health` reports not ready, and the master node won't start runs or move plates, until a person
has checked the robot (and plate) and called `POST /reset`.

## Stations

`stations` in config.yaml maps the master node's station names to the nav team's saved places:

| Station (master node) | Place (`locations.yaml`) |
|---|---|
| `home` | `home` |
| `flex` | `opentrons` |
| `tecan` | `tecan_spectro` |
| `storage` | `home` for now (no storage place saved yet) |

To add or move a place: drive the robot there and `ros2 run ranger_obstacle_avoidance locations save <name>`,
then set that station's `location`. Places are re-read for every job.

## Run it

The first time, copy `config.example.yaml` to `config.yaml` and set `server.api_key` (the same key
goes under the thor entry in `master-node/instruments.toml`).

```bash
cd ~/lab_thor_server
./thorctl.sh start        # stop | restart | status | log
python3 -m unittest -v test_server     # tests with a fake arm stack and fake driving, nothing moves
```

```bash
KEY=$(python3 -c "import yaml;print(yaml.safe_load(open('config.yaml'))['server']['api_key'])")
curl -H "X-API-Key: $KEY" localhost:8030/health
curl -H "X-API-Key: $KEY" -X POST localhost:8030/jobs -d '{"operation":"transfer",
  "params":{"from":{"station":"flex","slot":"D2"},"to":{"station":"tecan","slot":"carrier"}}}'
curl -H "X-API-Key: $KEY" localhost:8030/jobs/<job_id>
curl -H "X-API-Key: $KEY" -X POST localhost:8030/jobs/<job_id>/cancel   # stops the drive / arm
curl -H "X-API-Key: $KEY" -X POST localhost:8030/reset    # after checking the robot by hand
```

Driving needs the nav stack up and localized (`~/jayanth/ranger_mini/scripts/nav_bringup_3dloc.sh`);
moving the arm needs the arm stack up (`~/perception_stack/system/bringup_all.sh`).

## Before the arm moves (arm_only, full)

- **Slots:** `stations.*.slots` maps the master node's slot names to arm frames in
  `perception_stack/franka_ros2/config/static_tfs.yaml`. Confirm `flex: B1..D2 -> b1..d2_slot` is
  really the Flex deck, and calibrate storage positions (none yet).
- **Tecan tray:** `place_seat` has no measured insertion depth for `tecan_tray` yet
  (`franka_ros2/config/place_seat.yaml`), so placing into the Tecan is refused until it's measured.
- **Parking precision:** Nav2 counts within 20 cm as arrived. Placing in a slot needs the arm to
  know where its base really is at each station (e.g. an AprilTag on each instrument), or much
  tighter docking.
- **Flows:** the pick flow finds the plate with the camera from the arm's home pose; check the
  camera can see each source slot from there, or add a viewing move.

Every job's generated flows and log are kept in `jobs/<job id>/`.
