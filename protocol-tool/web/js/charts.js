// Result analysis charts: standard curves, dose-response, dilution series (x-y with a fitted
// curve) and group comparisons (bars). Plain SVG. Colours are the validated categorical slots
// 1-3 (--chart-1..3 in styles.css); samples are diamonds, standards circles, so identity never
// rests on colour alone. Every chart has its table underneath and a tooltip on every mark.

import { h, s, formatValue, showTooltip, hideTooltip } from "./dom.js";

const W = 640, H = 340;
const M = { top: 16, right: 20, bottom: 52, left: 64 };
const SLOTS = ["var(--chart-1)", "var(--chart-2)", "var(--chart-3)"];
const SAMPLE_COLOR = "var(--chart-2)";
const MAX_SERIES_PER_CHART = 3; // slots 1-3 are the ones validated for scatter (all pairs)

const KIND_LABEL = {
  standard_curve: "Standard curve", dose_response: "Dose-response",
  dilution_series: "Dilution series", group_comparison: "Group comparison",
};
const FIT_LABEL = { linear: "Linear", linear_zero: "Linear through zero", quadratic: "Quadratic", log_linear: "Log-linear", "4pl": "4PL", "5pl": "5PL" };

export function renderAnalysis(a) {
  const head = h("div", { class: "an-head" },
    h("h4", {}, a.title),
    h("span", { class: "muted small" }, [KIND_LABEL[a.kind] ?? a.kind, a.measurement].filter(Boolean).join(" · ")));
  if (a.error) return h("section", { class: "an" }, head, h("p", { class: "form-error" }, a.error));

  const parts = [head];
  for (const series of a.series) if (series.fit) parts.push(fitLine(series, a.series.length > 1));
  if (a.warnings.length) parts.push(h("ul", { class: "an-notes" }, a.warnings.map((w) => h("li", {}, w))));
  if (a.kind === "group_comparison") {
    parts.push(barChart(a), groupTable(a));
  } else {
    // More series than the validated slots: one small chart each, so colours never repeat.
    const chunks = a.series.length > MAX_SERIES_PER_CHART ? a.series.map((x) => [x]) : [a.series];
    parts.push(h("div", { class: chunks.length > 1 ? "an-multiples" : "" }, chunks.map((chunk) => xyChart(a, chunk))));
    parts.push(pointTable(a));
    if (a.samples.length) parts.push(sampleTable(a));
  }
  return h("section", { class: "an" }, parts);
}

function fitLine(series, named) {
  const f = series.fit;
  const bits = [`${FIT_LABEL[f.model] ?? f.model}: ${f.equation}`, f.r2 != null ? `R² ${f.r2.toFixed(4)}` : null,
    f.ec50 != null ? `EC50 ${formatValue(f.ec50)}` : null,
    f.efficiency_percent != null ? `efficiency ${f.efficiency_percent.toFixed(1)}%` : null].filter(Boolean);
  return h("p", { class: "an-fit" }, named ? h("strong", {}, `${series.name}: `) : null, bits.join(" · "));
}

// --- Scales and ticks --------------------------------------------------------------------------

function linearScale([d0, d1], [r0, r1]) {
  const f = (v) => r0 + ((v - d0) / (d1 - d0 || 1)) * (r1 - r0);
  f.ticks = () => niceTicks(d0, d1);
  return f;
}

function logScale([d0, d1], [r0, r1]) {
  const [l0, l1] = [Math.log10(d0), Math.log10(d1)];
  const f = (v) => r0 + ((Math.log10(v) - l0) / (l1 - l0 || 1)) * (r1 - r0);
  f.ticks = () => {
    const out = [];
    for (let e = Math.floor(l0); e <= Math.ceil(l1); e++) if (10 ** e >= d0 * 0.999 && 10 ** e <= d1 * 1.001) out.push(10 ** e);
    return out;
  };
  return f;
}

