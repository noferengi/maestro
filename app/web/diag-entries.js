/* ============================================================
   diag-entries.js — Middle panel: entry timeline and task summary
   Depends on: diag-utils.js, diag-tasks.js (renderTaskList)
   ============================================================ */

// ── Middle Panel: Entry timeline ─────────────────────────────

async function selectTask(taskId) {
    selectedTaskId  = taskId;
    selectedEntryId = null;
    renderTaskList(allDiagTasks); // re-render to update active state

    const entryList = document.getElementById('entry-list');
    entryList.innerHTML = '<p class="diag-loading">Loading entries...</p>';
    document.getElementById('conversation-detail').innerHTML = '<p class="diag-loading">Loading...</p>';
    document.getElementById('detail-header').innerHTML = '<span>TASK SUMMARY</span>';

    // Switching tasks — clear the session cache so stale data isn't reused
    cachedSession      = null;
    renderedSessionKey = null;

    try {
        // __file_summaries__ is a synthetic task for budget entries with no task association.
        // Use the special sentinel value so the backend returns null-task entries.
        const fetchTaskId = taskId === '__file_summaries__' ? '__file_summaries__' : taskId;
        // GET /api/budget-entries returns DESC; reverse for chronological order
        const resp = await fetch(`${API_BASE}/budget-entries?task_id=${encodeURIComponent(fetchTaskId)}&limit=500`);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const entries = await resp.json();
        currentEntries  = entries.reverse(); // ascending chronological
        currentSessions = detectSessions(currentEntries);

        const task = allDiagTasks.find(t => t.id === taskId);
        const title = task ? task.title : taskId;
        document.getElementById('entries-header').innerHTML =
            `<span>ENTRIES</span>
             <span class="diag-count" id="diag-entry-count">${currentEntries.length}</span>
             <span style="font-weight:400;text-transform:none;font-size:0.68rem;color:#adb5bd;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:160px" title="${escapeHtml(title)}">${escapeHtml(title)}</span>`;

        renderEntryList(currentSessions);
        renderTaskSummary(taskId);
    } catch (e) {
        entryList.innerHTML = `<div class="diag-error">Failed to load entries: ${escapeHtml(e.message)}</div>`;
    }
}

function renderTaskSummary(taskId) {
    const detail = document.getElementById('conversation-detail');
    const task   = allDiagTasks.find(t => t.id === taskId);
    renderedSessionKey = null; // summary view — no session in DOM

    document.getElementById('detail-header').innerHTML =
        `<span>TASK SUMMARY</span>
         <span class="diag-conv-type-label">${escapeHtml(task?.type || '?')}</span>`;

    if (!task || currentSessions.length === 0) {
        detail.innerHTML = '<p class="diag-empty">No LLM activity for this task yet.</p>';
        return;
    }

    const grandPP = currentEntries.reduce((s, e) => s + (e.prompt_cost || 0), 0);
    const grandTG = currentEntries.reduce((s, e) => s + (e.generation_cost || 0), 0);

    let rows = '';
    currentSessions.forEach((group, si) => {
        const pp    = group.reduce((s, e) => s + (e.prompt_cost || 0), 0);
        const tg    = group.reduce((s, e) => s + (e.generation_cost || 0), 0);
        const label = group.length > 1 ? `Session ${si + 1}` : `Call ${si + 1}`;
        rows += `<tr class="diag-summary-row" onclick="selectEntry(${group[0].id})">
            <td>${escapeHtml(label)}</td>
            <td class="col-r">${group.length}</td>
            <td class="col-r">${fmtTokens(pp)}</td>
            <td class="col-r">${fmtTokens(tg)}</td>
            <td class="col-r col-bold">${fmtTokens(pp + tg)}</td>
            <td class="col-dim">${formatTimestamp(group[0].created_at)}</td>
        </tr>`;
    });

    detail.innerHTML = `
        <div class="diag-summary-head">
            <div class="diag-summary-title" title="${escapeHtml(task.title)}">${escapeHtml(task.title)}</div>
            <div class="diag-summary-meta">
                <span class="diag-task-type-badge">${escapeHtml(task.type || '?')}</span>
                <span>${task.entry_count} calls · ${currentSessions.length} session${currentSessions.length !== 1 ? 's' : ''}</span>
                <span>${fmtTokens(grandPP + grandTG)} tokens total</span>
            </div>
        </div>
        <div class="diag-summary-scroll">
            <table class="diag-summary-table">
                <thead><tr>
                    <th>Session</th>
                    <th class="col-r">Turns</th>
                    <th class="col-r">Prompt</th>
                    <th class="col-r">Generated</th>
                    <th class="col-r">Total</th>
                    <th>First call</th>
                </tr></thead>
                <tbody>${rows}</tbody>
                <tfoot><tr class="diag-summary-totals">
                    <td>ALL SESSIONS</td>
                    <td class="col-r">${currentEntries.length}</td>
                    <td class="col-r">${fmtTokens(grandPP)}</td>
                    <td class="col-r">${fmtTokens(grandTG)}</td>
                    <td class="col-r col-bold">${fmtTokens(grandPP + grandTG)}</td>
                    <td></td>
                </tr></tfoot>
            </table>
        </div>`;
}

