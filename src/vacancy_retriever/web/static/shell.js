"use strict";

/* The page shell: the app bar (undo / redo, Claude limits, settings, debug, sidebar), the compact title that
   replaces the big one once it scrolls away, sticky offsets, and the Apply sidebar. */

/* ---------- sticky offsets: the app bar, then the tab bar, then sub-tabs + filters, then a group heading ---------- */
function measureSticky() {
  const height = (node) => (node ? node.getBoundingClientRect().height : 0);  // fractional: no sliver shows through
  const root = document.documentElement.style;
  root.setProperty("--top-h", `${height($("appbar")) + height(document.querySelector(".top"))}px`);
  root.setProperty("--sub-h", `${height(document.querySelector(".subbar"))}px`);
  root.setProperty("--gh-h", `${height(document.querySelector(".group > .group-head, .group > .section-head, .group > .table-head"))}px`);
}
if (window.ResizeObserver) {
  const observer = new ResizeObserver(measureSticky);
  document.querySelectorAll("#appbar, .top, .subbar").forEach((node) => observer.observe(node));
}
measureSticky();

/* Wide tables: titles and rows are separate boxes (so the titles stick with plain CSS); they scroll sideways together. */
document.querySelectorAll("[data-hsync]").forEach((box) => {
  box.addEventListener("scroll", () => {
    document.querySelectorAll(`[data-hsync="${box.dataset.hsync}"]`).forEach((other) => {
      if (other !== box && other.scrollLeft !== box.scrollLeft) other.scrollLeft = box.scrollLeft;
    });
  }, { passive: true });
});

/* ---------- the compact title appears in the app bar once the big one has gone under the bars ---------- */
(() => {
  const big = $("app-title");
  if (!big) return;
  let queued = false;
  const update = () => {
    queued = false;
    const covered = big.getBoundingClientRect().bottom <= parseFloat(getComputedStyle(document.documentElement)
      .getPropertyValue("--top-h") || "0");
    document.body.classList.toggle("title-compact", covered);
  };
  window.addEventListener("scroll", () => { if (!queued) { queued = true; requestAnimationFrame(update); } }, { passive: true });
  window.addEventListener("resize", update);
  update();
})();

/* ---------- Claude limits: a ring with the fuller of the two windows; the bubble shows both ---------- */
function limitLevel(percent) {
  if (percent >= UI.limits.dangerPercent) return "danger";
  if (percent >= UI.limits.warnPercent) return "warn";
  return "ok";
}

function renderLimits(limits) {
  const ring = $("limits-btn");
  if (!ring || !limits) return;
  const fullest = Math.max(0, ...limits.bars.map((bar) => bar.percent));
  ring.style.setProperty("--fill", fullest);
  ring.dataset.level = limitLevel(fullest);
  ring.title = limits.bars.length ? `Claude limits: ${fullest}% of the fuller window` : "Claude limits: not known yet";
  $("limits-updated").textContent = limits.updated;
  $("limits-bars").replaceChildren(...limits.bars.map((bar) => {
    const item = el("li", "limit");
    item.dataset.level = limitLevel(bar.percent);
    const head = el("div", "limit-head");
    const name = el("span", "limit-name", bar.label);
    if (bar.sublabel) name.append(el("span", "limit-sub", bar.sublabel));
    head.append(name, el("span", "limit-pct", `${bar.percent}%`));
    const track = el("div", "meter");
    const fill = el("div", "meter-fill");
    fill.style.width = `${bar.percent}%`;
    track.append(fill);
    item.append(head, track, el("div", "limit-note", bar.note));
    return item;
  }));
  $("limits-empty").hidden = limits.bars.length > 0;
}

if ($("limits-btn")) {
  renderLimits(JSON.parse($("limits-data").textContent));
  popover($("limits-btn"), $("limits-pop"));
  $("limits-refresh").addEventListener("click", async () => {
    try { await api("POST", "/api/claude-limits/refresh"); toast("Asking Claude… the limits update in a few seconds"); }
    catch (e) { toast(e.message); }
    setTimeout(() => api("GET", "/api/status").then((s) => renderLimits(s.limits)).catch(() => {}), UI.limits.refreshWaitMs);
  });
}