function niceTicks(lo, hi, count = 5) {
  const span = hi - lo || Math.abs(hi) || 1;
  const raw = span / count;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 5, 10].map((m) => m * mag).find((st) => span / st <= count) ?? 10 * mag;
  const out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-9; v += step) out.push(Math.abs(v) < step * 1e-9 ? 0 : v);
  return out;
}

function niceDomain(values, includeZero) {
  let lo = Math.min(...values), hi = Math.max(...values);
  if (includeZero) { lo = Math.min(0, lo); hi = Math.max(0, hi); }
  const pad = (hi - lo || Math.abs(hi) || 1) * 0.06;
  const ticks = niceTicks(lo - (lo < 0 || !includeZero ? pad : 0), hi + pad);
  return [Math.min(ticks[0], lo), Math.max(ticks[ticks.length - 1], hi)];
}

function logDomain(values) {
  const pos = values.filter((v) => v > 0);
  return [10 ** Math.floor(Math.log10(Math.min(...pos))), 10 ** Math.ceil(Math.log10(Math.max(...pos)))];
}

function tickLabel(v) {
  const a = Math.abs(v);
  if (a >= 1e9 || (a > 0 && a < 1e-3)) return v.toExponential(0).replace("e+", "e");
  if (a >= 1e6) return `${+(v / 1e6).toPrecision(3)}M`;
  if (a >= 1000) return `${+(v / 1000).toPrecision(3)}K`;
  return String(+v.toPrecision(4));
}

const fold = (v) => `${v >= 10 ? v.toFixed(1) : v.toFixed(2)}×`;

// --- X-Y chart (curves) ---------------------------------------------------------------------------

function xyChart(a, seriesList) {
  // Samples sit on the standard curve at the concentration in the well (before the dilution is multiplied back).
  const samples = a.kind === "standard_curve" && seriesList.includes(a.series[0])
    ? a.samples.filter((r) => r.value != null && r.signal != null).map((r) => ({ ...r, xWell: r.value / (r.dilution || 1) }))
    : [];
  const points = seriesList.flatMap((sr) => sr.points);
  const xs = [...points.map((p) => p.x), ...seriesList.flatMap((sr) => sr.curve.map((c) => c.x)), ...samples.map((r) => r.xWell)];
  const ys = [...points.flatMap((p) => [p.y - (p.sd ?? 0), p.y + (p.sd ?? 0)]), ...seriesList.flatMap((sr) => sr.curve.map((c) => c.y)),
    ...samples.map((r) => r.signal)].filter(Number.isFinite);
  if (!points.length || !ys.length) return h("p", { class: "empty" }, "No readings to plot.");

  const x = a.x_log ? logScale(logDomain(xs), [M.left, W - M.right]) : linearScale(niceDomain(xs, true), [M.left, W - M.right]);
  const yDom = niceDomain(ys, a.kind !== "dose_response");
  const y = linearScale(yDom, [H - M.bottom, M.top]);

  const svg = s("svg", { class: "chart", viewBox: `0 0 ${W} ${H}`, role: "img",
    "aria-label": `${a.title}: ${a.y_label} against ${a.x_label}. The table below lists every value.` });
  axes(svg, x, y, a.x_label, a.y_label);

  seriesList.forEach((sr) => {
    const color = SLOTS[a.series.indexOf(sr) % SLOTS.length] ?? SLOTS[0];
    if (sr.curve.length > 1) {
      const d = sr.curve.filter((c) => c.y != null).map((c, k) => `${k ? "L" : "M"}${x(c.x).toFixed(1)},${y(c.y).toFixed(1)}`).join("");
      svg.append(s("path", { d, fill: "none", stroke: color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }));
    }
    for (const p of sr.points) {
      const cx = x(p.x), cy = y(p.y);
      if (p.sd) {
        const [t, b] = [y(p.y + p.sd), y(p.y - p.sd)];
        svg.append(s("path", { d: `M${cx},${t}V${b}M${cx - 4},${t}H${cx + 4}M${cx - 4},${b}H${cx + 4}`, stroke: color, "stroke-width": 1.5, fill: "none" }));
      }
      const tip = [h("strong", {}, p.name), h("br"), `${a.x_label}: ${formatValue(p.x)}`, h("br"),
        `Signal: ${formatValue(p.y)}${p.sd != null ? ` ± ${formatValue(p.sd)}` : ""} (n=${p.n})`,
        p.cv != null ? [h("br"), `CV ${p.cv.toFixed(1)}%`] : null, h("br"), `Wells ${p.wells.join(", ")}`];
      svg.append(marker("circle", cx, cy, color, tip));
    }
  });
  for (const r of samples) {
    const tip = [h("strong", {}, r.name), h("br"), `Signal: ${formatValue(r.signal)}`, h("br"),
      `Result: ${formatValue(r.value)}${r.value_sd != null ? ` ± ${formatValue(r.value_sd)}` : ""}${r.dilution !== 1 ? ` (×${r.dilution} dilution)` : ""}`,
      r.flag ? [h("br"), r.flag] : null];
    svg.append(marker("diamond", x(r.xWell), y(r.signal), SAMPLE_COLOR, tip, Boolean(r.flag)));
  }

  const legend = [];
  if (seriesList.length + (samples.length ? 1 : 0) >= 2) {
    seriesList.forEach((sr) => legend.push(key("circle", SLOTS[a.series.indexOf(sr) % SLOTS.length], sr.name)));
    if (samples.length) legend.push(key("diamond", SAMPLE_COLOR, "Samples (read off the curve)"));
  }
  return h("figure", { class: "an-chart" },
    seriesList.length === 1 && a.series.length > 1 ? h("figcaption", { class: "small" }, seriesList[0].name) : null,
    legend.length ? h("div", { class: "legend an-legend" }, legend) : null,
    svg);
}

