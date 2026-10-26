# Instrument server API

The contract an instrument server follows so the master node can drive it with
`driver = "job_api"` in `instruments.toml`. With it, a new instrument (washer,
qPCR, Thor, or anything added later) needs only a registry entry, no master
node code.

The Opentrons Flex and the existing Tecan reader API keep their own APIs
(`opentrons_flex` and `tecan_infinite` drivers); this contract is for new servers.

## Endpoints

All bodies are JSON. If the registry entry has an `api_key`, every request
carries it as `X-API-Key`; reject requests without it (401).

### `GET /health`

```json
{"ready": true, "detail": "idle"}
```

`ready: false` (with the reason in `detail`) stops a run at its preflight
check, before anything moves. The protocol tool's status bar shows `detail`.

A **transport** (Thor) also reports where it is. The master node only moves a
plate, and only lets an instrument start, when Thor is home and empty-handed:

```json
{"ready": true, "detail": "parked", "at_home": true, "holding_plate": false}
```

### `POST /jobs`

Starts one operation. Return as soon as the instrument has accepted it.

```json
{
  "operation": "wash",
  "params": {"program": "", "cycles": 3, "volume_ul": 300, "soak_s": 0},
  "plate": {"barcode": "PL-0001", "labware": "corning_96_wellplate_360ul_flat", "slot": "carrier"}
}
```

Response (201 or 200):

```json
{"job_id": "j-42"}
```

Refuse a job you can't do (wrong operation, busy, plate missing) with a 4xx or
5xx and a message in the body; the run fails at that step with your message.

### `GET /jobs/{job_id}`

```json
{"state": "running", "message": "cycle 2 of 3", "result": {}}
```

`state` is `queued`, `running`, `succeeded`, `failed` or `cancelled`. The
master node polls every `poll_interval_s` (default 2 s) and shows each new
state in the run's event log. Report `succeeded` only once the instrument is
done with the plate; for Thor, once the plate is placed and Thor is back
home. On `failed`, put the reason in `message`.

`result` is what the job produced, kept with the run:

```json
{
  "measurements": [
    {"label": "Ct", "unit": "cycles", "values": {"A1": 21.4, "A2": 22.9, "B1": null}}
  ],
  "metadata": {"run_file": "SYBR-40", "instrument_serial": "..."}
}
```

`measurements` (well name → number, or `null` for no reading) show as a plate
heatmap and in the CSV download. Anything else in `result` is stored as is
and shown with the run.

### `POST /jobs/{job_id}/cancel`

Stop the job (the run was aborted). Then report `cancelled` or `failed`.

### `POST /access/open`, `POST /access/close`

Only if the registry entry sets `options = { access = true }`: the
instrument has a drawer or lid the plate goes through. The master node opens
it before Thor moves a plate in or out, and closes it once Thor reports home
and empty-handed.

### `POST /reset` (transport only)

After an aborted or failed move, Thor can't know where it or the plate is,
so it reports `ready: false`. Once a person has checked the robot is parked
at home with nothing in its gripper, the protocol tool's Reset button (next to
Thor in the status bar) calls this. Mark the robot home and empty-handed and
return the new `/health`. Refuse with 409 while a job is running.

## Operations and their params

| Kind | `operation` | `params` |
|---|---|---|
| `plate_washer` | `wash` | `program` (saved program name, or `""`), `cycles`, `volume_ul`, `soak_s` |
| `qpcr` | `qpcr` | `program` (run file or protocol saved on the instrument), `params` (object) |
| `plate_reader` | `read` | `mode`, `wells`, `wavelengths_nm`, `excitation_nm`, `emission_nm`, `gain`, `integration_ms`, `flashes` (see `ReadSpec` in `master_node/models.py`) |
| `liquid_handler` | `liquid_handling` | `protocol_filename`, `protocol_b64` (the Opentrons protocol file, base64), `runtime_parameters` |
| `transport` | `transfer` | `from`, `to`: `{"station": "storage" or an instrument id, "slot": "..."}` |

The station names Thor receives are the registry ids (`flex`, `tecan`,
`washer`, ...) and `storage`; the slots are the registry's `plate_slots`
(e.g. Flex deck slot `D2`, or `carrier`) and the storage slot the scientist
enters.

## Registry entry

```toml
[[instrument]]
id = "washer"
kind = "plate_washer"
name = "Plate washer"
driver = "job_api"
url = "http://192.168.0.116:8010"
api_key = "a long random string"
options = { access = false }
[instrument.limits]
programs = ["ELISA-3x300"]
max_volume_ul = 400
```

Restart the master node after editing `instruments.toml`. Its status bar entry
in the protocol tool shows whether `/health` answers.

## Trying a server

```powershell
# What the master node will call first:
Invoke-RestMethod http://192.168.0.116:8010/health -Headers @{"X-API-Key" = "..."}
```

Until a server exists, set its entry's `driver = "sim"` to plan and run
workflows with a simulated instrument in its place.
