"use strict";

let _defnId = null;
let _toolManifest = [];
let _unsaved = false;
let _isBuiltin = false;

// ── Preset tool sets ──────────────────────────────────────────────────────────

const PRESETS = {
  none:        [],
  files:       ["Files", "Code Analysis"],
  development: ["Files", "Code Analysis", "Git", "Testing", "Code Quality", "Tasks"],
  full:        null, // all
};

// Tasks included in the "development" preset at the tool level
const DEV_EXTRA_TOOLS = new Set([
  "get_task", "list_tasks", "write_task_status", "write_task_history",
]);

// ── Init ──────────────────────────────────────────────────────────────────────

async function init() {
  const parts = window.location.pathname.split("/");
  _defnId = parts[parts.length - 2]; // /agents/{id}/edit

  await loadToolManifest();
  await loadDefinition();
}

async function loadToolManifest() {
  try {
    const res = await fetch("/api/agent-definitions/tool-manifest");
    _toolManifest = await res.json();
  } catch (e) {
    showToast("Could not load tool manifest", true);
    _toolManifest = [];
  }
  buildToolGroups();
}

async function loadDefinition() {
  try {
    const res = await fetch(`/api/agent-definitions/${_defnId}`);
    if (!res.ok) throw new Error("Not found");
    const d = await res.json();
    populateForm(d);
  } catch (e) {
    showToast("Failed to load agent definition", true);
  }
}

// ── Tool groups UI ────────────────────────────────────────────────────────────

function buildToolGroups() {
  const container = document.getElementById("tool-groups-container");
  if (!_toolManifest.length) {
    container.innerHTML = `<div style="color:#64748b;font-size:13px">No tools found.</div>`;
    return;
  }

  // Group by category preserving insertion order
  const groups = {};
  for (const tool of _toolManifest) {
    const cat = tool.category || "Other";
    if (!groups[cat]) groups[cat] = [];
    groups[cat].push(tool);
  }

  container.innerHTML = Object.entries(groups).map(([cat, tools]) => {
    const items = tools.map(t => {
      const alwaysOn = t.always_on;
      const cls = alwaysOn ? "tool-item always-on" : "tool-item";
      const label = alwaysOn ? `${t.name} <span style="color:#475569">(always on)</span>` : t.name;
      return `
<label class="${cls}" title="${esc(t.description)}">
  <input type="checkbox" data-tool="${esc(t.name)}"
    ${alwaysOn ? "checked disabled" : `onchange="updateCounts(); markUnsaved()"`}>
  ${label}
</label>`;
    }).join("");

    const total = tools.length;
    return `
<details class="tool-group">
  <summary data-category="${esc(cat)}">${esc(cat)} <span class="cat-count" data-cat="${esc(cat)}">(0/${total})</span></summary>
  <div class="tool-grid">${items}</div>
</details>`;
  }).join("");

  updateCounts();
}

function updateCounts() {
  const groups = {};
  document.querySelectorAll("input[data-tool]").forEach(cb => {
    const cat = cb.closest("details")?.querySelector("summary")?.dataset?.category;
    if (!cat) return;
    if (!groups[cat]) groups[cat] = { checked: 0, total: 0 };
    groups[cat].total++;
    if (cb.checked) groups[cat].checked++;
  });

  document.querySelectorAll(".cat-count").forEach(span => {
    const cat = span.dataset.cat;
    if (groups[cat]) {
      span.textContent = `(${groups[cat].checked}/${groups[cat].total})`;
    }
  });
}

function applyPreset(name) {
  const preset = PRESETS[name];
  document.querySelectorAll("input[data-tool]:not([disabled])").forEach(cb => {
    if (preset === null) {
      cb.checked = true;
    } else if (preset.length === 0) {
      cb.checked = false;
    } else {
      const cat = cb.closest("details")?.querySelector("summary")?.dataset?.category;
      const toolName = cb.dataset.tool;
      cb.checked = preset.includes(cat) || DEV_EXTRA_TOOLS.has(toolName);
    }
  });
  updateCounts();
  markUnsaved();
}

