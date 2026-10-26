# Master node

The orchestrator. It runs in the lab, has no AI and is deterministic. It
receives workflows from the [protocol tool](../protocol-tool), picks an
instrument for each step from the **instrument registry**, checks every step
against that instrument's **safety limits**, and then runs the steps one at a
time, telling the transport (Thor) to move the plate between instruments.

```
                      protocol tool (scientist UI, planner services)
                                  │  POST /plan, POST /runs, status
                                  ▼
 instruments.toml ──▶  master node: registry · planner · sequencer · results
                                  │
        ┌───────────────┬─────────┴─────┬───────────────┬───────────────┐
        ▼               ▼               ▼               ▼               ▼
   Opentrons Flex   Tecan reader   washer server   qPCR server    Thor server
   (its own API)    API on .116    (job API)       (job API)      (job API; manual until then)
```

A submitted workflow is a list of steps, for example `liquid_handling → wash →
hold → read → qpcr`. The planner turns it into the steps the master node
executes:

| Step | Talks to | What happens |
|---|---|---|
| Decode + plan | | Validates the workflow, selects instruments, checks limits (on `POST /runs`) |
| Fetch / Move plate to X | transport, X | Opens X's drawer or lid if it has one, then moves the plate there |
| X is clear | transport | Interlock: the transport must report home and empty-handed; then drawers and lids close |
| Run protocol / Read plate / Wash plate / qPCR run | X | Starts the operation, waits for it, stores what it returns |
| Hold | | The plate waits where it is |

A plate move only happens when the next step is on a different instrument.
One run executes at a time; the rest queue. Every state change is saved to
SQLite before the next device command.

**Safety behaviour**

