"use strict";

const tab = document.body.dataset.tab;
let wasRunning = !$("cancel-btn").hidden;
let pollTimer = null;

/* ---------- run status ---------- */
const PHASE_ORDER = ["collecting", "prefilter", "details", "scoring", "dedup", "committing"];
function renderStages(s) {
  const index = PHASE_ORDER.indexOf(s.phase);
  document.querySelectorAll("#stages li").forEach((li, i) => {
    li.classList.toggle("done", index > i);
    li.classList.toggle("now", index === i);
  });
}
function renderSources(s) {
  const strip = $("source-strip");
  const sources = s.sources || [];
  if (strip.dataset.run !== String(s.run_id) || strip.children.length !== sources.length) {
    strip.dataset.run = String(s.run_id);
    strip.innerHTML = sources.map((src) => `<span data-id="${escapeHtml(src.id)}" title="${escapeHtml(src.name)}">`
      + `<img src="/icons/${encodeURIComponent(src.id)}" alt="" loading="lazy"></span>`).join("");
  }
  const done = new Set(s.done_ids || []);
  const failed = new Set(s.error_ids || []);
  for (const span of strip.children) {
    const id = span.dataset.id;
    span.className = failed.has(id) ? "err" : done.has(id) ? "ok" : id === s.current_id ? "now" : "";
  }
}
function renderStatus(s) {
  renderLimits(s.limits);
  const running = !!s.running;
  document.body.classList.toggle("running", running);
  $("run-group").hidden = running;
  $("cancel-btn").hidden = !running || s.owned === false;
  $("progress").hidden = !running;
  $("last-run").textContent = s.last_run_short || "";
  $("last-run").title = s.summary_line || "";
  if (typeof s.feed_count === "number") $("feed-count").textContent = s.feed_count;
  if (running) {
    renderStages(s);
    renderSources(s);
    $("progress-bar").style.width = `${s.percent || 0}%`;
    $("progress-percent").textContent = `${Math.round(s.percent || 0)}%`;
    const what = s.current || s.message || s.phase || "";
    $("progress-phase").textContent = s.owned === false ? `${what} — started from the terminal` : what;
  }
  const notice = $("notice");
  const llmError = s.last_run && s.last_run.summary && s.last_run.summary.llm_error;
  let text = "";
  let kind = "warn";
  if (!running && (s.last_error || s.phase === "failed")) { text = s.last_error || s.message; kind = "error"; }
  else if (!running && s.pending_llm) { text = `${s.pending_llm} items are waiting for evaluation (LLM limit or error). They go first in the next run.`; }
  else if (!running && llmError) { text = `LLM: ${llmError}`; kind = "error"; }
  notice.hidden = !text;
  notice.textContent = text;
  notice.className = `notice ${kind}`;
  if (wasRunning && !running) {
    if (s.phase === "cancelled") toast("Run cancelled. Nothing was saved.");
    else setTimeout(() => location.reload(), 500);
  }
  wasRunning = running;
}

async function poll() {
  try {
    const s = await api("GET", "/api/status");
    renderStatus(s);
    schedule(s.running ? 1500 : 15000);
  } catch (e) {
    schedule(5000);
  }
}
function schedule(ms) { clearTimeout(pollTimer); pollTimer = setTimeout(poll, ms); }

async function startRun(options) {
  try {
    await api("POST", "/api/run", options);
    $("run-panel").hidden = true;
    wasRunning = true;
    renderStatus({ running: true, owned: true, percent: 0, phase: "Starting", last_run_short: $("last-run").textContent });
    schedule(500);
  } catch (e) { toast(e.message); }
}
$("run-btn").addEventListener("click", () => startRun());

