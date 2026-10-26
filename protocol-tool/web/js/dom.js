// DOM helpers shared by the views. Everything is built with createElement and
// textContent, so text from the AI model or uploaded documents can never inject markup.

export const $ = (id) => document.getElementById(id);

export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  setAttrs(el, attrs);
  appendChildren(el, children);
  return el;
}

export function s(tag, attrs = {}, ...children) {
  const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
  setAttrs(el, attrs);
  appendChildren(el, children);
  return el;
}

function setAttrs(el, attrs) {
  for (const [key, value] of Object.entries(attrs)) {
    if (value == null || value === false) continue;
    if (key === "class") el.setAttribute("class", value);
    else if (key === "style") Object.assign(el.style, value);
    else if (key === "dataset") Object.assign(el.dataset, value);
    else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
    else el.setAttribute(key, value === true ? "" : value);
  }
}

function appendChildren(el, children) {
  for (const child of children.flat(Infinity)) {
    if (child == null || child === false) continue;
    el.append(child instanceof Node ? child : String(child));
  }
}

const ICONS = {
  check: '<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M2.5 6.3 5 8.6l4.5-5.2" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/></svg>',
  cross: '<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M3.2 3.2l5.6 5.6M8.8 3.2 3.2 8.8" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"/></svg>',
  spinner: '<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M6 1.5A4.5 4.5 0 1 0 10.5 6" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"/></svg>',
  dash: '<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M3 6h6" fill="none" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"/></svg>',
  clock: '<svg viewBox="0 0 12 12" aria-hidden="true"><circle cx="6" cy="6" r="4.6" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M6 3.5V6l1.7 1.2" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>',
  stop: '<svg viewBox="0 0 12 12" aria-hidden="true"><rect x="3" y="3" width="6" height="6" rx="1" fill="currentColor"/></svg>',
  warn: '<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M6 1.6 11 10.4H1L6 1.6Z" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/><path d="M6 5v2.3" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/><circle cx="6" cy="8.8" r=".7" fill="currentColor"/></svg>',
  file: '<svg viewBox="0 0 12 12" aria-hidden="true"><path d="M3 1.5h4l2.5 2.5v6.5H3z" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round"/><path d="M7 1.5V4h2.5" fill="none" stroke="currentColor" stroke-width="1.2"/></svg>',
  trash: '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 7h14M10 7V5h4v2M7 7l1 12h8l1-12" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"/></svg>',
};

export function icon(name) {
  const template = document.createElement("template");
  template.innerHTML = ICONS[name] ?? "";
  return template.content.firstChild ?? document.createTextNode("");
}

// --- Formatting -------------------------------------------------------------------

export function formatTime(iso) {
  const date = new Date(iso);
  const time = date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return date.toDateString() === new Date().toDateString()
    ? time
    : `${date.toLocaleDateString([], { day: "numeric", month: "short" })} ${time}`;
}

