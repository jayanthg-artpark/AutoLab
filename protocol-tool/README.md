# Protocol tool

The website scientists use to plan an experiment with an AI assistant, check
it, approve it and send it to the lab's [master node](../master-node).
The assistant can be a free model (Google Gemini's free tier, or a model running
on this PC through Ollama) or Claude (paid).

It holds the scientist UI and the planner services:

| Planner service | AI? | What it does |
|---|---|---|
| Extract and clarify | yes | Works out what the experiment needs (assay, samples, controls, replicates, readout), records assumptions and open questions, looking things up in the lab's kit manuals and assay templates |
| Build protocol IR | yes | Writes the Opentrons protocols and the workflow: the ordered steps done to the plate (liquid handling, read, wash, qPCR, hold) |
| Select instruments | no | The master node picks an instrument from its registry for each step (`POST /plan`) |
| Validate, simulate | no | Every protocol goes through the Opentrons simulator; the master node checks every step against its instrument's safety limits |

**Design tab**
- **Chat** (left). Describe the experiment, or attach a protocol PDF, paper,
  plate map, image or text file. The assistant records the requirements,
  writes the protocols, simulates them, sets the workflow, and fixes whatever
  the simulator or the master node reports before replying.
- **Workflow** (top right). The requirements (with open questions) and the
  steps in order. Click a liquid-handling step to see its protocol on the
  deck, or a read step to edit it.
- **Flex deck**. The selected protocol's deck exactly as the simulator loads
  it: labware, modules, adapters, trash, pipettes and liquids, with the slot
  where Thor places the sample plate outlined. **Code** shows the protocol, and
  **Steps** shows the simulated run command by command.
- **Plate reader**: the selected read step's mode, wavelengths, gain, flashes,
  and the wells to read (click, drag, or click a row or column label). The
  assistant fills it in; you can change anything, and it sees your edits on
  its next reply.
- **Review and send.** Enabled once every protocol has passed simulation.
  **Review plan…** asks the master node exactly what it would do: the
  instrument for each step, every plate move and the safety checks. **Approve
  and send to lab** sends that plan; if anything changed after the review, it
  refuses and asks you to review again.

**Runs tab.** Each run's progress step by step, with the plate heatmap and CSV
download when it finishes, and **Explain**: the AI model's plain-language
summary of the results, or what to check when a run failed (turn off with
`[results] explain = false`).

## Results: curves and calculated values

While designing, the assistant also plans the **analysis** (`set_analysis`): the
plate map (blanks, standards with their concentrations, samples with their
dilutions, groups) and the fit biologists use for that assay. It shows under
**Results** in the Workflow panel and travels with the run. As soon as the
readings arrive, the protocol tool computes it, with no AI involved, and the
Runs tab plots it:

| Analysis | Typical assays | Fit and output |
|---|---|---|
| Standard curve | Bradford, BCA (linear or quadratic); ELISA (4PL/5PL, optional 450 − 570 nm reference); qPCR (log-linear Ct vs quantity) | Curve with standards ± SD, samples read off the curve and multiplied by their dilution, R², qPCR efficiency, flags for samples outside the standard range |
| Dose-response | Viability, IC50/EC50 | 4PL per compound: EC50, Hill slope, top, bottom |
| Dilution series | Linearity, pipetting checks | Signal vs dilution, optional fit |
| Group comparison | Treated vs control | Mean ± SD, CV, fold change, Welch's t-test |

Every chart has its table underneath, and **Download analysis CSV** exports
it. Blanks are subtracted, missing wells are left out and reported, and an R²
below 0.98 is flagged. **Explain** summarises these computed numbers; the AI
model never calculates them. With simulated instruments the readings are a
fixed pattern, so curves won't fit; real readings will.

## Knowledge: kit manuals, templates and other labs' protocols

The assistant searches the `knowledge` folder by keyword while it designs:

