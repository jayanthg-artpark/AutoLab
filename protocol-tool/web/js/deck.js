// Opentrons Flex deck for one protocol, as the simulator reports it. Rows A (back) to D
// (front), columns 1-3, plus staging column 4 when used. The lab's fixed fixtures (trash
// bin, waste chute) are always drawn; the slot holding the sample plate that Thor brings
// is outlined once the protocol has chosen it.

import { $, h, s, icon, rowLabel, showTooltip, hideTooltip } from "./dom.js";

const ROWS = ["A", "B", "C", "D"];
// The lab's deck rules, as the server reports them (see setDeckRules).
let rules = { thor_slots: [], trash_slot: "A3", waste_chute_slot: "D3", blocked_slots: ["A1", "A2"] };

export function setDeckRules(next) {
  rules = next;
}

// `protocol` is one of the design's protocols (with its simulation), or null.
export function renderDeck(protocol, count = 1) {
  const sim = protocol?.simulation;
  const deck = sim?.deck ?? { slots: {}, pipettes: [], liquids: [], off_deck: [] };
  const plateSlot = protocol?.plate_slot || "";
  const liquids = Object.fromEntries(deck.liquids.map((l) => [l.id, l]));
  const usesStaging = Object.keys(deck.slots).some((name) => name.endsWith("4"));
  const cols = usesStaging ? 4 : 3;
  const thermocycler = Object.entries(deck.slots).find(([, c]) => c.module?.model.startsWith("thermocycler"));

  const grid = $("deck");
  grid.style.gridTemplateColumns = `repeat(3, minmax(0, 1fr))${usesStaging ? " minmax(0, 0.8fr)" : ""}`;
  const cells = [];
  ROWS.forEach((row, r) => {
    for (let col = 1; col <= cols; col++) {
      const name = `${row}${col}`;
      // A thermocycler sits in B1 and covers A1 as well.
      if (thermocycler && name === "A1") continue;
      const span = thermocycler && name === "B1" ? 2 : 1;
      const cell = renderSlot(name, deck.slots[name] ?? fixture(name), { thor: name === plateSlot, liquids, staging: col === 4 });
      cell.style.gridRow = span === 2 ? "1 / span 2" : String(r + 1);
      cell.style.gridColumn = String(col);
      if (span === 2) cell.classList.add("span-2");
      cells.push(cell);
    }
  });
  grid.replaceChildren(...cells);

  // Caption, pipettes and liquids.
  $("deck-caption").textContent = !protocol?.code
    ? "Fills in when the assistant writes a protocol."
    : `${protocol.title || protocol.id} · v${protocol.version}${count > 1 ? ` · one of ${count} protocols` : ""}`;
  const meta = [];
  if (deck.pipettes.length) {
    const byMount = Object.fromEntries(deck.pipettes.map((p) => [p.mount, p.display_name]));
    meta.push(h("div", {}, h("strong", {}, "Pipettes "), `Left: ${byMount.left ?? "none"} · Right: ${byMount.right ?? "none"}`));
  }
  if (deck.liquids.length) {
    meta.push(
      h("div", { class: "legend" },
        h("strong", {}, "Liquids"),
        deck.liquids.map((l) => h("span", { title: l.description || l.name }, h("i", { class: "swatch", style: { background: l.color } }), l.name)),
      ),
    );
  }
  if (deck.off_deck.length) {
    meta.push(h("div", {}, h("strong", {}, "Off deck "), deck.off_deck.map((l) => l.display_name).join(", ")));
  }
  $("deck-meta").replaceChildren(...meta);
}

// The trash bin and waste chute are part of the deck even before any protocol loads them.
function fixture(name) {
  if (name === rules.trash_slot) return { trash: true, labware: [] };
  if (name === rules.waste_chute_slot) return { waste_chute: true, labware: [] };
  if ((rules.blocked_slots ?? []).includes(name)) return { blocked: true, labware: [] };
  return undefined;
}

