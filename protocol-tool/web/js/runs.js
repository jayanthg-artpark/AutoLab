// Runs tab: what the master node is doing, step by step, and the plate readings.

import { api, request } from "./api.js";
import { renderAnalysis } from "./charts.js";
import { $, h, icon, formatTime, formatDuration, formatValue, parseWell, rowLabel, range, showTooltip, hideTooltip } from "./dom.js";

const FINISHED = new Set(["succeeded", "failed", "aborted", "interrupted"]);
// Instrument names from the master node's registry (see loadInstruments).
const deviceNames = { master: "Master" };
let instrumentsLoaded = false;

async function loadInstruments() {
  if (instrumentsLoaded) return;
  try {
    for (const i of (await api("/api/instruments")).instruments) deviceNames[i.id] = i.name;
    instrumentsLoaded = true;
  } catch {
    // Show ids until the master node answers.
  }
}
const STATUS_ICON = { queued: "clock", running: "spinner", succeeded: "check", failed: "cross", aborted: "stop", interrupted: "stop" };
const STEP_ICON = { running: "spinner", done: "check", failed: "cross", skipped: "dash" };
// Sequential single-hue ramp (blue 100 -> 700). Reversed in dark mode so low readings recede.
const RAMP = ["#cde2fb", "#b7d3f6", "#9ec5f4", "#86b6ef", "#6da7ec", "#5598e7", "#3987e5", "#2a78d6", "#256abf", "#1c5cab", "#184f95", "#104281", "#0d366b"];

export function pill(status, label = status) {
  return h("span", { class: `pill ${status}` }, STATUS_ICON[status] ? icon(STATUS_ICON[status]) : null, label);
}