function setCheckedTools(toolNames) {
  const allowed = new Set(toolNames || []);
  document.querySelectorAll("input[data-tool]:not([disabled])").forEach(cb => {
    cb.checked = allowed.has(cb.dataset.tool);
  });
  updateCounts();
}

function getCheckedTools() {
  const tools = ["submit_work", "report_tool_bug"]; // always-on baseline
  document.querySelectorAll("input[data-tool]:not([disabled]):checked").forEach(cb => {
    if (!tools.includes(cb.dataset.tool)) tools.push(cb.dataset.tool);
  });
  return tools;
}

// ── Form population ───────────────────────────────────────────────────────────

function populateForm(d) {
  _isBuiltin = !!d.is_builtin;

  document.getElementById("ed-title").textContent = d.display_name || "Agent Editor";
  document.getElementById("ed-name").value = d.name || "";
  document.getElementById("ed-display-name").value = d.display_name || "";
  document.getElementById("ed-description").value = d.description || "";
  document.getElementById("ed-intent").value = d.intent || "";
  document.getElementById("ed-system-prompt").value = d.system_prompt || "";

  // User prompt template
  const hasTpl = !!(d.user_prompt_template);
  document.getElementById("chk-user-tpl").checked = hasTpl;
  document.getElementById("ed-user-tpl").value = d.user_prompt_template || "";
  toggleUserTpl();

  // Tools
  setCheckedTools(d.allowed_tools || []);

  // Limits
  document.getElementById("ed-max-turns").value = d.max_turns != null ? d.max_turns : "";
  document.getElementById("ed-max-tokens").value = d.max_tokens != null ? d.max_tokens : "";

  // Gate
  document.getElementById("ed-gate-type").value = d.gate_type || "llm_judge";
  document.getElementById("ed-verifier").value = d.verifier || "none";
  document.getElementById("ed-verifier-cmd").value = d.verifier_cmd || "";
  toggleVerifierCmd();

  // Behavior type & config
  const btSelect = document.getElementById("ed-behavior-type");
  btSelect.value = d.behavior_type || "";
  const bc = d.behavior_config && Object.keys(d.behavior_config).length
    ? JSON.stringify(d.behavior_config, null, 2)
    : "";
  document.getElementById("ed-behavior-config").value = bc;
  onBehaviorTypeChange();

  // Built-in mode
  if (_isBuiltin) {
    document.body.classList.add("is-builtin");
    document.getElementById("builtin-banner").classList.add("visible");
    document.getElementById("ed-builtin-badge").style.display = "";
    document.getElementById("btn-clone").style.display = "";
    document.getElementById("btn-save").style.display = "none";
  } else {
    document.body.classList.remove("is-builtin");
    document.getElementById("builtin-banner").classList.remove("visible");
    document.getElementById("ed-builtin-badge").style.display = "none";
    document.getElementById("btn-clone").style.display = "none";
    document.getElementById("btn-save").style.display = "";
  }

  clearUnsaved();
}

function onBehaviorTypeChange() {
  const bt = document.getElementById("ed-behavior-type").value;
  const badge = document.getElementById("ed-behavior-badge");
  if (bt) {
    badge.textContent = bt.replace(/_/g, " ");
    badge.style.display = "";
  } else {
    badge.style.display = "none";
  }
}

// ── Save ──────────────────────────────────────────────────────────────────────

