# Lab automation

```
 This PC: protocol tool                                Lab PC: master node (orchestrator, no AI)
 ┌──────────────────────────────────┐                 ┌───────────────────────────────────────┐
 │ Scientist UI: chat, PDFs, approve│   plan / run    │ instrument registry · safety limits   │
 │ Planner services:                │ ──────────────▶ │ select instruments · plate moves      │
 │  extract & clarify   (AI)        │                 │ run queue · interlocks · results      │
 │  build protocol IR   (AI)        │ ◀────────────── └───┬───────┬────────┬────────┬──────┬──┘
 │  validate/simulate   (Opentrons) │  status/results     │       │        │        │      │
 │ Knowledge: kit manuals, templates│                     ▼       ▼        ▼        ▼      ▼
 │ Results: AI summaries (optional) │                   Flex    Tecan   washer    qPCR   Thor
 └──────────────────────────────────┘                          (servers on the instrument PC, .116)
```

- [`protocol-tool/`](protocol-tool): where scientists plan the experiment with
  an AI model. It works out the requirements (using the lab's kit manuals and
  assay templates), writes and simulates the Opentrons protocols, and builds the
  workflow: the steps done to the plate across the lab's instruments. The
  scientist reviews exactly what will happen and approves it.
- [`master-node/`](master-node): the lab service. For each step it picks an
  instrument from its registry and checks the step against that instrument's
  safety limits. It has Thor move the plate between instruments and runs each
  step, then stores the results. Instruments are added in `instruments.toml`;
  new instrument servers follow [INSTRUMENT_API.md](master-node/INSTRUMENT_API.md).

- [`thor-server/`](thor-server): runs on the Jetson Thor (Ranger Mini base + Franka FR3 arm) in
  `~/lab_thor_server`. It's the master node's transport: it moves a plate between stations by
  driving there and running the existing pick-and-place stack's flows. It starts in `dry_run` mode
  (nothing moves); see its README for the steps to real moves.

## Start it (Windows)

1. **Lab PC:** copy the `master-node` folder over (without `.venv`), copy
   `instruments.example.toml` to `instruments.toml` there (the Flex, the Tecan
   at .116, Thor by hand), then double-click `run-master-node.cmd`. Allow it
   through Windows Firewall on private networks when asked. Full steps:
   [master-node/README.md](master-node/README.md#on-the-lab-pc).
2. **This PC:** in `protocol-tool\config.toml`, set `[master] url` to the lab
   PC's address (for example `http://192.168.0.50:8000`). Then double-click
   `protocol-tool\run-protocol-tool.cmd`. It opens <http://localhost:5500>.

Both `config.toml` files already share the same API key, so the two sides
recognise each other. To try everything on one PC, leave `url` as
`http://localhost:8000` and run both `.cmd` files here: the master node's
`instruments.toml` here simulates Thor, the Flex, the Tecan, a washer and a qPCR.

The AI model is Google Gemini's free tier. Its key goes in `protocol-tool\.env`
(`GEMINI_API_KEY=...`). Other options, a free local model through Ollama or
Claude, are in [protocol-tool/README.md](protocol-tool/README.md#choose-the-ai-model).

Describe an experiment (or attach a PDF). When every protocol passes
simulation, fill in the plate details, click **Review plan…**, check the steps
and instruments, and click **Approve and send to lab**. Follow the run on the
**Runs** tab. Until Thor is connected, someone moves the plate by hand and
clicks **Plate moved** there. Tip racks stay on the Flex between runs; the Runs
tab shows how many tips each has left. Kit manuals and assay templates go in
`protocol-tool\knowledge\`.
