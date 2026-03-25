/* ============================================================
   diag-tasks.js — Left panel: task list
   Depends on: diag-utils.js
   ============================================================ */

// ── Left Panel: Task list ────────────────────────────────────

async function loadTasks() {
    const list = document.getElementById('task-list');
    list.innerHTML = '<p class="diag-loading">Loading...</p>';
    try {
        const [taskResp, llmResp] = await Promise.all([
            fetch(`${API_BASE}/diagnostics/tasks`),
            fetch(`${API_BASE}/llms`),
        ]);
        if (!taskResp.ok) throw new Error(`HTTP ${taskResp.status}`);
        allDiagTasks = await taskResp.json();
        if (llmResp.ok) {
            const llms = await llmResp.json();
            allDiagLlms = Object.fromEntries(llms.map(l => [l.id, { name: l.name, max_context: l.max_context || 0 }]));
        }
        document.getElementById('diag-task-count').textContent = allDiagTasks.length;
        renderTaskList(allDiagTasks);
    } catch (e) {
        list.innerHTML = `<div class="diag-error">Failed to load tasks: ${escapeHtml(e.message)}</div>`;
    }
}

function renderTaskList(tasks) {
    const list = document.getElementById('task-list');
    if (tasks.length === 0) {
        list.innerHTML = '<p class="diag-empty">No tasks with LLM activity found.</p>';
        return;
    }
    list.innerHTML = tasks.map(t => {
        const totalTok = (t.total_prompt_tokens || 0) + (t.total_completion_tokens || 0);
        const active   = t.id === selectedTaskId ? ' active' : '';
        return `<div class="diag-task-item${active}" onclick="selectTask('${escapeHtml(t.id)}')">
            <div class="diag-task-title" title="${escapeHtml(t.title)}">${escapeHtml(t.title)}</div>
            <div class="diag-task-meta">
                <span class="diag-task-type-badge">${escapeHtml(t.type || '?')}</span>
                <span>${t.entry_count} calls</span>
                <span>${fmtTokens(totalTok)} tok</span>
                <span title="${escapeHtml(t.last_activity || '')}">${formatTimestamp(t.last_activity)}</span>
            </div>
        </div>`;
    }).join('');
}

function filterTasks(query) {
    const q = query.toLowerCase();
    const filtered = q
        ? allDiagTasks.filter(t =>
            (t.title || '').toLowerCase().includes(q) ||
            (t.id || '').toLowerCase().includes(q))
        : allDiagTasks;
    document.getElementById('diag-task-count').textContent = filtered.length;
    renderTaskList(filtered);
}
