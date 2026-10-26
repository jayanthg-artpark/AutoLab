// Protocol tool: wires the chat, workflow, deck, plate reader, review and runs views together.

import { api, ApiError } from "./api.js";
import { $, h, icon } from "./dom.js";
import { createChat } from "./chat.js";
import { renderDeck, setDeckRules } from "./deck.js";
import { createReader } from "./reader.js";
import { createRuns, pill } from "./runs.js";
import { createWorkflow } from "./workflow.js";

const SESSION_KEY = "protocol-tool.session";
const SCIENTIST_KEY = "protocol-tool.scientist";

let session = null;
let chatBusy = false;
let actionBusy = false;
let explainEnabled = false;
// What the side panels show: one protocol on the deck, one read step in the reader panel.
const selection = { protocolId: null, readStepId: null };
// The plan the scientist is reviewing: { hash, body }.
let review = null;

const store = {
  get(key) { try { return localStorage.getItem(key); } catch { return null; } },
  set(key, value) { try { localStorage.setItem(key, value); } catch { /* storage blocked */ } },
};

const chat = createChat({
  getSession: () => session,
  onSession: applySession,
  onBusyChange: (busy) => {
    chatBusy = busy;
    renderSend();
    if (!busy) refreshAssistant(); // a failed reply may mean the key stopped working
  },
});
const reader = createReader({ getSession: () => session, onSession: applySession });
const runs = createRuns({ explainEnabled: () => explainEnabled, onOpenDesign: openDesign });

// A run's design, copied into a new session: change it with the AI, or review and send it again.
function openDesign({ session: reopened, send }) {
  store.set(SESSION_KEY, reopened.id);
  applySession(reopened);
  chat.render(session);
  const form = $("send-form");
  for (const key of ["name", "scientist", "storage_slot", "barcode", "notes"]) {
    if (send?.[key] != null && form.elements[key]) form.elements[key].value = send[key];
  }
  form.elements.name.dataset.touched = send?.name ? "1" : "";
  $("send-ok").hidden = true;
  $("send-error").hidden = true;
  showTab("design");
}
const workflow = createWorkflow({
  onSelect(step) {
    if (step.type === "read") selection.readStepId = step.id;
    if (step.type === "liquid_handling") selection.protocolId = step.protocol_id;
    renderDesign();
  },
});

function applySession(next) {
  session = next;
  renderDesign();
}

function selectedProtocol() {
  return session.design.protocols.find((p) => p.id === selection.protocolId) ?? null;
}

function renderDesign() {
  if (!session) return;
  const design = session.design;
  // Keep the selection on things that still exist; default to the first of each.
  if (!design.protocols.some((p) => p.id === selection.protocolId)) selection.protocolId = design.protocols[0]?.id ?? null;
  const reads = design.workflow.filter((s) => s.type === "read");
  if (!reads.some((s) => s.id === selection.readStepId)) selection.readStepId = reads[0]?.id ?? null;

  workflow.render(session, selection);
  const protocol = selectedProtocol();
  renderDeck(protocol, design.protocols.length);
  renderProtocolPicker();
  const readStep = reads.find((s) => s.id === selection.readStepId) ?? null;
  const position = readStep ? `Step ${design.workflow.indexOf(readStep) + 1} of the workflow` : "";
  reader.render(session, readStep, position);
  renderSend();
}

function renderProtocolPicker() {
  const picker = $("protocol-select");
  const protocols = session.design.protocols;
  picker.hidden = protocols.length < 2;
  picker.replaceChildren(...protocols.map((p) => h("option", { value: p.id, selected: p.id === selection.protocolId }, p.title || p.id)));
}

$("protocol-select").addEventListener("change", (e) => {
  selection.protocolId = e.target.value;
  renderDesign();
});

// --- Session -----------------------------------------------------------------------------

async function loadSession() {
  const id = store.get(SESSION_KEY);
  if (id) {
    try {
      return await api(`/api/sessions/${encodeURIComponent(id)}`);
    } catch (err) {
      if (err.status !== 404) throw err;
    }
  }
  return newSession();
}

async function newSession() {
  const created = await api("/api/sessions", { method: "POST" });
  store.set(SESSION_KEY, created.id);
  return created;
}

$("new-design").addEventListener("click", async () => {
  if (session?.transcript.length && !confirm("Start a new design? The current conversation and protocols will be put aside.")) return;
  try {
    applySession(await newSession());
    chat.render(session);
    $("send-form").elements.name.dataset.touched = "";
    $("send-ok").hidden = true;
  } catch (err) {
    alert(err.message);
  }
});

// --- Review and send ----------------------------------------------------------------------