function axes(svg, x, y, xLabel, yLabel) {
  for (const t of y.ticks()) {
    svg.append(s("line", { x1: M.left, x2: W - M.right, y1: y(t), y2: y(t), class: "grid" }));
    svg.append(s("text", { x: M.left - 8, y: y(t), class: "tick", "text-anchor": "end", "dominant-baseline": "middle" }, tickLabel(t)));
  }
  for (const t of x.ticks()) {
    svg.append(s("line", { x1: x(t), x2: x(t), y1: H - M.bottom, y2: H - M.bottom + 4, class: "axis" }));
    svg.append(s("text", { x: x(t), y: H - M.bottom + 18, class: "tick", "text-anchor": "middle" }, tickLabel(t)));
  }
  svg.append(s("line", { x1: M.left, x2: W - M.right, y1: H - M.bottom, y2: H - M.bottom, class: "axis" }));
  svg.append(s("text", { x: (M.left + W - M.right) / 2, y: H - 8, class: "axis-label", "text-anchor": "middle" }, xLabel));
  svg.append(s("text", { x: 14, y: (M.top + H - M.bottom) / 2, class: "axis-label", "text-anchor": "middle",
    transform: `rotate(-90 14 ${(M.top + H - M.bottom) / 2})` }, yLabel));
}

// A dot or diamond with a 2px surface ring, and a 24px invisible hit area for hover and focus.
function marker(shape, cx, cy, color, tip, hollow = false) {
  const g = s("g", { class: "mark", tabindex: "0" });
  const show = (e) => showTooltip(e.currentTarget, ...tip);
  g.addEventListener("mouseenter", show);
  g.addEventListener("focus", show);
  g.addEventListener("mouseleave", hideTooltip);
  g.addEventListener("blur", hideTooltip);
  g.append(s("circle", { cx, cy, r: 12, fill: "transparent" }));
  const fill = hollow ? "var(--surface)" : color;
  if (shape === "diamond") {
    g.append(s("path", { d: `M${cx},${cy - 6}L${cx + 6},${cy}L${cx},${cy + 6}L${cx - 6},${cy}Z`, fill, stroke: "var(--surface)", "stroke-width": 2, "paint-order": "stroke" }));
    if (hollow) g.append(s("path", { d: `M${cx},${cy - 4.5}L${cx + 4.5},${cy}L${cx},${cy + 4.5}L${cx - 4.5},${cy}Z`, fill: "none", stroke: color, "stroke-width": 2 }));
  } else {
    g.append(s("circle", { cx, cy, r: 4.5, fill, stroke: "var(--surface)", "stroke-width": 2, "paint-order": "stroke" }));
  }
  return g;
}