export function formatDuration(ms) {
  const sec = Math.max(0, Math.round(ms / 1000));
  if (sec < 60) return `${sec} s`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min} min ${sec % 60} s`;
  return `${Math.floor(min / 60)} h ${min % 60} min`;
}

export function formatValue(v) {
  return v == null ? "—" : v.toLocaleString(undefined, { maximumSignificantDigits: 4 });
}

export function rowLabel(index) {
  let label = "";
  for (let n = index + 1; n > 0; n = Math.floor((n - 1) / 26)) {
    label = String.fromCharCode(65 + ((n - 1) % 26)) + label;
  }
  return label;
}

export function parseWell(name) {
  const match = /^([A-Za-z]+)(\d+)$/.exec(name);
  if (!match) return null;
  const row = [...match[1].toUpperCase()].reduce((acc, ch) => acc * 26 + ch.charCodeAt(0) - 64, 0) - 1;
  return { row, col: Number(match[2]) - 1 };
}

export function range(n) {
  return Array.from({ length: n }, (_, i) => i);
}

export function debounce(fn, ms) {
  let timer;
  const debounced = (...args) => {
    clearTimeout(timer);
    timer = setTimeout(() => fn(...args), ms);
  };
  debounced.flush = (...args) => {
    clearTimeout(timer);
    fn(...args);
  };
  return debounced;
}

// --- Tooltip ------------------------------------------------------------------------

export function showTooltip(target, ...content) {
  const tip = $("tooltip");
  tip.replaceChildren(...content);
  tip.hidden = false;
  const rect = target.getBoundingClientRect();
  const width = tip.offsetWidth;
  const left = Math.min(Math.max(8, rect.left + rect.width / 2 - width / 2), innerWidth - width - 8);
  const top = rect.top - tip.offsetHeight - 8;
  tip.style.left = `${left}px`;
  tip.style.top = `${top < 8 ? rect.bottom + 8 : top}px`;
}

export function hideTooltip() {
  $("tooltip").hidden = true;
}

// --- Markdown (the subset the assistant uses in chat) ---------------------------------------

export function markdown(text) {
  const out = document.createDocumentFragment();
  const lines = text.replace(/\r/g, "").split("\n");
  let i = 0;
  const isBlockStart = (line) =>
    /^```/.test(line) || /^#{1,4}\s/.test(line) || /^\s*([-*+]|\d+[.)])\s+/.test(line) || /^\|.*\|\s*$/.test(line);

  while (i < lines.length) {
    const line = lines[i];
    if (/^```/.test(line)) {
      const body = [];
      for (i++; i < lines.length && !/^```/.test(lines[i]); i++) body.push(lines[i]);
      i++;
      out.append(h("pre", {}, h("code", {}, body.join("\n"))));
    } else if (/^#{1,4}\s/.test(line)) {
      const level = line.match(/^#+/)[0].length;
      out.append(h(level <= 2 ? "h4" : "h5", {}, inline(line.replace(/^#+\s*/, ""))));
      i++;
    } else if (/^\s*([-*+]|\d+[.)])\s+/.test(line)) {
      const ordered = /^\s*\d/.test(line);
      const list = h(ordered ? "ol" : "ul");
      while (i < lines.length && /^\s*([-*+]|\d+[.)])\s+/.test(lines[i])) {
        const item = [lines[i].replace(/^\s*([-*+]|\d+[.)])\s+/, "")];
        for (i++; i < lines.length && /^\s{2,}\S/.test(lines[i]) && !/^\s*([-*+]|\d+[.)])\s+/.test(lines[i]); i++) {
          item.push(lines[i].trim());
        }
        list.append(h("li", {}, inline(item.join(" "))));
      }
      out.append(list);
    } else if (/^\|.*\|\s*$/.test(line) && /^\|?[\s:|-]+\|?\s*$/.test(lines[i + 1] ?? "")) {
      const cells = (row) => row.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
      const head = cells(line);
      const rows = [];
      for (i += 2; i < lines.length && /^\|.*\|\s*$/.test(lines[i]); i++) rows.push(cells(lines[i]));
      out.append(
        h("table", {},
          h("thead", {}, h("tr", {}, head.map((c) => h("th", {}, inline(c))))),
          h("tbody", {}, rows.map((r) => h("tr", {}, r.map((c) => h("td", {}, inline(c)))))),
        ),
      );
    } else if (!line.trim()) {
      i++;
    } else {
      const para = [];
      while (i < lines.length && lines[i].trim() && !isBlockStart(lines[i])) para.push(lines[i++]);
      const p = h("p");
      para.forEach((l, k) => p.append(...(k ? [h("br")] : []), ...inline(l)));
      out.append(p);
    }
  }
  return out;
}

function inline(text) {
  const parts = text.split(/(`[^`]+`|\*\*[^*]+\*\*|__[^_]+__|\*[^*\s][^*]*\*)/g);
  return parts.filter(Boolean).map((part) => {
    if (/^`[^`]+`$/.test(part)) return h("code", {}, part.slice(1, -1));
    if (/^(\*\*|__).+(\*\*|__)$/.test(part)) return h("strong", {}, part.slice(2, -2));
    if (/^\*[^*\s].*\*$/.test(part)) return h("em", {}, part.slice(1, -1));
    return document.createTextNode(part);
  });
}