| Folder | What | How it gets there |
|---|---|---|
| `manuals\` | Kit manuals, IFUs (PDF, `.md`, `.txt`) | Drop files in, or import a URL |
| `templates\` | Assay templates (`.md`, `.txt`) | Write them; two generic examples are included |
| `protocols\` | Example protocols from other labs | Imported (below) |
| `reference\` | The Opentrons Python API docs and official Flex examples | Imported (below) |

New files are searchable at once, with no restart. The assistant follows a kit manual's
volumes, times and wavelengths, adapts example protocols (most Opentrons Library
protocols are for the OT-2, so it moves them to Flex pipettes, labware and
slots), and lists what it used under the requirements.

Import from public sources, in this folder:

```powershell
.\.venv\Scripts\python -m protocol_tool.knowledge_import opentrons-library    # ~800 protocols, Opentrons Protocol Library
.\.venv\Scripts\python -m protocol_tool.knowledge_import opentrons-docs       # Python API docs + official Flex examples
.\.venv\Scripts\python -m protocol_tool.knowledge_import github owner/repo    # a lab's GitHub repo of Opentrons protocols
.\.venv\Scripts\python -m protocol_tool.knowledge_import protocols-io "ELISA" "Bradford" --limit 25
.\.venv\Scripts\python -m protocol_tool.knowledge_import url https://vendor.com/kit-manual.pdf
```

| Source | Terms recorded |
|---|---|
| Opentrons Protocol Library | No license file: kept as an internal reference, credit the authors, don't republish |
| Opentrons Python API docs | Apache-2.0 |
| GitHub repositories | The repository's own license (or "internal reference only" if it has none) |
| protocols.io | CC BY 4.0: cite the authors and DOI. Needs a free token: sign in at https://www.protocols.io/developers and add `PROTOCOLS_IO_TOKEN=<token>` to `.env` |
| A URL | Whatever you pass with `--license` |

Every imported document starts with its source, and `knowledge\sources.json`
lists them all. Re-running an import refreshes it. The imported folders are in
`.gitignore`, so they aren't republished with the code. Scanned PDFs without a
text layer can't be searched.

## Run it

On Windows, double-click **`run-protocol-tool.cmd`**. It opens
http://localhost:5500 in your browser. The first time, it sets up its Python
packages, including the Opentrons simulator, which takes a few minutes. Keep
the window open; closing it stops the site.

Or from PowerShell:

```powershell
cd D:\testing\protocol-tool
python -m venv .venv                                  # setup, once
.\.venv\Scripts\python -m pip install -e ".[dev]"     # setup, once
.\.venv\Scripts\python -m protocol_tool               # http://localhost:5500
```

The master node must be running too, so "Send to lab" has somewhere to send
to. Set its address in `config.toml`:

```toml
[master]
url = "http://192.168.0.50:8000"   # the lab PC's IP address (run ipconfig there)
api_key = "..."                    # same as api_key in the master node's config.toml
```

See [Running the master node on the lab PC](../master-node/README.md#on-the-lab-pc).

## Choose the AI model

Set `[assistant] provider` in `config.toml`. Keys go in a file called `.env` in
this folder, one `NAME=value` per line. The file is re-read on every check, so
after adding a key just click **Check again** on the page; no restart needed.

| Provider | Cost | Setup |
|---|---|---|
| `gemini` | Free tier (rate-limited) | Get a free key at https://aistudio.google.com/apikey, then add `GEMINI_API_KEY=<your key>` to `.env` |
| `ollama` | Free, runs on this PC | Install Ollama from https://ollama.com, run `ollama pull qwen3:30b`, and give it a longer context (Ollama settings â†’ Context length 32k, or set `OLLAMA_CONTEXT_LENGTH=32768`). No key needed |
| `claude` | Paid API credits | Key from https://console.anthropic.com, then add `ANTHROPIC_API_KEY=<your key>` to `.env` |
| `openai` | Depends | Any other OpenAI-compatible server: set `url`, `model` and `api_key_env` |

For example, to add the Gemini key from PowerShell:

```powershell
Set-Content D:\testing\protocol-tool\.env "GEMINI_API_KEY=<your key>"
```

Things to know about the free options:

- **Gemini's free tier:** Google may use what you send to improve its
  products (check their current terms). Don't send confidential protocols or
  data on the free tier. The free tier is rate-limited; the tool waits and
  retries when it hits the limit.
- **Ollama** keeps everything on this PC. A model like `qwen3:30b` fits in the
  24 GB GPU here. Local models make more mistakes than Claude or Gemini, so
  expect more simulate-and-fix rounds.
- **PDFs:** Gemini and Ollama receive the PDF's text, not the page images.
  Tables and figures may come through poorly, and scanned PDFs have no text.
  Paste the key steps into the chat when that happens.
- **Every protocol goes through the Opentrons simulator,** whichever model
  wrote it, and every workflow through the master node's checks. Review stays
  locked until they pass.

## Configure

Copy `config.example.toml` to `config.toml`. The settings that matter most:

- **`[lab] inventory`**: the pipettes, modules, tip racks and labware your
  Flex actually has. The assistant designs only with what's listed, so keep it
  accurate.
- **Flex only, newest API.** Every protocol must be a Flex protocol at
  `[lab] api_level` (default `"latest"`: the newest level the installed
  `opentrons` package supports, 2.31 now) and may load only the labware in
  `[lab] labware` (the lab's Flex labware; `*` wildcards allowed). OT-2 tip racks
  always fail, and the simulator itself refuses OT-2 pipettes and modules. A
  protocol that breaks these rules fails its simulation, so it can't be sent,
  and designs checked under older rules are checked again when you review them.
  The Flex's robot software must support the same API level; if it is older,
  update the Flex or set `api_level` to what it supports.
- **`[lab] thor_slots`, `blocked_slots`, `trash_slot`, `waste_chute_slot`**: the
  deck rules. Thor can place the sample plate in B1–B3, C1–C3, D1 and D2.
  **A1 and A2 stay empty** (`blocked_slots`): no labware, tip racks, reservoirs,
  modules or fixtures, no gripper moves into them, and no Thermocycler (it
  covers A1); the deck view shows them as "Kept empty". A3 holds the trash bin,
  and D3 holds the waste chute. The assistant picks one
  of Thor's slots for each protocol, and the simulation fails if a protocol
  breaks these rules. Keep `thor_slots` the same as the Flex's `plate_slots` in
  the master node's `instruments.toml`.
- **The lab's instruments** are not set here: the assistant reads them (and
  their limits) from the master node's registry on every message.
- **`[master] url` and `api_key`**: how to reach the master node.
- **Tip racks** need no setting. The master node keeps count of the tips left
  in each rack on the Flex, and the assistant is told which racks are there so
  it reuses them. The simulation fails if two pipettes share a tip rack, since
  the count is kept per pipette. The **Runs** tab lists the racks with the tips
  left: click **New rack** after putting a full one on the deck, and
  **Removed** after taking one off. Details:
  [master-node/README.md](../master-node/README.md#tip-racks).
- **`[assistant]`**: which model, as above. With Claude, the default is Claude
  Opus 5.5 at `effort = "high"`, with server-side fallbacks on: if a safety
  classifier declines a request, the API retries it on Anthropic's recommended
  fallback model.

## How it fits together

```
browser â”€â”€â–¶ protocol tool server â”€â”€â–¶ AI model API       (Claude, Gemini or Ollama)
                                 â”€â”€â–¶ Opentrons simulator (subprocess)
                                 â”€â”€â–¶ knowledge\          (kit manuals, assay templates)
                                 â”€â”€â–¶ master node         (registry, plan, send, run status)
