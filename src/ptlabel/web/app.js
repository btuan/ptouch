// ptlabel web UI. One canonical Job is edited, previewed and printed;
// templates and CSV batches load *into* that job (print = what you see).
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

const TOKEN_KEY = "ptlabel.token";
const DRAFT_KEY = "ptlabel.draft.v2";
const PRINTER_KEY = "ptlabel.printer";
const DOTS_PER_MM = 360 / 25.4;
const CSS_PX_PER_MM = 96 / 25.4;
const FIT_MAX = 4;

const ITEM_DEFAULTS = {
  text: { type: "text", text: "Label" },
  qr: { type: "qr", data: "https://example.com" },
  datamatrix: { type: "datamatrix", data: "SN-0001" },
  barcode: { type: "barcode", data: "SKU-0001" },
  image: { type: "image", src: "" },
  spacer: { type: "spacer", width_mm: 2 },
  rule: { type: "rule" },
};
const KIND = { text: "Text", qr: "QR", datamatrix: "Matrix", barcode: "Barcode", image: "Image", spacer: "Space", rule: "Line" };
const KIND_LONG = { text: "Text", qr: "QR code", datamatrix: "Data Matrix code", barcode: "Barcode",
                    image: "Image", spacer: "Space", rule: "Divider line" };
const DEMO = { labels: [{ items: [
  { type: "qr", data: "https://example.com/asset/A-00123" },
  { type: "text", text: "A-00123\nLab / Rack B", font: "sans-bold" },
] }] };

// [key, label, kind, opts]   opts.def = server default, shown as placeholder
const FIELDS = {
  text: [["text", "Text", "textarea", { wide: true, help: "Press Enter for a second line." }],
         ["font", "Font", "font"],
         ["size_mm", "Size (mm)", "number", { def: "fit tape", step: 0.5 }],
         ["align", "Line alignment", "select", { options: ["left", "center", "right"] }],
         ["max_width_mm", "Max width (mm)", "number", { def: "none" }]],
  qr: [["data", "Link or text", "text", { wide: true }],
       ["ecc", "Damage tolerance", "select", { options: ["L", "M", "Q", "H"], default: "M",
          labels: { L: "Low (smallest)", M: "Medium", Q: "High", H: "Highest" } }],
       ["height_pct", "Height (%)", "number", { def: "100" }]],
  datamatrix: [["data", "Data", "text", { wide: true }], ["height_pct", "Height (%)", "number", { def: "100" }]],
  barcode: [["data", "Data", "text", { wide: true }],
            ["symbology", "Format", "select", { options: ["code128", "code39", "ean13", "ean8", "upca", "itf"],
               labels: { code128: "Code 128", code39: "Code 39", ean13: "EAN-13", ean8: "EAN-8", upca: "UPC-A", itf: "ITF" } }],
            ["module_mm", "Bar width (mm)", "number", { def: "0.25", step: 0.05 }],
            ["_hint", "", "hint", { wide: true }],
            ["show_text", "Show text under bars", "check", { default: true }]],
  image: [["src", "Image file", "file", { wide: true }], ["threshold", "Black/white cutoff", "number", { def: "128" }],
          ["dither", "Dither (for photos)", "check"], ["invert", "Invert", "check"]],
  spacer: [["width_mm", "Width (mm)", "number", { step: 0.5 }]],
  rule: [["thickness_mm", "Thickness (mm)", "number", { def: "0.3", step: 0.1 }]],
};
const LABEL_FIELDS = [
  ["justify", "Arrange items", "select", { options: ["center", "start", "end", "between"],
     labels: { center: "Centre", start: "Left", end: "Right", between: "Spread out" } }],
  ["length_mm", "Label length (mm)", "number", { def: "fit content" }],
  ["gap_mm", "Space between items (mm)", "number", { def: "1.5", step: 0.5 }],
  ["pad_mm", "Edge margin (mm)", "number", { def: "0.4", step: 0.1 }],
  ["border", "Border", "check"], ["invert", "Invert (white on black)", "check"],
];
const SEP_LABELS = { half_cut: "Half-cut — backing stays, labels peel off one strip", full_cut: "Full cut — separate pieces",
                     none: "No cut — one continuous label" };
const jobFields = () => {
  const p = printerInfo();
  const seps = p.half_cut === false ? ["full_cut", "none"] : ["half_cut", "full_cut", "none"];
  const def = p.half_cut === false ? "full_cut" : "half_cut";
  return [
    ["separator", "Between labels", "select", { wide: true, options: ["", ...seps],
       labels: { "": `Printer default (${def.replace("_", "-")})`, ...SEP_LABELS } }],
    ...(p.half_cut === false ? [] : [["full_cut_every", "Also full-cut every … labels", "number", { def: "only at end", min: 1 }]]),
    ...(p.hires === false ? [] : [["hires", "Sharper text", "check", { help: "Prints at 360×720 dpi; slower." }]]),
    ["mirror", "Mirror", "check", { help: "For clear tape read from the back." }],
    ["chain", "Don't cut after the last label", "check", { help: "Saves leader tape on the next print; the label stays in the printer." }],
  ];
};

const state = {
  job: structuredClone(DEMO),
  sel: { label: 0, item: 0 },
  tab: "design",
  meta: { fonts: ["sans"], tape_widths: [6, 9, 12, 18, 24, 36] },
  printer: null,
  templates: [],
  thumbs: {},
  tpl: { name: null, vars: {}, rows: "" },
  source: null,
  render: null,      // last successful render
  pending: true,     // an edit hasn't been rendered yet
  zoom: "fit",
};