/* ---------- settings: the gear swaps the whole page for the settings screen (settings.js) ---------- */
function setSettingsOpen(open) {
  document.body.classList.toggle("settings-open", open);
  $("settings-btn").setAttribute("aria-pressed", String(open));
  $("settings").hidden = !open;
  if (open && window.Settings) window.Settings.show();
}
$("settings-btn").addEventListener("click", () => setSettingsOpen(!document.body.classList.contains("settings-open")));
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape" || !document.body.classList.contains("settings-open")) return;
  if (document.querySelector("[aria-expanded='true'] + .popover:not([hidden])")) return;  // Escape closes the popover first
  setSettingsOpen(false);
});

/* ---------- Apply sidebar: ▶ sends a vacancy here; the server makes the note, CV, letter and Drive folder ---------- */
const applySidebar = $("apply-sidebar");
let applyTimer = null;
let applyItems = 0;

function applicationRow(item) {
  const li = el("li", `app-item app-${item.state}`);
  li.dataset.id = item.id;
  const head = el("div", "app-head");
  if (item.active) head.append(el("span", "spinner"));
  head.append(el("b", "app-item-title", item.title));
  const dismiss = el("button", "icon-btn small app-dismiss");
  Object.assign(dismiss, { title: "Remove from the sidebar (files stay)", type: "button" });
  dismiss.setAttribute("aria-label", "Remove from the sidebar");
  dismiss.innerHTML = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4 4l8 8M12 4l-8 8"/></svg>';
  dismiss.dataset.app = "dismiss";
  head.append(dismiss);
  li.append(head);
  if (item.employer) li.append(el("div", "app-employer", item.employer));
  li.append(el("div", "app-status", item.status));
  if (item.error) li.append(el("div", "app-error", item.error));
  if (item.warning) li.append(el("div", "app-warning", item.warning));
  const actions = el("div", "app-actions");
  const button = (action, label) => {
    const b = el("button", "btn small", label);
    b.type = "button";
    b.dataset.app = action;
    actions.append(b);
  };
  if (item.has_pdf) button("reveal", "Show PDF");
  if (item.pdf_removed) button("retry", "Rebuild PDF");
  if (item.has_letter) button("letter", "Cover letter");
  if (item.note_url) {
    const a = el("a", "btn small", "Obsidian");  // the Drive folder link is in the note
    a.href = item.note_url;
    actions.append(a);
  }
  if (item.active) button("stop", "Stop");
  if (["failed", "waiting", "cancelled"].includes(item.state)) button("retry", "Retry");
  if (actions.children.length) li.append(actions);
  return li;
}

function sidebarOpen() {
  const saved = store.get(UI.storage.sidebarOpen);
  return saved === null ? applyItems > 0 : saved === "1";
}

function layoutSidebar() {
  if (!applySidebar) return;
  const open = sidebarOpen();
  applySidebar.hidden = !open;
  document.body.classList.toggle("with-sidebar", open);
  $("sidebar-btn").setAttribute("aria-pressed", String(open));
  $("apply-count").textContent = applyItems || "";
  $("apply-count").hidden = !applyItems;
  $("apply-empty").hidden = applyItems > 0;
  measureSticky();
}

async function refreshApplications() {
  if (!applySidebar) return;
  clearTimeout(applyTimer);
  try {
    const data = await api("GET", "/api/applications");
    applyItems = data.items.length;
    $("apply-list").replaceChildren(...data.items.map(applicationRow));
    layoutSidebar();
    applyTimer = setTimeout(refreshApplications,
      data.items.some((item) => item.active) ? UI.applications.activeMs : UI.applications.idleMs);
  } catch (e) {
    applyTimer = setTimeout(refreshApplications, UI.applications.retryMs);
  }
}

if (applySidebar) {
  $("sidebar-btn").addEventListener("click", () => {
    store.set(UI.storage.sidebarOpen, sidebarOpen() ? "0" : "1");
    layoutSidebar();
  });
  $("apply-list").addEventListener("click", async (event) => {
    const control = event.target.closest("[data-app]");
    if (!control) return;
    const id = control.closest(".app-item").dataset.id;
    const action = control.dataset.app;
    try {
      if (action === "letter") {
        const data = await api("GET", `/api/applications/${id}/letter`);
        await writeClipboard(data.text);
        toast("Cover letter copied");
        return;
      }
      await api("POST", `/api/applications/${id}/${action}`);
      if (action === "retry") toast("Continuing from where it stopped");
      if (action === "stop") toast("Stopped — what is made stays; Retry continues");
    } catch (e) { toast(e.message); }
    refreshApplications();
  });
  layoutSidebar();
  refreshApplications();
}
