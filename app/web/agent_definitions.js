"use strict";

let _definitions = [];

async function init() {
  try {
    const res = await fetch("/api/agent-definitions");
    _definitions = await res.json();
    renderGrid(_definitions);
  } catch (e) {
    showToast("Failed to load agent definitions", true);
  }
}

function applyFilter() {
  const q = document.getElementById("search-input").value.toLowerCase().trim();
  const filtered = q
    ? _definitions.filter(d =>
        (d.name || "").toLowerCase().includes(q) ||
        (d.display_name || "").toLowerCase().includes(q) ||
        (d.description || "").toLowerCase().includes(q)
      )
    : _definitions;
  renderGrid(filtered);
}

function renderGrid(defs) {
  const grid = document.getElementById("grid");
  const count = document.getElementById("count-label");
  count.textContent = `${defs.length} definition${defs.length !== 1 ? "s" : ""}`;

  if (!defs.length) {
    grid.innerHTML = `<div id="empty-state">No agent definitions found. Click <strong>+ New Agent</strong> to create one.</div>`;
    return;
  }

  grid.innerHTML = defs.map(d => {
    const toolCount = (d.allowed_tools || []).length;
    const gateClass = `badge-${d.gate_type || "none"}`;
    const gateLabel = { llm_judge: "LLM Judge", single_pass: "Single Pass", none: "No Gate" }[d.gate_type] || d.gate_type;
    const initials = (d.display_name || d.name || "?").slice(0, 2).toUpperCase();
    return `
<div class="tpl-card">
  <div class="card-header">
    <div class="card-icon">🤖</div>
    <div class="card-title-wrap">
      <div class="card-name" title="${esc(d.display_name)}">${esc(d.display_name)}</div>
      <div class="card-slug">${esc(d.name)}</div>
    </div>
  </div>
  <div class="card-meta">
    <span class="badge ${gateClass}">${esc(gateLabel)}</span>
    <span class="badge badge-tools">${toolCount} tool${toolCount !== 1 ? "s" : ""}</span>
  </div>
  <div class="card-desc">${esc(d.description || "No description.")}</div>
  <div class="card-actions">
    <a href="/agents/${d.id}/edit" class="btn btn-secondary btn-sm">Edit →</a>
    <button class="btn btn-danger btn-sm" onclick="deleteDefinition(${d.id}, ${JSON.stringify(d.display_name)})">Delete</button>
  </div>
</div>`;
  }).join("");
}

async function createBlank() {
  window.location.href = "/agents/new";
}

async function deleteDefinition(id, name) {
  if (!confirm(`Delete agent definition "${name}"?\n\nThis cannot be undone.`)) return;
  try {
    const res = await fetch(`/api/agent-definitions/${id}`, { method: "DELETE" });
    if (res.ok) {
      _definitions = _definitions.filter(d => d.id !== id);
      applyFilter();
      showToast(`Deleted "${name}"`);
    } else {
      const err = await res.json().catch(() => ({}));
      showToast(err.detail || "Delete failed", true);
    }
  } catch (e) {
    showToast("Network error", true);
  }
}

function esc(s) {
  return String(s || "").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}

let _toastTimer = null;
function showToast(msg, isError = false) {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.className = "show" + (isError ? " toast-error" : "");
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => { t.className = ""; }, 3000);
}

document.addEventListener("DOMContentLoaded", init);
