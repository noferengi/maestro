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
        const pp         = group.reduce((s, e) => s + (e.prompt_cost || 0), 0);
        const tg         = group.reduce((s, e) => s + (e.generation_cost || 0), 0);
        const peakCtx    = Math.max(...group.map(e => e.prompt_cost || 0));
        const isMultiSum = group.length > 1;
        const agentTurns = isMultiSum ? group.length - 1 : 1;
        const label      = isMultiSum ? `Session ${si + 1}` : `Call ${si + 1}`;
        rows += `<tr class="diag-summary-row" onclick="selectEntry(${group[0].id})">
            <td>${escapeHtml(label)}</td>
            <td class="col-r" title="${group.length} LLM calls total (entry 0 is setup)">${agentTurns}</td>
            <td class="col-r" title="Largest single prompt sent (context window high-water mark)">${fmtTokens(peakCtx)}</td>
            <td class="col-r" title="Sum of all prompt tokens across turns (re-sends full history each turn)">${fmtTokens(pp)}</td>
            <td class="col-r">${fmtTokens(tg)}</td>
            <td class="col-r col-bold" title="Billing total: cumulative prompt + completion across all turns">${fmtTokens(pp + tg)}</td>
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
                    <th class="col-r" title="Largest single prompt sent (context window high-water mark)">Peak Ctx</th>
                    <th class="col-r" title="Sum of all prompt tokens (each turn re-sends full history)">Prompt Billed</th>
                    <th class="col-r">Generated</th>
                    <th class="col-r" title="Billing total: cumulative prompt + completion">Total Billed</th>
                    <th>First call</th>
                </tr></thead>
                <tbody>${rows}</tbody>
                <tfoot><tr class="diag-summary-totals">
                    <td>ALL SESSIONS</td>
                    <td class="col-r">${currentEntries.length}</td>
                    <td class="col-r">—</td>
                    <td class="col-r">${fmtTokens(grandPP)}</td>
                    <td class="col-r">${fmtTokens(grandTG)}</td>
                    <td class="col-r col-bold">${fmtTokens(grandPP + grandTG)}</td>
                    <td></td>
                </tr></tfoot>
            </table>
        </div>`;
}

/**
 * Group entries into sessions using a multi-stream algorithm.
 *
 * Background: budget_entries are stored in DB insertion order.  When multiple
 * agents run concurrently (e.g. subdivision + research + file-summary), their
 * entries are interleaved.  A naïve "did context grow vs. previous row?" check
 * produces false session breaks every time a smaller-context entry from a
 * concurrent agent is sandwiched between two larger-context entries from the
 * main session.
 *
 * Instead, maintain a set of open streams.  For each new entry, find the best
 * stream to attach it to using a "tightest growing fit" heuristic: prefer streams
 * where the context is growing (ratio >= 1.0), and among those pick the one with
 * the ratio closest to 1.0 (minimum growth = tightest fit).  Ratios above 3x cap
 * (a different agent starting fresh) and time gaps > 10 min close the stream.
 * Falls back to the smallest-drop shrink candidate for context-trimming tolerance.
 *
 * This naturally reassembles each concurrent agent's accumulating conversation
 * into a single session, regardless of interleaving order in the DB.
 *
 * Operates on ascending-order entries.
 */
// Multi-stream heuristic for entries that predate session_id tagging.
function _detectSessionsHeuristic(entries) {
    if (entries.length === 0) return [];

    const TIME_WINDOW_MS = 10 * 60 * 1000; // 10 minutes
    const CTX_FLOOR      = 0.85;            // allow up to 15% context drop (trimming)
    // Ratio above this = different agent starting fresh (not a continuation).
    // Without this cap, a small-context stream (e.g. file summary at 594 tokens)
    // would attract the very first large turn of a new agent (e.g. subdivision at 2200).
    const MAX_GROWTH     = 3.0;

    // Each open stream is an array of entries; streams are closed when no new
    // entry fits within the time window.
    const openStreams  = [];  // streams still accepting entries
    const closedStreams = []; // streams that timed out or were explicitly closed

    for (const curr of entries) {
        const currPP   = curr.prompt_cost || 0;
        const currTime = new Date(curr.created_at);

        // Age out streams whose last entry is older than TIME_WINDOW_MS from curr.
        for (let si = openStreams.length - 1; si >= 0; si--) {
            const last     = openStreams[si][openStreams[si].length - 1];
            const lastTime = new Date(last.created_at);
            if (currTime - lastTime >= TIME_WINDOW_MS) {
                closedStreams.push(...openStreams.splice(si, 1));
            }
        }

        // Find the best stream to attach curr to.
        //
        // Strategy: prefer streams where curr's context is GROWING (ratio >= 1.0),
        // among those pick the tightest fit (ratio closest to 1.0). This keeps
        // concurrent agents separated: when two streams overlap in context size,
        // the entry flows into the one it grew from rather than an adjacent one.
        //
        // Streams where context drops slightly (0.85..1.0) are a fallback for
        // context trimming. Ratios above MAX_GROWTH start a new stream.
        const growCandidates   = []; // { stream, ratio }  ratio >= 1.0
        const shrinkCandidates = []; // { stream, ratio }  0.85 <= ratio < 1.0

        for (const stream of openStreams) {
            const lastPP = (stream[stream.length - 1].prompt_cost) || 1;
            const ratio  = currPP / lastPP;
            if (ratio >= CTX_FLOOR && ratio <= MAX_GROWTH) {
                (ratio >= 1.0 ? growCandidates : shrinkCandidates).push({ stream, ratio });
            }
        }

        let bestStream = null;
        if (growCandidates.length > 0) {
            bestStream = growCandidates.reduce((a, b) => a.ratio <= b.ratio ? a : b).stream;
        } else if (shrinkCandidates.length > 0) {
            bestStream = shrinkCandidates.reduce((a, b) => a.ratio >= b.ratio ? a : b).stream;
        }

        if (bestStream) {
            bestStream.push(curr);
        } else {
            openStreams.push([curr]);
        }
    }

    // Collect all streams (open ones are still valid — they just haven't received
    // a closing entry yet), then sort by each stream's first entry timestamp so
    // the output is in chronological order.
    const all = [...closedStreams, ...openStreams];
    all.sort((a, b) => new Date(a[0].created_at) - new Date(b[0].created_at));
    return all;
}