function renderSend() {
  if (!session) return;
  const design = session.design;
  const problems = session.send_problems;
  const protocol = selectedProtocol();
  const status = $("sim-status");
  let kind = "";
  let text;
  if (!design.workflow.length) {
    text = problems[0];
  } else if (problems.length) {
    kind = problems.some((p) => p.includes("failed")) ? "bad" : "stale";
    text = problems.join(" ");
  } else {
    kind = "ok";
    const sims = design.protocols.filter((p) => p.code).map((p) => p.simulation);
    const warnings = sims.reduce((n, s) => n + s.warnings.length, 0);
    text = `Ready to review · ${design.workflow.length} step${design.workflow.length > 1 ? "s" : ""}`
      + (sims.length ? ` · ${sims.length === 1 ? "protocol passes" : `all ${sims.length} protocols pass`} simulation` : "")
      + (warnings ? ` · ${warnings} warning${warnings > 1 ? "s" : ""}` : "");
  }
  status.className = `sim-status ${kind}`;
  status.replaceChildren(...(kind ? [icon({ ok: "check", bad: "cross", stale: "warn" }[kind])] : []), h("span", {}, text));

  const pillEl = $("sim-pill");
  if (!protocol?.code) pillEl.replaceChildren();
  else if (!protocol.simulation_current) pillEl.replaceChildren(pill("stale", "Not simulated"));
  else pillEl.replaceChildren(protocol.simulation.ok ? pill("passed", "Simulation passed") : pill("failed", "Simulation failed"));

  const name = $("send-form").elements.name;
  if (!name.dataset.touched && design.title) name.value = design.title;

  const busy = chatBusy || actionBusy;
  $("simulate-btn").disabled = busy || !design.protocols.some((p) => p.code);
  $("send-btn").disabled = busy || problems.length > 0;
  $("show-code").disabled = !protocol?.code;
  $("show-steps").disabled = !protocol?.simulation;
}

async function runAction(button, label, fn) {
  const original = button.textContent;
  actionBusy = true;
  button.textContent = label;
  renderSend();
  try {
    await fn();
  } finally {
    actionBusy = false;
    button.textContent = original;
    renderSend();
  }
}

$("simulate-btn").addEventListener("click", () =>
  runAction($("simulate-btn"), "Simulating…", async () => {
    $("send-error").hidden = true;
    try {
      applySession(await api(`/api/sessions/${session.id}/simulate`, { method: "POST" }));
    } catch (err) {
      showSendError(err.message);
    }
  }),
);

$("send-form").addEventListener("input", (e) => {
  if (e.target.name === "name") e.target.dataset.touched = "1";
  e.target.removeAttribute("aria-invalid");
});

function sendBody() {
  const form = $("send-form");
  return Object.fromEntries(["name", "scientist", "storage_slot", "barcode", "notes"].map((k) => [k, form.elements[k].value.trim()]));
}

// Review: the master node plans the run (instrument per step, plate moves, safety checks)
// without queuing it; the scientist approves exactly that plan.
$("send-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const form = e.currentTarget;
  const missing = [...form.querySelectorAll("[required]")].filter((el) => !el.value.trim());
  if (missing.length) {
    missing.forEach((el) => el.setAttribute("aria-invalid", "true"));
    missing[0].focus();
    showSendError("Fill in the run name, scientist and plate storage slot.");
    return;
  }
  const body = sendBody();
  runAction($("send-btn"), "Planning…", async () => {
    $("send-error").hidden = true;
    $("send-ok").hidden = true;
    try {
      const plan = await api(`/api/sessions/${session.id}/plan`, { method: "POST", json: body });
      review = { hash: plan.plan_hash, body };
      showPlan(plan, body);
    } catch (err) {
      showSendError(err instanceof ApiError && err.status === 502 ? `The master node is not reachable: ${err.message}` : err.message);
    }
  });
});

function showPlan(plan, body) {
  const names = plan.instruments;
  const label = (id) => (id === "master" ? "Master" : names[id]?.name ?? id);
  const where = ({ station, slot }) => (station === "storage" ? `storage ${slot}` : `${label(station)}${slot === "carrier" ? "" : ` ${slot}`}`);
  const simulated = [...new Set(plan.steps.map((s) => s.device))].filter((id) => names[id]?.driver === "sim").map(label);

  $("plan-intro").textContent = `"${body.name}" for ${body.scientist}: plate ${body.barcode || "(no barcode)"} from storage slot ${body.storage_slot}. `
    + "This is exactly what the master node will do. Approving sends it to the lab queue."
    + (simulated.length ? ` Simulated (no real instrument): ${simulated.join(", ")}.` : "");
  $("plan-warnings").replaceChildren(...plan.warnings.map((w) => h("li", { class: "warning" }, w)));
  $("plan-steps").replaceChildren(
    ...plan.steps.slice(1).map((step) => {
      const detail = step.key === "transfer" ? `${where(step.params.from)} → ${where(step.params.to)}` : step.params.summary ?? "";
      return h("li", { class: "step" },
        h("span", { class: "step-icon", "aria-hidden": "true" }),
        h("span", { class: "step-label" }, step.label,
          h("span", { class: "device-tag" }, label(step.device)),
          names[step.device]?.driver === "sim" ? h("span", { class: "device-tag sim" }, "simulated") : null),
        h("span", { class: "step-time" }),
        detail ? h("span", { class: "step-detail" }, detail) : null,
      );
    }),
  );
  $("plan-error").hidden = true;
  $("approve-btn").disabled = false;
  $("plan-dialog").showModal();
}