// ---------------------------------------------------------------- api
class NetError extends Error {}
async function api(path, opts = {}) {
  const headers = { "content-type": "application/json", ...(opts.headers || {}) };
  const tok = localStorage.getItem(TOKEN_KEY);
  if (tok) headers.authorization = `Bearer ${tok}`;
  let r;
  try { r = await fetch(path, { ...opts, headers }); }
  catch { throw new NetError("Can't reach the ptlabel server. Is `ptlabel serve` running?"); }
  if (r.status === 401) {
    const t = prompt("This server needs an access token:");
    if (t) { localStorage.setItem(TOKEN_KEY, t); return api(path, opts); }
  }
  const body = r.status === 204 ? null : await r.json().catch(() => null);
  if (!r.ok) {
    const err = new Error(errText(body) || `${r.status} ${r.statusText}`);
    err.loc = Array.isArray(body?.detail) ? body.detail[0]?.loc : null;
    throw err;
  }
  return body;
}
function errText(body) {
  if (!body) return "";
  const d = body.detail;
  if (typeof d === "string") return d.replace(/^label (\d+):/, "Label $1:");
  if (Array.isArray(d)) return d.map(e => `${describeLoc(e.loc)}: ${e.msg}`).join("\n");
  return JSON.stringify(d);
}
function describeLoc(loc = []) {
  const path = loc.filter(x => x !== "body");
  const out = [];
  for (let i = 0; i < path.length; i++) {
    if (path[i] === "labels" && typeof path[i + 1] === "number") { out.push(`Label ${path[++i] + 1}`); continue; }
    if (path[i] === "items" && typeof path[i + 1] === "number") { out.push(`item ${path[++i] + 1}`); continue; }
    if (["text", "qr", "datamatrix", "barcode", "image", "spacer", "rule"].includes(path[i])) continue;
    out.push(String(path[i]).replace(/_/g, " "));
  }
  return out.join(" · ") || "Spec";
}

// ---------------------------------------------------------------- helpers
function clean(obj) {
  if (Array.isArray(obj)) return obj.map(clean);
  if (obj && typeof obj === "object") {
    const out = {};
    for (const [k, v] of Object.entries(obj)) if (v !== "" && v !== null && v !== undefined) out[k] = clean(v);
    return out;
  }
  return obj;
}
const currentJob = () => clean(structuredClone(state.job));
const printerInfo = () => (state.meta.printers || []).find(p => p.key === state.printerKey) || {};
const P = path => state.printerKey ? `${path}${path.includes("?") ? "&" : "?"}printer=${state.printerKey}` : path;
const label = () => state.job.labels[state.sel.label];
const plural = (n, w) => `${n} ${w}${n === 1 ? "" : "s"}`;
function summary(it) {
  if (it.type === "text") return it.text.replace(/\n/g, " · ") || "(empty)";
  if (it.type === "spacer") return `${it.width_mm ?? ""} mm gap`;
  if (it.type === "rule") return "vertical line";
  if (it.type === "image") return it.src ? "image" : "choose a file…";
  return it.data || "(empty)";
}
function labelName(l, i) {
  const t = l.items.find(x => x.type === "text") ?? l.items.find(x => x.data);
  return `${i + 1} · ${t ? summary(t) : KIND_LONG[l.items[0].type]}`;
}
let toastTimer;
function toast(msg, { bad = false, action = null, sticky = bad } = {}) {
  const t = $("#toast");
  $("#toast-text").textContent = msg;
  t.className = "toast" + (bad ? " bad" : "");
  t.setAttribute("role", bad ? "alert" : "status");
  const a = $("#toast-action"); a.hidden = !action;
  if (action) { a.textContent = action.label; a.onclick = () => { t.hidden = true; action.fn(); }; }
  $("#toast-close").hidden = !sticky;
  t.hidden = false;
  clearTimeout(toastTimer);
  if (!sticky) toastTimer = setTimeout(() => (t.hidden = true), action ? 8000 : 4000);
}
$("#toast-close").onclick = () => ($("#toast").hidden = true);
function el(tag, attrs = {}, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null && v !== false) e.setAttribute(k, v === true ? "" : v);
  }
  e.append(...kids.filter(k => k !== null && k !== undefined && k !== false));
  return e;
}
let uid = 0;

// keep keyboard focus across re-renders: elements carry data-k keys
function withFocus(fn) {
  const k = document.activeElement?.dataset?.k;
  fn();
  if (!k) return;
  const target = $(`[data-k="${k}"]`);
  if (target && !target.disabled) target.focus();
  else $(`[data-k="${k.replace(/:.*/, "")}"]`)?.focus();
}

