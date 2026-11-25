"use strict";

/* The page shell: the app bar (undo / redo, settings, debug, sidebar), the compact title that
   replaces the big one once it scrolls away, sticky offsets, and the Apply sidebar. */

/* ---------- sticky offsets: the app bar, then the tab bar, then sub-tabs + filters, then a group heading ---------- */
function measureSticky() {
  const height = (node) => (node ? node.getBoundingClientRect().height : 0);  // fractional: no sliver shows through
  const root = document.documentElement.style;
  root.setProperty("--top-h", `${height($("appbar")) + height(document.querySelector(".top"))}px`);
  root.setProperty("--sub-h", `${height(document.querySelector(".subbar"))}px`);
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

/* A group's rounded bottom has reached its stuck head and pushes it up: the line under the head goes while it does. */
(() => {
  let queued = false;
  const update = () => {
    queued = false;
    document.querySelectorAll(".group > .card-head").forEach((head) => {
      const stuckAt = parseFloat(getComputedStyle(head).top) || 0;
      head.parentElement.classList.toggle("head-pushed", head.getBoundingClientRect().top < stuckAt - 0.5);
    });
  };
  const schedule = () => { if (!queued) { queued = true; requestAnimationFrame(update); } };
  window.addEventListener("scroll", schedule, { passive: true });
  window.addEventListener("resize", update);
  // a row leaving or coming back (Undo) moves the heads without any scroll
  document.querySelectorAll(".group-body").forEach((body) => new MutationObserver(schedule).observe(body, { childList: true }));
  update();
})();

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

/* ---------- settings: the gear swaps the whole page for the settings screen (settings.js) ---------- */
let scrollBeforeSettings = 0;
function setSettingsOpen(open) {
  if (open) scrollBeforeSettings = window.scrollY;
  document.body.classList.toggle("settings-open", open);
  $("settings-btn").setAttribute("aria-pressed", String(open));
  $("settings").hidden = !open;
  window.scrollTo(0, open ? 0 : scrollBeforeSettings);
  if (open) window.Settings.show(); else window.Settings.closed();
}
$("settings-btn").addEventListener("click", () => setSettingsOpen(!document.body.classList.contains("settings-open")));
document.addEventListener("keydown", (event) => {
  if (event.key !== "Escape" || !document.body.classList.contains("settings-open")) return;
  setSettingsOpen(false);
});

/* ---------- Apply sidebar: ▶ puts a vacancy into the purgatory; Generate makes the note and what is checked (CV, letter).
   Sections and their order come from the server (views.SIDEBAR_SECTIONS). ---------- */
const applySidebar = $("apply-sidebar");
let applyTimer = null;
let applyRefresh = 0;
let applyItems = 0;
let applyCurrent = 0;  // rows of the current session's sections: the number on the sidebar button

function applicationRow(item) {
  const li = el("li", `app-item app-${item.state} app-kind-${item.kind}`);
  li.dataset.id = item.id;
  const head = el("div", "app-head");
  if (item.active) head.append(el("span", "spinner"));
  const title = el(item.card_url ? "a" : "b", "app-item-title", item.title);
  if (item.card_url) {
    title.href = item.card_url;
    title.title = "Open this vacancy's card";
  }
  head.append(title);
  li.append(head);
  const back = el("button", "btn small app-return", "Return to feed");
  back.type = "button";
  back.addEventListener("click", () => setVacancyStatus(String(item.vacancy_id), item.vacancy_status, "new", item.title));
  li.append(back);
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
  if (item.draft) li.append(generateChoice(item));
  if (item.has_pdf) button("reveal", "PDF");
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

/* Purgatory: «Generate Obsidian note» always makes the note, plus the CV and the cover letter if checked. The letter
   carries the CV's link, so it is off and unavailable while the CV is off; hands-on work has no CV, only the letter. */
function generateChoice(item) {
  const box = el("div", "app-generate");
  const inputs = {};
  const option = (name, label, checked) => {
    const line = el("label", "app-option");
    const input = el("input");
    Object.assign(input, { type: "checkbox", name, checked });
    inputs[name] = input;
    line.append(input, document.createTextNode(` ${label}`));
    return line;
  };
  const generate = el("button", "btn small primary", "Generate Obsidian note");
  Object.assign(generate, { type: "button" });
  generate.dataset.app = "generate";
  if (!item.manual) box.append(option("cv", "CV", item.with_cv));
  box.append(option("letter", "Cover letter", item.with_letter), generate);
  const sync = () => {
    if (inputs.cv) {
      inputs.letter.disabled = !inputs.cv.checked;
      if (!inputs.cv.checked) inputs.letter.checked = false;
      inputs.letter.parentElement.classList.toggle("is-disabled", inputs.letter.disabled);
    }
  };
  box.addEventListener("change", sync);
  sync();
  return box;
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
  $("apply-count").textContent = applyCurrent || "";  // only the current session (user, 18.11.2026)
  $("apply-count").hidden = !applyCurrent;
  $("apply-empty").hidden = applyItems > 0;
  measureSticky();
}

async function refreshApplications() {
  if (!applySidebar) return;
  clearTimeout(applyTimer);
  const refresh = ++applyRefresh;
  try {
    const data = await api("GET", "/api/applications");
    if (refresh !== applyRefresh) return;
    const items = data.sections.flatMap((section) => section.items);
    applyItems = items.length;
    applyCurrent = data.sections.filter((section) => section.current).reduce((n, section) => n + section.items.length, 0);
    $("apply-list").replaceChildren(...data.sections.flatMap((section) => [
      el("li", `app-section app-section-${section.key}`, section.title),
      ...section.items.map((item) => {
        const row = applicationRow(item);
        row.classList.add(`app-in-${section.key}`);
        return row;
      }),
    ]));
    layoutSidebar();
    applyTimer = setTimeout(refreshApplications,
      items.some((item) => item.active) ? UI.applications.activeMs : UI.applications.idleMs);
  } catch (e) {
    if (refresh === applyRefresh) applyTimer = setTimeout(refreshApplications, UI.applications.retryMs);
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
      if (action === "generate") {
        const row = control.closest(".app-item");
        const checked = (name) => !!row.querySelector(`input[name="${name}"]:checked`);
        await api("POST", `/api/applications/${id}/generate`, { cv: checked("cv"), letter: checked("letter") });
        refreshApplications();
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
  setupManualVacancy();
}

/* «+ Add vacancy»: a vacancy the user found himself. Generate there = the same pipeline and sidebar row (spinner,
   Stop, Retry) as Generate in the purgatory; the window closes, the row shows the progress. Esc, ×, Cancel or Back
   close it (user, 20.11.2026). */
function setupManualVacancy() {
  const dialog = $("manual-dialog");
  const form = $("manual-form");
  if (!dialog || !$("add-vacancy")) return;
  const cv = form.querySelector('input[name="cv"]');
  const letter = form.querySelector('input[name="letter"]');
  const error = $("manual-error");
  const sync = () => {
    letter.disabled = !cv.checked;
    if (!cv.checked) letter.checked = false;
    letter.parentElement.classList.toggle("is-disabled", letter.disabled);
  };
  cv.addEventListener("change", sync);
  // Generate only when the title, the link and the description are filled (user, 20.11.2026)
  const required = ["manual-title", "manual-url", "manual-text"].map((id) => $(id));
  const ready = () => {
    $("manual-submit").disabled = !required.every((input) => input.value.trim())
      || !/^https?:\/\/\S+$/.test($("manual-url").value.trim());
  };
  required.forEach((input) => input.addEventListener("input", ready));
  let pushed = false;
  const open = () => {
    error.hidden = true;
    dialog.showModal();
    ready();
    $("manual-title").focus();
    history.pushState({ manual: true }, "");
    pushed = true;
  };
  dialog.addEventListener("close", () => {
    if (pushed) { pushed = false; history.back(); }
  });
  window.addEventListener("popstate", () => {
    if (dialog.open) { pushed = false; dialog.close(); }
  });
  $("add-vacancy").addEventListener("click", open);
  $("manual-text").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) form.requestSubmit($("manual-submit"));
  });
  form.addEventListener("submit", async (event) => {
    if (event.submitter && event.submitter.value === "cancel") return;
    event.preventDefault();
    const button = $("manual-submit");
    if (button.disabled) return;
    button.disabled = true;
    error.hidden = true;
    try {
      await api("POST", "/api/applications/manual", {
        title: $("manual-title").value.trim(), company: $("manual-company").value.trim(),
        url: $("manual-url").value.trim(), description: $("manual-text").value, cv: cv.checked, letter: letter.checked,
      });
      form.reset();
      sync();
      dialog.close();
      store.set(UI.storage.sidebarOpen, "1");
      toast("Added — the progress is in the sidebar");
      refreshApplications();
    } catch (e) {
      error.textContent = e.message;
      error.hidden = false;
    } finally {
      ready();
    }
  });
  sync();
}