/* Run options: period in days + source groups; the last choice is remembered in this browser. */
const RUN_KEY = "vr-run-options";
const groupBoxes = [...document.querySelectorAll(".run-groups input")];
try {
  const saved = JSON.parse(localStorage.getItem(RUN_KEY) || "null");
  if (saved) {
    $("run-days").value = saved.days || "";
    groupBoxes.forEach((box) => { box.checked = saved.groups.includes(box.value); });
  }
} catch (e) { /* no storage */ }
$("run-more").addEventListener("click", (event) => { event.stopPropagation(); $("run-panel").hidden = !$("run-panel").hidden; });
document.addEventListener("click", (event) => { if (!event.target.closest("#run-group")) $("run-panel").hidden = true; });
$("run-defaults").addEventListener("click", (event) => {  // back to: since the last run, every group
  event.stopPropagation();
  $("run-days").value = "";
  groupBoxes.forEach((box) => { box.checked = true; });
  try { localStorage.removeItem(RUN_KEY); } catch (e) { /* no storage */ }
});
$("run-custom").addEventListener("click", () => {
  const days = parseInt($("run-days").value, 10) || null;
  const groups = groupBoxes.filter((box) => box.checked).map((box) => box.value);
  if (!groups.length) { toast("Choose at least one group of sources"); return; }
  try { localStorage.setItem(RUN_KEY, JSON.stringify({ days, groups })); } catch (e) { /* no storage */ }
  const missed = $("run-missed");
  startRun({ days, groups: groups.length === groupBoxes.length ? null : groups, include_missed: !!(missed && missed.checked) });
});
/* Missed period after a break: stays in the panel until it is included in a run or dismissed. */
if ($("missed-dismiss")) {
  $("missed-dismiss").addEventListener("click", async (event) => {
    event.stopPropagation();
    try {
      await api("POST", "/api/missed/dismiss");
      $("missed").remove();
      document.querySelector("#run-more .dot-alert")?.remove();
    } catch (e) { toast(e.message); }
  });
}

let cancelArmed = null;
$("cancel-btn").addEventListener("click", async () => {
  const button = $("cancel-btn");
  if (!cancelArmed) {
    button.textContent = "Click to confirm";
    button.classList.add("armed");
    cancelArmed = setTimeout(() => { cancelArmed = null; button.textContent = "Cancel"; button.classList.remove("armed"); }, 4000);
    return;
  }
  clearTimeout(cancelArmed);
  cancelArmed = null;
  button.textContent = "Cancel";
  button.classList.remove("armed");
  try { await api("POST", "/api/cancel"); toast("Cancelling…"); } catch (e) { toast(e.message); }
  schedule(500);
});

/* ---------- vacancy rows: focus, keyboard, actions ---------- */
/* A reported vacancy is in the feed only in debug mode (user, 29.10.2026). */
const counted = (el) => !(el.classList.contains("reported") && !debugOn());
const visibleRows = () => [...document.querySelectorAll(".vac")].filter((el) => !el.closest(".collapsed") && counted(el));
let current = null;

