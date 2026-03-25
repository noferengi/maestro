/* ============================================================
   diagnostics.js — LLM Diagnostics Viewer
   Three-panel: Task list → Entry timeline → Conversation detail
   ============================================================ */

const API_BASE = '/api';

let selectedTaskId     = null;
let selectedEntryId    = null;
let allDiagTasks       = [];   // from GET /api/diagnostics/tasks
let allDiagLlms        = {};   // id → name, from GET /api/llms
let currentEntries     = [];   // lightweight entries for selected task (ascending order)
let currentSessions    = [];   // output of detectSessions()
let cachedSession      = null; // { groupKey, fullEntries, boundaries } — avoids re-fetching same session
let renderedSessionKey = null; // groupKey of session currently in the DOM

// ── Helpers ──────────────────────────────────────────────────

function escapeHtml(str) {
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

/** 1024-based token formatting. e.g. 350100 → "341.9K" */
function fmtTokens(n) {
    if (n >= 1048576) return `${(n / 1048576).toFixed(1)}M`;
    if (n >= 1024)    return `${(n / 1024).toFixed(1)}K`;
    return String(n);
}

function formatTimestamp(isoStr) {
    if (!isoStr) return '';
    const d = new Date(isoStr);
    if (isNaN(d)) return isoStr;
    return d.toLocaleString('en-US', {
        month: 'short', day: 'numeric',
        hour: '2-digit', minute: '2-digit', hour12: false,
    });
}

/** Classify entry type from first system message content */
function labelEntry(systemContent) {
    const lc = (systemContent || '').toLowerCase();
    if (lc.includes('codebase surveyor'))                        return 'surveyor';
    if (lc.includes('software architect'))                       return 'designer';
    if (lc.includes('design evaluator') || lc.includes('design judge')) return 'judge';
    if (lc.includes('design reviewer'))                          return 'reviewer';
    if (lc.includes('research agent'))                           return 'research';
    if (lc.includes('software quality analyst'))                 return 'pitfall';
    if (lc.length > 400)                                         return 'maestro_loop';
    return 'unknown';
}

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
            allDiagLlms = Object.fromEntries(llms.map(l => [l.id, l.name]));
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
        // GET /api/budget-entries returns DESC; reverse for chronological order
        const resp = await fetch(`${API_BASE}/budget-entries?task_id=${encodeURIComponent(taskId)}&limit=500`);
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
        const timeDiffMs = new Date(curr.created_at) - new Date(prev.created_at);
        const contextGrowing = curr.prompt_cost > prev.prompt_cost;
        const withinWindow   = timeDiffMs < 5 * 60 * 1000; // 5 minutes

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

        const firstEntry = group[0];
        const lastEntry  = group[group.length - 1];
        const totalPP    = group.reduce((s, e) => s + (e.prompt_cost || 0), 0);
        const totalTG    = group.reduce((s, e) => s + (e.generation_cost || 0), 0);
        const sessionInfo = `${fmtTokens(totalPP + totalTG)} tok`;

        html += `<div class="diag-session">
            <div class="diag-session-header">
                <span>${escapeHtml(label)}</span>
                <span class="diag-session-info">${escapeHtml(sessionInfo)}</span>
            </div>`;

        group.forEach(entry => {
            const active   = entry.id === selectedEntryId ? ' active' : '';
            const tcBadge  = entry.tool_calls > 0
                ? `<span title="${entry.tool_calls} tool call(s)">&#9881; ${entry.tool_calls}</span>`
                : '';
            html += `<div class="diag-entry-item${active}" onclick="selectEntry(${entry.id})">
                <div class="diag-entry-dot type-unknown" id="dot-${entry.id}"></div>
                <div class="diag-entry-body">
                    <div class="diag-entry-id">#${entry.id}</div>
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

// ── Conversation grouping helpers ────────────────────────────

function groupMessages(messages) {
    const out = [];
    let i = 0;
    while (i < messages.length) {
        const msg = messages[i];
        if (msg.role === 'assistant' && msg.tool_calls && msg.tool_calls.length > 0) {
            const group = [msg];
            let j = i + 1;
            while (j < messages.length && messages[j].role === 'tool') {
                group.push(messages[j]);
                j++;
            }
            out.push({ type: 'tool_group', messages: group });
            i = j;
        } else {
            out.push({ type: 'single', message: msg, index: i });
            i++;
        }
    }
    return out;
}

function renderToolGroup(groupMsgs, startIndex, highlighted) {
    let inner = '';
    groupMsgs.forEach((msg, offset) => {
        inner += renderMessage(msg, startIndex + offset, highlighted);
    });
    return `<div class="diag-tool-group">${inner}</div>`;
}

function buildSessionSummary(anchorEntryId) {
    const fullEntries = cachedSession?.fullEntries;
    if (!fullEntries || fullEntries.length === 0) return '';

    let rows = '';
    let grandPP = 0, grandTG = 0, grandCost = 0, grandCached = 0;

    fullEntries.forEach((fe, i) => {
        const pp      = fe.prompt_cost || 0;
        const tg      = fe.generation_cost || 0;
        const usage   = fe.response_data?.usage || {};
        const cached  = usage.prompt_tokens_details?.cached_tokens || 0;
        const cacheStr = (pp > 0 && cached > 0) ? `${Math.round(cached / pp * 100)}%` : '—';
        const finish   = fe.response_data?.choices?.[0]?.finish_reason || '?';
        const finishLabel = finish === 'tool_calls' ? 'tools' : finish === 'stop' ? 'stop' : finish === 'length' ? 'limit' : finish;
        const finishTitle = finish === 'tool_calls' ? 'Made tool calls' : finish === 'stop' ? 'Completed normally' : finish === 'length' ? 'Hit token limit' : finish;
        const tcCount  = fe.tool_calls || 0;
        const costUc   = fe.expense?.total_cost_microcents || 0;
        const ppCostUc = fe.expense?.prompt_cost_microcents || 0;
        const tgCostUc = fe.expense?.completion_cost_microcents || 0;
        const hasCost  = costUc > 0;
        const costStr  = hasCost ? `$${(costUc / 100_000_000).toFixed(4)}` : '—';
        const ppCostStr = hasCost ? `$${(ppCostUc / 100_000_000).toFixed(4)}` : '—';
        const tgCostStr = hasCost ? `$${(tgCostUc / 100_000_000).toFixed(4)}` : '—';

        grandPP     += pp;
        grandTG     += tg;
        grandCost   += costUc;
        grandCached += cached;

        const isAnchor = fe.id === anchorEntryId;
        const rowClass = isAnchor ? 'diag-summary-row diag-turn-anchor-row' : 'diag-summary-row';

        const llmName = allDiagLlms[fe.llm_id] || fe.llm_id || '—';
        rows += `<tr class="${rowClass}" data-entry-id="${fe.id}" onclick="selectEntry(${fe.id})">
            <td class="col-dim">${i + 1}</td>
            <td title="${escapeHtml(fe.expense?.remote_call_id || '')}">#${fe.id}</td>
            <td class="col-dim" title="${escapeHtml(finishTitle)}">${escapeHtml(finishLabel)}</td>
            <td class="col-dim col-llm" title="${escapeHtml(fe.llm_id || '')}">${escapeHtml(llmName)}</td>
            <td class="col-r col-dim">${tcCount > 0 ? tcCount : '—'}</td>
            <td class="col-r">${fmtTokens(pp)}</td>
            <td class="col-r">${fmtTokens(tg)}</td>
            <td class="col-r col-bold">${fmtTokens(pp + tg)}</td>
            <td class="col-r col-dim">${cacheStr}</td>
            <td class="col-r col-dim">${ppCostStr}</td>
            <td class="col-r col-dim">${tgCostStr}</td>
            <td class="col-r col-dim">${costStr}</td>
        </tr>`;
    });

    const grandCacheStr = (grandPP > 0 && grandCached > 0) ? `${Math.round(grandCached / grandPP * 100)}%` : '—';
    const grandHasCost  = grandCost > 0;
    const grandCostStr  = grandHasCost ? `$${(grandCost / 100_000_000).toFixed(4)}` : '—';

    return `<div class="diag-turn-table-wrap">
        <table class="diag-summary-table">
            <thead><tr>
                <th class="col-dim">#</th>
                <th>Entry</th>
                <th>Finish</th>
                <th>LLM</th>
                <th class="col-r">Calls</th>
                <th class="col-r">Prompt</th>
                <th class="col-r">Generated</th>
                <th class="col-r">Total</th>
                <th class="col-r">Cache</th>
                <th class="col-r">PP Cost</th>
                <th class="col-r">TG Cost</th>
                <th class="col-r">Total Cost</th>
            </tr></thead>
            <tbody>${rows}</tbody>
            <tfoot><tr class="diag-summary-totals">
                <td colspan="5">ALL TURNS</td>
                <td class="col-r">${fmtTokens(grandPP)}</td>
                <td class="col-r">${fmtTokens(grandTG)}</td>
                <td class="col-r col-bold">${fmtTokens(grandPP + grandTG)}</td>
                <td class="col-r col-dim">${grandCacheStr}</td>
                <td colspan="2"></td>
                <td class="col-r col-dim">${grandCostStr}</td>
            </tr></tfoot>
        </table>
    </div>`;
}

// ── Right Panel: Conversation detail ─────────────────────────

async function selectEntry(entryId) {
    selectedEntryId = entryId;

    // Update active state in middle panel and scroll the session into view
    document.querySelectorAll('.diag-entry-item').forEach(el => el.classList.remove('active'));
    const item = document.querySelector(`.diag-entry-item[onclick="selectEntry(${entryId})"]`);
    if (item) {
        item.classList.add('active');
        const sessionContainer = item.closest('.diag-session');
        (sessionContainer || item).scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }

    const sessionGroup = currentSessions.find(g => g.some(e => e.id === entryId));
    const sessionKey   = sessionGroup ? sessionGroup.map(e => e.id).join(',') : String(entryId);

    // Reset all dots to unknown (gray) when changing sessions — they'll be re-colored below
    if (renderedSessionKey !== null && renderedSessionKey !== sessionKey) {
        document.querySelectorAll('.diag-entry-dot').forEach(dot => {
            dot.className = 'diag-entry-dot type-unknown';
        });
    }

    // ── Path 1: same session already in DOM → DOM-only scroll, zero fetch, zero re-render ──
    // Only valid for accumulating sessions (Maestro Loop) where the DOM holds all turns.
    // Non-accumulating sessions (separate pipeline stages) each have independent context
    // and require a re-render (Path 2) whenever a different entry is selected.
    if (renderedSessionKey === sessionKey && cachedSession?.groupKey === sessionKey
            && sessionGroup?.length > 1) {
        const lastFull  = cachedSession.fullEntries[cachedSession.fullEntries.length - 1];
        const firstFull = cachedSession.fullEntries[0];
        const sessionIsAccumulating = (lastFull?.prompt_data?.length ?? 0)
            > (firstFull?.prompt_data?.length ?? 0);
        if (sessionIsAccumulating) {
            jumpToEntry(entryId, sessionGroup);
            return;
        }
        // Non-accumulating: fall through to Path 2 to re-render from cache
        renderedSessionKey = null;
    }

    // ── Path 2: same session data cached but not rendered (e.g. user switched away) ──
    if (cachedSession?.groupKey === sessionKey) {
        const { fullEntries, boundaries } = cachedSession;
        const selectedFull  = fullEntries.find(f => f.id === entryId);
        const fullEntry     = fullEntries[fullEntries.length - 1];
        const highlightFrom = Array.isArray(selectedFull?.prompt_data) ? selectedFull.prompt_data.length : null;
        renderedSessionKey  = sessionKey;
        renderConversation(fullEntry, highlightFrom, entryId, selectedFull, boundaries);
        return;
    }

    // ── Path 3: different session → full fetch ──
    const detail = document.getElementById('conversation-detail');
    detail.innerHTML = '<p class="diag-loading">Loading conversation...</p>';

    try {
        const isMultiTurn = sessionGroup && sessionGroup.length > 1;
        let fullEntry, highlightFrom, selectedFull, sessionBoundaries;

        if (isMultiTurn) {
            const responses = await Promise.all(
                sessionGroup.map(e => fetch(`${API_BASE}/budget-entries/${e.id}/full`))
            );
            for (const r of responses) {
                if (!r.ok) throw new Error(`HTTP ${r.status}`);
            }
            const fullEntries = await Promise.all(responses.map(r => r.json()));
            selectedFull      = fullEntries.find(f => f.id === entryId);
            fullEntry         = fullEntries[fullEntries.length - 1];
            highlightFrom     = Array.isArray(selectedFull?.prompt_data) ? selectedFull.prompt_data.length : null;
            sessionBoundaries = sessionGroup.map((e, i) => ({
                entryId:     e.id,
                startMsgIdx: i === 0 ? 0 : (Array.isArray(fullEntries[i - 1].prompt_data)
                    ? fullEntries[i - 1].prompt_data.length : 0),
            }));
            cachedSession = { groupKey: sessionKey, fullEntries, boundaries: sessionBoundaries };
        } else {
            const resp = await fetch(`${API_BASE}/budget-entries/${entryId}/full`);
            if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
            selectedFull      = await resp.json();
            fullEntry         = selectedFull;
            highlightFrom     = null;
            sessionBoundaries = null;
            cachedSession     = { groupKey: sessionKey, fullEntries: [selectedFull], boundaries: null };
        }

        renderedSessionKey = sessionKey;
        renderConversation(fullEntry, highlightFrom, entryId, selectedFull, sessionBoundaries);
    } catch (e) {
        detail.innerHTML = `<div class="diag-error">Failed to load entry: ${escapeHtml(e.message)}</div>`;
    }
}


/**
 * DOM-only jump to a different entry in the already-rendered session.
 * No fetch, no re-render — just update highlights, swap the anchor divider, scroll.
 */
function jumpToEntry(entryId, sessionGroup) {
    if (!cachedSession?.boundaries) return;
    const idx = sessionGroup.findIndex(e => e.id === entryId);
    if (idx < 0) return;

    // Derive highlightFrom: = boundaries[idx+1].startMsgIdx, or end-of-messages if last entry
    const nextBoundary  = cachedSession.boundaries[idx + 1];
    const lastFull      = cachedSession.fullEntries[cachedSession.fullEntries.length - 1];
    const highlightFrom = nextBoundary
        ? nextBoundary.startMsgIdx
        : (Array.isArray(lastFull?.prompt_data) ? lastFull.prompt_data.length : 0);

    // Re-highlight prompt messages
    document.querySelectorAll('.diag-msg[data-msg-idx]').forEach(el => {
        const mIdx = parseInt(el.dataset.msgIdx, 10);
        if (isNaN(mIdx)) return;
        el.classList.toggle('msg-highlighted', mIdx >= highlightFrom);
    });

    // Swap anchor divider (blue selected ↔ gray other)
    document.querySelectorAll('.diag-turn-divider[data-entry-id]').forEach(el => {
        const eid      = parseInt(el.dataset.entryId, 10);
        const isAnchor = eid === entryId;
        el.classList.toggle('diag-turn-divider-anchor', isAnchor);
        el.classList.toggle('diag-turn-divider-other',  !isAnchor);
        if (isAnchor)               { el.id = 'turn-anchor'; }
        else if (el.id === 'turn-anchor') { el.removeAttribute('id'); }
    });

    // Swap anchor row in the turn summary table
    document.querySelectorAll('.diag-turn-table-wrap tr[data-entry-id]').forEach(tr => {
        const eid = parseInt(tr.dataset.entryId, 10);
        tr.classList.toggle('diag-turn-anchor-row', eid === entryId);
    });

    // Reset all dots then color only the clicked entry's dot
    document.querySelectorAll('.diag-entry-dot').forEach(d => { d.className = 'diag-entry-dot type-unknown'; });
    const jumpedFull = cachedSession.fullEntries.find(f => f.id === entryId);
    if (jumpedFull) {
        const msgs     = Array.isArray(jumpedFull.prompt_data) ? jumpedFull.prompt_data : [];
        const firstSys = msgs.find(m => m.role === 'system');
        const dot      = document.getElementById(`dot-${entryId}`);
        if (dot) dot.className = `diag-entry-dot type-${labelEntry(firstSys?.content || '')}`;
    }

    // Smooth scroll to the anchor
    requestAnimationFrame(() => {
        const anchor = document.getElementById('turn-anchor');
        if (anchor) anchor.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
}

/**
 * Render a conversation in the right panel.
 * @param {object} entry          - The full entry to render (may be the last in a session)
 * @param {number|null} highlightFrom - Message index where the "selected" entry's turn begins
 * @param {number|null} anchorEntryId - The entry ID the user actually clicked
 * @param {object|null} selectedFull  - The selected entry's full data (for its response)
 */
function renderConversation(entry, highlightFrom, anchorEntryId, selectedFull, sessionBoundaries) {
    const detail = document.getElementById('conversation-detail');

    const messages  = Array.isArray(entry.prompt_data)  ? entry.prompt_data  : [];
    const respData  = entry.response_data || {};
    const choices   = respData.choices || [];
    const respMsg   = choices[0]?.message || {};
    const finish    = choices[0]?.finish_reason || 'unknown';

    // If showing a session snapshot, the selected entry's response is highlighted
    const isSessionView  = highlightFrom !== null && highlightFrom !== undefined;
    // The selected entry's response comes from selectedFull (or entry if same)
    const selectedResp   = selectedFull ? (selectedFull.response_data?.choices?.[0]?.message || {}) : respMsg;
    const selectedFinish = selectedFull ? (selectedFull.response_data?.choices?.[0]?.finish_reason || 'unknown') : finish;

    // Detect whether this session genuinely accumulates context (each turn's prompt
    // includes all prior turns). This is true for the Maestro Loop but NOT for separate
    // pipeline stage calls that happen to be close in time (e.g. intake stages). In the
    // non-accumulating case, each entry has its own independent context starting at msg 0,
    // so we render the anchor entry's own prompt_data rather than the last entry's.
    const isAccumulating = !isSessionView || (anchorEntryId === entry.id)
        || (messages.length > (highlightFrom ?? messages.length));

    // When not accumulating, show the anchor entry's own context (like a standalone view)
    // but keep the turn summary table. effectiveBoundaries/effectiveHighlight are only
    // meaningful for accumulating sessions.
    const effectiveMessages   = isAccumulating
        ? messages
        : (Array.isArray(selectedFull?.prompt_data) ? selectedFull.prompt_data : messages);
    const effectiveBoundaries = isAccumulating ? sessionBoundaries : null;
    const effectiveHighlight  = isAccumulating ? highlightFrom : null;

    // Infer type from first system message (of the anchor entry's context)
    const firstSys  = effectiveMessages.find(m => m.role === 'system');
    const entryType = labelEntry(firstSys?.content || '');

    // Update the type dot for the selected entry in the middle panel
    const dotId = anchorEntryId || entry.id;
    const dot = document.getElementById(`dot-${dotId}`);
    if (dot) dot.className = `diag-entry-dot type-${entryType}`;

    // Header: for accumulating sessions show "selected in session → last"; otherwise just selected
    const displayId = (isSessionView && isAccumulating)
        ? `#${anchorEntryId} <span style="color:#adb5bd;font-weight:400">in session → #${entry.id}</span>`
        : `#${anchorEntryId || entry.id}`;

    // For header stats, use selected entry's data (not always the last entry)
    const headerEntry = selectedFull || entry;

    document.getElementById('detail-header').innerHTML =
        `<span>CONVERSATION</span>
         <span class="diag-conv-id">${displayId}</span>
         <span class="diag-conv-type-label type-${entryType}">${escapeHtml(entryType)}</span>`;

    // Finish badge reflects the SELECTED entry's result, not the last one
    const finishForBadge = selectedFinish;
    const finishClass    = `finish-${finishForBadge}`;
    const finishLabel    = finishForBadge === 'length'     ? 'HIT TOKEN LIMIT'
                         : finishForBadge === 'tool_calls' ? 'MADE TOOL CALLS'
                         : finishForBadge === 'stop'       ? 'COMPLETED'
                         : finishForBadge.toUpperCase();

    let html = buildSessionSummary(anchorEntryId || entry.id);

    const llmHeaderName = allDiagLlms[headerEntry.llm_id] || headerEntry.llm_id || null;
    html += `<div class="diag-conv-header">
        <span class="diag-conv-id">#${anchorEntryId || entry.id}</span>
        <span class="diag-finish-badge ${finishClass}">${escapeHtml(finishLabel)}</span>
        <span>${effectiveMessages.length} msgs</span>
        <span>pp=${fmtTokens(headerEntry.prompt_cost || 0)}</span>
        <span>tg=${fmtTokens(headerEntry.generation_cost || 0)}</span>
        ${llmHeaderName ? `<span class="diag-conv-llm" title="${escapeHtml(headerEntry.llm_id || '')}">${escapeHtml(llmHeaderName)}</span>` : ''}
        <span>${formatTimestamp(headerEntry.created_at)}</span>
    </div>`;

    html += '<div class="diag-messages">';

    // Render all prompt messages grouped, inserting turn dividers at session boundaries
    const grouped = groupMessages(effectiveMessages);
    let msgIdx = 0;
    grouped.forEach(g => {
        if (effectiveBoundaries) {
            const boundary = effectiveBoundaries.find(b => b.startMsgIdx === msgIdx);
            if (boundary) {
                const isAnchor   = boundary.entryId === anchorEntryId;
                const divClass   = isAnchor
                    ? 'diag-turn-divider diag-turn-divider-anchor'
                    : 'diag-turn-divider diag-turn-divider-other';
                const anchorAttr = isAnchor ? ' id="turn-anchor"' : '';
                html += `<div class="${divClass}"${anchorAttr} data-entry-id="${boundary.entryId}"><span>── Entry #${boundary.entryId} ──</span></div>`;
            }
        }
        const highlighted = isAccumulating && isSessionView && msgIdx >= effectiveHighlight;
        if (g.type === 'tool_group') {
            html += renderToolGroup(g.messages, msgIdx, highlighted);
            msgIdx += g.messages.length;
        } else {
            html += renderMessage(g.message, msgIdx, highlighted);
            msgIdx++;
        }
    });

    // Show [RESPONSE] unless it is genuinely visible in the highlighted accumulated context
    // (i.e. the next entry's prompt carried the response forward). For non-accumulating
    // sessions, always show it.
    const responseInContext = isAccumulating && isSessionView
        && (anchorEntryId !== entry.id) && messages.length > highlightFrom;
    const isLastEntry = !responseInContext;
    const reasoning = selectedResp.reasoning_content || '';
    const content   = selectedResp.content || '';
    const toolCalls = selectedResp.tool_calls || [];

    if (isLastEntry && (content || toolCalls.length > 0 || reasoning)) {
        const hlClass = (isSessionView && isAccumulating) ? ' msg-highlighted' : '';
        html += `<div class="diag-msg msg-assistant${hlClass}">
            <div class="diag-msg-header">
                <span class="diag-msg-idx">[RESPONSE]</span>
                <span>ASSISTANT</span>
                ${toolCalls.length > 0 ? `<span>↓ ${toolCalls.length} tool call(s)</span>` : ''}
            </div>
            <div class="diag-msg-body">`;

        if (reasoning) {
            html += `<div class="diag-reasoning-toggle" onclick="toggleReasoning(this)">
                        &#9658; Reasoning (${reasoning.length.toLocaleString()} chars) — click to expand
                     </div>
                     <div class="diag-reasoning-body" style="display:none">${escapeHtml(reasoning)}</div>`;
        }
        if (toolCalls.length > 0) {
            toolCalls.forEach(tc => { html += renderToolCall(tc); });
        }
        if (content) {
            html += `<div class="diag-msg-text" style="margin-top:${(reasoning || toolCalls.length) ? '0.5rem' : '0'}">${escapeHtml(content)}</div>`;
        }
        html += `</div></div>`;
    }

    // For the last entry in a session, mark the end of the conversation.
    if (isSessionView && anchorEntryId === entry.id) {
        html += `<div class="diag-turn-divider diag-turn-divider-end">
            <span>── end of session ──</span>
        </div>`;
    }

    html += '</div>'; // .diag-messages
    detail.innerHTML = html;

    // Scroll to the anchor divider
    if (isSessionView) {
        requestAnimationFrame(() => {
            const anchor = detail.querySelector('#turn-anchor');
            if (anchor) anchor.scrollIntoView({ behavior: 'smooth', block: 'start' });
        });
    }
}

function renderMessage(msg, index, highlighted) {
    const role    = msg.role || 'unknown';
    const content = typeof msg.content === 'string' ? msg.content
                  : Array.isArray(msg.content)
                    ? msg.content.filter(p => p.type === 'text').map(p => p.text).join('\n')
                    : '';
    const toolCalls     = msg.tool_calls || [];
    const toolCallId    = msg.tool_call_id || '';
    const reasoningCont = msg.reasoning_content || '';

    // [SYSTEM] injected warnings — render as banner instead of user message
    if (role === 'user' && content.startsWith('[SYSTEM]')) {
        return renderSystemWarning(content);
    }

    const roleClass = `msg-${role}${highlighted ? ' msg-highlighted' : ''}`;
    let label = role.toUpperCase();
    if (role === 'tool') label = 'TOOL RESULT';
    if (toolCalls.length > 0) label += ` ↓ ${toolCalls.length} tool call(s)`;
    // tool_call_id shown in the tool call block above — omit from header

    let bodyHtml = '';

    if (reasoningCont) {
        bodyHtml += `<div class="diag-reasoning-toggle" onclick="toggleReasoning(this)">
            &#9658; Reasoning (${reasoningCont.length.toLocaleString()} chars) — click to expand
        </div>
        <div class="diag-reasoning-body" style="display:none">${escapeHtml(reasoningCont)}</div>`;
    }

    if (toolCalls.length > 0) {
        toolCalls.forEach(tc => { bodyHtml += renderToolCall(tc); });
    }

    if (content) {
        bodyHtml += `<div class="diag-msg-text">${escapeHtml(content)}</div>`;
    }

    // Assemble
    const isToolRole = role === 'tool';
    if (isToolRole) {
        const bodyId   = `tr-body-${index}`;
        const lineCount = content ? content.split('\n').length : 0;
        const sizeHint = lineCount > 0 ? ` (${lineCount} line${lineCount !== 1 ? 's' : ''})` : '';
        return `<div class="diag-msg msg-tool${highlighted ? ' msg-highlighted' : ''}" data-msg-idx="${index}">
            <div class="diag-msg-header diag-tool-result-header" onclick="toggleToolResult('${bodyId}', this)">
                <span class="diag-msg-idx">[${index}]</span>
                <span class="diag-tool-result-label">TOOL RESULT</span>
                <span class="diag-tool-result-hint">— click to expand${escapeHtml(sizeHint)}</span>
                <span class="diag-tool-result-toggle">▸</span>
            </div>
            <div class="diag-msg-body tool-result" id="${bodyId}" style="display:none">${escapeHtml(content)}</div>
        </div>`;
    }

    // Non-tool roles: wrap everything in diag-msg-body
    if (!bodyHtml) {
        bodyHtml = '<span style="color:#adb5bd;font-style:italic">[empty]</span>';
    }

    return `<div class="diag-msg ${roleClass}" data-msg-idx="${index}">
        <div class="diag-msg-header">
            <span class="diag-msg-idx">[${index}]</span>
            <span>${escapeHtml(label)}</span>
        </div>
        <div class="diag-msg-body">${bodyHtml}</div>
    </div>`;
}

function renderToolCall(tc) {
    const fn      = tc.function || {};
    const name    = fn.name || '?';
    const argsRaw = fn.arguments;
    const callId  = tc.id || '';

    let argsFormatted;
    if (argsRaw === null || argsRaw === undefined || argsRaw === '') {
        argsFormatted = '(no args)';
    } else if (typeof argsRaw === 'object') {
        argsFormatted = JSON.stringify(argsRaw, null, 2);
    } else {
        try { argsFormatted = JSON.stringify(JSON.parse(argsRaw), null, 2); }
        catch (_) { argsFormatted = String(argsRaw); }
    }

    const idHtml = callId
        ? `<span class="diag-tool-call-id-toggle" onclick="this.nextElementSibling.style.display=this.nextElementSibling.style.display==='none'?'inline':'none'">
               id ▸</span><span class="diag-tool-call-id-value" style="display:none">${escapeHtml(callId)}</span>`
        : '';

    return `<div class="diag-tool-call">
        <div class="diag-tool-call-header">
            <span class="diag-tool-call-label">TOOL CALL</span>
            <span class="diag-tool-call-name-text">${escapeHtml(name)}()</span>
            ${idHtml}
        </div>
        <div class="diag-tool-args-label">ARGUMENTS</div>
        <div class="diag-tool-call-args">${escapeHtml(argsFormatted)}</div>
    </div>`;
}

function renderSystemWarning(content) {
    let cls = 'warn-turns';
    if (/CRITICAL|forced/i.test(content))        cls = 'warn-critical';
    else if (/context.?window/i.test(content))   cls = 'warn-context';
    else if (/turns.?remaining/i.test(content))  cls = 'warn-turns';
    return `<div class="diag-system-warn ${cls}">&#9888; ${escapeHtml(content)}</div>`;
}

function toggleToolResult(bodyId, header) {
    const body   = document.getElementById(bodyId);
    const toggle = header.querySelector('.diag-tool-result-toggle');
    const hint   = header.querySelector('.diag-tool-result-hint');
    if (!body || !toggle) return;
    const collapsed = body.style.display === 'none';
    body.style.display  = collapsed ? 'block' : 'none';
    toggle.textContent  = collapsed ? '▾' : '▸';
    if (hint) hint.textContent = hint.textContent.replace(
        collapsed ? 'expand' : 'collapse',
        collapsed ? 'collapse' : 'expand'
    );
}

function toggleReasoning(el) {
    const body      = el.nextElementSibling;
    const collapsed = body.style.display === 'none';
    body.style.display = collapsed ? 'block' : 'none';
    const chars     = body.textContent.length;
    el.innerHTML    = collapsed
        ? `&#9660; Reasoning (${chars.toLocaleString()} chars) — click to collapse`
        : `&#9658; Reasoning (${chars.toLocaleString()} chars) — click to expand`;
}

// ── Init ─────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', loadTasks);