export function createRuns({ explainEnabled = () => false, onOpenDesign = () => {} } = {}) {
  let designInfo = null; // { runId, available, source } for the selected run
  let runs = [];
  let selectedId = null;
  let selected = null;
  let results = null;
  let note = null; // { runId, text } once the AI summary is loaded
  let analysis = null; // { runId, analyses } once computed
  let measurementIndex = 0;
  let showTable = false;

  async function refreshTips() {
    let racks;
    try {
      racks = (await api("/api/tips")).racks;
    } catch {
      return;
    }
    $("tips-empty").hidden = racks.length > 0;
    $("tip-racks").replaceChildren(
      ...racks.map((rack) =>
        h("li", { class: "tip-rack" },
          h("span", { class: "slot-tag" }, rack.slot),
          h("span", { class: "tip-name", title: rack.load_name }, tipName(rack.load_name)),
          h("span", { class: "tip-actions" },
            h("button", { type: "button", class: "btn ghost small", onclick: () => tipAction(rack, "fresh") }, "New rack"),
            h("button", { type: "button", class: "btn ghost small", onclick: () => tipAction(rack, "remove") }, "Removed"),
          ),
          h("span", { class: `tip-meter${rack.remaining < 16 ? " low" : ""}`, "aria-hidden": "true" },
            h("span", { style: { width: `${(rack.remaining / 96) * 100}%` } })),
          h("span", { class: "tip-left" },
            `${rack.remaining} of 96 tips left${rack.next_tip ? ` · next ${rack.next_tip}` : " · empty, replace it"}`),
        ),
      ),
    );
  }

  function tipName(loadName) {
    const size = /tiprack_(\d+)ul/.exec(loadName);
    if (!size) return loadName;
    return `${size[1]} µL ${loadName.includes("filter") ? "filter " : ""}tips`;
  }

  async function tipAction(rack, action) {
    const question = action === "fresh"
      ? `Mark ${rack.slot} as a full, new ${tipName(rack.load_name)} rack?`
      : `Remove the record for the rack in ${rack.slot}? Do this when the rack is taken off the deck.`;
    if (!confirm(question)) return;
    try {
      if (action === "fresh") await api(`/api/tips/${rack.slot}/fresh`, { method: "POST" });
      else await api(`/api/tips/${rack.slot}`, { method: "DELETE" });
    } catch (err) {
      alert(err.message);
    }
    await refreshTips();
  }

  async function refresh() {
    refreshTips();
    await loadInstruments();
    try {
      runs = await api("/api/runs?limit=50");
      $("runs-updated").textContent = `Updated ${new Date().toLocaleTimeString()}`;
    } catch (err) {
      $("runs-updated").textContent = err.message;
      return;
    }
    renderList();
    if (selectedId && (!selected || !FINISHED.has(selected.status) || (selected.has_results && !results))) {
      await refreshSelected();
    }
  }

  function renderList() {
    $("runs-empty").hidden = runs.length > 0;
    $("runs").replaceChildren(
      ...runs.map((run) =>
        h("li", {},
          h("button", {
            type: "button", class: "run-item",
            "aria-current": run.id === selectedId ? "true" : null,
            onclick: () => select(run.id),
          },
            h("span", { class: "run-name" }, run.name),
            h("span", { class: "run-sub" }, [run.scientist, formatTime(run.created_at), run.current_step].filter(Boolean).join(" · ")),
            pill(run.status),
          ),
        ),
      ),
    );
  }

  async function select(id) {
    if (id !== selectedId) {
      selectedId = id;
      selected = null;
      results = null;
      note = null;
      analysis = null;
      measurementIndex = 0;
    }
    renderList();
    await refreshSelected();
  }

  async function refreshSelected() {
    if (!selectedId) return;
    try {
      selected = await api(`/api/runs/${encodeURIComponent(selectedId)}`);
    } catch (err) {
      if (err.status === 404) {
        selectedId = selected = null;
        $("run-detail").hidden = true;
      }
      return;
    }
    if (selected.has_results && results?.runId !== selected.id) {
      try {
        results = { runId: selected.id, data: await api(`/api/runs/${encodeURIComponent(selected.id)}/results`) };
      } catch {
        // Retry on the next refresh.
      }
    }
    if (designInfo?.runId !== selected.id) {
      try {
        designInfo = { runId: selected.id, ...(await api(`/api/runs/${encodeURIComponent(selected.id)}/design`)) };
      } catch {
        designInfo = null; // retry on the next refresh
      }
    }
    const planned = selected.protocol.analysis?.length > 0;
    if (planned && selected.has_results && FINISHED.has(selected.status) && analysis?.runId !== selected.id) {
      try {
        analysis = { runId: selected.id, analyses: (await api(`/api/runs/${encodeURIComponent(selected.id)}/analysis`)).analyses };
      } catch {
        // Retry on the next refresh.
      }
    }
    if (FINISHED.has(selected.status) && explainEnabled() && note?.runId !== selected.id) {
      try {
        const saved = (await api(`/api/runs/${encodeURIComponent(selected.id)}/explain`)).note;
        note = { runId: selected.id, text: saved?.text ?? "" };
      } catch {
        // Retry on the next refresh.
      }
    }
    renderRun();
  }

  function renderRun() {
    const run = selected;
    $("run-detail").hidden = false;
    $("run-title").textContent = run.protocol.name;
    const plate = run.protocol.plate;
    $("run-meta").textContent = [
      run.id, run.protocol.scientist,
      `plate ${plate.barcode || "(no barcode)"} from ${plate.storage_slot}`,
    ].join(" · ");
    $("run-status").replaceChildren(pill(run.status));
    $("abort-btn").hidden = FINISHED.has(run.status);
    $("open-design-btn").hidden = !(designInfo?.runId === run.id && designInfo.available);
    $("run-error").hidden = !run.error;
    $("run-error").textContent = run.error;
    const move = run.context?.manual_move;
    $("manual-move").hidden = !move;
    if (move) $("manual-move-text").textContent = `Take it from ${place(move.from)} to ${place(move.to)}, then click Plate moved.`;
    $("steps").replaceChildren(...run.steps.map(renderStep));
    $("event-count").textContent = `(${run.events.length})`;
    $("events").replaceChildren(
      ...run.events.slice().reverse().map((e) =>
        h("li", { class: e.level }, h("time", { datetime: e.at }, new Date(e.at).toLocaleTimeString()), h("span", {}, e.message)),
      ),
    );
    renderAnalyses();
    renderResults();
    renderExplain();
  }

  // --- Analysis: curves, interpolated samples, comparisons (computed by the protocol tool) ---

  let analysisShown = null;
  function renderAnalyses() {
    const data = analysis?.runId === selected?.id ? analysis.analyses : null;
    $("analysis").hidden = !data?.length;
    if (!data?.length || analysisShown === data) return; // don't rebuild charts on every poll
    analysisShown = data;
    $("analysis-view").replaceChildren(...data.map(renderAnalysis));
  }

  $("analysis-csv").addEventListener("click", async () => {
    if (!selected) return;
    try {
      const response = await request(`/api/runs/${encodeURIComponent(selected.id)}/analysis.csv`);
      const url = URL.createObjectURL(await response.blob());
      h("a", { href: url, download: `${selected.id}-analysis.csv` }).click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (err) {
      alert(`Could not download the analysis: ${err.message}`);
    }
  });

  // --- AI summary of a finished run (optional, [results] explain) -------------------------

  function renderExplain() {
    const run = selected;
    const show = FINISHED.has(run.status) && explainEnabled() && note?.runId === run.id;
    $("explain").hidden = !show;
    if (!show) return;
    $("explain-text").textContent = note.text || (run.status === "succeeded"
      ? "Ask the AI model for a plain-language summary of the results."
      : "Ask the AI model what most likely went wrong and what to check before running again.");
    $("explain-text").classList.toggle("muted", !note.text);
    $("explain-btn").textContent = note.text ? "Explain again" : run.status === "succeeded" ? "Explain results" : "Why did it fail?";
  }

  $("explain-btn").addEventListener("click", async () => {
    if (!selected) return;
    const button = $("explain-btn");
    const runId = selected.id;
    button.disabled = true;
    button.textContent = "Explaining…";
    try {
      const saved = (await api(`/api/runs/${encodeURIComponent(runId)}/explain`, { method: "POST" })).note;
      note = { runId, text: saved.text };
    } catch (err) {
      alert(`Could not explain the run: ${err.message}`);
    } finally {
      button.disabled = false;
      if (selected?.id === runId) renderExplain();
    }
  });

  function renderStep(step) {
    let time = "";
    if (step.started_at && step.finished_at && step.key !== "decode_plan") {
      time = formatDuration(new Date(step.finished_at) - new Date(step.started_at));
    } else if (step.status === "running" && step.started_at) {
      time = formatDuration(Date.now() - new Date(step.started_at));
    }
    return h("li", { class: `step ${step.status}` },
      h("span", { class: "step-icon", title: step.status }, STEP_ICON[step.status] ? icon(STEP_ICON[step.status]) : null),
      h("span", { class: "step-label" }, step.label,
        h("span", { class: "device-tag" }, deviceNames[step.device] ?? step.device),
        h("span", { class: "visually-hidden" }, `(${step.status})`)),
      h("span", { class: "step-time" }, time),
      step.detail ? h("span", { class: "step-detail" }, step.detail) : null,
    );
  }

  // --- Results heatmap ---------------------------------------------------------------------

  const isDark = () => matchMedia("(prefers-color-scheme: dark)").matches;
  const ramp = () => (isDark() ? RAMP.slice().reverse() : RAMP);
  const rgb = (hex) => [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16));

  function colorAt(t) {
    const steps = ramp();
    const pos = Math.min(Math.max(t, 0), 1) * (steps.length - 1);
    const i = Math.min(Math.floor(pos), steps.length - 2);
    const [a, b] = [rgb(steps[i]), rgb(steps[i + 1])];
    return `rgb(${a.map((c, k) => Math.round(c + (b[k] - c) * (pos - i))).join(",")})`;
  }

  function renderResults() {
    const data = results?.runId === selected?.id ? results.data : null;
    $("results").hidden = !data;
    if (!data) return;

    const select = $("measurement");
    select.hidden = data.measurements.length < 2;
    select.replaceChildren(...data.measurements.map((m, i) => h("option", { value: i, selected: i === measurementIndex }, m.label)));
    const m = data.measurements[measurementIndex] ?? data.measurements[0];
    const view = $("plate-view");
    if (!m) return view.replaceChildren(h("p", { class: "empty" }, "The reader returned no measurements."));

    const grid = new Map();
    let rows = 8, cols = 12;
    for (const [well, value] of Object.entries(m.values)) {
      const pos = parseWell(well);
      if (!pos) continue;
      grid.set(`${pos.row},${pos.col}`, { well, value });
      rows = Math.max(rows, pos.row + 1);
      cols = Math.max(cols, pos.col + 1);
    }
    const numbers = [...grid.values()].map((c) => c.value).filter(Number.isFinite);
    if (!numbers.length) return view.replaceChildren(h("p", { class: "empty" }, "The reader returned no numeric values."));
    const min = Math.min(...numbers), max = Math.max(...numbers);
    const scaled = (v) => (max > min ? (v - min) / (max - min) : 0.5);
    const unit = m.unit ? ` ${m.unit}` : "";

    $("toggle-table").setAttribute("aria-pressed", String(showTable));
    $("toggle-table").textContent = showTable ? "Heatmap" : "Table";

    if (showTable) {
      return view.replaceChildren(
        h("div", { class: "plate-wrap" },
          h("table", { class: "plate-table" },
            h("caption", { class: "visually-hidden" }, `${m.label}${unit} by well`),
            h("thead", {}, h("tr", {}, h("th"), range(cols).map((c) => h("th", { scope: "col" }, c + 1)))),
            h("tbody", {}, range(rows).map((r) =>
              h("tr", {}, h("th", { scope: "row" }, rowLabel(r)), range(cols).map((c) => h("td", {}, formatValue(grid.get(`${r},${c}`)?.value)))),
            )),
          ),
        ),
      );
    }

    const plate = h("div", {
      class: "plate", role: "img",
      "aria-label": `${m.label} heatmap, ${formatValue(min)} to ${formatValue(max)}${unit}. Use the Table button for values.`,
      style: { gridTemplateColumns: `18px repeat(${cols}, minmax(0, 1fr))` },
    }, h("span"), range(cols).map((c) => h("span", { class: "axis" }, c + 1)));
    for (const r of range(rows)) {
      plate.append(h("span", { class: "axis" }, rowLabel(r)));
      for (const c of range(cols)) {
        const cell = grid.get(`${r},${c}`);
        const well = cell?.well ?? `${rowLabel(r)}${c + 1}`;
        const ok = Number.isFinite(cell?.value);
        const text = ok ? `${formatValue(cell.value)}${unit}` : "not read";
        plate.append(h("button", {
          type: "button", class: ok ? "cell" : "cell missing",
          style: ok ? { background: colorAt(scaled(cell.value)) } : {},
          "aria-label": `${well}: ${text}`,
          onmouseenter: (e) => showTooltip(e.currentTarget, h("strong", {}, well), h("br"), `${m.label}: ${text}`),
          onfocus: (e) => showTooltip(e.currentTarget, h("strong", {}, well), h("br"), `${m.label}: ${text}`),
          onmouseleave: hideTooltip,
          onblur: hideTooltip,
        }));
      }
    }
    view.replaceChildren(
      h("div", { class: "plate-wrap" }, plate),
      h("div", { class: "scale" },
        h("span", {}, m.label + unit),
        h("span", { class: "val" }, formatValue(min)),
        h("span", { class: "scale-bar", style: { background: `linear-gradient(to right, ${ramp().join(",")})` } }),
        h("span", { class: "val" }, formatValue(max)),
      ),
    );
  }

  function place({ station, slot }) {
    if (station === "storage") return `storage slot ${slot}`;
    const name = deviceNames[station] ?? station;
    return slot === "carrier" ? `the ${name} plate carrier` : `${name} slot ${slot}`;
  }

  // --- Actions ------------------------------------------------------------------------------

  $("plate-moved").addEventListener("click", async () => {
    if (!selected) return;
    const button = $("plate-moved");
    button.disabled = true;
    try {
      await api(`/api/runs/${encodeURIComponent(selected.id)}/plate-moved`, { method: "POST" });
    } catch (err) {
      alert(`Could not confirm: ${err.message}`);
    } finally {
      button.disabled = false;
    }
    await refreshSelected();
  });

  $("open-design-btn").addEventListener("click", async () => {
    if (!selected) return;
    const button = $("open-design-btn");
    button.disabled = true;
    try {
      onOpenDesign(await api(`/api/runs/${encodeURIComponent(selected.id)}/reopen`, { method: "POST" }));
    } catch (err) {
      alert(`Could not open the design: ${err.message}`);
    } finally {
      button.disabled = false;
    }
  });

  $("abort-btn").addEventListener("click", async () => {
    if (!selected) return;
    if (!confirm(`Abort "${selected.protocol.name}"?\n\nDevices stop where they are; someone needs to check where the plate is.`)) return;
    try {
      await api(`/api/runs/${encodeURIComponent(selected.id)}/abort`, { method: "POST" });
    } catch (err) {
      alert(`Could not abort: ${err.message}`);
    }
    await refreshSelected();
  });
  $("csv-btn").addEventListener("click", async () => {
    if (!selected) return;
    try {
      const response = await request(`/api/runs/${encodeURIComponent(selected.id)}/results.csv`);
      const url = URL.createObjectURL(await response.blob());
      h("a", { href: url, download: `${selected.id}.csv` }).click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (err) {
      alert(`Could not download results: ${err.message}`);
    }
  });
  $("toggle-table").addEventListener("click", () => {
    showTable = !showTable;
    renderResults();
  });
  $("measurement").addEventListener("change", (e) => {
    measurementIndex = Number(e.target.value);
    renderResults();
  });
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", renderResults);

  return { refresh, select, hasActiveRun: () => runs.some((r) => r.status === "running") };
}