function focusRow(row, { scroll = true } = {}) {
  if (!row) return;
  if (current && current !== row) current.classList.remove("focus", "open");
  current = row;
  row.classList.add("focus", "open");
  loadDebug(row);
  row.focus({ preventScroll: true });
  if (scroll) row.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

function move(delta) {
  const rows = visibleRows();
  if (!rows.length) return;
  const index = current ? rows.indexOf(current) : -1;
  focusRow(rows[Math.max(0, Math.min(rows.length - 1, index + delta))]);
}

function updateCounts(row) {
  const group = row.closest(".group");
  if (group) refreshGroupCount(group);
}

/* Group counts follow what is shown: reported vacancies count only in debug mode; a group left empty hides. */
function refreshGroupCount(group) {
  const counter = group.querySelector(".group-count");
  if (!counter) return;
  const shown = [...group.querySelectorAll(".vac")].filter(counted).length;
  counter.textContent = shown;
  if (tab === "feed") group.classList.toggle("nothing-shown", shown === 0 && group.querySelector(".vac") !== null);
}
function refreshGroupCounts() { document.querySelectorAll(".group").forEach(refreshGroupCount); }

const LABELS = { applied: "Applied — sent to the sidebar", not_now: "Moved to Not now", skipped: "Skipped", new: "Returned to feed",
  auto_skipped: "Auto-skipped" };

/* ---------- status changes with undo / redo history (kept per browser tab across pages) ---------- */
const HISTORY_KEY = "vr-history";
function loadHistory() {
  try { return JSON.parse(sessionStorage.getItem(HISTORY_KEY)) || { undo: [], redo: [] }; }
  catch (e) { return { undo: [], redo: [] }; }
}
function saveHistory() {
  try { sessionStorage.setItem(HISTORY_KEY, JSON.stringify(history)); } catch (e) { /* private mode */ }
  $("undo-btn").disabled = !history.undo.length;
  $("redo-btn").disabled = !history.redo.length;
  $("undo-btn").title = history.undo.length ? `Undo: ${history.undo[history.undo.length - 1].title}` : "Nothing to undo";
  $("redo-btn").title = history.redo.length ? `Redo: ${history.redo[history.redo.length - 1].title}` : "Nothing to redo";
}
const history = loadHistory();
const removedRows = new Map(); // id → {row, parent, next}: rows taken off this page, for an in-place undo

function rowTitle(row) {
  const title = row.querySelector(".title");
  if (!title) return `#${row.dataset.id}`;
  const copy = title.cloneNode(true);
  copy.querySelectorAll(".vac-id, .flag, .new").forEach((el) => el.remove());
  return copy.textContent.trim().slice(0, 60);
}

async function postStatus(id, status) {
  const data = await api("POST", `/api/vacancies/${id}/status`, { status });
  if (typeof data.feed_count === "number") $("feed-count").textContent = data.feed_count;
  refreshApplications();
}

function belongsHere(status) {
  return tab === "feed" ? status === "new" : tab === "processed" && status !== "new";
}

function takeOff(row) {
  const rows = visibleRows();
  const following = rows[rows.indexOf(row) + 1] || rows[rows.indexOf(row) - 1];
  removedRows.set(row.dataset.id, { row, parent: row.parentNode, next: row.nextElementSibling });
  row.classList.add("leaving");
  setTimeout(() => {
    const parent = row.parentNode;
    row.remove();
    if (parent) updateCounts(parent);
    current = null;
    if (following) focusRow(following);
  }, 180);
}

function putBack(id) {
  const kept = removedRows.get(id);
  if (!kept) return false;
  removedRows.delete(id);
  kept.row.classList.remove("leaving");
  kept.parent.insertBefore(kept.row, kept.next && kept.next.parentNode === kept.parent ? kept.next : null);
  updateCounts(kept.parent);
  focusRow(kept.row);
  return true;
}

/* Show the result of an undo / redo on this page: move the row in or out, or reload when it is not here. */
function reflect(id, status) {
  const row = document.querySelector(`.vac[data-id="${id}"]`);
  if (belongsHere(status)) {
    if (row && !row.classList.contains("leaving")) {
      const label = row.querySelector(".st");
      if (label) { label.className = `st st-${status}`; label.textContent = LABELS[status] || status; }
      return;
    }
    if (!putBack(id)) setTimeout(() => location.reload(), 400);
  } else if (row) {
    takeOff(row);
  }
}

async function setStatus(row, status) {
  const id = row.dataset.id;
  const previous = tab === "processed" ? row.querySelector(".st").className.replace("st st-", "") : "new";
  try { await postStatus(id, status); } catch (e) { toast(e.message); return; }
  history.undo.push({ id, from: previous, to: status, title: rowTitle(row) });
  history.redo = [];
  saveHistory();
  if (belongsHere(status)) reflect(id, status); else takeOff(row);
  toast(LABELS[status] || "Saved", undo);
}

async function undo() {
  const entry = history.undo.pop();
  if (!entry) return;
  try { await postStatus(entry.id, entry.from); } catch (e) { history.undo.push(entry); toast(e.message); return; }
  history.redo.push(entry);
  saveHistory();
  reflect(entry.id, entry.from);
  toast(`Undone: ${entry.title}`);
}

async function redo() {
  const entry = history.redo.pop();
  if (!entry) return;
  try { await postStatus(entry.id, entry.to); } catch (e) { history.redo.push(entry); toast(e.message); return; }
  history.undo.push(entry);
  saveHistory();
  reflect(entry.id, entry.to);
  toast(`Redone: ${LABELS[entry.to] || entry.to} — ${entry.title}`);
}

$("undo-btn").addEventListener("click", undo);
$("redo-btn").addEventListener("click", redo);
saveHistory();

/* ---------- debug mode ---------- */
const debugToggle = $("debug-toggle");
function debugOn() { return document.body.classList.contains("debug"); }
function setDebug(on) {
  document.body.classList.toggle("debug", on);
  debugToggle.checked = on;
  refreshGroupCounts();
  try { localStorage.setItem("vr-debug", on ? "1" : ""); } catch (e) { /* private mode */ }
  if (on && current && current.classList.contains("open")) loadDebug(current);
}
try { setDebug(localStorage.getItem("vr-debug") === "1"); } catch (e) { setDebug(false); }
debugToggle.addEventListener("change", () => setDebug(debugToggle.checked));

const debugCache = {};
async function debugInfo(row) {
  const id = row.dataset.id;
  if (!debugCache[id]) debugCache[id] = await api("GET", `/api/vacancies/${id}/debug`);
  return debugCache[id];
}
async function loadDebug(row) {
  const box = row && row.querySelector(".debug-box");
  if (!box || !debugOn() || box.dataset.loaded) return;
  box.dataset.loaded = "1";
  try {
    const info = await debugInfo(row);
    const list = (items) => `<ul>${items.map((line) => `<li>${escapeHtml(line)}</li>`).join("")}</ul>`;
    box.innerHTML = `<h4>id ${info.id} · ${escapeHtml(info.verdict)}</h4>${list(info.explain)}`
      + `<h4>Raw items</h4>${list(info.raw)}`
      + (info.facts ? `<details><summary>Facts JSON</summary><pre>${escapeHtml(info.facts)}</pre></details>` : "");
  } catch (e) { box.dataset.loaded = ""; toast(e.message); }
}
async function copyDebug(row, what) {
  try {
    if (what === "id") { await writeClipboard(row.dataset.id); toast(`Copied id ${row.dataset.id}`); return; }
    const info = await debugInfo(row);
    await writeClipboard(info.report);
    toast("Debug report copied");
  } catch (e) { toast(e.message); }
}

document.querySelectorAll("[data-notice]").forEach((box) => {
  box.querySelector("[data-notice-copy]").addEventListener("click", async () => {
    try { await writeClipboard(box.querySelector(".dev-notice-text").textContent); toast("Copied — paste it to Claude"); }
    catch (e) { toast(e.message); }
  });
  box.querySelector("[data-notice-dismiss]").addEventListener("click", async () => {
    try { await api("POST", `/api/notices/${encodeURIComponent(box.dataset.notice)}/dismiss`); box.remove(); }
    catch (e) { toast(e.message); }
  });
});

/* ---------- debug mode: Report a wrong evaluation ---------- */

let reportRow = null;
function openReport(row) {
  reportRow = row;
  $("report-title").textContent = `#${row.dataset.id} · ${rowTitle(row)}`;
  $("report-text").value = "";
  $("report-dialog").showModal();
  $("report-text").focus();
}
async function submitReport() {
  const text = $("report-text").value.trim();
  if (!text || !reportRow) { toast("Write what is wrong first"); return; }
  try {
    const data = await api("POST", `/api/vacancies/${reportRow.dataset.id}/report`, { text });
    $("report-dialog").close();
    delete debugCache[reportRow.dataset.id];
    const title = reportRow.querySelector(".title");
    if (title && !title.querySelector(".reported")) {
      const flag = document.createElement("span");
      flag.className = "debug-only flag reported";
      flag.textContent = "Reported";
      title.querySelector(".vac-id").after(flag);
    }
    const count = document.querySelector('a[href="/processed?filter=reported"] .count');
    if (count) count.textContent = data.reported;
    if (tab === "feed" && !reportRow.classList.contains("reported")) {  // out of the feed once debug mode is off
      reportRow.classList.add("reported");
      refreshGroupCount(reportRow.closest(".group"));
      const feedCount = $("feed-count");
      if (feedCount) feedCount.textContent = Math.max(0, Number(feedCount.textContent) - 1);
    }
    toast("Reported — see Processed → Reported");
  } catch (e) { toast(e.message); }
}
$("report-submit").addEventListener("click", (event) => { event.preventDefault(); submitReport(); });
$("report-text").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) { event.preventDefault(); submitReport(); }
});