```

The browser only talks to this server. The AI model's API key and the master
node's API key stay on the server.

| | |
|---|---|
| `protocol_tool/assistant.py` | Chat loop, the tools (`record_requirements`, `search_knowledge`, `read_knowledge`, `write_protocol`, `set_workflow`, `set_reader_settings`), system prompt, run summaries, Claude client |
| `protocol_tool/analysis.py` | Result analysis: curve fits (linear, quadratic, log-linear, 4PL, 5PL), interpolation, comparisons, CSV |
| `protocol_tool/workflow.py` | Design â†’ run submission (the IR the master node accepts), and the hash an approval is tied to |
| `protocol_tool/knowledge.py` | Keyword search over the `knowledge` folder |
| `protocol_tool/knowledge_import.py` | Imports from the Opentrons Protocol Library and docs, GitHub, protocols.io and URLs |
| `protocol_tool/openai_compat.py` | Gemini, Ollama and other OpenAI-compatible models |
| `protocol_tool/model_status.py` | Checks that the configured model and key work; `.env` loading |
| `protocol_tool/providers.py` | Default model, URL and key name for each provider |
| `protocol_tool/simulator.py` | Runs `opentrons.cli analyze` and turns its output into the deck, step list and tip use |
| `protocol_tool/main.py` | HTTP API: sessions, chat (streamed), simulate, plan review, approve and send, run summaries, relays to the master node |
| `protocol_tool/models.py` | Design (requirements, protocols, workflow), reader settings, simulation, session |
| `web/` | The page: `js/chat.js`, `js/workflow.js`, `js/charts.js`, `js/deck.js`, `js/reader.js`, `js/runs.js`, `js/main.js` |
| `tests/` | Simulator (real Opentrons), chat loop (scripted model), model adapters and checks (fake APIs), API (fake master node) |

```powershell
.\.venv\Scripts\python -m pytest      # 85 tests, about 65 s (runs the real simulator)
```

## Before putting it on a network

- **The simulator runs generated code.** It executes the protocol's Python,
  which the AI model writes, possibly from an uploaded document. It runs in a
  subprocess with a timeout and without API keys in its environment. On a
  shared server, also run it in a container or VM: point `[simulator] python`
  at an isolated interpreter.
- **The site has no login.** Keep it on the lab network or VPN, or put it
  behind your organisation's sign-in. Anyone who can open it can use your
  model API key and send runs to the lab.
- **Review the protocol before approving.** The same code later runs on the
  Flex. "Simulation passed" means the protocol is valid Opentrons code, and the
  master node's checks mean each step is within its instrument's limits. Neither
  means the experiment is scientifically right, so check the requirements, the
  code, the deck and the volumes.