$("approve-btn").addEventListener("click", async () => {
  if (!review) return;
  const button = $("approve-btn");
  button.disabled = true;
  button.textContent = "Sending…";
  try {
    const result = await api(`/api/sessions/${session.id}/send`, { method: "POST", json: { ...review.body, plan_hash: review.hash } });
    store.set(SCIENTIST_KEY, review.body.scientist);
    review = null;
    $("plan-dialog").close();
    applySession(result.session);
    const ok = $("send-ok");
    ok.replaceChildren(
      `Approved and sent to the lab as run ${result.run.id}. `,
      h("a", { href: `#run=${result.run.id}`, onclick: (ev) => { ev.preventDefault(); showTab("runs"); runs.select(result.run.id); } }, "Follow it in Runs"),
    );
    ok.hidden = false;
  } catch (err) {
    $("plan-error").textContent = err instanceof ApiError && err.status === 502 ? `The master node is not reachable: ${err.message}` : err.message;
    $("plan-error").hidden = false;
    button.disabled = false;
  } finally {
    button.textContent = "Approve and send to lab";
  }
});

function showSendError(message) {
  $("send-error").textContent = message;
  $("send-error").hidden = !message;
}

// --- Code and simulated-steps dialogs ---------------------------------------------------------

$("show-code").addEventListener("click", () => {
  const protocol = selectedProtocol();
  $("code-view").replaceChildren(...protocol.code.replace(/\n$/, "").split("\n").map((line) => h("span", {}, line || " ")));
  $("download-code").href = `/api/sessions/${session.id}/protocol.py?protocol=${encodeURIComponent(protocol.id)}`;
  $("code-title").textContent = `${protocol.title || protocol.id} · v${protocol.version}`;
  $("code-dialog").showModal();
});
$("copy-code").addEventListener("click", async () => {
  try {
    await navigator.clipboard.writeText(selectedProtocol().code);
    $("copy-code").textContent = "Copied";
    setTimeout(() => ($("copy-code").textContent = "Copy"), 1500);
  } catch {
    alert("Copy failed; use Download instead.");
  }
});

$("show-steps").addEventListener("click", () => {
  const protocol = selectedProtocol();
  const sim = protocol.simulation;
  const notices = [
    ...sim.errors.map((e) => h("li", { class: "error" }, e)),
    ...sim.warnings.map((w) => h("li", { class: "warning" }, w)),
  ];
  const shown = sim.steps.slice(0, 3000);
  $("steps-title").textContent = `Simulated run · ${protocol.title || protocol.id}`;
  $("steps-view").replaceChildren(
    ...[
      h("p", { class: "muted small" },
        `${sim.ok ? "Passed" : "Failed"} · ${sim.stats.commands} commands · ${sim.stats.tips} tips · ${sim.stats.aspirates} aspirates · simulated in ${sim.duration_s} s`),
      notices.length ? h("ul", { class: "notice-list" }, notices) : null,
      h("ol", { class: "step-list" }, shown.map((step) => h("li", {}, step))),
      sim.steps.length > shown.length ? h("p", { class: "muted small" }, `…and ${sim.steps.length - shown.length} more steps`) : null,
    ].filter(Boolean),
  );
  $("steps-dialog").showModal();
});

for (const button of document.querySelectorAll("dialog [data-close]")) {
  button.addEventListener("click", () => button.closest("dialog").close());
}

// --- Tabs, lab status, polling ---------------------------------------------------------------

function showTab(name) {
  for (const tab of ["design", "runs"]) {
    $(`tab-${tab}`).setAttribute("aria-selected", String(tab === name));
    $(`view-${tab}`).hidden = tab !== name;
  }
  if (name === "runs") runs.refresh();
}
$("tab-design").addEventListener("click", () => showTab("design"));
$("tab-runs").addEventListener("click", () => showTab("runs"));