async function copyText(row) {
  try {
    const data = await api("GET", `/api/vacancies/${row.dataset.id}/fulltext`);
    await writeClipboard(data.text);
    toast("Full text copied");
  } catch (e) { toast(`Couldn't copy: ${e.message}`); }
}

function toggleFullText(row) {
  const pre = row.querySelector(".fulltext");
  if (pre) pre.hidden = !pre.hidden;
}

/* Return: a folded card opens with its full text; on an open card it opens the vacancy's site (the first source),
   so the thin source line need not be aimed at with the mouse. */
function enterRow(row) {
  if (!row.classList.contains("open")) {
    row.classList.add("open");
    toggleFullText(row);
    return;
  }
  const site = row.querySelector(".sources a.src[href]");
  if (site) site.click();  // the link's own target="_blank" rel="noopener noreferrer"
  else toggleFullText(row);
}

/* Return opens a site only when it is pressed on the page itself or on a card — not on a focused link or button, and
   not in the first second after the window comes back. 01.11.2026: the sidebar's Obsidian link kept the focus, Return
   meant for the browser's «Open Obsidian?» reached the page and opened the card's LinkedIn page twice. */
let windowFocusedAt = 0;
window.addEventListener("focus", () => { windowFocusedAt = performance.now(); });

function returnOnPage(event) {
  const target = event.target;
  const onPage = target === document.body || target === document.documentElement || target.matches(".vac");
  return onPage && performance.now() - windowFocusedAt > 1000;
}