// ---------------------------------------------------------------- form fields
function field(obj, [key, labelText, kind, opt = {}], onchange) {
  const id = `f${++uid}`;
  const cls = "field" + (opt.wide ? " wide" : "") + (kind === "check" ? " check" : "");
  const set = v => { if (v === "" || v === undefined) delete obj[key]; else obj[key] = v; onchange(); };
  const help = opt.help ? el("small", { class: "help" }, opt.help) : null;
  let input;
  if (kind === "check") {
    input = el("input", { type: "checkbox", id }); input.checked = obj[key] ?? opt.default ?? false;
    input.addEventListener("change", () => set(input.checked === (opt.default ?? false) ? undefined : input.checked));
    return el("label", { class: cls, for: id }, input, el("span", {}, labelText, help));
  }
  if (kind === "hint") {
    const h = { code128: "Code 128: any letters, digits and symbols.", code39: "Code 39: A–Z, 0–9, space and - . $ / + %",
                ean13: "EAN-13: 12 digits (check digit added).", ean8: "EAN-8: 7 digits (check digit added).",
                upca: "UPC-A: 11 digits (check digit added).", itf: "ITF: an even number of digits." };
    return el("p", { class: "hint field wide" }, h[obj.symbology ?? "code128"]);
  }
  if (kind === "textarea") {
    input = el("textarea", { rows: 2, id }); input.value = obj[key] ?? "";
    input.addEventListener("input", () => set(input.value));
  } else if (kind === "select" || kind === "font") {
    const options = kind === "font" ? state.meta.fonts : opt.options;
    input = el("select", { id });
    for (const o of options) input.append(el("option", { value: o }, opt.labels?.[o] ?? o));
    input.value = obj[key] ?? (kind === "font" ? "sans" : opt.default ?? options[0]);
    input.addEventListener("change", () => set(input.value));
  } else if (kind === "number") {
    input = el("input", { type: "number", id, step: opt.step ?? 1, min: opt.min ?? 0, inputmode: "decimal", placeholder: opt.def });
    input.value = obj[key] ?? "";
    input.addEventListener("input", () => set(input.value === "" ? "" : +input.value));
  } else if (kind === "file") {
    input = el("input", { type: "file", id, accept: "image/*" });
    input.addEventListener("change", () => {
      const f = input.files[0]; if (!f) return;
      const r = new FileReader(); r.onload = () => set(r.result); r.readAsDataURL(f);
    });
  } else {
    input = el("input", { type: "text", id, autocomplete: "off" }); input.value = obj[key] ?? "";
    input.addEventListener("input", () => set(input.value));
  }
  return el("label", { class: cls, for: id }, el("span", {}, labelText), input, help);
}

// ---------------------------------------------------------------- design panel
function renderDesign() { withFocus(renderDesignInner); }
function renderDesignInner() {
  $("#items-label-name").textContent = state.job.labels.length > 1 ? `label ${state.sel.label + 1}` : "the label";
  const list = $("#items"); list.replaceChildren();
  const items = label().items;
  items.forEach((it, i) => {
    const name = KIND_LONG[it.type];
    const tools = el("span", { class: "tools" },
      el("button", { "data-k": `item-${i}:left`, "aria-label": `Move ${name} left`, title: "Move left", disabled: i === 0, onclick: () => move(i, -1) }, "←"),
      el("button", { "data-k": `item-${i}:right`, "aria-label": `Move ${name} right`, title: "Move right", disabled: i === items.length - 1, onclick: () => move(i, 1) }, "→"),
      el("button", { "data-k": `item-${i}:del`, "aria-label": `Remove ${name}`, title: "Remove", onclick: () => removeItem(i) }, "✕"));
    list.append(el("li", { class: i === state.sel.item ? "on" : "" },
      el("button", { class: "row", "data-k": `item-${i}`, "aria-pressed": i === state.sel.item,
                     onclick: () => { state.sel.item = i; renderDesign(); } },
        el("span", { class: "kind" }, KIND[it.type]), el("span", { class: "sum" }, summary(it))), tools));
  });
  const add = $("#add-items"); add.replaceChildren();
  for (const t of Object.keys(ITEM_DEFAULTS)) add.append(el("button", { class: "btn sm", "data-k": `add-${t}`, onclick: () => {
    label().items.push(structuredClone(ITEM_DEFAULTS[t])); state.sel.item = label().items.length - 1; edited(true);
  } }, `+ ${KIND_LONG[t]}`));

  const it = items[state.sel.item];
  $("#item-card").hidden = !it;
  if (it) {
    $("#item-card-title").textContent = KIND_LONG[it.type];
    const form = $("#item-form"); form.replaceChildren();
    for (const f of FIELDS[it.type]) form.append(field(it, f, () => {
      refreshSummaries(); edited(f[0] === "symbology");
    }));
  }
  const lf = $("#label-form"); lf.replaceChildren();
  for (const f of LABEL_FIELDS) lf.append(field(label(), f, () => { notes(); edited(); }));
  const jf = $("#job-form"); jf.replaceChildren();
  for (const f of jobFields()) jf.append(field(state.job, f, () => { notes(); edited(); }));
  notes();
  renderStrip();
  renderActionbar();
}
function notes() {
  const l = label();
  const arr = { center: "Centred", start: "Left", end: "Right", between: "Spread" }[l.justify ?? "center"];
  $("#layout-note").textContent = `${arr} · ${l.length_mm ? `${l.length_mm} mm long` : "fits content"}`;
  const sep = { half_cut: "Half-cut between", full_cut: "Full cut between", none: "No cuts between" }[state.job.separator ?? (printerInfo().half_cut === false ? "full_cut" : "half_cut")];
  $("#cut-note").textContent = `${sep} · ${state.job.chain ? "no cut at end" : "full cut at end"}${state.job.hires ? " · sharp" : ""}`;
}
function refreshSummaries() {
  $$("#items li .sum").forEach((s, i) => (s.textContent = summary(label().items[i])));
  $$("#label-strip button:not(.add)").forEach((b, i) => (b.textContent = labelName(state.job.labels[i], i)));
}
function move(i, d) {
  const items = label().items; [items[i], items[i + d]] = [items[i + d], items[i]];
  state.sel.item = i + d;
  const k = document.activeElement?.dataset?.k;
  if (k) document.activeElement.dataset.k = k.replace(/item-\d+/, `item-${i + d}`);
  edited(true);
}
function removeItem(i) {
  const li = state.sel.label, snapshot = structuredClone(label());
  const removed = label().items.splice(i, 1)[0];
  if (!label().items.length) label().items.push(structuredClone(ITEM_DEFAULTS.text));
  state.sel.item = Math.min(state.sel.item, label().items.length - 1); edited(true);
  toast(`Removed ${KIND_LONG[removed.type]}`, { action: { label: "Undo", fn: () => {
    state.job.labels[li] = snapshot; state.sel = { label: li, item: i }; edited(true);
  } } });
}