// --- AI model connection -----------------------------------------------------------------------

async function refreshAssistant(refresh = false) {
  let status;
  try {
    const health = await api(`/api/health${refresh ? "?refresh=true" : ""}`);
    status = health.assistant;
    explainEnabled = Boolean(health.explain);
    if (health.deck) {
      setDeckRules(health.deck);
      if (session) renderDesign();
    }
  } catch (err) {
    status = { ok: false, state: "error", detail: err.message };
  }
  if (status.label) {
    for (const el of document.querySelectorAll(".assistant-name")) {
      el.dataset.label = status.label;
      el.textContent = status.label;
    }
    if (!session?.transcript.length && !chat.isBusy()) chat.render(session);
  }
  const chip = $("claude-chip");
  chip.hidden = false;
  chip.title = `${status.model ? `${status.model}: ` : ""}${status.detail}`;
  chip.replaceChildren(
    h("span", { class: `dot ${status.ok ? "ok" : "bad"}`, "aria-hidden": "true" }),
    h("strong", {}, status.label || "AI model"),
    status.ok ? "ready" : "not connected",
    status.free ? h("span", { class: "mode" }, "free") : null,
  );
  $("claude-banner").hidden = status.ok;
  $("claude-banner-text").textContent = status.detail;
}

$("claude-recheck").addEventListener("click", async () => {
  const button = $("claude-recheck");
  button.disabled = true;
  button.textContent = "Checking…";
  await refreshAssistant(true);
  button.disabled = false;
  button.textContent = "Check again";
});

// One chip per instrument in the master node's registry.
async function refreshDevices() {
  const list = $("devices");
  try {
    const devices = await api("/api/devices");
    // An instrument that isn't ready during a run is just busy with it, not broken.
    let busy = runs.hasActiveRun();
    if (!busy && Object.values(devices).some((d) => !d.ok)) {
      try {
        busy = (await api("/api/runs?limit=5")).some((r) => r.status === "running");
      } catch {
        // Keep showing "not ready".
      }
    }
    list.replaceChildren(
      ...Object.entries(devices).map(([id, d]) => {
        const state = d.ok ? "ok" : busy ? "busy" : "bad";
        return h("li", { class: "device", title: `${d.detail}${d.kind ? ` (${d.kind.replace("_", " ")})` : ""}` },
          h("span", { class: `dot ${state}`, "aria-hidden": "true" }),
          h("strong", {}, shortName(d.name ?? id)),
          d.ok ? "ready" : busy ? "in use" : "not ready",
          !["job_api", "opentrons_flex", "tecan_infinite", "http"].includes(d.mode) ? h("span", { class: "mode" }, d.mode) : null,
          d.kind === "transport" && state === "bad"
            ? h("button", { type: "button", class: "chip-btn", title: "Mark it parked at home and empty-handed",
                onclick: () => resetTransport(d.name ?? id) }, "Reset")
            : null,
        );
      }),
    );
  } catch (err) {
    list.replaceChildren(h("li", { class: "device", title: err.message }, h("span", { class: "dot bad", "aria-hidden": "true" }), "Master node offline"));
  }
}

// Temporary: after an aborted or failed move Thor can't tell where it is, so a person checks and says so.
async function resetTransport(name) {
  if (!confirm(`Is ${name} parked at home with nothing in its gripper?\n\nOnly then mark it home and empty-handed, so runs can start again.`)) return;
  try {
    await api("/api/transport/reset", { method: "POST" });
  } catch (err) {
    alert(err.message);
  }
  await refreshDevices();
}

// "Opentrons Flex" -> "Flex", "Tecan Infinite M200 Pro" -> "Tecan": keeps the status bar short.
function shortName(name) {
  const known = { "Jetson Thor": "Thor", "Opentrons Flex": "Flex" };
  return known[name] ?? (name.startsWith("Tecan") ? "Tecan" : name);
}

function every(ms, fn) {
  const tick = async () => {
    if (!document.hidden) await fn();
    setTimeout(tick, ms);
  };
  tick();
}

async function init() {
  $("send-form").elements.scientist.value = store.get(SCIENTIST_KEY) ?? "";
  try {
    applySession(await loadSession());
    chat.render(session);
  } catch (err) {
    $("messages").replaceChildren(h("p", { class: "msg-error" }, `Could not load the design: ${err.message}`));
  }
  const linked = /^#run=(.+)$/.exec(location.hash);
  if (linked) {
    showTab("runs");
    runs.select(decodeURIComponent(linked[1]));
  }
  every(8000, refreshDevices);
  every(15000, refreshAssistant);
  every(2500, async () => {
    if (!$("view-runs").hidden) await runs.refresh();
  });
}

init();