async function saveDefinition() {
  if (_isBuiltin) {
    showToast("Built-in definitions cannot be saved. Clone it first.", true);
    return;
  }

  const maxTurnsRaw = document.getElementById("ed-max-turns").value.trim();
  const maxTokensRaw = document.getElementById("ed-max-tokens").value.trim();
  const hasTpl = document.getElementById("chk-user-tpl").checked;
  const behaviorConfigRaw = document.getElementById("ed-behavior-config").value.trim();

  let behaviorConfig = null;
  if (behaviorConfigRaw) {
    try {
      behaviorConfig = JSON.parse(behaviorConfigRaw);
    } catch {
      showToast("Behavior Config is not valid JSON", true);
      return;
    }
  }

  const body = {
    name:                 document.getElementById("ed-name").value.trim(),
    display_name:         document.getElementById("ed-display-name").value.trim(),
    description:          document.getElementById("ed-description").value.trim(),
    intent:               document.getElementById("ed-intent").value.trim(),
    system_prompt:        document.getElementById("ed-system-prompt").value,
    allowed_tools:        getCheckedTools(),
    gate_type:            document.getElementById("ed-gate-type").value,
    verifier:             document.getElementById("ed-verifier").value,
    verifier_cmd:         document.getElementById("ed-verifier-cmd").value.trim() || null,
    max_turns:            maxTurnsRaw ? parseInt(maxTurnsRaw, 10) : null,
    max_tokens:           maxTokensRaw ? parseInt(maxTokensRaw, 10) : null,
    user_prompt_template: hasTpl ? document.getElementById("ed-user-tpl").value : null,
    behavior_type:        document.getElementById("ed-behavior-type").value || null,
    behavior_config:      behaviorConfig,
  };

  if (!body.name) { showToast("Slug (name) is required", true); return; }
  if (!body.display_name) { showToast("Display name is required", true); return; }

  try {
    const res = await fetch(`/api/agent-definitions/${_defnId}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      showToast(err.detail || "Save failed", true);
      return;
    }
    const d = await res.json();
    document.getElementById("ed-title").textContent = d.display_name || "Agent Editor";
    clearUnsaved();
    showToast("Saved");
  } catch (e) {
    showToast("Network error", true);
  }
}

async function cloneAndRedirect() {
  try {
    const res = await fetch(`/api/agent-definitions/${_defnId}/clone`, { method: "POST" });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      showToast(err.detail || "Clone failed", true);
      return;
    }
    const cloned = await res.json();
    showToast(`Cloned → "${cloned.display_name}"`);
    setTimeout(() => { window.location.href = `/agents/${cloned.id}/edit`; }, 600);
  } catch (e) {
    showToast("Network error", true);
  }
}

// ── UI toggles ────────────────────────────────────────────────────────────────

function toggleUserTpl() {
  const checked = document.getElementById("chk-user-tpl").checked;
  const area = document.getElementById("user-tpl-area");
  const hint = document.getElementById("user-tpl-default-hint");
  if (checked) {
    area.classList.add("visible");
    hint.style.display = "none";
  } else {
    area.classList.remove("visible");
    hint.style.display = "";
  }
}

function toggleVerifierCmd() {
  const v = document.getElementById("ed-verifier").value;
  document.getElementById("verifier-cmd-group").style.display = v === "none" ? "none" : "";
}

// ── System prompt generation ──────────────────────────────────────────────────

async function generateSystemPrompt() {
  const btn = document.getElementById("btn-gen-system");
  const textarea = document.getElementById("ed-system-prompt");
  const displayName = document.getElementById("ed-display-name").value.trim();
  const intent = document.getElementById("ed-intent").value.trim();
  const agentName = document.getElementById("ed-name").value.trim();

  btn.disabled = true;
  btn.textContent = "⏳ Generating…";

  try {
    const res = await fetch("/api/pipelines/generate-field", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        field: "system_prompt",
        node_state: { label: displayName, intent, agent_type: agentName },
      }),
    });

    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      showToast(err.detail || "Generation failed", true);
      return;
    }

    // Stream response into textarea
    textarea.value = "";
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      textarea.value += decoder.decode(value, { stream: true });
    }
    markUnsaved();
  } catch (e) {
    showToast("Generation error: " + e.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = "⚡ Generate";
  }
}

// ── Unsaved state ─────────────────────────────────────────────────────────────

function markUnsaved() {
  _unsaved = true;
  document.getElementById("unsaved-indicator").style.display = "";
}

function clearUnsaved() {
  _unsaved = false;
  document.getElementById("unsaved-indicator").style.display = "none";
}

// ── Toast ──────────────────────────────────────────────────────────────────────

let _toastTimer = null;
function showToast(msg, isError = false) {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.className = "show" + (isError ? " toast-error" : "");
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => { t.className = ""; }, 3000);
}

function esc(s) {
  return String(s || "").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;");
}

// ── Warn on unsaved navigation ────────────────────────────────────────────────

window.addEventListener("beforeunload", e => {
  if (_unsaved) {
    e.preventDefault();
    e.returnValue = "";
  }
});

document.addEventListener("DOMContentLoaded", init);
