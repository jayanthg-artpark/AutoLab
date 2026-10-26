// Plate-reader settings of one read step: mode, wavelengths and other settings, and a well
// picker. Edits are saved to the server after a short pause; the assistant sees them next turn.

import { api } from "./api.js";
import { $, h, rowLabel, range, debounce } from "./dom.js";

export function createReader({ getSession, onSession }) {
  let draft = null; // reader settings as shown
  let stepId = null; // the read step being edited
  let plate = { rows: 8, cols: 12 };
  let dirty = false;
  let paint = null; // { value } while dragging across wells

  const fields = {
    wavelengths: $("r-wavelengths"),
    excitation: $("r-excitation"),
    emission: $("r-emission"),
    gain: $("r-gain"),
    integration: $("r-integration"),
    flashes: $("r-flashes"),
  };

  // `step` is a read step of the workflow, or null to hide the panel.
  function render(session, step, position = "") {
    if (!session) return;
    $("reader-panel").hidden = !step;
    if (!step) {
      stepId = draft = null;
      return;
    }
    // The sample plate's shape, from the first protocol that loads it.
    plate = { rows: 8, cols: 12 };
    for (const p of session.design.protocols) {
      const slot = p.simulation?.deck.slots[p.plate_slot];
      const top = slot?.labware?.[slot.labware.length - 1];
      if (top?.rows && top?.cols) {
        plate = { rows: top.rows, cols: top.cols };
        break;
      }
    }
    if (step.id !== stepId) {
      if (dirty && stepId) save.flush(); // pending edits belong to the step being left
      stepId = step.id;
      dirty = false;
    }
    if (!dirty) draft = structuredClone(step.settings);
    $("reader-caption").textContent = `${position}${step.label ? ` · ${step.label}` : ""}.`;
    draw();
  }

  function draw() {
    if (!draft) return;
    for (const button of $("reader-mode").querySelectorAll("button")) {
      button.setAttribute("aria-checked", String(button.dataset.mode === draft.mode));
    }
    for (const label of $("reader-fields").querySelectorAll("label")) {
      label.hidden = !label.dataset.modes.split(" ").includes(draft.mode);
    }
    setValue(fields.wavelengths, draft.wavelengths_nm.join(", "));
    setValue(fields.excitation, draft.excitation_nm ?? "");
    setValue(fields.emission, draft.emission_nm ?? "");
    setValue(fields.gain, draft.gain ?? "");
    setValue(fields.integration, draft.integration_ms);
    setValue(fields.flashes, draft.flashes);
    drawWells();
  }

  function setValue(input, value) {
    if (document.activeElement !== input) input.value = value;
  }

  function drawWells() {
    const selected = new Set(draft.wells);
    const picker = $("well-picker");
    picker.style.gridTemplateColumns = `18px repeat(${plate.cols}, minmax(0, 1fr))`;
    const cells = [h("button", { type: "button", class: "axis", title: "Toggle all", onclick: () => toggleWells(allWells()) }, "")];
    for (const c of range(plate.cols)) {
      cells.push(h("button", { type: "button", class: "axis", title: `Toggle column ${c + 1}`, onclick: () => toggleWells(range(plate.rows).map((r) => `${rowLabel(r)}${c + 1}`)) }, c + 1));
    }
    for (const r of range(plate.rows)) {
      const row = rowLabel(r);
      cells.push(h("button", { type: "button", class: "axis", title: `Toggle row ${row}`, onclick: () => toggleWells(range(plate.cols).map((c) => `${row}${c + 1}`)) }, row));
      for (const c of range(plate.cols)) {
        const well = `${row}${c + 1}`;
        cells.push(h("button", { type: "button", class: "well", dataset: { well }, "aria-label": well, "aria-pressed": String(selected.has(well)) }));
      }
    }
    picker.replaceChildren(...cells);
    const inPlate = draft.wells.filter((w) => allWells().includes(w)).length;
    $("well-count").textContent = `· ${inPlate} of ${plate.rows * plate.cols}`;
  }

  function allWells() {
    return range(plate.rows).flatMap((r) => range(plate.cols).map((c) => `${rowLabel(r)}${c + 1}`));
  }

  function toggleWells(wells) {
    const selected = new Set(draft.wells);
    const turnOn = wells.some((w) => !selected.has(w));
    wells.forEach((w) => (turnOn ? selected.add(w) : selected.delete(w)));
    update({ wells: sortWells([...selected]) });
  }

  function sortWells(wells) {
    return wells.sort((a, b) => a[0].localeCompare(b[0]) || Number(a.slice(1)) - Number(b.slice(1)));
  }

  // --- Editing ------------------------------------------------------------------------------

  function update(patch) {
    draft = { ...draft, ...patch };
    dirty = true;
    draw();
    $("reader-saved").textContent = "Saving…";
    save();
  }

  const save = debounce(async () => {
    const session = getSession();
    if (!session) return;
    const error = $("reader-error");
    if (!draft.wells.length) {
      error.textContent = "Select at least one well to read.";
      error.hidden = false;
      $("reader-saved").textContent = "Not saved";
      return;
    }
    try {
      const updated = await api(`/api/sessions/${session.id}/design`, { method: "PATCH", json: { reader: draft, step_id: stepId } });
      error.hidden = true;
      dirty = false;
      $("reader-saved").textContent = "Saved";
      onSession(updated);
    } catch (err) {
      error.textContent = err.message.replace(/^reader(\.[\w.]+)?: /gm, "");
      error.hidden = false;
      $("reader-saved").textContent = "Not saved";
    }
  }, 500);

  function numberOrNull(input) {
    const text = input.value.trim();
    return text === "" ? null : Number(text);
  }

  $("reader-mode").addEventListener("click", (e) => {
    const mode = e.target.closest("button")?.dataset.mode;
    if (!mode || mode === draft?.mode) return;
    const patch = { mode };
    if (mode === "fluorescence" && draft.excitation_nm == null) Object.assign(patch, { excitation_nm: 485, emission_nm: 535 });
    if (mode === "absorbance" && !draft.wavelengths_nm.length) patch.wavelengths_nm = [600];
    update(patch);
  });

  fields.wavelengths.addEventListener("change", () => {
    const values = fields.wavelengths.value.split(/[\s,;]+/).filter(Boolean).map(Number);
    if (values.some((v) => !Number.isInteger(v))) {
      $("reader-error").textContent = "Wavelengths must be whole numbers in nm, separated by commas.";
      $("reader-error").hidden = false;
      return;
    }
    update({ wavelengths_nm: values });
  });
  const numeric = {
    excitation: "excitation_nm", emission: "emission_nm", gain: "gain",
    integration: "integration_ms", flashes: "flashes",
  };
  for (const [key, field] of Object.entries(numeric)) {
    fields[key].addEventListener("change", () => {
      const value = numberOrNull(fields[key]);
      const optional = key === "gain" || key === "excitation" || key === "emission";
      update({ [field]: value === null && !optional ? 0 : value });
    });
  }
  $("wells-all").addEventListener("click", () => update({ wells: allWells() }));
  $("wells-none").addEventListener("click", () => update({ wells: [] }));

  // Click or drag across wells; the first well decides whether the drag selects or clears.
  const picker = $("well-picker");
  const wellAt = (e) => document.elementFromPoint(e.clientX, e.clientY)?.closest?.(".well");
  const setWell = (el) => {
    if (!el || !paint) return;
    const want = paint.value;
    if ((el.getAttribute("aria-pressed") === "true") === want) return;
    el.setAttribute("aria-pressed", String(want));
    paint.changed = true;
  };
  picker.addEventListener("pointerdown", (e) => {
    const el = e.target.closest(".well");
    if (!el || !draft) return;
    e.preventDefault();
    paint = { value: el.getAttribute("aria-pressed") !== "true", changed: false };
    picker.setPointerCapture(e.pointerId);
    setWell(el);
  });
  picker.addEventListener("pointermove", (e) => setWell(wellAt(e)));
  const endPaint = () => {
    if (!paint) return;
    const changed = paint.changed;
    paint = null;
    if (changed) {
      const wells = [...picker.querySelectorAll('.well[aria-pressed="true"]')].map((el) => el.dataset.well);
      // Keep any selected wells outside this plate's grid out of the request.
      update({ wells: sortWells(wells) });
    }
  };
  picker.addEventListener("pointerup", endPaint);
  picker.addEventListener("pointercancel", endPaint);
  picker.addEventListener("keydown", (e) => {
    const el = e.target.closest(".well");
    if (el && (e.key === "Enter" || e.key === " ")) {
      e.preventDefault();
      toggleWells([el.dataset.well]);
    }
  });

  return { render };
}