function key(shape, color, label) {
  const icon = s("svg", { viewBox: "0 0 14 14", width: 14, height: 14, "aria-hidden": "true" },
    shape === "diamond" ? s("path", { d: "M7,1L13,7L7,13L1,7Z", fill: color }) : s("circle", { cx: 7, cy: 7, r: 5, fill: color }));
  return h("span", {}, icon, label);
}

// --- Bar chart (group comparison) ------------------------------------------------------------------

function barChart(a) {
  const groups = a.groups.filter((g) => g.mean != null);
  if (!groups.length) return h("p", { class: "empty" }, "No readings to plot.");
  const values = groups.flatMap((g) => [g.mean + (g.sd ?? 0), ...g.values]);
  const y = linearScale(niceDomain([...values, ...groups.map((g) => g.mean - (g.sd ?? 0))], true), [H - M.bottom, M.top]);
  const band = (W - M.left - M.right) / groups.length;
  const barW = Math.min(24, band * 0.5);
  const svg = s("svg", { class: "chart", viewBox: `0 0 ${W} ${H}`, role: "img",
    "aria-label": `${a.title}: mean ${a.y_label} per group. The table below lists every value.` });
  for (const t of y.ticks()) {
    svg.append(s("line", { x1: M.left, x2: W - M.right, y1: y(t), y2: y(t), class: "grid" }));
    svg.append(s("text", { x: M.left - 8, y: y(t), class: "tick", "text-anchor": "end", "dominant-baseline": "middle" }, tickLabel(t)));
  }
  const base = y(Math.max(0, y.ticks()[0] ?? 0));
  groups.forEach((g, i) => {
    const cx = M.left + band * (i + 0.5);
    const top = y(g.mean);
    const [y0, y1] = [Math.min(top, base), Math.max(top, base)];
    const r = Math.min(4, (y1 - y0) / 2);
    const up = top <= base;
    // Rounded data end, square at the baseline.
    const d = up
      ? `M${cx - barW / 2},${y1}V${y0 + r}Q${cx - barW / 2},${y0} ${cx - barW / 2 + r},${y0}H${cx + barW / 2 - r}Q${cx + barW / 2},${y0} ${cx + barW / 2},${y0 + r}V${y1}Z`
      : `M${cx - barW / 2},${y0}V${y1 - r}Q${cx - barW / 2},${y1} ${cx - barW / 2 + r},${y1}H${cx + barW / 2 - r}Q${cx + barW / 2},${y1} ${cx + barW / 2},${y1 - r}V${y0}Z`;
    const tip = [h("strong", {}, g.name), h("br"), `Mean ${formatValue(g.mean)}${g.sd != null ? ` ± ${formatValue(g.sd)}` : ""} (n=${g.n})`,
      g.fold_change != null ? [h("br"), `${fold(g.fold_change)} control${g.p_value != null ? `, p = ${formatValue(g.p_value)}` : ""}`] : null];
    const mark = s("g", { class: "mark", tabindex: "0" });
    mark.addEventListener("mouseenter", (e) => showTooltip(e.currentTarget, ...tip));
    mark.addEventListener("focus", (e) => showTooltip(e.currentTarget, ...tip));
    mark.addEventListener("mouseleave", hideTooltip);
    mark.addEventListener("blur", hideTooltip);
    mark.append(s("rect", { x: cx - band / 2 + 2, y: M.top, width: band - 4, height: H - M.bottom - M.top, fill: "transparent" }));
    mark.append(s("path", { d, fill: SLOTS[0], "fill-opacity": 0.85 }));
    if (g.sd) {
      const [t, b] = [y(g.mean + g.sd), y(g.mean - g.sd)];
      mark.append(s("path", { d: `M${cx},${t}V${b}M${cx - 5},${t}H${cx + 5}M${cx - 5},${b}H${cx + 5}`, stroke: "var(--ink-2)", "stroke-width": 1.5, fill: "none" }));
    }
    g.values.forEach((v, k) => {
      const jitter = ((k % 5) - 2) * 3;
      mark.append(s("circle", { cx: cx + barW / 2 + 8 + jitter, cy: y(v), r: 3, fill: "var(--ink-2)", "fill-opacity": 0.55 }));
    });
    svg.append(mark);
    svg.append(s("text", { x: cx, y: H - M.bottom + 18, class: "tick", "text-anchor": "middle" }, g.name.length > 14 ? `${g.name.slice(0, 13)}…` : g.name));
    if (g.fold_change != null) {
      svg.append(s("text", { x: cx, y: Math.min(y0, y(g.mean + (g.sd ?? 0))) - 8, class: "tick", "text-anchor": "middle" }, fold(g.fold_change)));
    }
  });
  svg.append(s("line", { x1: M.left, x2: W - M.right, y1: base, y2: base, class: "axis" }));
  svg.append(s("text", { x: 14, y: (M.top + H - M.bottom) / 2, class: "axis-label", "text-anchor": "middle",
    transform: `rotate(-90 14 ${(M.top + H - M.bottom) / 2})` }, a.y_label));
  return h("figure", { class: "an-chart" }, svg);
}

