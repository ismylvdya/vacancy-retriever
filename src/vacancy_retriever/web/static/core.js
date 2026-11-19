"use strict";

/* Shared helpers for every script: DOM, server calls, clipboard, toast, browser storage. */

const $ = (id) => document.getElementById(id);

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null && text !== "") node.textContent = text;
  return node;
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
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

async function writeClipboard(text) {
  if (navigator.clipboard && window.isSecureContext) {
    try { await navigator.clipboard.writeText(text); return; } catch (e) { /* not allowed here: the old way below */ }
  }
  const area = document.createElement("textarea");
  area.value = text;
  document.body.appendChild(area);
  area.select();
  document.execCommand("copy");
  area.remove();
}

/* localStorage that never throws (private windows, blocked storage). */
const store = {
  get(key, fallback = null) {
    try { const value = localStorage.getItem(key); return value === null ? fallback : value; } catch (e) { return fallback; }
  },
  set(key, value) {
    try { localStorage.setItem(key, value); } catch (e) { /* no storage */ }
  },
};

let toastTimer = null;
function toast(text, undo) {
  $("toast-text").textContent = text;
  const undoBtn = $("toast-undo");
  undoBtn.hidden = !undo;
  undoBtn.onclick = undo ? () => { hideToast(); undo(); } : null;
  $("toast").hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(hideToast, undo ? UI.toast.undoMs : UI.toast.plainMs);
}
function hideToast() { $("toast").hidden = true; }
