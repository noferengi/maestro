/* ============================================================
   diag-session.js — Session selection, per-turn summary table, and DOM-only navigation
   Note: This view is a reconstruction of the conversation flow
   and may not exactly reflect the underlying storage format.
   Depends on: diag-utils.js, diag-entries.js (detectSessions, currentSessions)
   ============================================================ */

// ── Conversation grouping helpers ────────────────────────────

/**
 * Collapse consecutive [assistant + tool…] message runs into tool_group objects.
 * Everything else becomes a single-message object.
 * Used by renderConversation() to render tool call/result pairs as a visual unit.
 *
 * boundaries: optional array of {startMsgIdx, entryId} to prevent grouping
 * across entry turn boundaries.
 */
function groupMessages(messages, boundaries = []) {
    const out = [];
    let i = 0;
    while (i < messages.length) {
        const msg = messages[i];
        if (msg.role === 'assistant' && msg.tool_calls && msg.tool_calls.length > 0) {
            const group = [msg];
            let j = i + 1;
            while (j < messages.length && messages[j].role === 'tool') {
                // If message j starts a new entry boundary, don't group it into this assistant group
                if (boundaries.some(b => b.startMsgIdx === j)) {
                    break;
                }
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

// ── Per-turn summary table (shown above conversation) ────────

/**
 * Build the sticky per-turn summary table for the currently cached session.
 * Returns an HTML string; inserted by renderConversation() before the message list.
 * anchorEntryId — the entry the user clicked; its row gets the anchor highlight class.
 */
function buildSessionSummary(anchorEntryId) {
    const fullEntries = cachedSession?.fullEntries;
    if (!fullEntries || fullEntries.length === 0) return '';

    let rows = '';
    let grandPP = 0, grandTG = 0, grandCost = 0, grandCached = 0;
    let lastMaxCtx = 0;

    fullEntries.forEach((fe, i) => {
        const pp       = fe.prompt_cost || 0;
        const tg       = fe.generation_cost || 0;
        const usage    = fe.response_data?.usage || {};
        const cached   = usage.prompt_tokens_details?.cached_tokens || 0;
        const cacheStr = (pp > 0 && cached > 0) ? `${Math.round(cached / pp * 100)}%` : '—';
        const finish   = fe.response_data?.choices?.[0]?.finish_reason || '?';
        const finishLabel = finish === 'tool_calls' ? 'tools'
                          : finish === 'stop'       ? 'stop'
                          : finish === 'length'     ? 'limit'
                          : finish;
        const finishTitle = finish === 'tool_calls' ? 'Made tool calls'
                          : finish === 'stop'       ? 'Completed normally'
                          : finish === 'length'     ? 'Hit token limit'
                          : finish;
        const tcCount  = fe.tool_calls || 0;
        const costUc   = fe.expense?.total_cost_microcents || 0;
        const ppCostUc = fe.expense?.prompt_cost_microcents || 0;
        const tgCostUc = fe.expense?.completion_cost_microcents || 0;
        const hasCost  = costUc > 0;
        const costStr   = hasCost ? `$${(costUc   / 100_000_000).toFixed(4)}` : '—';
        const ppCostStr = hasCost ? `$${(ppCostUc / 100_000_000).toFixed(4)}` : '—';
        const tgCostStr = hasCost ? `$${(tgCostUc / 100_000_000).toFixed(4)}` : '—';

        grandPP     += pp;
        grandTG     += tg;
        grandCost   += costUc;
        grandCached += cached;

        const isAnchor = fe.id === anchorEntryId;
        const rowClass = isAnchor ? 'diag-summary-row diag-turn-anchor-row' : 'diag-summary-row';

        const llmInfo  = allDiagLlms[fe.llm_id] || {};
        const llmName  = llmInfo.name || fe.llm_id || '—';
        const maxCtx   = llmInfo.max_context || 0;
        if (maxCtx > 0) lastMaxCtx = maxCtx;
        const prevPp   = i > 0 ? (fullEntries[i-1].prompt_cost || 0) : 0;
        const deltaPp  = i > 0 ? pp - prevPp : pp;
        const ctxPct   = maxCtx > 0 ? Math.round(pp / maxCtx * 100) : null;
        const ctxClass = ctxPct == null ? '' :
                         ctxPct >= 90 ? 'ctx-critical' :
                         ctxPct >= 75 ? 'ctx-warn' :
                         ctxPct >= 50 ? 'ctx-caution' : '';
        const ctxStr   = ctxPct != null ? `${ctxPct}%` : '—';
        rows += `<tr class="${rowClass}" data-entry-id="${fe.id}" onclick="selectEntry(${fe.id})">
            <td class="col-dim">${i + 1}</td>
            <td title="${escapeHtml(fe.expense?.remote_call_id || '')}">#${fe.id}</td>
            <td class="col-dim" title="${escapeHtml(finishTitle)}">${escapeHtml(finishLabel)}</td>
            <td class="col-dim col-llm" title="${escapeHtml(fe.llm_id || '')}">${escapeHtml(llmName)}</td>
            <td class="col-r col-dim">${tcCount > 0 ? tcCount : '—'}</td>
            <td class="col-r">${fmtTokens(pp)}</td>
            <td class="col-r col-dim">${fmtTokens(deltaPp)}</td>
            <td class="col-r ${ctxClass}" title="${pp} / ${maxCtx} tokens">${ctxStr}</td>
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
                <th class="col-r">Δ Prompt</th>
                <th class="col-r">Ctx%</th>
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
                <td class="col-r" title="Final prompt size (last turn)">${fmtTokens(fullEntries[fullEntries.length - 1].prompt_cost || 0)}</td>
                <td class="col-r">${fmtTokens(fullEntries[fullEntries.length - 1].prompt_cost - fullEntries[0].prompt_cost || 0)}</td>
                <td class="col-r">${fmtTokens(fullEntries[fullEntries.length - 1].prompt_cost / (lastMaxCtx || 1) * 100)}</td>
                <td class="col-r">${fmtTokens(grandTG)}</td>
                <td class="col-r col-bold">${fmtTokens(grandPP + grandTG)}</td>
                <td class="col-r col-dim">${grandCacheStr}</td>
                <td colspan="2"></td>
                <td class="col-r col-dim">${grandCostStr}</td>
            </tr></tfoot>
        </table>
    </div>`;
}

// ── Right Panel: Entry selection and session navigation ───────

/**
 * Select an entry from the middle panel.
 * Three fetch paths:
 *   Path 1 — same session already rendered, accumulating context → DOM-only jumpToEntry()
 *   Path 2 — same session data cached but not rendered → re-render from cache
 *   Path 3 — different session → full fetch then renderConversation()
 */
async function selectEntry(entryId, targetMsgIdx = null) {
    selectedEntryId = entryId;

    // Update active state in middle panel and scroll the session into view
    let _didScrollMiddle = false;
    document.querySelectorAll('.diag-entry-item').forEach(el => {
        const eid = parseInt(el.dataset.entryId, 10);
        const mid = el.dataset.msgIdx === 'null' ? null : parseInt(el.dataset.msgIdx, 10);
        const active = eid === entryId && mid === targetMsgIdx;
        el.classList.toggle('active', active);
        if (active && !_didScrollMiddle) {
            _didScrollMiddle = true;
            const sessionContainer = el.closest('.diag-session');
            (sessionContainer || el).scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        }
    });
    // Fallback: if no item matched exactly (e.g. summary-table click with targetMsgIdx=null
    // and no turn item yet rendered, or first-entry SYSTEM/USER items don't have mid=null),
    // still scroll the session container for that entry into view.
    if (!_didScrollMiddle) {
        const anyItem = document.querySelector(`.diag-entry-item[data-entry-id="${entryId}"]`);
        if (anyItem) {
            (anyItem.closest('.diag-session') || anyItem).scrollIntoView({ behavior: 'smooth', block: 'nearest' });
        }
    }

    const sessionGroup = currentSessions.find(g => g.some(e => e.id === entryId));
    const sessionKey   = sessionGroup ? sessionGroup.map(e => e.id).join(',') : String(entryId);

    // Reset all dots to unknown (gray) when changing sessions — re-colored below
    if (renderedSessionKey !== null && renderedSessionKey !== sessionKey) {
        document.querySelectorAll('.diag-entry-dot').forEach(dot => {
            dot.className = 'diag-entry-dot type-unknown';
        });
    }

    // ── Path 1: same session already in DOM → DOM-only scroll, zero fetch, zero re-render ──
    if (renderedSessionKey === sessionKey && cachedSession?.groupKey === sessionKey
            && sessionGroup?.length > 1) {
        const lastFull  = cachedSession.fullEntries[cachedSession.fullEntries.length - 1];
        const firstFull = cachedSession.fullEntries[0];
        const sessionIsAccumulating = (lastFull?.prompt_data?.length ?? 0)
            > (firstFull?.prompt_data?.length ?? 0);
        if (sessionIsAccumulating) {
            jumpToEntry(entryId, sessionGroup, targetMsgIdx);
            return;
        }
        // Non-accumulating: fall through to Path 2 to re-render from cache
        renderedSessionKey = null;
    }

    // ── Path 2: same session data cached but not rendered (e.g. user switched away) ──
    if (cachedSession?.groupKey === sessionKey) {
        const { fullEntries, boundaries } = cachedSession;
        const selectedFull     = fullEntries.find(f => f.id === entryId);
        const fullEntry        = fullEntries[fullEntries.length - 1];
        const selectedBoundary = (boundaries || []).find(b => b.entryId === entryId);
        const highlightFrom    = selectedBoundary?.startMsgIdx ?? null;
        renderedSessionKey  = sessionKey;
        renderConversation(fullEntry, highlightFrom, entryId, selectedFull, boundaries, targetMsgIdx);
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
            sessionBoundaries = sessionGroup.map((e, i) => ({
                entryId:     e.id,
                startMsgIdx: i === 0 ? 0 : (Array.isArray(fullEntries[i - 1].prompt_data)
                    ? fullEntries[i - 1].prompt_data.length : 0),
            }));
            // highlightFrom = start of the selected turn's own new messages (not end of its prompt)
            const selectedBoundary = sessionBoundaries.find(b => b.entryId === entryId);
            highlightFrom = selectedBoundary?.startMsgIdx ?? null;
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
        renderConversation(fullEntry, highlightFrom, entryId, selectedFull, sessionBoundaries, targetMsgIdx);
    } catch (e) {
        detail.innerHTML = `<div class="diag-error">Failed to load entry: ${escapeHtml(e.message)}</div>`;
    }
}

/**
 * DOM-only jump to a different entry in the already-rendered session.
 * No fetch, no re-render — just update highlights, swap the anchor divider, scroll.
 * Only called from selectEntry() Path 1 (accumulating sessions).
 */
function jumpToEntry(entryId, sessionGroup, targetMsgIdx = null) {
    if (!cachedSession?.boundaries) return;
    const idx = sessionGroup.findIndex(e => e.id === entryId);
    if (idx < 0) return;

    // Derive highlightFrom: start of the selected turn's own new messages (= this boundary's startMsgIdx).
    // Using nextBoundary.startMsgIdx (old code) was off-by-one: it highlighted the NEXT turn's content.
    const thisBoundary  = cachedSession.boundaries[idx];
    const highlightFrom = thisBoundary ? thisBoundary.startMsgIdx : 0;

    // Re-highlight prompt messages
    document.querySelectorAll('.diag-msg[data-msg-idx]').forEach(el => {
        const mIdx = parseInt(el.dataset.msgIdx, 10);
        if (isNaN(mIdx)) return;
        el.classList.toggle('msg-highlighted', mIdx >= highlightFrom);
    });

    // Swap anchor divider (blue selected ↔ gray other)
    document.querySelectorAll('.diag-turn-divider[data-entry-id]').forEach(el => {
        const eid      = parseInt(el.dataset.entryId, 10);
        const mIdx     = el.hasAttribute('data-msg-idx') ? parseInt(el.getAttribute('data-msg-idx'), 10) : null;
        
        // Match entryId AND msgIdx if provided. Handle targetMsgIdx -1 as user prompt.
        const isAnchor = eid === entryId && (
            targetMsgIdx === null || 
            targetMsgIdx === mIdx || 
            (targetMsgIdx === -1 && mIdx !== 0 && mIdx !== null)
        );
        
        el.classList.toggle('diag-turn-divider-anchor', isAnchor);
        el.classList.toggle('diag-turn-divider-other',  !isAnchor);
        if (isAnchor)                     { el.id = 'turn-anchor'; }
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
        let scrollTarget = document.getElementById('turn-anchor');
        if (scrollTarget) scrollTarget.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
}
