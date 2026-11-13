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
         ["italic", "Italic", "check"], ["underline", "Underline", "check"],
         ["size_mm", "Size (mm)", "number", { def: "fit tape", step: 0.5 }],
         ["align", "Line alignment", "select", { options: ["left", "center", "right"] }],
         ["max_width_mm", "Max width (mm)", "number", { def: "none" }]],
  qr: [["data", "Link or text", "text", { wide: true }],
       ["ecc", "Damage tolerance", "select", { options: ["L", "M", "Q", "H"], default: "M",
          labels: { L: "Low (smallest)", M: "Medium", Q: "High", H: "Highest" } }],
       ["height_pct", "Height (%)", "number", { def: "100" }]],
  datamatrix: [["data", "Data", "text", { wide: true }],
               ["gs1", "GS1 (Application Identifiers)", "check", { help: "Write data as (01)09501101530003(17)261231(10)LOT7(21)SN42 — checked and encoded with FNC1." }],
               ["height_pct", "Height (%)", "number", { def: "100" }]],
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
const CUT_NAMES = { half_cut: "half-cut", full_cut: "full cut", none: "no cut" };
const jobDefaultCut = () => state.job.separator ?? (printerInfo().half_cut === false ? "full_cut" : "half_cut");
const labelFields = () => [
  ["justify", "Arrange items", "select", { wide: true, options: ["center", "start", "end", "between"],
     labels: { center: "Centre", start: "Left", end: "Right", between: "Spread out (first at start, last at end)" },
     help: label().length_mm ? null : "Left, Right and Spread out only differ once the label has a fixed length." }],
  ...(() => {
    const last = state.sel.label === state.job.labels.length - 1, copies = state.job.copies ?? 1;
    if (last) return [["cut_after", "Cut after this label", "select", { wide: true, disabled: true,
      options: ["__end"], labels: { __end: copies > 1 ? `Between copies: ${CUT_NAMES[copyCut()]} (set under Job)`
        : state.job.chain ? "No cut — Job: don't cut after the last label" : "Full cut — end of the job" },
      help: copies > 1 ? "The last label of the set is followed by the next copy; choose that cut under Job."
                       : "The last label's cut is the end of the job (set under Job)." }]];
    return [["cut_after", "Cut after this label", "select", { wide: true,
      options: ["", "half_cut", "full_cut", "none"],
      labels: { "": `Job default (${CUT_NAMES[jobDefaultCut()]})`, half_cut: "Half-cut — same strip",
                full_cut: "Full cut — separate piece", none: "No cut — runs into the next" },
      help: "A full cut starts a new row in the preview." }]];
  })(),
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
    ...((state.job.copies ?? 1) > 1 ? [["copy_cut", `Between the ${state.job.copies} copies`, "select", { wide: true,
       options: ["", ...seps], labels: { "": `Same as between labels (${CUT_NAMES[jobDefaultCut()]})`, ...SEP_LABELS } }]] : []),
    ["separator", "Between labels", "select", { wide: true, options: ["", ...seps],
       labels: { "": `Printer default (${def.replace("_", "-")})`, ...SEP_LABELS } }],
    ...(p.half_cut === false ? [] : [["full_cut_every", "Also full-cut every … labels", "number", { def: "only at end", min: 1, wide: true,
         help: "Counts every label printed, across copies." }]]),
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
  saved: { id: null, name: null },   // saved-label record this job came from
  savedSnap: null,                   // that record's job JSON, to detect unsaved changes
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
  if (it.type === "text") return it.text.split("\n").map(x => x.trim()).filter(Boolean).join(" · ") || "(empty)";
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
function toast(msg, { bad = false, action = null, sticky = bad, ms = null } = {}) {
  const t = $("#toast");
  $("#toast-text").textContent = msg;
  t.className = "toast" + (bad ? " bad" : "");
  t.setAttribute("role", bad ? "alert" : "status");
  const a = $("#toast-action"); a.hidden = !action;
  if (action) { a.textContent = action.label; a.onclick = () => { t.hidden = true; action.fn(); }; }
  $("#toast-close").hidden = !(sticky || bad);
  t.hidden = false;
  clearTimeout(toastTimer);
  if (!sticky) toastTimer = setTimeout(() => (t.hidden = true), ms ?? (action ? 10000 : 4000));
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
  const a = document.activeElement, k = a?.dataset?.k;
  let sel = null;
  try { sel = a && "selectionStart" in a ? [a.selectionStart, a.selectionEnd] : null; } catch {}
  fn();
  if (!k) return;
  const target = $(`[data-k="${k}"]`);
  if (target && !target.disabled) {
    target.focus({ preventScroll: true });
    if (sel && sel[0] != null) try { target.setSelectionRange(sel[0], sel[1]); } catch {}
  } else $(`[data-k="${k.replace(/:.*/, "")}"]`)?.focus();
}

// ---------------------------------------------------------------- form fields
function field(obj, [key, labelText, kind, opt = {}], onchange) {
  const id = `f${++uid}`;
  const dk = `fld:${key}`;
  const cls = "field" + (opt.wide ? " wide" : "") + (kind === "check" ? " check" : "");
  const set = v => { if (v === "" || v === undefined) delete obj[key]; else obj[key] = v; onchange(); };
  const help = opt.help ? el("small", { class: "help" }, opt.help) : null;
  let input;
  if (kind === "check") {
    input = el("input", { type: "checkbox", id, "data-k": dk }); input.checked = obj[key] ?? opt.default ?? false;
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
    const fontLabel = o => o.split("-").map(w => w[0].toUpperCase() + w.slice(1)).join(" ");   // sans-bold → Sans Bold
    for (const o of options) input.append(el("option", { value: o }, kind === "font" ? fontLabel(o) : opt.labels?.[o] ?? o));
    input.value = kind === "font" ? (state.meta.font_aliases?.[obj[key]] ?? obj[key] ?? "sans")
                                  : obj[key] ?? opt.default ?? options[0];
    if (opt.disabled || input.selectedIndex < 0) input.value = options[0];   // e.g. the synthetic "end of job" value
    if (opt.disabled) input.disabled = true;
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
  input.dataset.k = dk;
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
      refreshSummaries(); edited(f[0] === "symbology" || f[0] === "gs1");
    }));
  }
  const ls = $("#layout-section");
  if (state.job.labels.length * (state.job.copies ?? 1) > 1 && !state.layoutClosedByUser) ls.open = true;
  const lf = $("#label-form"); lf.replaceChildren();
  for (const f of labelFields()) lf.append(field(label(), f, () => { notes(); edited(f[0] === "length_mm" || f[0] === "cut_after"); }));
  const jf = $("#job-form"); jf.replaceChildren();
  for (const f of jobFields()) jf.append(field(state.job, f, () => {
    notes(); edited(f[0] === "separator" || f[0] === "full_cut_every" || f[0] === "chain" || f[0] === "copy_cut");
  }));
  notes();
  renderStrip();
  renderActionbar();
}
function notes() {
  const l = label(), i = state.sel.label, last = i === state.job.labels.length - 1;
  const arr = { center: "Centred", start: "Left", end: "Right", between: "Spread out" }[l.justify ?? "center"];
  const cut = last ? null : l.cut_after ? `${CUT_NAMES[l.cut_after]} after` : null;
  $("#layout-label-name").textContent = state.job.labels.length > 1 ? `· ${labelName(l, i)}` : "";
  $("#layout-note").textContent = [arr, l.length_mm ? `${l.length_mm} mm long` : "fits content", cut].filter(Boolean).join(" · ");
  const mixed = state.job.labels.some((_, j) => { const c = effectiveCut(j); return c && c !== jobDefaultCut(); });
  const sep = mixed ? "mixed cuts" : { half_cut: "half-cuts", full_cut: "full cuts", none: "no cuts" }[jobDefaultCut()];
  const n = state.job.labels.length;
  queueMicrotask(() => ($("#job-btn").title = `Job settings: ${$("#cut-note").textContent}. Cuts, quality and other settings for the whole print.`));
  $("#cut-note-short").textContent = n > 1 ? `· ${{ half_cut: "half", full_cut: "full", none: "none" }[jobDefaultCut()]}` : "";
  $("#cut-note").textContent = n > 1
    ? `${mixed ? "mixed cuts" : `${sep} between labels`}${state.job.full_cut_every ? `, full every ${state.job.full_cut_every}` : ""}${state.job.hires ? " · sharp" : ""}`
    : `${state.job.chain ? "no cut at end (chain)" : "full cut at end"}${state.job.hires ? " · sharp" : ""}`;
}
/** The cut after label i within one set: its own setting, else full_cut_every, else the job default. */
const copyCut = () => state.job.copy_cut ?? jobDefaultCut();
function effectiveCut(i) {
  const l = state.job.labels[i];
  if (i >= state.job.labels.length - 1) return (state.job.copies ?? 1) > 1 ? copyCut() : null;
  if (l.cut_after) return l.cut_after;
  if (state.job.full_cut_every && (i + 1) % state.job.full_cut_every === 0) return "full_cut";
  return jobDefaultCut();
}
function refreshSummaries() {
  $$("#items li .sum").forEach((s, i) => (s.textContent = summary(label().items[i])));
  if (state.job.labels.length > 1) $("#label-name").textContent = `· ${labelName(label(), state.sel.label).replace(/^\d+ · /, "")}`;
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

function renderStrip() {           // the bar above the preview + the clickable boxes on it
  const n = state.job.labels.length, i = state.sel.label;
  $("#label-pos").textContent = n > 1 ? `Label ${i + 1} of ${n}` : "1 label";
  $("#label-name").textContent = n > 1 ? `· ${labelName(state.job.labels[i], i).replace(/^\d+ · /, "")}` : "";
  $("#label-hint").hidden = n < 2;
  $("#del-label").disabled = n < 2;
  renderBoxes();
}

/** A box over each label in the preview (positions from the server's label_boxes, in % of the
 *  image, so they follow zoom). The box's main button selects/drags; ⧉ and × duplicate and delete.
 *  Only the first set is interactive when copies repeat. */
function renderBoxes() {
  const wrap = $("#lboxes"), r = state.render, n = state.job.labels.length;
  const focused = document.activeElement?.closest?.("#lboxes") ? document.activeElement.dataset.k : null;
  wrap.replaceChildren();
  if (!r?.boxes || r.boxes.length < 1 || n < 1) return;
  r.boxes.forEach((b, j) => {
    const i = j % n, primary = j < n;
    const name = labelName(state.job.labels[i] ?? state.job.labels[0], i);
    const pick = el("button", {
      class: "lsel", "data-k": primary ? `lbox-${i}` : null,
      tabindex: primary && i === state.sel.label ? 0 : -1, "aria-hidden": primary ? null : "true",
      "aria-pressed": i === state.sel.label, "aria-label": `Label ${name}`,
      title: n > 1 ? `${name}\nClick to select · drag to reorder (drop below a row for a new piece) · Delete removes` : name,
      onclick: e => { if (boxDrag.justDropped) { e.preventDefault(); return; } selectLabel(i); },
      onkeydown: e => boxKey(e, i),
    });
    const acts = primary ? el("span", { class: "lacts" },
      el("button", { class: "lact", "data-k": `lcopy-${i}`, tabindex: -1, title: "Duplicate this label",
                     "aria-label": `Duplicate label ${name}`, onclick: e => { e.stopPropagation(); duplicateLabel(i); } }, "⧉"),
      n > 1 ? el("button", { class: "lact del", "data-k": `ldel-${i}`, tabindex: -1, title: "Delete this label",
                             "aria-label": `Delete label ${name}`, onclick: e => { e.stopPropagation(); deleteLabel(i); } }, "×") : null) : null;
    const box = el("div", {
      class: "lbox" + (i === state.sel.label ? " current" : "") + (primary ? "" : " copy"), "data-i": i,
      "data-row": b[1], style: `left:${b[0] * 100}%;top:${b[1] * 100}%;width:${(b[2] - b[0]) * 100}%;height:${(b[3] - b[1]) * 100}%`,
    }, pick, acts);
    if (primary && n > 1) pick.addEventListener("pointerdown", e => startBoxDrag(e, i, box));
    wrap.append(box);
  });
  if (focused) $(`#lboxes [data-k="${focused}"]`)?.focus({ preventScroll: true });
}

function selectLabel(i, { focus = true } = {}) {
  state.sel = { label: i, item: 0 }; selectTab("design"); renderDesign();
  if (focus) focusChip(i);
  highlightLabel(true);
}

function focusChip(i) {
  const go = () => $(`#lboxes [data-k="lbox-${i}"]`)?.focus({ preventScroll: true });
  go(); requestAnimationFrame(go);
}

// ---- cuts between labels as a list of gaps (gap k = the cut after label k)
const STRENGTH = { none: 0, half_cut: 1, full_cut: 2 };
const gapsNow = () => state.job.labels.slice(0, -1).map((_, k) => effectiveCut(k) ?? jobDefaultCut());
/** Write gaps back to the labels (null where it's the job default); the last label's cut is the job's end. */
function applyGaps(gaps) {
  const def = jobDefaultCut(), labels = state.job.labels;
  labels.forEach((l, k) => {
    const g = gaps[k];
    if (k === labels.length - 1 || g == null || (g === def && !(state.job.full_cut_every && g !== "full_cut"))) delete l.cut_after;
    else l.cut_after = g;
  });
}
/** Gaps after removing label i: its two gaps merge into the stronger one (a row break survives). */
function gapsWithout(gaps, i) {
  const g = [...gaps];
  if (i === 0) g.splice(0, 1);
  else if (i >= g.length) g.splice(g.length - 1, 1);
  else g.splice(i - 1, 2, STRENGTH[g[i - 1]] >= STRENGTH[g[i]] ? g[i - 1] : g[i]);
  return g;
}
const halfOrDefault = () => (jobDefaultCut() === "full_cut" ? "half_cut" : jobDefaultCut());

/**
 * Move label `from` next to an anchor, inferring cuts from where it's dropped:
 *  mode "after"   -> anchor, half-cut, moved, (anchor's old gap)       join the anchor's row
 *  mode "before"  -> (anchor's old leading gap), moved, half-cut, anchor
 *  mode "newline" -> anchor, FULL cut, moved, (anchor's old gap)       a new piece after the anchor
 *  mode "swap"    -> keyboard Alt+arrows: labels move, the row layout (gaps) stays as it is
 * `anchor` is the anchor's index before the move.
 */
function moveLabel(from, anchor, { mode = "swap", announce = true } = {}) {
  const labels = state.job.labels, n = labels.length;
  const before = structuredClone(labels), beforeSel = { ...state.sel };
  let gaps = gapsNow(), to;
  if (mode === "swap") {
    to = Math.max(0, Math.min(n - 1, anchor));
    if (to === from) return;
    const [l] = labels.splice(from, 1); labels.splice(to, 0, l);
  } else {
    if (anchor === from) return;
    gaps = gapsWithout(gaps, from);
    const [l] = labels.splice(from, 1);
    const a = anchor > from ? anchor - 1 : anchor;          // anchor's index after removal
    if (mode === "before") {
      to = a; labels.splice(to, 0, l);
      gaps.splice(to, 0, halfOrDefault());                 // moved -> anchor; gap before moved = anchor's old leading gap
    } else {
      to = a + 1; labels.splice(to, 0, l);
      const old = gaps[a];                                 // anchor's old gap (undefined if anchor was last)
      gaps.splice(a, old === undefined ? 0 : 1, mode === "newline" ? "full_cut" : halfOrDefault(), ...(old === undefined ? [] : [old]));
    }
  }
  applyGaps(gaps);
  state.sel = { label: to, item: 0 };
  edited(true);
  focusChip(to);
  if (announce) toast(mode === "newline" ? "Moved label to a new piece (full cut before it)"
                    : mode === "swap" ? `Moved label to position ${to + 1}` : `Moved label to position ${to + 1}, same piece`,
    { action: { label: "Undo", fn: () => { state.job.labels = before; state.sel = beforeSel; edited(true); focusChip(from); } } });
}

function deleteLabel(i) {
  if (state.job.labels.length < 2) return;
  const before = structuredClone(state.job.labels), beforeSel = { ...state.sel };
  const name = labelName(state.job.labels[i], i);
  const gaps = gapsWithout(gapsNow(), i);
  state.job.labels.splice(i, 1);
  applyGaps(gaps);
  const sel = state.sel.label;
  state.sel = { label: sel > i ? sel - 1 : Math.min(sel, state.job.labels.length - 1), item: 0 };
  edited(true);
  focusChip(state.sel.label);
  toast(`Deleted label ${name}`, { action: { label: "Undo", fn: () => {
    state.job.labels = before; state.sel = beforeSel; edited(true); focusChip(i);
  } } });
}

function duplicateLabel(i) {
  const gaps = gapsNow(), labels = state.job.labels;
  labels.splice(i + 1, 0, structuredClone(labels[i]));
  gaps.splice(i, 0, halfOrDefault());          // i, half-cut, copy, (i's old gap)
  applyGaps(gaps);
  state.sel = { label: i + 1, item: 0 };
  edited(true); focusChip(i + 1);
}

function boxKey(e, i) {
  const n = state.job.labels.length;
  const step = { ArrowLeft: -1, ArrowUp: -1, ArrowRight: 1, ArrowDown: 1 }[e.key];
  if (e.altKey && step) { e.preventDefault(); moveLabel(i, i + step, { mode: "swap" }); }
  else if (e.altKey && (e.key === "Home" || e.key === "End")) { e.preventDefault(); moveLabel(i, e.key === "Home" ? 0 : n - 1, { mode: "swap" }); }
  else if ((e.key === "Delete" || e.key === "Backspace") && n > 1) { e.preventDefault(); deleteLabel(i); }
  else if (step) { e.preventDefault(); selectLabel(Math.max(0, Math.min(n - 1, i + step))); }
  else if (e.key === "Home" || e.key === "End") { e.preventDefault(); selectLabel(e.key === "Home" ? 0 : n - 1); }
}

// Drag a label in the preview. Beside a label = join that row (half-cut); in the space below a
// row = start a new piece there (full cut). Mouse/pen start after 6 px; touch: press and hold.
const boxDrag = { justDropped: false };
function startBoxDrag(e, from, box) {
  if (e.button !== 0) return;
  const pane = $("#preview-wrap"), touch = e.pointerType === "touch", pick = e.currentTarget;
  const x0 = e.clientX, y0 = e.clientY, sx = pane.scrollLeft, sy = pane.scrollTop;
  let armed = !touch, dragging = false, drop = null;
  pick.setPointerCapture(e.pointerId);
  const hold = touch ? setTimeout(() => { armed = true; box.classList.add("lifted"); navigator.vibrate?.(10); }, 300) : null;
  const others = () => [...$$("#lboxes .lbox:not(.copy)")].filter(b => b !== box);
  const marker = $("#newline-marker") ?? $("#lboxes").appendChild(el("div", { id: "newline-marker", hidden: true }));
  const clear = () => { others().forEach(b => b.classList.remove("drop-before", "drop-after")); marker.hidden = true; };
  const move = ev => {
    const dx = ev.clientX - x0, dy = ev.clientY - y0;
    if (!armed) {
      if (Math.hypot(dx, dy) > 8) { clearTimeout(hold); pane.scrollLeft = sx - dx; pane.scrollTop = sy - dy; }
      return;
    }
    if (!dragging) {
      if (Math.hypot(dx, dy) < 6 && !touch) return;
      dragging = true; box.classList.add("dragging"); document.body.classList.add("dragging-label");
    }
    box.style.transform = `translate(${dx}px, ${dy}px)`;
    clear(); drop = null;
    const rest = others(); if (!rest.length) return;
    // rows (top -> bottom) from the other boxes
    const rows = [];
    for (const b of rest) {
      const r = b.getBoundingClientRect(), row = rows.find(x => Math.abs(x.top - r.top) < 2);
      if (row) { row.boxes.push(b); row.bottom = Math.max(row.bottom, r.bottom); } else rows.push({ top: r.top, bottom: r.bottom, boxes: [b] });
    }
    rows.sort((p, q) => p.top - q.top);
    // below a row (and above the next one): a new piece after that row's last label
    for (let k = 0; k < rows.length; k++) {
      const row = rows[k], nextTop = rows[k + 1]?.top ?? Infinity;
      if (ev.clientY > row.bottom + 2 && ev.clientY < nextTop - 2) {
        const lastBox = row.boxes.at(-1), bi = +lastBox.dataset.i;
        const lr = $("#lboxes").getBoundingClientRect(), br = lastBox.getBoundingClientRect();
        Object.assign(marker.style, { left: `${row.boxes[0].getBoundingClientRect().left - lr.left}px`,
          top: `${row.bottom - lr.top + 6}px`, width: `${Math.max(60, box.getBoundingClientRect().width)}px`,
          height: `${br.height}px` });
        marker.hidden = false;
        drop = { anchor: bi, mode: "newline" };
        return;
      }
    }
    // otherwise beside the nearest label: join its row
    let best = null, bestD = Infinity;
    for (const b of rest) {
      const r = b.getBoundingClientRect();
      const d = Math.hypot(Math.max(r.left - ev.clientX, 0, ev.clientX - r.right), Math.max(r.top - ev.clientY, 0, ev.clientY - r.bottom));
      if (d < bestD) { bestD = d; best = b; }
    }
    const r = best.getBoundingClientRect(), bi = +best.dataset.i, after = ev.clientX > r.left + r.width / 2;
    best.classList.add(after ? "drop-after" : "drop-before");
    drop = { anchor: bi, mode: after ? "after" : "before" };
  };
  const end = ev => {
    clearTimeout(hold);
    pick.removeEventListener("pointermove", move); pick.removeEventListener("pointerup", end);
    pick.removeEventListener("pointercancel", end);
    box.classList.remove("lifted", "dragging"); box.style.transform = "";
    document.body.classList.remove("dragging-label"); clear();
    if (dragging) {
      boxDrag.justDropped = true; setTimeout(() => (boxDrag.justDropped = false), 0);
      if (ev.type === "pointerup" && drop) moveLabel(from, drop.anchor, { mode: drop.mode });
    }
  };
  pick.addEventListener("pointermove", move);
  pick.addEventListener("pointerup", end);
  pick.addEventListener("pointercancel", end);
}
$("#del-label").onclick = () => deleteLabel(state.sel.label);
// a native image drag would cancel the pointer gesture used to reorder labels
$("#tape").addEventListener("dragstart", e => e.preventDefault());
$("#dup-label").onclick = () => duplicateLabel(state.sel.label);
$("#add-label").onclick = () => {
  state.job.labels.push({ items: [structuredClone(ITEM_DEFAULTS.text)] });
  state.sel = { label: state.job.labels.length - 1, item: 0 }; edited(true); focusChip(state.sel.label);
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
  if (n >= 1 && n <= 100) {
    const crossed = (n > 1) !== ((state.job.copies ?? 1) > 1);    // "Cut after each copy" appears/disappears
    if (n > 1) state.job.copies = n; else { delete state.job.copies; delete state.job.copy_cut; }
    edited(crossed);
  }
};
$("#copies").onchange = e => {
  const n = Math.max(1, Math.min(100, Math.round(+e.target.value || 1)));
  e.target.value = n;
  if (n > 1) state.job.copies = n; else { delete state.job.copies; delete state.job.copy_cut; }
  edited(true);
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
  if (empty.length && !problem) { out.className = "rowcheck bad"; out.textContent = `Fill in: ${empty.map(k => t.variables[k].description || k).join(", ")}`; }
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
    const undo = snapshot();
    if (mode === "replace") { state.job.labels = made; state.sel = { label: 0, item: 0 }; }
    else { state.job.labels.push(...made); state.sel = { label: state.job.labels.length - made.length, item: 0 }; }
    state.source = `From template “${t.name}”${rows.length ? ` · ${plural(rows.length, "row")}` : ""}`;
    selectTab("design"); edited(true, true);
    if (mode === "replace") setSaved(null);
    const ih = $("#items-h"); ih.tabIndex = -1; ih.focus({ preventScroll: true });
    toast(`${mode === "replace" ? "Loaded" : "Added"} ${plural(made.length, "label")}`, { action: { label: "Undo", fn: undo } });
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

// ---------------------------------------------------------------- saved-label link
// state.saved = the library record this job was opened from / saved as; state.savedSnap = its JSON
// at that moment, so we can tell when the job has unsaved changes.
function persistDraft() {
  try {
    localStorage.setItem(DRAFT_KEY, JSON.stringify(state.job));
    localStorage.setItem(DRAFT_KEY + ".saved", JSON.stringify({ ...state.saved, snap: state.savedSnap, source: state.source }));
  } catch {}
}
const isLinked = () => state.saved?.id != null;
const isDirty = () => isLinked() && state.savedSnap !== JSON.stringify(currentJob());
function setSaved(saved, snap) {
  state.saved = saved?.id != null ? { id: saved.id, name: saved.name } : { id: null, name: null };
  state.savedSnap = state.saved.id != null ? (snap ?? JSON.stringify(currentJob())) : null;
  persistDraft(); syncSaveForm(); renderStats();
  if (state.tab === "library") loadSaved();
}
/** Capture everything a job replacement changes; returns a function that puts it back. */
function snapshot() {
  const s = { job: structuredClone(state.job), sel: { ...state.sel }, saved: { ...state.saved },
              snap: state.savedSnap, source: state.source };
  return () => {
    state.job = s.job; state.sel = s.sel; state.source = s.source;
    edited(true, true); setSaved(s.saved, s.snap);
    const h = $("#items-h"); if (state.tab === "design") { h.tabIndex = -1; h.focus({ preventScroll: true }); }
  };
}

// ---------------------------------------------------------------- library
const lib = { saved: [], history: [], histLimit: 20, q: "" };
async function authImg(img, path) {
  try {
    const headers = {};
    const tok = localStorage.getItem(TOKEN_KEY);
    if (tok) headers.authorization = `Bearer ${tok}`;
    const r = await fetch(path, { headers });
    if (r.ok) img.src = URL.createObjectURL(await r.blob());
    else img.closest(".lib-thumb")?.remove();
  } catch {}
}
function ago(iso) {
  const s = (Date.now() - new Date(iso)) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  const d = new Date(iso);
  return d.toLocaleDateString(undefined, { month: "short", day: "numeric", year: d.getFullYear() === new Date().getFullYear() ? undefined : "numeric" });
}
function loadJob(job, { source = null, savedId = null, savedName = null, what = "Opened" } = {}) {
  const undo = snapshot();
  state.job = structuredClone(job); state.sel = { label: 0, item: 0 };
  state.source = source;
  selectTab("design"); edited(true, true);
  setSaved(savedId != null ? { id: savedId, name: savedName } : null);
  toast(`${what}`, { action: { label: "Undo", fn: undo }, ms: 15000 });
  const h = $("#items-h"); h.tabIndex = -1; h.focus({ preventScroll: true });
}
let formFor;   // saved id the name field was last filled for
function syncSaveForm() {
  const has = isLinked();
  if (formFor !== state.saved?.id) { $("#lib-name").value = has ? state.saved.name ?? "" : ""; formFor = state.saved?.id; }
  $("#lib-save-btn").textContent = has ? "Update" : "Save";
  $("#lib-save-new").hidden = !has;
  $("#lib-save-note").textContent = !has ? `${plural(state.job.labels.length, "label")} in this job, not saved yet.`
    : isDirty() ? `Unsaved changes to “${state.saved.name}”. Update overwrites it; Save as new keeps both.`
    : `Saved as “${state.saved.name}”. No changes since.`;
}
async function saveJob(asNew, { keepName = false } = {}) {
  const field = $("#lib-name");
  let name = keepName && isLinked() ? state.saved.name : field.value.trim();
  if (!name) { field.focus(); return toast("Give it a name first", { bad: true }); }
  if (asNew && isLinked() && name === state.saved.name) name = `${name} (copy)`;
  field.value = name;     // the server adds " (2)" etc. if another saved label has this name
  const tape = state.render?.tape_mm ? `&tape_mm=${state.render.tape_mm}` : "";
  const id = state.saved?.id, update = id != null && !asNew;
  try {
    const r = update
      ? await api(P(`/labels/${id}?x=1${tape}`), { method: "PUT", body: JSON.stringify({ name, job: currentJob() }) })
      : await api(P(`/labels?x=1${tape}`), { method: "POST", body: JSON.stringify({ name, job: currentJob(),
          origin: state.source?.startsWith("Designed by Claude") ? "claude" : state.source?.startsWith("From template") ? "template" : "manual" }) });
    formFor = r.id; field.value = r.name;
    setSaved({ id: r.id, name: r.name });
    toast((update ? `Updated “${r.name}”` : `Saved “${r.name}”`)
      + (r.name !== name ? ` (“${name}” was taken)` : ""));
    if (keepName) $("#print").focus();
  } catch (e) {
    if (update && /no saved label/.test(e.message)) {
      setSaved(null); field.value = name;
      toast(`“${state.saved?.name ?? name}” was deleted, so there's nothing to update. Press Save to keep this as a new label.`, { bad: true });
    } else toast(e.message, { bad: true });
  }
}
$("#lib-save").onsubmit = e => { e.preventDefault(); saveJob(false); };
$("#lib-save-new").onclick = () => saveJob(true);

async function loadSaved() {
  const ul = $("#lib-list");
  try { lib.saved = await api(`/labels${lib.q ? `?q=${encodeURIComponent(lib.q)}` : ""}`); }
  catch (e) { ul.replaceChildren(el("li", { class: "lib-empty" }, e.message)); return; }
  ul.replaceChildren();
  for (const l of lib.saved) {
    const img = el("img", { alt: "" });
    const current = l.id === state.saved?.id;
    const li = el("li", { class: "lib-item" + (current ? " current" : ""), "aria-current": current ? "true" : null },
      el("div", { class: "lib-thumb" }, img),
      el("div", { class: "lib-main" },
        el("div", { class: "lib-title", title: l.name }, l.name),
        el("div", { class: "lib-meta" }, `${plural(l.labels, "label")} · ${ago(l.updated_at)}`)),
      el("div", { class: "btn-row" },
        el("button", { class: "btn sm", "aria-label": `Open ${l.name}`, onclick: () => openSaved(l.id) }, "Open"),
        el("button", { class: "btn ghost sm", "aria-label": `Delete ${l.name}`, onclick: () => deleteSaved(l) }, "Delete")));
    ul.append(li);
    authImg(img, `/labels/${l.id}/thumb.png`);
  }
  if (!lib.saved.length) ul.append(el("li", { class: "lib-empty" }, lib.q ? "Nothing matches." : "Nothing saved yet."));
}
async function openSaved(id) {
  try {
    const l = await api(`/labels/${id}`);
    loadJob(l.job, { savedId: l.id, savedName: l.name, source: `Saved “${l.name}”`, what: `Opened “${l.name}”` });
  } catch (e) { toast(e.message, { bad: true }); }
}
async function deleteSaved(l) {
  const rows = [...$$("#lib-list .lib-item")];
  const idx = lib.saved.findIndex(x => x.id === l.id);
  const wasCurrent = state.saved?.id === l.id, snap = state.savedSnap;
  try {
    await api(`/labels/${l.id}`, { method: "DELETE" });
    if (wasCurrent) setSaved(null);
    await loadSaved();
    const next = $$("#lib-list .lib-item button")[Math.min(idx, $$("#lib-list .lib-item").length - 1) * 2];
    (next || $("#lib-q")).focus();
    toast(`Deleted “${l.name}”`, { action: { label: "Undo", fn: async () => {
      try {
        await api(`/labels/${l.id}/restore`, { method: "POST" });
        if (wasCurrent && !isLinked()) setSaved({ id: l.id, name: l.name }, snap);
        await loadSaved(); toast(`Restored “${l.name}”`);
      } catch (e) { toast(e.message, { bad: true }); }
    } } });
  } catch (e) { toast(e.message, { bad: true }); }
}
let qTimer;
$("#lib-q").oninput = e => { clearTimeout(qTimer); qTimer = setTimeout(() => { lib.q = e.target.value.trim(); loadSaved(); }, 200); };

async function loadHistory() {
  const ul = $("#hist-list");
  try {
    const [h, st] = await Promise.all([api(`/history?limit=${lib.histLimit + 1}`), api("/history/stats")]);
    lib.history = h.slice(0, lib.histLimit);
    $("#hist-more").hidden = h.length <= lib.histLimit;
    $("#hist-stats").textContent = st.prints
      ? `${plural(st.prints, "print")} · ${st.tape_used_mm >= 1000 ? `${(st.tape_used_mm / 1000).toFixed(2)} m` : `${Math.round(st.tape_used_mm)} mm`} of tape`
      : "";
  } catch (e) { ul.replaceChildren(el("li", { class: "lib-empty" }, e.message)); return; }
  ul.replaceChildren();
  // Consecutive identical failures (same job, printer and reason) collapse into one row.
  const groups = [];
  for (const h of lib.history) {
    const g = groups.at(-1);
    if (!h.ok && g && !g.h.ok && g.h.summary === h.summary && g.h.printer === h.printer && g.h.error === h.error) {
      g.n++; g.oldest = h.at;
    } else groups.push({ h, n: 1, oldest: h.at });
  }
  for (const { h, n, oldest } of groups) {
    const img = el("img", { alt: "" });
    const where = h.model ? h.model.replace("PT-", "") : "?";
    let main;
    if (h.ok) {
      main = el("div", { class: "lib-main" },
        el("div", { class: "lib-title" }, h.summary),
        el("div", { class: "lib-meta" },
          `${ago(h.at)} · ${where} · ${h.tape_mm} mm · ${plural(h.labels, "label")}${h.template ? ` · ${h.template}` : ""}${h.source === "reprint" ? " · reprint" : ""}`));
    } else {
      const when = n > 1 ? `${ago(oldest)} – ${ago(h.at)}` : ago(h.at);
      main = el("div", { class: "lib-main" },
        el("div", { class: "lib-title" }, h.summary),
        el("details", { class: "fail" },
          el("summary", {},
            el("span", { class: "chip bad" }, n > 1 ? `Failed × ${n}` : "Failed"),
            el("span", { class: "lib-meta" }, ` ${when} · ${where}`)),
          el("p", { class: "fail-why" }, h.error || "Unknown error")));
    }
    const li = el("li", { class: "lib-item" + (h.ok ? "" : " failed") },
      h.ok ? el("div", { class: "lib-thumb" }, img) : null,
      main,
      el("div", { class: "btn-row" },
        el("button", { class: "btn sm", "aria-label": `Open the print of ${h.summary}`, onclick: () => openPrint(h.id) }, "Open"),
        el("button", { class: "btn ghost sm", "aria-label": `${h.ok ? "Reprint" : "Try again"}: ${h.summary}`,
                       onclick: e => reprintFromHistory(h, e.currentTarget.closest(".lib-item")) }, h.ok ? "Reprint" : "Try again")));
    ul.append(li);
    if (h.ok) authImg(img, `/history/${h.id}/thumb.png`);
  }
  if (!lib.history.length) ul.append(el("li", { class: "lib-empty" }, "Nothing printed yet."));
}
$("#hist-more").onclick = () => { lib.histLimit += 20; loadHistory(); };
async function openPrint(id) {
  try {
    const h = await api(`/history/${id}`);
    loadJob(h.job, { source: `From a print on ${new Date(h.at).toLocaleString()}`, what: "Opened a past print" });
  } catch (e) { toast(e.message, { bad: true }); }
}
function reprintFromHistory(h, li) {
  // Inline confirm in the row; the job in the editor is left alone.
  $$(".reprint-confirm").forEach(x => x.remove());
  const p = printerInfo();
  const labels = h.labels ? plural(h.labels, "label") : "this job";
  const len = h.length_mm ? `, about ${Math.round(h.length_mm)} mm of tape` : "";
  const warn = !state.printerKey ? "No printer selected."
    : !state.printer ? `${p.name ?? "The printer"} isn't responding, so this will probably fail.`
    : h.tape_mm && state.printer.tape_mm && h.tape_mm !== state.printer.tape_mm
      ? `This was printed on ${h.tape_mm} mm tape; ${state.printer.tape_mm} mm is loaded.` : null;
  const box = el("div", { class: "reprint-confirm", role: "group", "aria-label": "Confirm reprint" },
    el("p", {}, `Print ${labels} again on ${p.name ?? "the printer"}${len}?`),
    warn ? el("p", { class: "confirm-warn" }, warn) : null,
    el("div", { class: "btn-row" },
      el("button", { class: "btn ghost sm", onclick: () => { box.remove(); li.querySelector(":scope > .btn-row .ghost")?.focus(); } }, "Cancel"),
      el("button", { class: "btn primary sm", onclick: async e => {
        const b = e.currentTarget; b.disabled = true; b.textContent = "Printing…";
        try {
          const r = await api(P(`/history/${h.id}/reprint`), { method: "POST" });
          toast(`Printed ${plural(r.labels, "label")} (${r.length_mm} mm)`);
        } catch (err) { toast(`Print failed: ${err.message}`, { bad: true, sticky: false, ms: 12000 }); }
        finally { refreshPrinter(); await loadHistory(); $("#hist-list .lib-item .ghost")?.focus(); }
      } }, "Print")));
  box.addEventListener("keydown", e => { if (e.key === "Escape") { e.stopPropagation(); box.querySelector(".ghost").click(); } });
  $("#toast").hidden = true;
  li.append(box);
  box.querySelector(".primary").focus();
}
function refreshLibrary() { syncSaveForm(); loadSaved(); loadHistory(); }

// ---------------------------------------------------------------- spec drawer
$("#new-job").onclick = () => {
  const undo = snapshot();
  state.job = { labels: [{ items: [structuredClone(ITEM_DEFAULTS.text)] }] };
  state.sel = { label: 0, item: 0 }; edited(true);
  setSaved(null);
  toast("Started a new job", { action: { label: "Undo", fn: undo } });
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
  const undo = snapshot();
  state.job = job; state.sel = { label: 0, item: 0 }; state.source = null;
  $("#spec").close(); edited(true);
  toast("Applied", { action: { label: "Undo", fn: undo } });
};
$("#json-copy").onclick = async () => { await navigator.clipboard.writeText($("#json").value); toast("Copied"); };

// ---------------------------------------------------------------- tabs
function selectTab(tab) {
  state.tab = tab;
  try { localStorage.setItem("ptlabel.tab", tab); } catch {}
  $$(".tabs button").forEach(b => {
    const on = b.dataset.tab === tab;
    b.setAttribute("aria-selected", on); b.tabIndex = on ? 0 : -1;
  });
  $$(".panel").forEach(p => (p.hidden = p.id !== `panel-${tab}`));
  if (tab === "template") loadTemplates().catch(e => toast(e.message, { bad: true }));
  if (tab === "library") refreshLibrary();
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
  persistDraft();
  if (state.tab === "library") syncSaveForm();
  if (rerender) renderDesign();
  state.pending = true; syncPrintButton();
  clearTimeout(timer); timer = setTimeout(refreshPreview, 200);
}
async function refreshPreview() {
  const my = ++seq;
  $("#preview").classList.add("busy");
  try {
    const r = await api(P("/render?format=json&frame=false&collapse_copies=true"), { method: "POST", body: JSON.stringify(currentJob()) });
    if (my !== seq) return;
    state.render = { png: r.preview_png_base64, tape_mm: r.tape_mm, labels: r.labels, length_mm: r.length_mm,
                     spans: r.label_spans_mm, boxes: r.label_boxes, copies: r.copies, pieces: r.pieces, copyCut: r.copy_cut, collapsed: r.preview_copies === 1 && r.copies > 1,
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
    if (my === seq) { $("#preview").classList.remove("busy"); state.pending = false; renderStats(); syncPrintButton(); renderBoxes(); }
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
  const phone = innerWidth <= 480;
  btn.textContent = state.pending ? (phone ? "Updating…" : "Updating preview…")
    : phone ? `Print${c > 1 ? ` ×${c}` : n > 1 ? ` ${n}` : ""}` : `Print ${plural(n, "label")}${c > 1 ? ` × ${c}` : ""}`;
}
function renderStats() {
  const r = state.render;
  const st = $("#stats");
  if (state.error) st.textContent = "Fix the problem above to print";
  else if (r) st.textContent = `${r.collapsed ? `${r.copies} copies · ` : ""}${plural(r.labels, "label")}${r.pieces > 1 ? ` in ${r.pieces} pieces` : ""} · ${r.length_mm} mm of ${r.tape_mm} mm tape`;
  const src = $("#source");
  const key = JSON.stringify([state.saved, isDirty(), state.source]);
  if (src.dataset.key === key) return;
  src.dataset.key = key;
  const parts = [];
  if (isLinked()) parts.push(`“${state.saved.name}”${isDirty() ? " · unsaved changes" : " · saved"}`);
  else if (state.source) parts.push(state.source);
  src.replaceChildren(parts.join(" · "));
  if (isDirty()) src.append(" ", el("button", { class: "link", onclick: () => saveJob(false, { keepName: true }) }, "Update"),
                           " · ", el("button", { class: "link", onclick: () => { selectTab("library"); $("#lib-name").focus(); } }, "Save as new…"));
  src.hidden = !parts.length;
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
  const actualH = (img.naturalHeight / 2) / DOTS_PER_MM * CSS_PX_PER_MM;
  const roomH = Math.max(120, $(".preview-box").clientHeight - 112);       // leave space for the ruler and zoom
  const scale = state.zoom === "fit" ? Math.max(0.1, Math.min(FIT_MAX, room / actual, roomH / actualH)) : +state.zoom;
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
  const badge = $("#copies-badge"), rr = state.render;
  badge.hidden = !rr?.collapsed;
  if (rr?.collapsed) {
    const between = { half_cut: "half-cut", full_cut: "full cut ✂", none: "no cut" }[rr.copyCut] ?? "";
    badge.textContent = `× ${rr.copies}${between ? ` · ${between} between` : ""}`;
    badge.title = `Showing one set; ${rr.copies} copies print, ${plural(rr.labels, "label")}, ${rr.length_mm} mm in all`;
  }
  highlightLabel();
}
function highlightLabel(scroll = false) {
  $$("#lboxes .lbox").forEach(b => {
    const on = +b.dataset.i === state.sel.label, pick = b.querySelector(".lsel");
    b.classList.toggle("current", on); pick?.setAttribute("aria-pressed", on);
    if (pick && !b.classList.contains("copy")) pick.tabIndex = on ? 0 : -1;
  });
  const cur = $("#lboxes .lbox.current:not(.copy)");
  if (scroll && cur) cur.scrollIntoView({ block: "nearest", inline: "nearest", behavior: "smooth" });
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
  const pn = printerInfo().name ?? "the printer";
  $("#confirm-text").textContent = c > 1
    ? `Print ${c} copies of ${plural(n, "label")} (${r.labels} total) on ${pn}, about ${r.length_mm} mm of ${r.tape_mm} mm tape?`
    : `Print ${plural(n, "label")} on ${pn}, about ${r.length_mm} mm of ${r.tape_mm} mm tape?`;
  const loaded = state.printer?.tape_mm, w = $("#confirm-warn");
  const warns = [];
  if (!state.printer) warns.push(`${pn} isn't responding, so this will probably fail.`);
  else if (state.printer.problems?.length) warns.push(`${pn} reports: ${state.printer.problems.join(", ")}.`);
  if (loaded && loaded !== r.tape_mm) warns.push(`It has ${loaded} mm tape loaded; this is laid out for ${r.tape_mm} mm.`);
  w.hidden = !warns.length; w.textContent = warns.join(" ");
  $("#toast").hidden = true;
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
    const q = new URLSearchParams();
    if (state.saved?.id != null) q.set("label_id", state.saved.id);
    const r = await api(P(`/print${q.size ? `?${q}` : ""}`), { method: "POST", body: sent });
    toast(`Printed ${plural(r.labels, "label")} (${r.length_mm} mm)`);
  } catch (e) { toast(`Print failed: ${e.message}`, { bad: true, sticky: false, ms: 12000 }); }
  finally { syncPrintButton(); refreshPrinter(); if (state.tab === "library") loadHistory(); $("#print").focus(); }
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
  status.textContent = "Starting…"; status.className = "ai-status";
  $("#ai-result").hidden = true; draft.hidden = true; status.className = "ai-status";
  const tape = state.printer?.tape_mm ?? state.job.tape_mm ?? null;
  let finished = false;
  try {
    for await (const ev of sse("/design", { prompt, job: $("#ai-fresh").checked ? null : currentJob(), tape_mm: tape, printer: state.printerKey })) {
      if (ev.type === "status") status.textContent = ev.text;
      else if (ev.type === "preview") { draft.src = `data:image/png;base64,${ev.preview_png_base64}`; draft.hidden = false; }
      else if (ev.type === "error") { status.textContent = ev.msg; status.className = "ai-status bad"; finished = true; }
      else if (ev.type === "done") {
        finished = true;
        const undo = snapshot();
        state.job = ev.job; state.sel = { label: 0, item: 0 };
        state.source = `Designed by Claude · ${plural(ev.labels, "label")}`;
        edited(true, true);
        setSaved(null);     // a new design; "Save" in the Library keeps it (the old record is untouched)
        draft.hidden = true;
        status.textContent = `Done · ${ev.length_mm} mm`;
        $("#ai-summary").textContent = ev.summary;
        aiTemplate = ev.template || null;
        const sb = $("#ai-save-tpl");
        sb.hidden = !aiTemplate;
        if (aiTemplate) sb.textContent = `Save template “${aiTemplate.name}”`;
        $("#ai-result").hidden = false;
        $("#ai-prompt").value = "";
        $("#ai-fresh").checked = false;
        toast("Loaded Claude’s design", { action: { label: "Undo", fn: () => {
          undo(); $("#ai-result").hidden = true; status.textContent = ""; } }, ms: 15000 });
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
async function refreshPrinter(fresh = false) {
  const box = $("#printer");
  try {
    const key = state.printerKey;
    const s = await api(P(fresh ? "/printer/status?fresh=true" : "/printer/status"));
    $("#printer-retry").hidden = true;
    if (key !== state.printerKey) return;     // switched printers while waiting
    state.printer = s;
    box.className = "printer " + (s.ok ? "ok" : "warn");
    const words = s.problems?.length ? s.problems.join(", ") : (STATE_WORDS[s.state] ?? s.state);
    $("#printer-text").textContent = s.tape_mm ? `${s.tape_mm} mm tape · ${words}` : words;
    $("#printer-short").textContent = s.problems?.length ? s.problems[0] : s.tape_mm ? `${s.tape_mm} mm` : words;
    $("#printer").title = `${printerInfo().name ?? "Printer"} (${printerInfo().connection ?? "?"}): ${$("#printer-text").textContent}`;
  } catch (e) {
    state.printer = null;
    box.className = "printer bad";
    const conn = printerInfo().connection;
    $("#printer-text").textContent = e instanceof NetError ? "Can't reach ptlabel server"
      : !state.printerKey ? "No printer" : conn === "USB" ? "Not connected" : `Offline · ${conn}`;
    $("#printer-short").textContent = e instanceof NetError ? "No server" : !state.printerKey ? "None"
      : conn === "USB" ? "Unplugged" : "Offline";
    $("#printer").title = e.message;
    $("#printer-retry").hidden = false;
  }
  updateLoadedOption(); renderStats();
}
$("#printer-retry").onclick = async () => {
  const b = $("#printer-retry"); b.disabled = true;
  $("#printer-text").textContent = "Checking…"; $("#printer-short").textContent = "Checking…";
  try { await refreshPrinter(true); } finally { b.disabled = false; }
};
let pollTick = 0;
setInterval(() => {                       // every 10 s; every 30 s while the printer isn't answering
  pollTick++;
  if (document.visibilityState === "visible" && (state.printer || pollTick % 3 === 0)) refreshPrinter();
}, 10000);
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") refreshPrinter(); });

// ---------------------------------------------------------------- printer picker
const FIND = "__find";
function fillPrinters(ps, initial = false) {
  state.meta.printers = ps;
  let saved = null; try { saved = localStorage.getItem(PRINTER_KEY); } catch {}
  const prev = state.printerKey;
  const want = initial ? saved : prev;
  state.printerKey = ps.some(p => p.key === want) ? want : ps.some(p => p.key === saved) ? saved : ps[0]?.key ?? null;
  const pick = $("#printer-pick");
  const opts = ps.map(p => el("option", { value: p.key },
    `${p.name}${ps.filter(q => q.name === p.name).length > 1 ? ` (${p.key})` : ""} · ${p.connection === "USB" ? "USB" : "network"}`));
  if (!ps.length) opts.push(el("option", { value: "" }, "No printers found"));
  opts.push(el("option", { value: FIND }, "Find printers…"));
  pick.replaceChildren(...opts);
  pick.value = state.printerKey ?? "";
  pick.hidden = false;
  if (!initial && prev !== state.printerKey) printerChanged();
}
function printerChanged() {
  try { if (state.printerKey) localStorage.setItem(PRINTER_KEY, state.printerKey); } catch {}
  state.printer = null; state.thumbs = {};
  $("#printer-text").textContent = "Checking printer…"; $("#printer").className = "printer";
  const p = printerInfo();
  if (state.job.tape_mm && p.tape_widths && !p.tape_widths.includes(state.job.tape_mm)) delete state.job.tape_mm;
  if (p.half_cut === false && state.job.separator === "half_cut") delete state.job.separator;
  if (p.hires === false) delete state.job.hires;
  renderDesign(); renderActionbar(); edited(); refreshPrinter();
  if (state.tab === "template") loadTemplates().catch(() => {});
}
async function findPrinters() {
  const pick = $("#printer-pick");
  pick.disabled = true;
  pick.options[pick.options.length - 1].textContent = "Searching USB and network…";
  $("#printer").className = "printer"; $("#printer-text").textContent = "Searching for printers…";
  $("#printer-retry").hidden = true;
  $("#printer-short").textContent = "Searching…";
  try {
    const r = await api("/printers/discover", { method: "POST" });
    const before = new Set((state.meta.printers || []).map(p => p.key));
    fillPrinters(r.printers);
    const added = r.printers.filter(p => !before.has(p.key)).length;
    const other = r.other_devices.map(d => d.product).join(", ");
    toast(`${added ? `Found ${plural(added, "new printer")}` : r.printers.length ? "No new printers found" : "No printers found"}`
      + (other ? ` · found but not supported: ${other}` : ""), { ms: 10000 });
  } catch (e) { toast(e.message, { bad: true }); fillPrinters(state.meta.printers || []); }
  finally { pick.disabled = false; refreshPrinter(); }
}
$("#printer-pick").onchange = e => {
  if (e.target.value === FIND) { e.target.value = state.printerKey ?? ""; findPrinters(); return; }
  state.printerKey = e.target.value || null;
  printerChanged();
};

// ---------------------------------------------------------------- mobile "⋯" menu
const moreBtn = $("#more-btn"), moreMenu = $("#more-menu");
function closeMore(focusBtn = false) {
  moreMenu.hidden = true; moreBtn.setAttribute("aria-expanded", "false");
  if (focusBtn) moreBtn.focus();
}
moreBtn.onclick = () => {
  const open = moreMenu.hidden;
  moreMenu.hidden = !open; moreBtn.setAttribute("aria-expanded", String(open));
  if (open) moreMenu.querySelector("button").focus();
};
moreMenu.addEventListener("keydown", e => {
  const items = [...moreMenu.querySelectorAll("button")], i = items.indexOf(document.activeElement);
  if (e.key === "Escape") { e.preventDefault(); closeMore(true); }
  else if (e.key === "ArrowDown") { e.preventDefault(); items[(i + 1) % items.length].focus(); }
  else if (e.key === "ArrowUp") { e.preventDefault(); items[(i - 1 + items.length) % items.length].focus(); }
});
document.addEventListener("click", e => { if (!moreMenu.hidden && !e.target.closest(".more")) closeMore(); });
moreMenu.onclick = e => {
  const act = e.target.closest("button")?.dataset.act; if (!act) return;
  closeMore();
  if (act === "new") $("#new-job").click();
  else if (act === "spec") $("#spec-open").click();
  else if (act === "find") findPrinters();
  else if (act === "retry") refreshPrinter(true);
};

// ---------------------------------------------------------------- resizable editor panel
const EDITOR_KEY = "ptlabel.editorWidth", EDITOR_DEFAULT = 380, EDITOR_MIN = 300;
const editorMax = () => Math.max(EDITOR_MIN, Math.min(900, window.innerWidth - 420));   // keep room for the preview
function setEditorWidth(px, save = true) {
  const w = Math.round(Math.max(EDITOR_MIN, Math.min(editorMax(), px)));
  document.documentElement.style.setProperty("--editor-w", `${w}px`);
  const r = $("#resizer");
  r.setAttribute("aria-valuenow", w); r.setAttribute("aria-valuemin", EDITOR_MIN); r.setAttribute("aria-valuemax", editorMax());
  if (save) try { localStorage.setItem(EDITOR_KEY, w); } catch {}
  layoutPreview();
}
{
  const r = $("#resizer");
  r.addEventListener("pointerdown", e => {
    e.preventDefault(); r.setPointerCapture(e.pointerId);
    r.classList.add("dragging"); document.body.classList.add("resizing");
    const left = $(".layout").getBoundingClientRect().left;
    const move = ev => setEditorWidth(ev.clientX - left, false);
    const up = ev => {
      r.releasePointerCapture(ev.pointerId); r.classList.remove("dragging"); document.body.classList.remove("resizing");
      r.removeEventListener("pointermove", move); r.removeEventListener("pointerup", up);
      setEditorWidth(ev.clientX - left);
    };
    r.addEventListener("pointermove", move); r.addEventListener("pointerup", up);
  });
  r.addEventListener("dblclick", () => setEditorWidth(EDITOR_DEFAULT));
  r.addEventListener("keydown", e => {
    const cur = $("#editor").getBoundingClientRect().width, step = e.shiftKey ? 80 : 20;
    const to = { ArrowLeft: cur - step, ArrowRight: cur + step, Home: EDITOR_MIN, End: editorMax() }[e.key];
    if (to != null) { e.preventDefault(); setEditorWidth(to); }
  });
  let saved = null; try { saved = +localStorage.getItem(EDITOR_KEY); } catch {}
  setEditorWidth(saved || EDITOR_DEFAULT, false);
  window.addEventListener("resize", () => {
    let want = EDITOR_DEFAULT; try { want = +localStorage.getItem(EDITOR_KEY) || EDITOR_DEFAULT; } catch {}
    setEditorWidth(want, false);
  });
}

let layoutClick = false;
$("#layout-section > summary").addEventListener("click", () => (layoutClick = true));
$("#layout-section").addEventListener("toggle", e => {
  const d = e.currentTarget, byUser = layoutClick;
  layoutClick = false;
  if (!byUser) return;                              // opened by the app: don't move the page
  state.layoutClosedByUser = !d.open;
  if (d.open) requestAnimationFrame(() => d.scrollIntoView({ block: "nearest", behavior: "smooth" }));
});

// ---------------------------------------------------------------- whole-job settings popover
{
  const btn = $("#job-btn"), pop = $("#job-pop");
  const close = (focus = false) => { pop.hidden = true; btn.setAttribute("aria-expanded", "false"); if (focus) btn.focus(); };
  btn.onclick = () => {
    const open = pop.hidden;
    pop.hidden = !open; btn.setAttribute("aria-expanded", String(open));
    if (open) { $("#toast").hidden = true; pop.querySelector("select, input")?.focus(); }
  };
  pop.addEventListener("keydown", e => { if (e.key === "Escape") { e.stopPropagation(); close(true); } });
  document.addEventListener("pointerdown", e => { if (!pop.hidden && !e.target.closest(".job-wrap")) close(); });
}

// ---------------------------------------------------------------- boot
(async () => {
  try { const d = JSON.parse(localStorage.getItem(DRAFT_KEY)); if (d?.labels?.length) state.job = d; } catch {}
  try {
    const sv = JSON.parse(localStorage.getItem(DRAFT_KEY + ".saved"));
    if (sv?.id != null) { state.saved = { id: sv.id, name: sv.name }; state.savedSnap = sv.snap ?? null; }
    if (sv?.source) state.source = sv.source;
  } catch {}
  try { state.meta = await api("/meta"); } catch (e) { toast(e.message, { bad: true }); }
  fillPrinters(state.meta.printers || [], true);
  setTimeout(async () => {      // the server sweeps the network in the background at startup
    try {
      const ps = await api("/printers");
      if (JSON.stringify(ps.map(p => p.key)) !== JSON.stringify((state.meta.printers || []).map(p => p.key))) fillPrinters(ps);
    } catch {}
  }, 3500);
  if (state.meta.design) {
    $("#ai").hidden = false;
    $("#ai-model").textContent = "with Claude · ⌘↵";
  }
  syncSaveForm();
  if (isLinked()) loadSaved();        // names, for duplicate checks and the current-row highlight
  let tab = null; try { tab = localStorage.getItem("ptlabel.tab"); } catch {}
  if (tab && $(`#tab-${tab}`)) selectTab(tab);
  renderDesign();
  edited(false, true);
  refreshPrinter();
})();
