/**
 * app/web/summary-browser.js
 */

let currentProject = "";
let currentView = "directory"; // directory | module
let allScopes = [];

async function init() {
    await loadProjects();
    setupEventListeners();
}

async function loadProjects() {
    try {
        const r = await fetch("/api/projects");
        const projects = await r.json();
        const sel = document.getElementById("project-select");
        projects.forEach(p => {
            const opt = document.createElement("option");
            opt.value = p.name;
            opt.textContent = p.name;
            sel.appendChild(opt);
        });
        
        // Auto-select if in URL
        const urlParams = new URLSearchParams(window.location.search);
        const p = urlParams.get('project');
        if (p) {
            sel.value = p;
            currentProject = p;
            loadScopes();
        }
    } catch (e) {
        console.error("Failed to load projects", e);
    }
}

async function loadScopes() {
    if (!currentProject) return;
    
    const tree = document.getElementById("scope-tree");
    tree.innerHTML = '<div class="loading">Loading scopes...</div>';
    
    try {
        const r = await fetch(`/api/projects/${encodeURIComponent(currentProject)}/scope-summaries?scope_type=${currentView === 'directory' ? 'directory' : 'module'}`);
        allScopes = await r.json();
        
        // Also fetch project-level
        const r2 = await fetch(`/api/projects/${encodeURIComponent(currentProject)}/scope-summaries?scope_type=project`);
        const projects = await r2.json();
        
        renderTree(projects.concat(allScopes));
    } catch (e) {
        tree.innerHTML = `<div class="error">Error: ${e.message}</div>`;
    }
}

function renderTree(scopes) {
    const tree = document.getElementById("scope-tree");
    tree.innerHTML = "";
    
    if (scopes.length === 0) {
        tree.innerHTML = '<div class="empty-state">No summaries found. Run a survey.</div>';
        return;
    }

    scopes.forEach(s => {
        const item = document.createElement("div");
        item.className = "tree-item";
        item.dataset.type = s.scope_type;
        item.dataset.key = s.scope_key;
        
        const label = document.createElement("span");
        label.className = "item-label";
        const icon = s.scope_type === 'project' ? '🏗️' : (s.scope_type === 'directory' ? '📁' : '📦');
        label.textContent = `${icon} ${s.scope_key}`;
        
        const freshness = document.createElement("span");
        freshness.className = `freshness-badge ${s.staleness_state}`;
        freshness.textContent = s.staleness_state === 'fresh' ? '✓' : '○';
        
        item.appendChild(label);
        item.appendChild(freshness);
        item.onclick = () => selectScope(s);
        tree.appendChild(item);
    });
}

async function selectScope(scope) {
    document.querySelectorAll(".tree-item").forEach(i => i.classList.remove("selected"));
    const active = document.querySelector(`.tree-item[data-type="${scope.scope_type}"][data-key="${scope.scope_key}"]`);
    if (active) active.classList.add("selected");

    const detail = document.getElementById("summary-detail");
    detail.innerHTML = '<div class="loading">Loading detail...</div>';
    
    try {
        // Re-fetch fresh detail to be sure
        const r = await fetch(`/api/projects/${encodeURIComponent(currentProject)}/scope-summaries/${scope.scope_type}/${encodeURIComponent(scope.scope_key)}`);
        const data = await r.json();
        renderDetail(data);
    } catch (e) {
        detail.innerHTML = `<div class="error">Error: ${e.message}</div>`;
    }
}

function renderDetail(s) {
    const detail = document.getElementById("summary-detail");
    detail.innerHTML = `
        <div class="detail-header">
            <div class="title-row">
                <h2>${s.scope_key}</h2>
                <span class="scope-type-tag">${s.scope_type}</span>
            </div>
            <div class="meta-row">
                <span>Freshness: <b class="${s.staleness_state}">${s.staleness_state}</b></span>
                <span>Files: ${s.file_count}</span>
                <span>Last Updated: ${new Date(s.updated_at).toLocaleString()}</span>
            </div>
        </div>
        
        <div class="summary-section">
            <h3>Health & Purpose</h3>
            <div class="summary-text">${s.summary.replace(/\n/g, '<br>')}</div>
        </div>

        <div class="short-summary-section">
            <h3>Short Summary (Context Preamble)</h3>
            <div class="short-summary-text">${s.short_summary || 'N/A'}</div>
        </div>

        <div class="actions-row">
            <button onclick="resurveyScope('${s.scope_type}', '${s.scope_key}')" class="btn btn-sm">⟳ Re-survey this scope</button>
        </div>
    `;
}

async function resurveyScope(type, key) {
    try {
        await fetch(`/api/projects/${encodeURIComponent(currentProject)}/scope-summaries/${type}/${encodeURIComponent(key)}/re-survey`, {method: 'POST'});
        alert("Re-survey job enqueued.");
    } catch (e) {
        alert("Failed to enqueue: " + e.message);
    }
}

function setupEventListeners() {
    document.getElementById("project-select").onchange = (e) => {
        currentProject = e.target.value;
        loadScopes();
    };

    document.getElementById("view-dir").onclick = (e) => {
        currentView = "directory";
        e.target.classList.add("active");
        document.getElementById("view-module").classList.remove("active");
        loadScopes();
    };

    document.getElementById("view-module").onclick = (e) => {
        currentView = "module";
        e.target.classList.add("active");
        document.getElementById("view-dir").classList.remove("active");
        loadScopes();
    };

    document.getElementById("trigger-survey-btn").onclick = async () => {
        if (!currentProject) return;
        try {
            const r = await fetch(`/api/projects/${encodeURIComponent(currentProject)}/survey`, {method: 'POST'});
            const data = await r.json();
            alert(`Survey triggered. Enqueued ${data.jobs_enqueued} jobs.`);
        } catch (e) {
            alert("Error: " + e.message);
        }
    };
}

init();
