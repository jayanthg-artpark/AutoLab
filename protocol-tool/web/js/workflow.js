// Workflow panel: the requirements the assistant extracted, and the ordered steps done to the
// sample plate (the protocol IR). Clicking a liquid-handling step shows its protocol on the
// deck; clicking a read step opens it in the plate-reader panel.

import { $, h, icon, formatDuration } from "./dom.js";

export const STEP_TYPES = {
  liquid_handling: "Liquid handling",
  read: "Read",
  wash: "Wash",
  qpcr: "qPCR",
  hold: "Hold",
};

const ANALYSIS_NAMES = {
  standard_curve: "Standard curve", dose_response: "Dose-response", dilution_series: "Dilution series", group_comparison: "Group comparison",
};
const FIT_NAMES = { linear: "linear", linear_zero: "linear through zero", quadratic: "quadratic", log_linear: "log-linear", "4pl": "4PL", "5pl": "5PL" };

const REQUIREMENT_FIELDS = [
  ["assay", "Assay"], ["goal", "Goal"], ["samples", "Samples"], ["controls", "Controls"],
  ["replicates", "Replicates"], ["readout", "Readout"],
];

export function describeRead(settings) {
  const wells = `${settings.wells.length} well${settings.wells.length === 1 ? "" : "s"}`;
  if (settings.mode === "absorbance") return `Absorbance ${settings.wavelengths_nm.join(", ")} nm · ${wells}`;
  if (settings.mode === "fluorescence") return `Fluorescence Ex ${settings.excitation_nm} / Em ${settings.emission_nm} nm · ${wells}`;
  return `Luminescence ${settings.integration_ms} ms · ${wells}`;
}

export function describeStep(step, design) {
  switch (step.type) {
    case "liquid_handling": {
      const p = design.protocols.find((x) => x.id === step.protocol_id);
      return p ? `${p.title || p.id} · v${p.version}` : `Protocol "${step.protocol_id}" not written yet`;
    }
    case "read":
      return describeRead(step.settings);
    case "wash":
      return step.program ? `Program "${step.program}"` : `${step.cycles} × ${step.volume_ul} µL${step.soak_s ? `, soak ${step.soak_s} s` : ""}`;
    case "qpcr":
      return `Program "${step.program}"`;
    default:
      return formatDuration(step.seconds * 1000);
  }
}

export function createWorkflow({ onSelect }) {
  function render(session, selection) {
    const design = session.design;
    renderRequirements(design.requirements);
    $("workflow-empty").hidden = design.workflow.length > 0;
    $("workflow").replaceChildren(...design.workflow.map((step, i) => renderStep(step, i, design, selection)));
    renderAnalysisPlan(design.analyses ?? []);
  }

  // What will be calculated and plotted when the readings arrive.
  function renderAnalysisPlan(analyses) {
    $("analysis-plan").hidden = !analyses.length;
    $("analysis-plan-list").replaceChildren(...analyses.map((a) => {
      const fit = a.kind === "group_comparison" || a.fit === "none" ? "" : `, ${FIT_NAMES[a.fit] ?? a.fit} fit`;
      const counts = [a.standards.length ? `${a.standards.length} standards` : "",
        a.samples.length ? `${a.samples.length} ${a.kind === "group_comparison" ? "groups" : "samples"}` : ""].filter(Boolean).join(", ");
      return h("li", {}, h("strong", {}, a.title), ` · ${ANALYSIS_NAMES[a.kind]}${fit} · ${a.measurement}${counts ? ` · ${counts}` : ""}`);
    }));
  }

  function renderRequirements(req) {
    const panel = $("requirements");
    panel.hidden = !req;
    if (!req) return;
    const questions = req.open_questions ?? [];
    $("requirements-sub").textContent = [req.assay, questions.length ? `${questions.length} open question${questions.length > 1 ? "s" : ""}` : ""]
      .filter(Boolean).join(" · ");
    if (questions.length) panel.open = true;
    const rows = REQUIREMENT_FIELDS.filter(([key]) => req[key]).flatMap(([key, label]) => [h("dt", {}, label), h("dd", {}, req[key])]);
    const list = (label, items, cls) => (items?.length ? [h("dt", {}, label), h("dd", {}, h("ul", { class: cls }, items.map((t) => h("li", {}, t))))] : []);
    $("requirements-body").replaceChildren(
      ...rows,
      ...list("Open questions", questions, "questions"),
      ...list("Assumptions", req.assumptions),
      ...list("Based on", req.sources),
    );
  }

  function renderStep(step, index, design, selection) {
    const selectable = step.type === "liquid_handling" || step.type === "read";
    const selected = (step.type === "read" && step.id === selection.readStepId)
      || (step.type === "liquid_handling" && step.protocol_id === selection.protocolId);
    let state = null;
    if (step.type === "liquid_handling") {
      const p = design.protocols.find((x) => x.id === step.protocol_id);
      state = !p?.code ? "warn" : !p.simulation_current ? "warn" : p.simulation.ok ? "check" : "cross";
    }
    const body = [
      h("span", { class: "wf-num" }, index + 1),
      h("span", { class: "wf-main" },
        h("span", { class: "wf-title" },
          h("span", { class: `wf-type ${step.type}` }, STEP_TYPES[step.type] ?? step.type),
          step.label ? h("span", {}, step.label) : null,
          step.instrument ? h("span", { class: "device-tag" }, step.instrument) : null),
        h("span", { class: "wf-sub" }, describeStep(step, design))),
      state ? h("span", { class: `wf-state ${state}`, title: { check: "Simulation passed", cross: "Simulation failed", warn: "Not simulated" }[state] }, icon(state)) : null,
    ];
    return h("li", {},
      selectable
        ? h("button", { type: "button", class: "wf-step", "aria-current": selected ? "true" : null, onclick: () => onSelect(step) }, body)
        : h("div", { class: "wf-step" }, body));
  }

  return { render };
}