document.addEventListener("click", (event) => {
  const head = event.target.closest(".group-head");
  if (head) {
    const group = head.closest(".group");
    group.classList.toggle("collapsed");
    head.setAttribute("aria-expanded", String(!group.classList.contains("collapsed")));
    return;
  }
  const row = event.target.closest(".vac");
  if (!row) return;
  const button = event.target.closest("[data-action]");
  if (button) {
    const action = button.dataset.action;
    if (action === "status") setStatus(row, button.dataset.status);
    if (action === "copy") copyText(row);
    if (action === "copy-id") copyDebug(row, "id");
    if (action === "copy-debug") copyDebug(row, "report");
    if (action === "report") openReport(row);
    if (action === "fulltext") toggleFullText(row);
    return;
  }
  // A click on a closed card opens it; on an open card it does nothing (user, 31.10.2026) — text can be selected
  // and links clicked without the card folding. Esc closes, ↑ / ↓ move.
  if (event.target.closest("a, input, textarea")) return;
  if (!row.classList.contains("open")) focusRow(row, { scroll: false });
});

document.addEventListener("keydown", (event) => {
  if (event.metaKey || event.ctrlKey || event.altKey) return;
  if (event.target.closest("input, textarea, select")) return;
  const key = event.key.toLowerCase();
  if (key === "j" || key === "arrowdown") { event.preventDefault(); move(1); return; }
  if (key === "k" || key === "arrowup") { event.preventDefault(); move(-1); return; }
  if (!current) return;
  if (event.repeat) return;
  if (key === "escape") { current.classList.remove("open"); return; }
  if (key === "enter") {
    if (!returnOnPage(event)) return;  // a focused link or button keeps its own Return
    event.preventDefault();
    enterRow(current);
    return;
  }
  if (key === "c") { copyText(current); return; }
  if (tab === "feed") {
    const map = { arrowright: "applied", n: "not_now", arrowleft: "skipped" };
    if (map[key]) { event.preventDefault(); setStatus(current, map[key]); }
  } else if (tab === "processed" && key === "r") {
    setStatus(current, "new");
  }
});

const recalc = $("recalc-btn");
if (recalc) {
  recalc.addEventListener("click", async () => {
    try {
      const data = await api("POST", "/api/recalculate");
      toast(`Recalculated ${data.total}, changed ${data.changed}`);
      setTimeout(() => location.reload(), 800);
    } catch (e) { toast(e.message); }
  });
}

/* ---------- processed: multi-select filters ---------- */
const filterForm = $("filters");
if (filterForm) {
  const scroller = filterForm.querySelector(".filters-row");
  const fade = () => $("filters-scroll").classList.toggle("more",
    scroller.scrollLeft + scroller.clientWidth < scroller.scrollWidth - 2);
  const selects = [...filterForm.querySelectorAll(".fsel")];
  const snapshot = () => new URLSearchParams(new FormData(filterForm)).toString();
  const initial = snapshot();
  const place = (select) => {
    const box = select.querySelector("summary").getBoundingClientRect();
    const menu = select.querySelector(".fsel-menu");
    menu.style.top = `${box.bottom + 4}px`;
    menu.style.left = `${Math.max(8, Math.min(box.left, window.innerWidth - menu.offsetWidth - 8))}px`;
  };
  const searchIn = filterForm.querySelector(".search-in");
  if (searchIn) searchIn.addEventListener("change", () => filterForm.submit());
  const applyIfChanged = () => { if (snapshot() !== initial) filterForm.submit(); };
  selects.forEach((select) => select.addEventListener("toggle", () => {
    if (select.open) {
      selects.forEach((other) => { if (other !== select) other.open = false; });
      place(select);
    } else {
      applyIfChanged();
    }
  }));
  const closeAll = () => selects.forEach((select) => { select.open = false; });
  document.addEventListener("click", (event) => { if (!event.target.closest(".fsel")) closeAll(); });
  document.addEventListener("keydown", (event) => { if (event.key === "Escape") closeAll(); });
  scroller.addEventListener("scroll", () => { closeAll(); fade(); });
  window.addEventListener("resize", fade);
  // The open list is inside the scroller in the DOM: its wheel events must scroll the list, not the filter row.
  scroller.addEventListener("wheel", (event) => {
    if (event.target.closest(".fsel-menu")) return;
    if (Math.abs(event.deltaY) > Math.abs(event.deltaX)) { scroller.scrollLeft += event.deltaY; event.preventDefault(); }
  }, { passive: false });
  window.addEventListener("scroll", () => { if (!document.querySelector(".fsel[open]")) return; closeAll(); },
    { passive: true });
  fade();
}

if (tab === "feed") focusRow(visibleRows()[0], { scroll: false });
schedule(wasRunning ? 500 : 15000);