function renderStrip() {
  const s = $("#label-strip"); s.replaceChildren();
  state.job.labels.forEach((l, i) => s.append(el("button", {
    "aria-pressed": i === state.sel.label, "data-k": `label-${i}`, title: labelName(l, i),
    onclick: () => { state.sel = { label: i, item: 0 }; selectTab("design"); renderDesign(); highlightLabel(true); },
  }, labelName(l, i))));
  $("#label-strip [aria-pressed=true]")?.scrollIntoView({ block: "nearest", inline: "nearest" });
  s.append(el("button", { class: "add", "data-k": "label-add", title: "Add another label to this print job", onclick: () => {
    state.job.labels.push({ items: [structuredClone(ITEM_DEFAULTS.text)] });
    state.sel = { label: state.job.labels.length - 1, item: 0 }; edited(true);
  } }, "+ Add label"));
  $("#del-label").disabled = state.job.labels.length < 2;
}
$("#dup-label").onclick = () => {
  state.job.labels.splice(state.sel.label + 1, 0, structuredClone(label()));
  state.sel = { label: state.sel.label + 1, item: 0 }; edited(true);
};
$("#del-label").onclick = () => {
  if (state.job.labels.length < 2) return;
  const before = structuredClone(state.job.labels), at = state.sel.label;
  state.job.labels.splice(at, 1);
  state.sel = { label: Math.max(0, at - 1), item: 0 }; edited(true);
  toast(`Deleted label ${at + 1}`, { action: { label: "Undo", fn: () => { state.job.labels = before; state.sel = { label: at, item: 0 }; edited(true); } } });
};

function renderActionbar() {
  const sel = $("#tape-select");
  const widths = printerInfo().tape_widths ?? state.meta.tape_widths;
  if (sel.dataset.widths !== String(widths)) {
    sel.dataset.widths = String(widths);
    sel.replaceChildren(el("option", { value: "" }));
    for (const w of widths) sel.append(el("option", { value: w }, `${w} mm`));
  }
  updateLoadedOption();
  if (document.activeElement !== sel) sel.value = state.job.tape_mm ?? "";
  if (document.activeElement !== $("#copies")) $("#copies").value = state.job.copies ?? 1;
}
function updateLoadedOption() {
  const loaded = state.printer?.tape_mm;
  const opt = $("#tape-select").options[0];
  if (opt) opt.textContent = loaded ? `Loaded (${loaded} mm)` : "Auto";
}
$("#tape-select").onchange = e => { e.target.value ? (state.job.tape_mm = +e.target.value) : delete state.job.tape_mm; edited(); };
$("#copies").oninput = e => {
  const n = Math.round(+e.target.value);
  if (n >= 1 && n <= 100) { n > 1 ? (state.job.copies = n) : delete state.job.copies; edited(); }
};
$("#copies").onchange = e => {
  const n = Math.max(1, Math.min(100, Math.round(+e.target.value || 1)));
  e.target.value = n; n > 1 ? (state.job.copies = n) : delete state.job.copies; edited();
};