// --- Tables (every value the chart shows) ----------------------------------------------------------

// Numbers (right-aligned) versus text such as names and flags (left-aligned).
const NUMERIC = /^[-−\d.,\s±×%eEKM—]+$/;

function table(caption, head, rows) {
  return h("div", { class: "plate-wrap" },
    h("table", { class: "an-table" },
      h("caption", {}, caption),
      h("thead", {}, h("tr", {}, head.map((c) => h("th", { scope: "col" }, c)))),
      h("tbody", {}, rows.map((r) => h("tr", {}, r.map((c, i) => (i === 0
        ? h("th", { scope: "row" }, c)
        : h("td", { class: NUMERIC.test(String(c)) ? null : "txt" }, c))))))));
}

const sdText = (v, sd) => (v == null ? "—" : sd != null ? `${formatValue(v)} ± ${formatValue(sd)}` : formatValue(v));
const cvText = (cv) => (cv == null ? "—" : `${cv.toFixed(1)}%`);

function pointTable(a) {
  const named = a.series.length > 1;
  return table(a.kind === "standard_curve" ? "Standards" : "Points",
    [...(named ? ["Series"] : []), "Name", a.x_label, "Signal (mean ± SD)", "CV", "Wells"],
    a.series.flatMap((sr) => sr.points.map((p) => [...(named ? [sr.name] : []), p.name, formatValue(p.x), sdText(p.y, p.sd), cvText(p.cv), p.wells.join(" ")])));
}

function sampleTable(a) {
  return table("Samples", ["Sample", "Signal", a.x_label, "CV", "Dilution", "Flag", "Wells"],
    a.samples.map((r) => [r.name, sdText(r.signal, r.signal_sd), sdText(r.value, r.value_sd), cvText(r.cv),
      r.dilution !== 1 ? `×${r.dilution}` : "—", r.flag || "—", r.wells.join(" ")]));
}

function groupTable(a) {
  return table("Groups", ["Group", "Mean ± SD", "n", "CV", "Fold change", "p (Welch t-test)", "Wells"],
    a.groups.map((g) => [g.name, sdText(g.mean, g.sd), g.n, cvText(g.cv), g.fold_change != null ? fold(g.fold_change) : "—",
      g.p_value != null ? formatValue(g.p_value) : "—", g.wells.join(" ")]));
}
