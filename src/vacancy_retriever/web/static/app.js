"use strict";

const $ = (id) => document.getElementById(id);
let wasRunning = !$("run-btn") || $("run-btn").hidden;
let pollTimer = null;

function toast(text, ms = 2200) {
  const el = $("toast");
  el.textContent = text;
  el.hidden = false;
  clearTimeout(el._t);
  el._t = setTimeout(() => { el.hidden = true; }, ms);
}

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

function renderStatus(s) {
  const running = !!s.running;
  $("run-btn").hidden = running;
  $("cancel-btn").hidden = !running || s.owned === false;
  $("progress-box").hidden = !running;
  $("last-run").textContent = s.last_run_line || "";
  $("run-summary").textContent = running ? "" : (s.summary_line || "");
  if (typeof s.feed_count === "number") $("feed-count").textContent = s.feed_count;
  if (running) {
    $("progress-bar").style.width = `${s.percent || 0}%`;
    $("progress-percent").textContent = `${Math.round(s.percent || 0)}%`;
    const current = s.current ? `now: ${s.current}` : (s.message || s.phase || "");
    $("progress-phase").textContent = s.owned === false ? `${current} (started from the terminal)` : current;
    $("done-sources").textContent = (s.done_sources || []).map((n) => `✓ ${n}`).join("  ");
  }
  const pending = $("pending-banner");
  pending.hidden = !s.pending_llm || running;
  pending.textContent = s.pending_llm ? `${s.pending_llm} items waiting for evaluation (LLM limit or error) — they go first in the next run.` : "";
  const error = $("error-banner");
  const llmError = s.last_run && s.last_run.summary && s.last_run.summary.llm_error;
  const message = s.last_error || (!running && s.phase === "failed" ? s.message : "") || (!running && llmError ? `LLM: ${llmError}` : "");
  error.hidden = !message;
  error.textContent = message || "";
  if (wasRunning && !running) {
    if (s.phase === "cancelled") toast("Run cancelled — nothing was saved");
    if (document.body.dataset.tab !== "tokens" || s.phase === "done") setTimeout(() => location.reload(), 600);
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

function schedule(ms) {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(poll, ms);
}

$("run-btn").addEventListener("click", async () => {
  $("run-btn").disabled = true;
  try {
    await api("POST", "/api/run");
    wasRunning = true;
    renderStatus({ running: true, owned: true, percent: 0, phase: "starting", last_run_line: $("last-run").textContent });
    schedule(500);
  } catch (e) {
    toast(e.message, 4000);
  } finally {
    $("run-btn").disabled = false;
  }
});

$("cancel-btn").addEventListener("click", async () => {
  if (!confirm("Cancel the run? Everything collected in this run will be discarded.")) return;
  $("cancel-btn").disabled = true;
  try {
    await api("POST", "/api/cancel");
    toast("Cancelling…");
  } catch (e) {
    toast(e.message, 4000);
  } finally {
    $("cancel-btn").disabled = false;
    schedule(500);
  }
});

async function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const area = document.createElement("textarea");
  area.value = text;
  document.body.appendChild(area);
  area.select();
  document.execCommand("copy");
  area.remove();
}

document.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  const card = button.closest(".card");
  const id = card && card.dataset.id;
  if (button.dataset.action === "copy") {
    try {
      const data = await api("GET", `/api/vacancies/${id}/fulltext`);
      await copyText(data.text);
      toast("Full text copied");
    } catch (e) {
      toast(`Copy failed: ${e.message}`, 4000);
    }
  }
  if (button.dataset.action === "status") {
    button.disabled = true;
    try {
      const data = await api("POST", `/api/vacancies/${id}/status`, { status: button.dataset.status });
      card.classList.add("leaving");
      setTimeout(() => {
        const group = card.closest(".group");
        card.remove();
        if (group) {
          const counter = group.querySelector(".group-title .count");
          if (counter) counter.textContent = group.querySelectorAll(".card").length;
        }
      }, 250);
      if (typeof data.feed_count === "number") $("feed-count").textContent = data.feed_count;
      const labels = { applied: "Applied", skipped: "Skipped", not_now: "Not now", new: "Returned to feed" };
      toast(labels[button.dataset.status] || "Saved");
    } catch (e) {
      button.disabled = false;
      toast(e.message, 4000);
    }
  }
});

const recalc = $("recalc-btn");
if (recalc) {
  recalc.addEventListener("click", async () => {
    try {
      const data = await api("POST", "/api/recalculate");
      toast(`Recalculated ${data.total}, changed ${data.changed}`);
      setTimeout(() => location.reload(), 800);
    } catch (e) {
      toast(e.message, 4000);
    }
  });
}

schedule(wasRunning ? 500 : 15000);