// ---------------------------------------------------------------- templates panel
async function loadTemplates() {
  state.templates = await api("/templates");
  const grid = $("#tpl-grid"); grid.replaceChildren();
  for (const t of state.templates) {
    const img = el("img", { alt: "" });
    grid.append(el("button", { class: "tpl", "data-name": t.name, "aria-pressed": t.name === state.tpl.name,
                               onclick: () => pickTemplate(t.name) },
      el("div", { class: "thumb" }, img), el("strong", {}, t.name), el("span", {}, t.description || "")));
    const key = `${t.name}@${state.job.tape_mm ?? "auto"}`;
    if (state.thumbs[key]) { img.src = state.thumbs[key]; continue; }
    const vars = Object.fromEntries(Object.entries(t.variables).map(([k, v]) => [k, v.example ?? v.default ?? k]));
    api(P(`/templates/${t.name}/render?format=json&frame=false`), { method: "POST",
      body: JSON.stringify({ vars, job: clean({ tape_mm: state.job.tape_mm }) }) })
      .then(r => (img.src = state.thumbs[key] = `data:image/png;base64,${r.preview_png_base64}`)).catch(() => {});
  }
  if (!state.templates.length) grid.append(el("p", { class: "hint" }, "No templates yet. Design a label, then use “Save as template”."));
  if (state.tpl.name) pickTemplate(state.tpl.name, false);
}
const tplDef = () => state.templates.find(x => x.name === state.tpl.name);
function pickTemplate(name, reset = true) {
  const t = state.templates.find(x => x.name === name);
  if (!t) return;
  if (reset || state.tpl.name !== name) state.tpl = { name, vars: {}, rows: "" };
  $$(".tpl").forEach(b => b.setAttribute("aria-pressed", b.dataset.name === name));
  $("#tpl-card").hidden = false;
  $("#tpl-title").textContent = name;
  $("#tpl-desc").textContent = t.description || "";
  $("#tpl-cols").textContent = Object.keys(t.variables).join(", ");
  const box = $("#tpl-vars"); box.replaceChildren();
  for (const [k, v] of Object.entries(t.variables)) {
    const f = field(state.tpl.vars, [k, v.description || k, "text", { wide: true }], rowCheck);
    $("input", f).placeholder = v.default != null ? (v.default || "(blank)") : `e.g. ${v.example ?? ""}`;
    box.append(f);
  }
  $("#tpl-rows").value = state.tpl.rows;
  $("#tpl-rows").placeholder = [Object.keys(t.variables).join("\t"), Object.values(t.variables).map(v => v.example ?? "").join("\t")].join("\n");
  rowCheck();
}
function parseRows(text, varNames) {
  const lines = text.replace(/^﻿/, "").split(/\r?\n/).filter(l => l.trim());
  if (!lines.length) return { rows: [], problem: null };
  const tsv = lines[0].includes("\t");
  const split = l => {
    if (tsv) return l.split("\t").map(s => s.trim());
    const out = []; let cur = "", q = false;
    for (let i = 0; i < l.length; i++) {
      const c = l[i];
      if (q) { if (c === '"' && l[i + 1] === '"') { cur += '"'; i++; } else if (c === '"') q = false; else cur += c; }
      else if (c === '"') q = true; else if (c === ",") { out.push(cur.trim()); cur = ""; } else cur += c;
    }
    out.push(cur.trim()); return out;
  };
  const byLower = Object.fromEntries(varNames.map(v => [v.toLowerCase(), v]));
  const head = split(lines[0]);
  const mapped = head.map(h => byLower[h.toLowerCase()] ?? null);
  const unknown = head.filter((h, i) => !mapped[i]);
  const missing = varNames.filter(v => !mapped.includes(v) && !(tplDef()?.variables[v]?.default !== undefined));
  if (!mapped.some(Boolean)) return { rows: [], problem: `First row must name the columns (${varNames.join(", ")}).` };
  const rows = lines.slice(1).map(l => Object.fromEntries(split(l).flatMap((v, i) => mapped[i] ? [[mapped[i], v]] : [])));
  const notes = [];
  if (unknown.length) notes.push(`ignoring ${unknown.join(", ")}`);
  if (missing.length) notes.push(`${missing.join(", ")} from the fields above`);
  return { rows, problem: null, note: notes.join(" · "), cols: mapped.filter(Boolean) };
}
function rowCheck() {
  const t = tplDef(); if (!t) return;
  const { rows, problem, note, cols } = parseRows(state.tpl.rows, Object.keys(t.variables));
  const out = $("#tpl-rowcount");
  out.className = "rowcheck" + (problem ? " bad" : rows.length ? " ok" : "");
  out.textContent = problem ? problem : rows.length ? `✓ ${plural(rows.length, "row")} → ${plural(rows.length, "label")} · columns: ${cols.join(", ")}${note ? ` · ${note}` : ""}` : "";
  const n = rows.length || 1;
  const covered = new Set(cols ?? []);
  const empty = Object.entries(t.variables).filter(([k, v]) => v.default == null && !covered.has(k) && !state.tpl.vars[k]).map(([k]) => k);
  if (empty.length && !problem) { out.className = "rowcheck bad"; out.textContent = `Fill in: ${empty.join(", ")}`; }
  $("#tpl-replace").textContent = `Replace job with ${plural(n, "label")}`;
  $("#tpl-add").textContent = `Add ${plural(n, "label")}`;
  $("#tpl-replace").disabled = $("#tpl-add").disabled = !!problem || empty.length > 0;
}
$("#tpl-rows").oninput = e => { state.tpl.rows = e.target.value; rowCheck(); };
async function useTemplate(mode) {
  try {
    const t = await api(`/templates/${state.tpl.name}`);
    const defaults = Object.fromEntries(Object.entries(t.variables).map(([k, v]) => [k, v.default ?? ""]));
    const { rows } = parseRows(state.tpl.rows, Object.keys(t.variables));
    const fill = vals => JSON.parse(JSON.stringify(t.label),
      (k, v) => typeof v === "string" ? v.replace(/\{\{\s*(\w+)\s*\}\}/g, (_, n) => vals[n] ?? "") : v);
    const made = (rows.length ? rows : [{}]).map(r => fill({ ...defaults, ...state.tpl.vars, ...r }));
    const before = structuredClone(state.job), beforeSel = { ...state.sel };
    if (mode === "replace") { state.job.labels = made; state.sel = { label: 0, item: 0 }; }
    else { state.job.labels.push(...made); state.sel = { label: state.job.labels.length - made.length, item: 0 }; }
    state.source = `From template “${t.name}”${rows.length ? ` · ${plural(rows.length, "row")}` : ""}`;
    selectTab("design"); edited(true, true);
    toast(`${mode === "replace" ? "Loaded" : "Added"} ${plural(made.length, "label")}`, { action: { label: "Undo", fn: () => {
      state.job = before; state.sel = beforeSel; state.source = null; edited(true);
    } } });
  } catch (e) { toast(e.message, { bad: true }); }
}
$("#tpl-replace").onclick = () => useTemplate("replace");
$("#tpl-add").onclick = () => useTemplate("add");
$("#save-tpl").onclick = async () => {
  const name = $("#save-name").value.trim();
  if (!/^[a-z0-9][a-z0-9_-]*$/.test(name)) return toast("Template name: lowercase letters, numbers, - and _", { bad: true });
  const lab = clean(structuredClone(label()));
  const vars = [...new Set([...JSON.stringify(lab).matchAll(/\{\{\s*(\w+)\s*\}\}/g)].map(m => m[1]))];
  const body = { name, description: $("#save-desc").value, label: lab,
                 variables: Object.fromEntries(vars.map(v => [v, { example: v }])) };
  try {
    await api(`/templates/${name}`, { method: "PUT", body: JSON.stringify(body) });
    toast(`Saved template “${name}”${vars.length ? ` with ${plural(vars.length, "variable")}` : ""}`);
    state.tpl.name = name; state.thumbs = {};
  } catch (e) { toast(e.message, { bad: true }); }
};

