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

const GATE_LABELS = {
  llm_judge:   "LLM Judge",
  single_pass: "Single Pass",
  voting:      "Voting",
  test_suite:  "Test Suite",
  human:       "Human",
  none:        "No Gate",
};

const BEHAVIOR_LABELS = {
  intake_pipeline:   "Intake Pipeline",
  planning_pipeline: "Planning Pipeline",
  maestro_loop:      "MaestroLoop",
  conceptual_review: "Conceptual Review",
  optimization:      "Optimization",
  security:          "Security",
  final_review:      "Final Review",
  factory:           "Card Factory",
  voting_panel:      "Voting Panel",
  circuit_breaker:   "Circuit Breaker",
  fan_out_judge:     "Fan-Out + Judge",
  human_gate:        "Human Gate",
  arch_gen:          "Arch Gen",
  single_pass_llm:   "Single-Pass LLM",
};

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
    const gateLabel = GATE_LABELS[d.gate_type] || d.gate_type;
    const isBuiltin = !!d.is_builtin;
    const cardClass = isBuiltin ? "tpl-card is-builtin" : "tpl-card";
    const icon = isBuiltin ? "⚙️" : "🤖";

    const builtinBadge = isBuiltin
      ? `<span class="badge badge-builtin">Built-in</span>`
      : "";
    const behaviorBadge = d.behavior_type
      ? `<span class="badge badge-behavior" title="Behavior type">${esc(BEHAVIOR_LABELS[d.behavior_type] || d.behavior_type)}</span>`
      : "";

    const editBtn = isBuiltin
      ? `<a href="/agents/${d.id}/edit" class="btn btn-secondary btn-sm">View →</a>`
      : `<a href="/agents/${d.id}/edit" class="btn btn-secondary btn-sm">Edit →</a>`;

    const deleteBtn = isBuiltin
      ? ""
      : `<button class="btn btn-danger btn-sm" onclick="deleteDefinition(${d.id}, ${JSON.stringify(d.display_name)})">Delete</button>`;

    return `
<div class="${cardClass}">
  <div class="card-header">
    <div class="card-icon">${icon}</div>
    <div class="card-title-wrap">
      <div class="card-name" title="${esc(d.display_name)}">${esc(d.display_name)}</div>
      <div class="card-slug">${esc(d.name)}</div>
    </div>
  </div>
  <div class="card-meta">
    ${builtinBadge}
    <span class="badge ${gateClass}">${esc(gateLabel)}</span>
    ${behaviorBadge}
    <span class="badge badge-tools">${toolCount} tool${toolCount !== 1 ? "s" : ""}</span>
  </div>
  <div class="card-desc">${esc(d.description || "No description.")}</div>
  <div class="card-actions">
    ${editBtn}
    <button class="btn btn-secondary btn-sm" onclick="cloneDefinition(${d.id}, ${JSON.stringify(d.display_name)})">Clone</button>
    ${deleteBtn}
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

async function cloneDefinition(id, name) {
  try {
    const res = await fetch(`/api/agent-definitions/${id}/clone`, { method: "POST" });
    if (res.ok) {
      const cloned = await res.json();
      _definitions.push(cloned);
      applyFilter();
      showToast(`Cloned "${name}" → "${cloned.display_name}"`);
      // Navigate to the editor for the new clone
      setTimeout(() => { window.location.href = `/agents/${cloned.id}/edit`; }, 800);
    } else {
      const err = await res.json().catch(() => ({}));
      showToast(err.detail || "Clone failed", true);
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
