"use strict";

/* The settings screen (gear in the app bar): config/*.yaml as forms. Changes collect in a draft; Save writes them
   (the server checks every file before replacing it). The fit section shows the stars of every combination of
   facts for the chosen category, recomputed by the server's own scoring engine as you edit. */

const Settings = (() => {
  const root = $("settings");
  let snapshot = null;
  let draft = new Map();       // field id → new value
  let problems = {};           // field id → message from the server
  let selectedCategory = null;
  let fitPreview = null;
  let previewTimer = null;
  let reloadOnClose = false;

  const kindLabel = (kind) => (snapshot.kinds.find((k) => k.value === kind) || { label: kind }).label;

  /* ---------- values: the saved one, unless the draft has another ---------- */
  function original(id) {
    if (id in snapshot.values) return snapshot.values[id];
    let match = id.match(/^rules:categories\.(\w+)\.(\w+)$/);
    if (match) { const c = snapshot.categories.find((x) => x.key === match[1]); return c ? c[match[2]] : null; }
    match = id.match(/^rules:groups\.(\d)\.(\w+)$/);
    if (match) { const g = snapshot.groups.find((x) => String(x.number) === match[1]); return g ? g[match[2]] : null; }
    match = id.match(/^sources:sources\[(.+)\]\.enabled$/);
    if (match) { const s = snapshot.sources.find((x) => x.id === match[1]); return s ? s.enabled : null; }
    return null;
  }
  const value = (id) => (draft.has(id) ? draft.get(id) : original(id));
  const same = (a, b) => JSON.stringify(a ?? null) === JSON.stringify(b ?? null);

  function change(id, next, { rerender = false } = {}) {
    if (same(next, original(id))) draft.delete(id); else draft.set(id, next);
    delete problems[id];
    updateSavebar();
    if (id.startsWith("rules:")) schedulePreview();
    if (rerender) render();
  }

  /* ---------- controls: one function per kind of value ---------- */
  function control(field) {
    const id = field.id;
    const current = value(id);
    switch (field.kind) {
      case "toggle": return toggle(id, current);
      case "stars": return stars(id, current, { nullable: field.nullable || id.endsWith("max_stars") });
      case "choice": return choice(id, current, field.options);
      case "chips": return chips(id, current || [], field.options);
      case "number": return number(id, current, field);
      default: return text(id, current);
    }
  }

  function toggle(id, current) {
    const label = el("label", "toggle");
    const input = el("input");
    Object.assign(input, { type: "checkbox", checked: !!current });
    input.setAttribute("role", "switch");
    input.addEventListener("change", () => change(id, input.checked));
    label.append(input, el("i", "switch"));
    return label;
  }

  function stars(id, current, { nullable }) {
    const box = el("div", "stars-input");
    box.setAttribute("role", "radiogroup");
    const options = nullable ? [null, 1, 2, 3, 4, 5] : [1, 2, 3, 4, 5];
    options.forEach((n) => {
      const b = el("button", n === null ? "star-none" : "star", n === null ? "No cap" : "★");
      b.type = "button";
      b.setAttribute("role", "radio");
      b.setAttribute("aria-checked", String(current === n));
      b.setAttribute("aria-label", n === null ? "No cap" : `${n} of 5`);
      if (n !== null && current !== null && current !== undefined && n <= current) b.classList.add("on");
      b.addEventListener("click", () => change(id, n, { rerender: true }));
      box.append(b);
    });
    return box;
  }

  function choice(id, current, options) {
    if (options.length <= 5) {
      const seg = el("div", "segmented compact");
      options.forEach((o) => {
        const b = el("button", `seg${o.value === current ? " on" : ""}`, o.label);
        b.type = "button";
        b.setAttribute("aria-pressed", String(o.value === current));
        b.addEventListener("click", () => change(id, o.value, { rerender: true }));
        seg.append(b);
      });
      return seg;
    }
    const select = el("select", "input");
    options.forEach((o) => { const opt = el("option", "", o.label); opt.value = o.value; opt.selected = o.value === current; select.append(opt); });
    select.addEventListener("change", () => change(id, select.value));
    return select;
  }

  function chips(id, current, options) {
    const box = el("div", "chips");
    if (options && options.length) {          // a fixed set: every option is a chip that is on or off
      options.forEach((o) => {
        const on = current.includes(o.value);
        const b = el("button", `chip toggle-chip${on ? " on" : ""}`, o.label);
        b.type = "button";
        b.setAttribute("aria-pressed", String(on));
        b.addEventListener("click", () => change(id, on ? current.filter((v) => v !== o.value)
          : options.map((x) => x.value).filter((v) => v === o.value || current.includes(v)), { rerender: true }));
        box.append(b);
      });
      return box;
    }
    current.forEach((word) => {
      const chip = el("span", "chip", word);
      const remove = el("button", "chip-remove");
      Object.assign(remove, { type: "button", title: `Remove «${word}»` });
      remove.setAttribute("aria-label", `Remove ${word}`);
      remove.innerHTML = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4.5 4.5l7 7M11.5 4.5l-7 7"/></svg>';
      remove.addEventListener("click", () => change(id, current.filter((w) => w !== word), { rerender: true }));
      chip.append(remove);
      box.append(chip);
    });
    const add = el("input", "chip-add");
    Object.assign(add, { type: "text", placeholder: "Add…" });
    add.setAttribute("aria-label", "Add a word");
    add.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" || !add.value.trim()) return;
      event.preventDefault();
      const word = add.value.trim().toLowerCase();
      if (!current.includes(word)) change(id, [...current, word], { rerender: true });
      setTimeout(() => { const again = root.querySelector(`[data-field="${CSS.escape(id)}"] .chip-add`); if (again) again.focus(); });
    });
    box.append(add);
    return box;
  }

  function number(id, current, field) {
    const box = el("div", "number-input");
    const input = el("input", "input");
    Object.assign(input, { type: "number", value: current ?? "", inputMode: "decimal" });
    if (field.min !== null) input.min = field.min;
    if (field.max !== null) input.max = field.max;
    if (field.step !== null) input.step = field.step;
    if (field.nullable) input.placeholder = "No limit";
    input.addEventListener("input", () => change(id, input.value === "" ? null : Number(input.value)));
    box.append(input);
    if (field.unit) box.append(el("span", "unit", field.unit));
    return box;
  }

  function text(id, current) {
    const input = el("input", "input wide");
    Object.assign(input, { type: "text", value: current ?? "" });
    input.addEventListener("input", () => change(id, input.value));
    return input;
  }

  /* ---------- a labelled row ---------- */
  function row(field) {
    const item = el("div", `setting${draft.has(field.id) ? " changed" : ""}${problems[field.id] ? " invalid" : ""}`);
    item.dataset.field = field.id;
    const words = el("div", "setting-text");
    words.append(el("div", "setting-label", field.label));
    if (field.help) words.append(el("div", "setting-help", field.help));
    if (problems[field.id]) words.append(el("div", "setting-problem", problems[field.id]));
    const holder = el("div", "setting-control");
    holder.append(control(field));
    item.append(words, holder);
    return item;
  }

  /* ---------- custom blocks ---------- */
  function fitBlock() {
    const block = el("div", "fit");
    const tree = el("div", "fit-tree");
    ["it", "manual", "other", "drop", "hidden"].forEach((kind) => {
      const members = snapshot.categories.filter((c) => value(`rules:categories.${c.key}.kind`) === kind);
      if (!members.length) return;
      const branch = el("details", "fit-branch");
      branch.open = kind === "it" || kind === "manual" || members.some((c) => c.key === selectedCategory);
      const summary = el("summary", "fit-branch-head");
      summary.append(el("span", "fit-branch-title", kindLabel(kind)), el("span", "count", String(members.length)));
      branch.append(summary);
      members.forEach((c) => branch.append(categoryRow(c, kind)));
      tree.append(branch);
    });
    block.append(tree, matrixPanel());
    return block;
  }

  function categoryRow(category, kind) {
    const prefix = `rules:categories.${category.key}`;
    const line = el("div", `fit-row${category.key === selectedCategory ? " selected" : ""}`);
    line.dataset.field = `${prefix}.kind`;
    const name = el("button", "fit-name", category.label);
    name.type = "button";
    name.title = "Show its stars for every combination of facts";
    name.addEventListener("click", () => { selectedCategory = category.key; render(); });
    const kindSelect = el("select", "input small");
    snapshot.kinds.forEach((k) => { const o = el("option", "", k.label); o.value = k.value; o.selected = k.value === kind; kindSelect.append(o); });
    kindSelect.setAttribute("aria-label", `${category.label}: kind`);
    kindSelect.addEventListener("change", () => change(`${prefix}.kind`, kindSelect.value, { rerender: true }));
    line.append(name, kindSelect);
    const values = el("div", "fit-values");
    if (kind === "it" || kind === "manual") {
      const base = el("div", "fit-stars");
      base.append(el("span", "fit-stars-label", "Base"), stars(`${prefix}.base`, value(`${prefix}.base`), { nullable: false }));
      const cap = el("div", "fit-stars");
      cap.append(el("span", "fit-stars-label", "Max"), stars(`${prefix}.cap`, value(`${prefix}.cap`), { nullable: false }));
      values.append(base, cap);
    } else if (kind === "drop") {
      const reason = text(`${prefix}.drop_reason`, value(`${prefix}.drop_reason`));
      reason.placeholder = "Skip reason";
      reason.setAttribute("aria-label", `${category.label}: skip reason`);
      values.append(el("span", "fit-stars-label", "Reason"), reason);
    }
    if (values.children.length) line.append(values);
    for (const id of [`${prefix}.kind`, `${prefix}.base`, `${prefix}.cap`, `${prefix}.drop_reason`]) {
      if (draft.has(id)) line.classList.add("changed");
      if (problems[id]) line.classList.add("invalid");
    }
    return line;
  }

  function matrixPanel() {
    const panel = el("div", "fit-matrix");
    const fit = (fitPreview || snapshot.fit)[selectedCategory];
    const category = snapshot.categories.find((c) => c.key === selectedCategory);
    if (!fit || !category) {
      panel.append(el("p", "muted", "Pick an IT or hands-on category to see its stars for every combination of facts."));
      return panel;
    }
    panel.append(el("h3", "fit-matrix-title", category.label));
    const table = el("table", "matrix");
    const head = el("thead");
    const top = el("tr");
    top.append(el("th"));
    const sub = el("tr");
    sub.append(el("th"));
    (fit.english_axis ? [false, true] : [false]).forEach((english) => {
      const th = el("th", "matrix-group", fit.english_axis ? (english ? "English team" : "Local team") : "");
      th.colSpan = 2;
      top.append(th);
      sub.append(el("th", "", "Any experience"), el("th", "", `${fit.threshold}+ years`));
    });
    head.append(top, sub);
    const body = el("tbody");
    fit.rows.forEach((r) => {
      const tr = el("tr");
      tr.append(el("th", "matrix-row", r.label || "Any stack"));
      r.cells.forEach((cell) => {
        const td = el("td", "matrix-cell");
        td.dataset.stars = cell.stars;
        td.title = cell.why;
        td.append(el("span", "matrix-stars", "★".repeat(cell.stars)), el("span", "matrix-empty", "★".repeat(5 - cell.stars)));
        tr.append(td);
      });
      body.append(tr);
    });
    table.append(head, body);
    panel.append(table, el("p", "setting-help", "Hover a cell to see how its stars are counted."));
    return panel;
  }

  function groupsBlock() {
    const list = el("div", "rows");
    snapshot.groups.forEach((g) => {
      const prefix = `rules:groups.${g.number}`;
      const line = el("div", "group-setting");
      line.dataset.field = `${prefix}.title`;
      line.append(el("span", "group-number", String(g.number)));
      const title = text(`${prefix}.title`, value(`${prefix}.title`));
      title.setAttribute("aria-label", `Group ${g.number} title`);
      const note = text(`${prefix}.note`, value(`${prefix}.note`));
      note.placeholder = "Note";
      note.setAttribute("aria-label", `Group ${g.number} note`);
      const folded = el("div", "inline-toggle");
      folded.append(toggle(`${prefix}.collapsed`, value(`${prefix}.collapsed`)), el("span", "", "Folded"));
      line.append(title, note, folded);
      list.append(line);
    });
    return list;
  }

  function sourcesBlock() {
    const block = el("div", "source-grid");
    [["telegram", "Telegram"], ["email", "E-mail"], ["sites", "Sites"]].forEach(([kind, title]) => {
      const members = snapshot.sources.filter((s) => (kind === "sites" ? !["telegram", "email"].includes(s.type) : s.type === kind));
      if (!members.length) return;
      const column = el("div", "source-column");
      column.append(el("h3", "rows-title", title));
      members.forEach((s) => {
        const id = `sources:sources[${s.id}].enabled`;
        const line = el("div", "source-setting");
        line.dataset.field = id;
        const icon = el("img", "source-icon");
        Object.assign(icon, { src: `/icons/${encodeURIComponent(s.id)}`, alt: "", loading: "lazy" });
        line.append(icon, el("span", "source-name", s.name), toggle(id, value(id)));
        column.append(line);
      });
      block.append(column);
    });
    return block;
  }

  /* ---------- the screen ---------- */
  function section(s) {
    const card = el("section", "settings-card");
    card.id = `settings-${s.id}`;
    card.append(el("h2", "settings-card-title", s.title));
    if (s.intro) card.append(el("p", "settings-intro", s.intro));
    if (s.widget === "fit") card.append(fitBlock());
    if (s.widget === "groups") card.append(groupsBlock());
    if (s.widget === "sources") card.append(sourcesBlock());
    if (s.fields.length) {
      const fields = el("div", "rows");
      if (s.widget === "fit") fields.append(el("h3", "rows-title", "Penalties"));
      s.fields.forEach((f) => fields.append(row(f)));
      card.append(fields);
    }
    s.groups.forEach((g) => {
      const fields = el("div", "rows");
      fields.append(el("h3", "rows-title", g.title));
      g.fields.forEach((f) => fields.append(row(f)));
      card.append(fields);
    });
    return card;
  }

  function render() {
    if (!snapshot) return;
    const scroll = window.scrollY;
    const layout = el("div", "settings-layout");
    const nav = el("nav", "settings-nav");
    nav.setAttribute("aria-label", "Settings sections");
    snapshot.sections.forEach((s) => {
      const a = el("a", "settings-nav-link", s.title);
      a.href = `#settings-${s.id}`;
      a.addEventListener("click", (event) => {
        event.preventDefault();
        $(`settings-${s.id}`).scrollIntoView({ behavior: "smooth", block: "start" });
      });
      nav.append(a);
    });
    const body = el("div", "settings-body");
    snapshot.sections.forEach((s) => body.append(section(s)));
    layout.append(nav, body);
    root.replaceChildren(layout, savebar());
    window.scrollTo(0, scroll);
    updateSavebar();
  }

  function savebar() {
    const bar = el("div", "savebar");
    bar.id = "settings-savebar";
    bar.append(el("span", "savebar-text"));
    const discard = el("button", "btn", "Discard");
    discard.type = "button";
    discard.addEventListener("click", () => { draft = new Map(); problems = {}; fitPreview = null; render(); });
    const save = el("button", "btn primary", "Save");
    save.type = "button";
    save.addEventListener("click", saveDraft);
    bar.append(discard, save);
    return bar;
  }

  function updateSavebar() {
    const bar = $("settings-savebar");
    if (!bar) return;
    bar.hidden = draft.size === 0;
    const invalid = Object.keys(problems).length;
    bar.querySelector(".savebar-text").textContent = invalid
      ? `${invalid} value${invalid > 1 ? "s" : ""} to fix`
      : `${draft.size} unsaved change${draft.size > 1 ? "s" : ""}`;
    bar.classList.toggle("invalid", invalid > 0);
    root.querySelectorAll(".setting[data-field], .source-setting[data-field]").forEach((node) => {
      node.classList.toggle("changed", draft.has(node.dataset.field));
    });
  }

  function schedulePreview() {
    clearTimeout(previewTimer);
    previewTimer = setTimeout(async () => {
      const changes = Object.fromEntries([...draft].filter(([id]) => id.startsWith("rules:")));
      try {
        fitPreview = (await api("POST", "/api/settings/preview", { changes })).fit;
        const panel = root.querySelector(".fit-matrix");
        if (panel) panel.replaceWith(matrixPanel());
      } catch (e) { /* an invalid draft value: the matrix keeps the last valid state */ }
    }, 250);
  }

  async function saveDraft() {
    try {
      const response = await fetch("/api/settings", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ changes: Object.fromEntries(draft) }),
      });
      const data = await response.json();
      if (!response.ok) {
        problems = data.problems || {};
        render();
        toast(data.detail || `HTTP ${response.status}`);
        return;
      }
      snapshot = data.settings;
      draft = new Map();
      problems = {};
      fitPreview = null;
      render();
      if (data.recalculated) {
        reloadOnClose = true;
        toast(`Saved — the feed follows the new rules (${data.recalculated.changed} vacancies changed)`);
      } else if (data.run_active && data.files.includes("rules")) {
        toast("Saved — Recalculate the feed after the run");
      } else {
        toast("Saved");
      }
    } catch (e) { toast(e.message); }
  }

  async function show() {
    try {
      if (!snapshot) {
        root.replaceChildren(el("p", "settings-loading muted", "Loading settings…"));
        snapshot = await api("GET", "/api/settings");
        const firstIt = snapshot.categories.find((c) => c.kind === "it");
        selectedCategory = firstIt ? firstIt.key : null;
      }
      render();
    } catch (e) { root.replaceChildren(el("p", "settings-loading", `Settings are unavailable: ${e.message}`)); }
  }

  function closed() {
    if (reloadOnClose) location.reload();
  }

  return { show, closed };
})();
window.Settings = Settings;