// ---------------------------------------------------------------- spec drawer
$("#new-job").onclick = () => {
  const before = structuredClone(state.job);
  state.job = { labels: [{ items: [structuredClone(ITEM_DEFAULTS.text)] }] };
  state.sel = { label: 0, item: 0 }; edited(true);
  toast("Started a new job", { action: { label: "Undo", fn: () => { state.job = before; edited(true); } } });
};
$("#spec-open").onclick = () => { $("#json").value = JSON.stringify(currentJob(), null, 2); $("#spec").showModal(); };
$("#spec-close").onclick = () => $("#spec").close();
$("#json-apply").onclick = () => {
  let j;
  try { j = JSON.parse($("#json").value); } catch (e) { return toast(`Invalid JSON: ${e.message}`, { bad: true }); }
  const job = j && typeof j === "object" && "labels" in j ? j : { labels: [j] };
  const ok = Array.isArray(job.labels) && job.labels.length &&
    job.labels.every(l => l && Array.isArray(l.items) && l.items.length && l.items.every(i => i && ITEM_DEFAULTS[i.type]));
  if (!ok) return toast("Each label needs an items list, and every item needs a known type.", { bad: true });
  const before = structuredClone(state.job);
  state.job = job; state.sel = { label: 0, item: 0 }; state.source = null;
  $("#spec").close(); edited(true);
  toast("Applied", { action: { label: "Undo", fn: () => { state.job = before; edited(true); } } });
};
$("#json-copy").onclick = async () => { await navigator.clipboard.writeText($("#json").value); toast("Copied"); };

// ---------------------------------------------------------------- tabs
function selectTab(tab) {
  state.tab = tab;
  $$(".tabs button").forEach(b => {
    const on = b.dataset.tab === tab;
    b.setAttribute("aria-selected", on); b.tabIndex = on ? 0 : -1;
  });
  $$(".panel").forEach(p => (p.hidden = p.id !== `panel-${tab}`));
  if (tab === "template") loadTemplates().catch(e => toast(e.message, { bad: true }));
}
$$(".tabs button").forEach(b => {
  b.onclick = () => selectTab(b.dataset.tab);
  b.onkeydown = e => {
    if (e.key !== "ArrowRight" && e.key !== "ArrowLeft") return;
    const tabs = $$(".tabs button"); const i = tabs.indexOf(b);
    const next = tabs[(i + (e.key === "ArrowRight" ? 1 : -1) + tabs.length) % tabs.length];
    selectTab(next.dataset.tab); next.focus();
  };
});

// ---------------------------------------------------------------- preview
let timer, seq = 0;
function edited(rerender = false, keepSource = false) {
  if (!keepSource) state.source = null;
  try { localStorage.setItem(DRAFT_KEY, JSON.stringify(state.job)); } catch {}
  if (rerender) renderDesign();
  state.pending = true; syncPrintButton();
  clearTimeout(timer); timer = setTimeout(refreshPreview, 200);
}
async function refreshPreview() {
  const my = ++seq;
  $("#preview").classList.add("busy");
  try {
    const r = await api(P("/render?format=json&frame=false"), { method: "POST", body: JSON.stringify(currentJob()) });
    if (my !== seq) return;
    state.render = { png: r.preview_png_base64, tape_mm: r.tape_mm, labels: r.labels, length_mm: r.length_mm,
                     spans: r.label_spans_mm,
                     job: JSON.stringify(currentJob()) };
    state.error = null;
    const img = $("#preview-img");
    img.onload = layoutPreview;
    img.src = `data:image/png;base64,${r.preview_png_base64}`;
    img.alt = `Preview: ${plural(r.labels, "label")}, ${r.length_mm} mm long on ${r.tape_mm} mm tape`;
    $("#skeleton").hidden = true;
    $("#preview").classList.remove("stale");
    $("#error").hidden = true;
  } catch (e) {
    if (my !== seq) return;
    state.error = e.message;
    $("#error").textContent = e.message; $("#error").hidden = false;
    $("#preview").classList.add("stale");
    if (e.loc) focusErrorItem(e.loc);
  } finally {
    if (my === seq) { $("#preview").classList.remove("busy"); state.pending = false; renderStats(); syncPrintButton(); }
  }
}
function focusErrorItem(loc) {
  const p = loc.filter(x => x !== "body");
  const li = p[0] === "labels" ? p[1] : 0, ii = p.indexOf("items");
  if (typeof li === "number" && ii >= 0 && typeof p[ii + 1] === "number" && state.job.labels[li]?.items[p[ii + 1]]) {
    state.sel = { label: li, item: p[ii + 1] }; renderDesign();
  }
}
function syncPrintButton() {
  const r = state.render;
  const btn = $("#print");
  btn.disabled = state.pending || !!state.error || !r;
  const n = state.job.labels.length, c = state.job.copies ?? 1;
  btn.textContent = state.pending ? "Updating preview…" : `Print ${plural(n, "label")}${c > 1 ? ` × ${c}` : ""}`;
}
function renderStats() {
  const r = state.render;
  const st = $("#stats");
  if (state.error) st.textContent = "Fix the problem above to print";
  else if (r) st.textContent = `${plural(r.labels, "label")} · ${r.length_mm} mm long · ${r.tape_mm} mm tape`;
  const src = $("#source"); src.hidden = !state.source; src.textContent = state.source ?? "";
  const loaded = state.printer?.tape_mm;
  const warn = $("#tape-warn");
  warn.hidden = !(r && loaded && loaded !== r.tape_mm);
  if (!warn.hidden) {
    $("#tape-warn-text").textContent = `The printer has ${loaded} mm tape loaded, but this job is laid out for ${r.tape_mm} mm.`;
    $("#tape-warn-fix").textContent = `Use ${loaded} mm`;
  }
}
$("#tape-warn-fix").onclick = () => { delete state.job.tape_mm; renderActionbar(); edited(); };
function layoutPreview() {
  const img = $("#preview-img");
  if (!img.naturalWidth) return;
  const dots = img.naturalWidth / 2;                     // preview PNGs are 2x
  const actual = dots / DOTS_PER_MM * CSS_PX_PER_MM;     // css px at ~real size
  const room = $("#preview-wrap").clientWidth - 48;
  const scale = state.zoom === "fit" ? Math.max(0.5, Math.min(FIT_MAX, room / actual)) : +state.zoom;
  const w = actual * scale;
  img.style.width = `${w}px`;
  img.style.height = `${(img.naturalHeight / 2) / DOTS_PER_MM * CSS_PX_PER_MM * scale}px`;
  const mm = CSS_PX_PER_MM * scale;
  const ruler = $("#ruler");
  ruler.style.width = `${w}px`;
  ruler.style.background = `repeating-linear-gradient(to right, currentColor 0 1px, transparent 1px ${mm * 10}px) top/100% 9px no-repeat` +
    (mm >= 3 ? `, repeating-linear-gradient(to right, currentColor 0 1px, transparent 1px ${mm}px) top/100% 4px no-repeat` : "");
  const totalMm = dots / DOTS_PER_MM;
  const step = mm * 10 < 28 ? 50 : 10;
  ruler.replaceChildren(...Array.from({ length: Math.floor(totalMm / step) + 1 }, (_, i) =>
    el("span", { style: `left:${i * step * mm}px` }, `${i * step}`)));
  highlightLabel();
}
function highlightLabel(scroll = false) {
  const r = state.render, img = $("#preview-img"), box = $("#hl");
  const span = r?.spans?.[state.sel.label];
  if (!span || state.job.labels.length < 2 || !img.naturalWidth || (state.job.copies ?? 1) > 1) { box.hidden = true; return; }
  const pxPerMm = img.getBoundingClientRect().width / r.spans.at(-1)[1];
  box.hidden = false;
  box.style.left = `${span[0] * pxPerMm}px`; box.style.width = `${(span[1] - span[0]) * pxPerMm}px`;
  if (scroll) {
    const wrap = $("#preview-wrap");
    const x = img.getBoundingClientRect().left - wrap.getBoundingClientRect().left + wrap.scrollLeft + span[0] * pxPerMm;
    wrap.scrollTo({ left: Math.max(0, x - 40), behavior: "smooth" });
  }
}
$$(".zoom button").forEach(b => (b.onclick = () => {
  state.zoom = b.dataset.zoom;
  $$(".zoom button").forEach(x => x.setAttribute("aria-pressed", x === b));
  layoutPreview();
}));
window.addEventListener("resize", layoutPreview);