- **Limits before anything moves:** a step outside its instrument's limits
  (see [The instrument registry](#the-instrument-registry)) is refused when
  the run is submitted, with the reason.
- **Preflight check before each run:** the transport must be home and
  empty-handed, every instrument the run uses must report ready, and the tip
  racks on the Flex must have enough tips for all its protocols (see
  [Tip racks](#tip-racks)).
- **Failed steps:** if any step fails, the run stops there and the remaining
  steps are skipped. The plate stays where it is for a person to check.
- **Abort:** `POST /runs/{id}/abort` stops whatever device work is in
  progress (the transport's move or the instrument's job).
- **Restarts:** if the master node restarts mid-run, that run is marked
  `interrupted` and isn't resumed automatically. Runs that hadn't started yet
  are queued again.

## Run it

On Windows, double-click **`run-master-node.cmd`**. The first time, it sets up
its Python packages, which needs Python 3.11 or newer and an internet
connection. After that it just starts. Keep the window open; closing it stops
the master node.

Or from a terminal:

```sh
cd master-node
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
.\.venv\Scripts\python -m master_node     # http://localhost:8000, API docs at /docs
```

On Linux or macOS, use `.venv/bin/python` in place of `.\.venv\Scripts\python`.

The `instruments.toml` in this folder lists Thor, the Flex, the Tecan, a plate
washer and a qPCR, all **simulated**, so you can run whole workflows from the
protocol tool with no hardware. Simulated runs take a minute or two; set
`[sim] time_scale = 0.1` in `config.toml` to make them 10× faster.

## On the lab PC

The master node normally runs on a PC in the lab, next to the devices, while
the protocol tool runs on another PC. To set that up:

1. **Copy the `master-node` folder to the lab PC,** without its `.venv`
   folder: Python environments can't be moved between PCs. Leave out
   `master_node.db` too if you want to start with an empty run history.
   `config.toml` comes along; it holds the API key the protocol tool must send.
2. **Set up the registry:** copy `instruments.example.toml` to
   `instruments.toml`. It has the Flex (`172.25.68.5`), the Tecan reader API
   (`192.168.0.116:8000`) and Thor in manual mode. Add the washer and qPCR once
   their servers run.
3. **Install Python 3.11 or newer** on the lab PC from python.org, ticking
   "Add python.exe to PATH".
4. **Double-click `run-master-node.cmd`.** When Windows Firewall asks whether
   Python may accept connections, allow it on **private networks**. Or, in an
   administrator PowerShell:
   `New-NetFirewallRule -DisplayName "Master node" -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow -Profile Private`
5. **Find the lab PC's address:** run `ipconfig` there and note the IPv4
   address, e.g. `192.168.0.50`.
6. **On the protocol tool PC,** set `[master] url = "http://192.168.0.50:8000"`
   in `protocol-tool\config.toml` (`api_key` there must match this folder's
   `config.toml`), then restart the protocol tool. Check the connection with
   `Test-NetConnection 192.168.0.50 -Port 8000` in PowerShell.

The protocol tool's top bar shows one chip per registered instrument, or
"Master node offline", with the reason on hover, until it can reach the lab
PC. Both PCs need to be on the same network or VPN.

Older versions kept the devices in `config.toml` (`[thor]`, `[flex]`,
`[tecan]`, `[stations]`). The master node now refuses to start with those
sections and says to move them to `instruments.toml`.

## The instrument registry

`instruments.toml` lists every instrument. Each `[[instrument]]` has an `id`,
a `kind` (`transport`, `liquid_handler`, `plate_reader`, `plate_washer`,
`qpcr`), a `driver`, the server's `url`, where the plate goes on it
(`plate_slots`), and `limits`. Every option is explained in
[instruments.example.toml](instruments.example.toml).

| Driver | For |
|---|---|
| `sim` | Simulated: try workflows before the hardware is connected |
| `manual` | Transport only: a person moves the plate and clicks **Plate moved** |
| `job_api` | Any instrument server that follows [INSTRUMENT_API.md](INSTRUMENT_API.md): the washer, qPCR and Thor servers, and anything added later |
| `opentrons_flex` | The Opentrons Flex, through its own HTTP API (port 31950) |
| `tecan_infinite` | The Tecan Infinite M200 Pro reader API on the Tecan PC |

**Adding an instrument** needs no master node code: write its server to
[INSTRUMENT_API.md](INSTRUMENT_API.md), add an `[[instrument]]` entry with
`driver = "job_api"`, and restart the master node. The protocol tool's
assistant sees it, and its limits, on the next message.

**Selecting instruments:** a step can name an instrument (`"instrument":
"tecan"`); otherwise the planner picks the first one of the right kind whose
limits allow the step.

**Safety limits** the planner checks (all optional):

| Kind | Limits |
|---|---|
| any | `labware`: plate types it accepts |
| plate reader | `modes`, `wavelength_nm = [min, max]`, `max_wavelengths`, `excitation_nm`, `emission_nm`, `integration_ms`, `methods` |
| liquid handler | `max_protocol_kb` |
| plate washer | `programs`, `programs_only`, `max_volume_ul`, `max_cycles` |
| qPCR | `programs` |

For example, `modes = ["absorbance"]` on the Tecan makes the master node
refuse fluorescence and luminescence reads.

## Moving the plate by hand

With the transport's `driver = "manual"`, a person does Thor's job. When a run
reaches a **Fetch plate** or **Move plate** step, the run's page on the protocol
tool's Runs tab says where to take the plate from and to. Move it, then click
**Plate moved**, and the run carries on. The run waits up to the transport's
`timeout_s` (default 1 hour in the example registry) for the click, then fails.

## Tip racks

Tip racks stay on the Flex between runs, so the next run only needs its other
labware put on the deck. The master node remembers how many tips each rack has
used and starts every pipette at its rack's next unused tip:

- The protocol tool sends, with each liquid-handling step, which tip racks each
  pipette uses and how many times it picks up tips (from the simulation).
- Before starting, the master node checks the racks have enough tips left for
  all the run's protocols together, and are the same type as recorded. If not,
  the run fails at preflight with the reason, before anything moves.
- It adds a few lines to the end of each protocol that set each pipette's
  starting tip, then counts the tips as used when the Flex run starts. Tips
  count from A1 down each column (A1, B1 … H1, A2 …). An 8-channel pipette
  takes whole columns; a 96-channel pipette needs a full rack.
- A run that stops early still counts all its tips as used, so the count can
  only be too low, never too high: the Flex never reaches for a tip that isn't
  there.

When you put a **full rack** on the deck, click **New rack** next to it on the
protocol tool's Runs tab. When you **take a rack off**, click **Removed**. A
rack the master node hasn't seen before is treated as full.

The counts are in the `tip_racks` table of `master_node.db`.

## API

| | |
|---|---|
| `GET /health` | Liveness and each instrument's driver (no API key needed) |
| `GET /instruments` | The registry: kinds, plate positions, limits (no keys) |
| `GET /devices` | Live readiness of every instrument |
| `POST /plan` | Select instruments and check limits for a workflow, without queuing it |
| `POST /runs` | Submit a workflow (see `ProtocolSubmission` in `models.py`) |
| `GET /runs` | Recent runs, newest first |
| `GET /runs/{id}` | Run with steps, event log and instrument outputs |
| `POST /runs/{id}/plate-moved` | Confirm a plate was moved by hand (manual transport) |
| `POST /runs/{id}/abort` | Stop a queued or running run |
| `GET /runs/{id}/results` | Every measurement (reads, qPCR) as JSON |
| `GET /runs/{id}/results.csv` | Measurements as CSV, one row per well |
| `GET /tips` | Tip racks on the Flex: tips used and left, next tip |
| `POST /tips/{slot}/fresh` | A full rack was put in this slot |
| `DELETE /tips/{slot}` | The rack in this slot was taken off |

If `server.api_key` is set, send it as `X-API-Key` on every call except `/health`.

A submission:

```json
{
  "name": "IL-6 ELISA", "scientist": "A. Scientist",
  "plate": {"barcode": "PL-0001", "storage_slot": "R1-S3", "labware": "corning_96_wellplate_360ul_flat"},
  "steps": [
    {"type": "liquid_handling", "protocol_filename": "samples.py", "protocol_b64": "...", "deck_slot": "D2", "tips": []},
    {"type": "hold", "seconds": 7200, "label": "Incubate"},
    {"type": "wash", "cycles": 3, "volume_ul": 300},
    {"type": "read", "settings": {"mode": "absorbance", "wavelengths_nm": [450, 570], "wells": ["A1", "A2"]}},
    {"type": "qpcr", "program": "SYBR-40"}
  ]
}
```

Any step can carry `"instrument": "<registry id>"` and a `"label"`. The older
shape (`liquid_handling` and `read` at the top level) is still accepted as a
two-step workflow.

Read settings (`ReadSpec` in `models.py`):

| Mode | Fields |
|---|---|
| `absorbance` | `wavelengths_nm`: 1-10 wavelengths, 230-1000 nm (one read each), `flashes` 1-100 |
| `fluorescence` | `excitation_nm` 230-850, `emission_nm` 280-850 (longer than excitation), `gain` 1-255 or null for optimal, `flashes` 1-100 |
| `luminescence` | `integration_ms` 1-10000 |
| all | `wells` (required) |

These ranges are the Infinite M200 Pro's; a reader's `limits` can narrow them.
`read_from` and `shake_s` are accepted but ignored (the reader's API has no
such settings), and reads naming a saved Tecan `method` fail on the Tecan
driver: reads always use the settings above.

The protocol tool calls this API from its server, so browser CORS settings
(`server.cors_origins`) only matter if a page calls the master node directly.

## Only the master node controls the instruments

The protocol tool never talks to the instruments. It has no addresses for
them. It sends runs to the master node and asks it for status, results and
aborts, and the master node decides what the instruments do.

To make sure nothing else can command the instruments:

- **Opentrons Flex:** its HTTP API has no login, so keep it off the shared
  network. Connect it directly to the master node PC, with an Ethernet cable
  or by USB, and set its `url` in `instruments.toml` to the address it gets
  there (shown in the Opentrons App or on the Flex touchscreen under Networking).
- **Tecan:** the reader's API (port 8000 on the Tecan PC) has no login.
  Either run the master node on the Tecan PC, with the Tecan's `url =
  "http://127.0.0.1:8000"` and the master node's `[server] port` moved to
  another port such as 8100 (the Tecan API already uses 8000), or firewall the
  Tecan PC so port 8000 accepts only the master node PC.
- **Job API servers (washer, qPCR, Thor):** give each an `api_key` in
  `instruments.toml`; the server must reject requests without it. Thor is
  mobile, so it is usually on Wi-Fi; if possible, put it on a separate network
  that only the master node PC can reach.
- **Master node:** other computers reach only its API on port 8000, which
  needs `server.api_key`.

```sh
.\.venv\Scripts\python -m pytest          # 67 tests, about 3 s
```

## Layout

```
master_node/
  main.py        FastAPI app, routes, entry point
  registry.py    The instrument registry (instruments.toml)
  planner.py     Decode + plan: select instruments, check limits, add plate moves
  sequencer.py   Run queue, step execution, interlocks, abort
  tips.py        Tip rack counts and the starting-tip lines added to protocols
  store.py       SQLite persistence
  models.py      Workflow (the protocol IR), run, step and result models
  config.py      config.toml loading
  devices/       base.py (interfaces), job_api.py, flex.py, tecan.py, manual.py, sim.py
tests/           planner, registry, API end-to-end (simulated), HTTP drivers (mocked), tips
INSTRUMENT_API.md  The contract instrument servers follow
```
