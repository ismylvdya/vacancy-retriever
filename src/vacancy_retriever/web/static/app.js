"use strict";

const $ = (id) => document.getElementById(id);
const tab = document.body.dataset.tab;
let wasRunning = !$("cancel-btn").hidden;
let pollTimer = null;

async function api(method, url, body) {
  const response = await fetch(url, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
  return data;
}

/* ---------- toast with optional undo ---------- */
let toastTimer = null;
function toast(text, undo) {
  $("toast-text").textContent = text;
  const undoBtn = $("toast-undo");
  undoBtn.hidden = !undo;
  undoBtn.onclick = undo ? () => { hideToast(); undo(); } : null;
  $("toast").hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(hideToast, undo ? 6000 : 2500);
}
function hideToast() { $("toast").hidden = true; }

/* ---------- run status ---------- */
function renderStatus(s) {
  const running = !!s.running;
  $("run-btn").hidden = running;
  $("cancel-btn").hidden = !running || s.owned === false;
  $("progress").hidden = !running;
  $("last-run").textContent = s.last_run_short || "";
  $("last-run").title = s.summary_line || "";
  if (typeof s.feed_count === "number") $("feed-count").textContent = s.feed_count;
  if (running) {
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

$("run-btn").addEventListener("click", async () => {
  try {
    await api("POST", "/api/run");
    wasRunning = true;
    renderStatus({ running: true, owned: true, percent: 0, phase: "Starting", last_run_short: $("last-run").textContent });
    schedule(500);
  } catch (e) { toast(e.message); }
});

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
const visibleRows = () => [...document.querySelectorAll(".vac")].filter((el) => !el.closest(".collapsed"));
let current = null;

function focusRow(row, { scroll = true } = {}) {
  if (!row) return;
  if (current && current !== row) current.classList.remove("focus", "open");
  current = row;
  row.classList.add("focus", "open");
  row.focus({ preventScroll: true });
  if (scroll) row.scrollIntoView({ block: "nearest", behavior: "smooth" });
}

function move(delta) {
  const rows = visibleRows();
  if (!rows.length) return;
  const index = current ? rows.indexOf(current) : -1;
  focusRow(rows[Math.max(0, Math.min(rows.length - 1, index + delta))]);
}

function updateCounts(row, delta) {
  const group = row.closest(".group");
  const counter = group && group.querySelector(".group-count");
  if (counter) counter.textContent = Math.max(0, Number(counter.textContent) + delta);
}

const LABELS = { applied: "Applied", not_now: "Moved to Not now", skipped: "Skipped", new: "Returned to feed" };

async function setStatus(row, status) {
  const id = row.dataset.id;
  const previous = tab === "processed" ? row.querySelector(".st").className.replace("st st-", "") : "new";
  const parent = row.parentNode;
  const next = row.nextElementSibling;
  try {
    const data = await api("POST", `/api/vacancies/${id}/status`, { status });
    if (typeof data.feed_count === "number") $("feed-count").textContent = data.feed_count;
  } catch (e) { toast(e.message); return; }
  const rows = visibleRows();
  const following = rows[rows.indexOf(row) + 1] || rows[rows.indexOf(row) - 1];
  row.classList.add("leaving");
  setTimeout(() => {
    row.remove();
    updateCounts(parent, -1);
    current = null;
    if (following) focusRow(following);
  }, 180);
  const undoStatus = previous === "auto_skipped" ? null : previous;
  toast(LABELS[status] || "Saved", undoStatus ? async () => {
    try {
      const data = await api("POST", `/api/vacancies/${id}/status`, { status: undoStatus });
      if (typeof data.feed_count === "number") $("feed-count").textContent = data.feed_count;
      row.classList.remove("leaving");
      parent.insertBefore(row, next && next.parentNode === parent ? next : null);
      updateCounts(parent, 1);
      focusRow(row);
    } catch (e) { toast(e.message); }
  } : null);
}

async function copyText(row) {
  try {
    const data = await api("GET", `/api/vacancies/${row.dataset.id}/fulltext`);
    if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(data.text);
    else {
      const area = document.createElement("textarea");
      area.value = data.text;
      document.body.appendChild(area);
      area.select();
      document.execCommand("copy");
      area.remove();
    }
    toast("Full text copied");
  } catch (e) { toast(`Couldn't copy: ${e.message}`); }
}

function toggleFullText(row) {
  const pre = row.querySelector(".fulltext");
  if (pre) pre.hidden = !pre.hidden;
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
    if (action === "fulltext") toggleFullText(row);
    return;
  }
  if (event.target.closest("a, .fulltext")) return;
  if (row === current && row.classList.contains("open")) { row.classList.remove("open"); return; }
  focusRow(row, { scroll: false });
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
  if (key === "enter") { event.preventDefault(); current.classList.add("open"); toggleFullText(current); return; }
  if (key === "c") { copyText(current); return; }
  if (tab === "feed") {
    const map = { a: "applied", n: "not_now", s: "skipped" };
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
  window.addEventListener("scroll", closeAll, { passive: true });
  window.addEventListener("resize", fade);
  scroller.addEventListener("wheel", (event) => {
    if (Math.abs(event.deltaY) > Math.abs(event.deltaX)) { scroller.scrollLeft += event.deltaY; event.preventDefault(); }
  }, { passive: false });
  fade();
}

if (tab === "feed") focusRow(visibleRows()[0], { scroll: false });
schedule(wasRunning ? 500 : 15000);