// ---------------------------------------------------------------- print
$("#download").onclick = () => {
  if (!state.render) return;
  el("a", { href: `data:image/png;base64,${state.render.png}`, download: "label.png" }).click();
};
$("#print").onclick = () => {
  const r = state.render;
  if (!r || state.pending || r.job !== JSON.stringify(currentJob())) return;
  const n = state.job.labels.length, c = state.job.copies ?? 1;
  $("#confirm-text").textContent = c > 1
    ? `Print ${c} copies of ${plural(n, "label")} (${r.labels} total), about ${r.length_mm} mm of ${r.tape_mm} mm tape?`
    : `Print ${plural(n, "label")}, about ${r.length_mm} mm of ${r.tape_mm} mm tape?`;
  const loaded = state.printer?.tape_mm, w = $("#confirm-warn");
  w.hidden = !(loaded && loaded !== r.tape_mm);
  w.textContent = `Printer has ${loaded} mm tape loaded; this is laid out for ${r.tape_mm} mm.`;
  $("#confirm").hidden = false; $("#confirm-go").focus();
};
$("#confirm-cancel").onclick = () => { $("#confirm").hidden = true; $("#print").focus(); };
document.addEventListener("keydown", e => { if (e.key === "Escape" && !$("#confirm").hidden) $("#confirm-cancel").click(); });
$("#confirm-go").onclick = async () => {
  $("#confirm").hidden = true;
  const sent = state.render.job;
  if (sent !== JSON.stringify(currentJob())) return toast("The label changed; check the preview and print again.", { bad: true });
  const btn = $("#print"); btn.disabled = true; btn.textContent = "Printing…";
  try {
    const r = await api(P("/print"), { method: "POST", body: sent });
    toast(`Printed ${plural(r.labels, "label")} (${r.length_mm} mm)`);
  } catch (e) { toast(`Print failed: ${e.message}`, { bad: true }); }
  finally { syncPrintButton(); refreshPrinter(); }
};