function renderSlot(name, contents, { thor, liquids, staging }) {
  const labware = contents?.labware ?? [];
  const top = labware[labware.length - 1];
  const module = contents?.module;
  const classes = ["slot"];
  if (thor) classes.push("thor");
  if (staging) classes.push("staging");

  if (contents?.trash || contents?.waste_chute) {
    classes.push("trash");
    return h("div", { class: classes.join(" ") },
      h("span", { class: "slot-name" }, name),
      icon("trash"),
      h("span", { class: "slot-label" }, contents.waste_chute ? "Waste chute" : "Trash bin"),
    );
  }
  if (contents?.blocked) {
    classes.push("blocked");
    return h("div", { class: classes.join(" "), title: `${name} is kept empty: protocols may not use it` },
      h("span", { class: "slot-name" }, name),
      h("span", { class: "slot-label" }, "Kept empty"),
    );
  }
  if (!top && !module) {
    classes.push("empty");
    return h("div", { class: classes.join(" ") },
      h("span", { class: "slot-name" }, name),
      thor ? h("span", { class: "thor-badge" }, "Thor") : null,
    );
  }

  const tipLines = [
    h("strong", {}, `${name}: ${top?.display_name ?? module.display_name}`),
    top ? h("div", {}, top.load_name) : null,
    module && top ? h("div", {}, `on ${module.display_name}`) : null,
    labware.length > 1 ? h("div", {}, `on ${labware[0].display_name}`) : null,
    thor ? h("div", {}, "Sample plate: Thor places it here before this protocol and collects it afterwards") : null,
    ...liquidSummary(top, liquids),
  ];
  const el = h("div", {
    class: classes.join(" "),
    tabindex: "0",
    "aria-label": `Slot ${name}: ${top?.display_name ?? module.display_name}`,
    onmouseenter: (e) => showTooltip(e.currentTarget, ...tipLines),
    onfocus: (e) => showTooltip(e.currentTarget, ...tipLines),
    onmouseleave: hideTooltip,
    onblur: hideTooltip,
  },
    h("span", { class: "slot-name" }, name),
    thor ? h("span", { class: "thor-badge" }, "Thor") : null,
    module ? h("div", { class: "module-band" }, module.display_name) : null,
    top ? labwareSvg(top, liquids) : null,
    h("span", { class: "slot-label" }, top?.display_name ?? ""),
  );
  return el;
}

function liquidSummary(labware, liquids) {
  if (!labware) return [];
  const counts = {};
  for (const ids of Object.values(labware.liquids)) for (const id of ids) counts[id] = (counts[id] ?? 0) + 1;
  return Object.entries(counts).map(([id, n]) => h("div", {}, `${liquids[id]?.name ?? "Liquid"}: ${n} well${n === 1 ? "" : "s"}`));
}

// A small drawing of the labware: its wells, coloured by the liquid loaded into them.
function labwareSvg(labware, liquids) {
  const W = 128, H = 86, padX = 9, padY = 7;
  const svg = s("svg", { class: "labware", viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "xMidYMid meet", "aria-hidden": "true" });
  svg.append(s("rect", { x: 1, y: 1, width: W - 2, height: H - 2, rx: 5, fill: "var(--surface)", stroke: "var(--well-stroke)" }));
  const { rows, cols } = labware;
  if (!rows || !cols) return svg;

  const cw = (W - 2 * padX) / cols;
  const ch = (H - 2 * padY) / rows;
  const tipRack = labware.category === "tipRack";
  for (let r = 0; r < rows; r++) {
    for (let c = 0; c < cols; c++) {
      const liquidId = labware.liquids[`${rowLabel(r)}${c + 1}`]?.[0];
      const fill = liquidId ? liquids[liquidId]?.color ?? "var(--accent)" : tipRack ? "var(--tip)" : "none";
      const attrs = { fill, stroke: liquidId ? fill : "var(--well-stroke)", "stroke-width": 0.8 };
      if (rows === 1) {
        svg.append(s("rect", { x: padX + c * cw + cw * 0.1, y: padY, width: cw * 0.8, height: H - 2 * padY, rx: 2, ...attrs }));
      } else {
        const radius = Math.max(0.6, Math.min(cw, ch) * 0.36);
        svg.append(s("circle", { cx: padX + (c + 0.5) * cw, cy: padY + (r + 0.5) * ch, r: radius, ...attrs }));
      }
    }
  }
  return svg;
}