/**
 * Within a session_id group that has >1 entry, split on context drops > 40%.
 *
 * Multi-stage pipelines (Planning, Intake, etc.) call set_llm_session_context()
 * once per pipeline run, so all sub-stage calls share a single session_id.
 * Each sub-stage starts fresh with a new system prompt and lower prompt_cost.
 * A drop of more than 40% from the previous entry signals a fresh start.
 *
 * This is a client-side defensive split that works even for data that hasn't
 * been repaired by scripts/fix_session_grouping.py.
 */
function _splitByContextDrop(group) {
    const CTX_FLOOR = 0.60; // below 60% of previous = new sub-conversation
    if (group.length <= 1) return [group];

    const result  = [];
    let   current = [group[0]];
    let   prevPP  = group[0].prompt_cost || 0;

    for (let i = 1; i < group.length; i++) {
        const currPP = group[i].prompt_cost || 0;
        const dropped = prevPP > 0 && currPP < prevPP * CTX_FLOOR;
        if (dropped) {
            result.push(current);
            current = [group[i]];
        } else {
            current.push(group[i]);
        }
        prevPP = currPP;
    }
    result.push(current);
    return result;
}

// Authoritative grouping when entries carry session_id; heuristic fallback for legacy rows.
function detectSessions(entries) {
    if (entries.length === 0) return [];

    // Split: entries with a session_id (new) vs those without (legacy).
    const legacy = [];
    const bySessionId = {};  // session_id → entry[]

    for (const e of entries) {
        if (e.session_id) {
            if (!bySessionId[e.session_id]) bySessionId[e.session_id] = [];
            bySessionId[e.session_id].push(e);
        } else {
            legacy.push(e);
        }
    }

    // For session_id groups with >1 entry, further split on context drops so that
    // multi-stage pipelines (which share one session_id across all sub-stages) are
    // shown as separate conversations rather than one merged blob.
    const idGroups = Object.values(bySessionId).flatMap(g => _splitByContextDrop(g));
    const heuristicGroups = legacy.length > 0 ? _detectSessionsHeuristic(legacy) : [];

    // Merge and sort all groups by their first entry's timestamp.
    const all = [...idGroups, ...heuristicGroups];
    all.sort((a, b) => new Date(a[0].created_at) - new Date(b[0].created_at));
    return all;
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
        // getConceptualTurns skips entry 0 (used for SYSTEM/USER setup items) when counting
        // agent turns, so the agent turn count is group.length - 1 for multi-entry sessions.
        // For parallel sessions (no tool calls in any entry), all entries are content — no setup to skip.
        const basePP = group[0].prompt_cost || 0;
        const isParallelGroup = isMulti && basePP > 0 && group.every(e => (e.prompt_cost || 0) === basePP);
        const agentTurns = isParallelGroup ? group.length : isMulti ? group.length - 1 : 1;
        // Use stored agent_name when available (new entries with session_id), otherwise
        // fall back to generic "Session N" / "Call N" labels for legacy entries.
        const agentLabel = group[0].agent_name || null;
        const label   = isMulti
            ? agentLabel ? `${agentLabel} · ${agentTurns} turns` : `Session ${si + 1} · ${agentTurns} turns`
            : agentLabel ? `${agentLabel}` : `Call ${si + 1}`;

        const totalPP   = group.reduce((s, e) => s + (e.prompt_cost || 0), 0);
        const totalTG   = group.reduce((s, e) => s + (e.generation_cost || 0), 0);
        const peakCtx   = Math.max(...group.map(e => e.prompt_cost || 0));
        // totalPP+totalTG is a billing total (each turn re-sends full history),
        // so it far exceeds the context window.  Show peak context size as the
        // meaningful per-call limit; billing total in the tooltip.
        const sessionInfo = `ctx ${fmtTokens(peakCtx)} / ${fmtTokens(totalPP + totalTG)} billed`;

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