// ---------------------------------------------------------------- describe (Claude)
let aiTemplate = null, aiBusy = false;
async function* sse(path, body) {
  const headers = { "content-type": "application/json" };
  const tok = localStorage.getItem(TOKEN_KEY);
  if (tok) headers.authorization = `Bearer ${tok}`;
  const r = await fetch(path, { method: "POST", headers, body: JSON.stringify(body) });
  if (!r.ok) throw new Error(errText(await r.json().catch(() => null)) || `${r.status} ${r.statusText}`);
  const reader = r.body.getReader(), dec = new TextDecoder();
  let buf = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf("\n\n")) >= 0) {
      const chunk = buf.slice(0, i); buf = buf.slice(i + 2);
      const line = chunk.split("\n").find(l => l.startsWith("data: "));
      if (line) yield JSON.parse(line.slice(6));
    }
  }
}
$("#ai").onsubmit = async e => {
  e.preventDefault();
  const prompt = $("#ai-prompt").value.trim();
  if (!prompt || aiBusy) return;
  aiBusy = true;
  const go = $("#ai-go"), status = $("#ai-status"), draft = $("#ai-draft");
  go.disabled = true; go.textContent = "Designing…";
  $("#ai-result").hidden = true; draft.hidden = true; status.className = "ai-status";
  const tape = state.printer?.tape_mm ?? state.job.tape_mm ?? null;
  let finished = false;
  try {
    for await (const ev of sse("/design", { prompt, job: currentJob(), tape_mm: tape, printer: state.printerKey })) {
      if (ev.type === "status") status.textContent = ev.text;
      else if (ev.type === "preview") { draft.src = `data:image/png;base64,${ev.preview_png_base64}`; draft.hidden = false; }
      else if (ev.type === "error") { status.textContent = ev.msg; status.className = "ai-status bad"; finished = true; }
      else if (ev.type === "done") {
        finished = true;
        const before = structuredClone(state.job), beforeSel = { ...state.sel };
        state.job = ev.job; state.sel = { label: 0, item: 0 };
        state.source = `Designed by Claude · ${plural(ev.labels, "label")}`;
        edited(true, true);
        draft.hidden = true;
        status.textContent = `Done · ${ev.length_mm} mm`;
        $("#ai-summary").textContent = ev.summary;
        aiTemplate = ev.template || null;
        const sb = $("#ai-save-tpl");
        sb.hidden = !aiTemplate;
        if (aiTemplate) sb.textContent = `Save template “${aiTemplate.name}”`;
        $("#ai-result").hidden = false;
        $("#ai-prompt").value = "";
        toast("Loaded Claude’s design", { action: { label: "Undo", fn: () => {
          state.job = before; state.sel = beforeSel; state.source = null; edited(true);
        } } });
      }
    }
    if (!finished) { status.textContent = "The connection closed before the design finished."; status.className = "ai-status bad"; }
  } catch (err) {
    status.textContent = err instanceof TypeError ? "Can't reach the ptlabel server." : err.message;
    status.className = "ai-status bad";
  } finally { aiBusy = false; go.disabled = false; go.textContent = "Design"; }
};
$("#ai-prompt").onkeydown = e => {
  if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) { e.preventDefault(); $("#ai").requestSubmit(); }
};
$("#ai-save-tpl").onclick = async () => {
  if (!aiTemplate) return;
  try {
    await api(`/templates/${aiTemplate.name}`, { method: "PUT", body: JSON.stringify(aiTemplate) });
    toast(`Saved template “${aiTemplate.name}”`); state.thumbs = {};
    $("#ai-save-tpl").hidden = true;
  } catch (e) { toast(e.message, { bad: true }); }
};

// ---------------------------------------------------------------- printer
const STATE_WORDS = { idle: "ready", printing: "printing", warmup: "warming up", other: "busy", unknown: "unknown" };
async function refreshPrinter() {
  const box = $("#printer");
  $("#printer-retry").hidden = true;
  try {
    const key = state.printerKey;
    const s = await api(P("/printer/status"));
    if (key !== state.printerKey) return;     // switched printers while waiting
    state.printer = s;
    box.className = "printer " + (s.ok ? "ok" : "warn");
    const words = s.problems?.length ? s.problems.join(", ") : (STATE_WORDS[s.state] ?? s.state);
    $("#printer-text").textContent = s.tape_mm ? `${s.tape_mm} mm tape · ${words}` : words;
  } catch (e) {
    state.printer = null;
    box.className = "printer bad";
    $("#printer-text").textContent = e instanceof NetError ? "Can't reach ptlabel server"
      : printerInfo().connection === "USB" ? "Not connected" : "Printer offline";
    $("#printer").title = e.message;
    $("#printer-retry").hidden = false;
  }
  updateLoadedOption(); renderStats();
}
$("#printer-retry").onclick = refreshPrinter;
setInterval(() => { if (document.visibilityState === "visible") refreshPrinter(); }, 10000);
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") refreshPrinter(); });

// ---------------------------------------------------------------- boot
(async () => {
  try { const d = JSON.parse(localStorage.getItem(DRAFT_KEY)); if (d?.labels?.length) state.job = d; } catch {}
  try { state.meta = await api("/meta"); } catch (e) { toast(e.message, { bad: true }); }
  const ps = state.meta.printers || [];
  let saved = null; try { saved = localStorage.getItem(PRINTER_KEY); } catch {}
  state.printerKey = ps.some(p => p.key === saved) ? saved : ps[0]?.key ?? null;
  const pick = $("#printer-pick");
  pick.replaceChildren(...ps.map(p => el("option", { value: p.key }, `${p.name} · ${p.connection === "USB" ? "USB" : "network"}`)));
  pick.value = state.printerKey ?? "";
  pick.hidden = ps.length < 2;
  pick.onchange = () => {
    state.printerKey = pick.value;
    try { localStorage.setItem(PRINTER_KEY, pick.value); } catch {}
    state.printer = null; state.thumbs = {};
    $("#printer-text").textContent = "Checking printer…"; $("#printer").className = "printer";
    const p = printerInfo();
    if (state.job.tape_mm && p.tape_widths && !p.tape_widths.includes(state.job.tape_mm)) delete state.job.tape_mm;
    if (p.half_cut === false && state.job.separator === "half_cut") delete state.job.separator;
    if (p.hires === false) delete state.job.hires;
    renderDesign(); renderActionbar(); edited(); refreshPrinter();
    if (state.tab === "template") loadTemplates().catch(() => {});
  };
  if (state.meta.design) {
    $("#ai").hidden = false;
    $("#ai-model").textContent = "with Claude · ⌘↵";
  }
  renderDesign();
  edited(false, true);
  refreshPrinter();
})();