/**
 * Group consecutive entries into sessions.
 * A new session starts when prompt_cost drops (context reset) or time gap > 5 min.
 * Operates on ascending-order entries.
 */
function detectSessions(entries) {
    if (entries.length === 0) return [];

    const sessions = [];
    let current    = [entries[0]];

    for (let i = 1; i < entries.length; i++) {
        const prev = entries[i - 1];
        const curr = entries[i];
        const timeDiffMs     = new Date(curr.created_at) - new Date(prev.created_at);
        
        // Context is "growing" if prompt_cost is increasing or staying roughly the same.
        // We allow a small drop (e.g. 15%) to account for tool schema changes or context trimming
        // that shouldn't necessarily break a session.
        const contextGrowing = curr.prompt_cost > (prev.prompt_cost * 0.85);
        const withinWindow   = timeDiffMs < 10 * 60 * 1000; // 10 minutes

        if (contextGrowing && withinWindow) {
            current.push(curr);
        } else {
            sessions.push(current);
            current = [curr];
        }
    }
    sessions.push(current);
    return sessions;
}

function renderEntryList(sessions) {
    const list = document.getElementById('entry-list');
    if (sessions.length === 0) {
        list.innerHTML = '<p class="diag-empty">No entries found for this task.</p>';
        return;
    }

    let html = '';
    sessions.forEach((group, si) => {
        const isMulti = group.length > 1;
        const label   = isMulti
            ? `Session ${si + 1} · ${group.length} turns`
            : `Call ${si + 1}`;

        const totalPP   = group.reduce((s, e) => s + (e.prompt_cost || 0), 0);
        const totalTG   = group.reduce((s, e) => s + (e.generation_cost || 0), 0);
        const sessionInfo = `${fmtTokens(totalPP + totalTG)} tok`;

        html += `<div class="diag-session">
            <div class="diag-session-header">
                <span>${escapeHtml(label)}</span>
                <span class="diag-session-info">${escapeHtml(sessionInfo)}</span>
            </div>`;

        const turns = getConceptualTurns(group);
        turns.forEach(turn => {
            const entry        = turn.entry;
            const targetMsgIdx = turn.msgIdx !== undefined ? turn.msgIdx : null;
            const activeClass  = (entry.id === selectedEntryId) ? ' active' : '';

            const tcBadge = entry.tool_calls > 0
                ? `<span title="${entry.tool_calls} tool call(s)">&#9881; ${entry.tool_calls}</span>`
                : '';
            const abruptBadge = turn.isAbruptEnd
                ? `<span class="diag-abrupt-badge" title="Session ended with unresolved tool call">&#9888; abrupt</span>`
                : '';

            html += `<div class="diag-entry-item${activeClass}${turn.isAbruptEnd ? ' diag-entry-abrupt' : ''}"
                          data-entry-id="${entry.id}"
                          data-msg-idx="${targetMsgIdx}"
                          onclick="selectEntry(${entry.id}, ${targetMsgIdx})">
                <div class="diag-entry-dot type-unknown" id="dot-${entry.id}"></div>
                <div class="diag-entry-body">
                    <div class="diag-entry-id">${escapeHtml(turn.label)}${abruptBadge}</div>
                    <div class="diag-entry-meta">
                        <span>pp=${fmtTokens(entry.prompt_cost || 0)}</span>
                        <span>tg=${fmtTokens(entry.generation_cost || 0)}</span>
                        ${tcBadge}
                        <span>${formatTimestamp(entry.created_at)}</span>
                    </div>
                </div>
            </div>`;
        });

        html += '</div>';
    });

    list.innerHTML = html;
}
