/* ============================================================
   diag-render.js — Right panel: conversation and message rendering
   Note: This view is a reconstruction of the conversation flow
   and may not exactly reflect the underlying storage format.
   Depends on: diag-utils.js, diag-session.js
   ============================================================ */

/**
 * Estimate the character length of a single message object.
 * Used to proportion token deltas by content type without per-message token counts.
 */
function _msgCharLen(msg) {
    if (!msg) return 0;
    let n = 0;
    if (typeof msg.content === 'string') {
        n += msg.content.length;
    } else if (Array.isArray(msg.content)) {
        msg.content.forEach(p => { if (p.type === 'text') n += (p.text || '').length; });
    }
    if (msg.tool_calls) {
        try { n += JSON.stringify(msg.tool_calls).length; } catch (_) {}
    }
    return n;
}

/**
 * Build a context-window usage bar for a turn divider.
 *
 * The bar represents the FULL max_context window.  Used tokens fill from
 * the left; the remaining capacity is visible as empty track.  At early
 * turns the bar is mostly empty — at late turns it fills up, going from
 * dark to vivid as the context saturates.
 *
 * Segments are FLAT flex children (no nesting).  flex-grow = token count,
 * so the browser distributes width in exact proportion to real usage.
 *
 * Content-type colours (estimated via character-count proportioning of
 * new messages in prompt_data):
 *   ctx-seg-base  (gray)        — turn 0: initial system + task context
 *   ctx-seg-asst  (purple)      — prior turn's assistant output carried forward
 *   ctx-seg-tool  (teal)        — tool results, user nudges, system injections
 *   ctx-seg-current             — brightens sub-segments for the current entry
 *   ctx-seg-free  (transparent) — remaining context capacity, labelled with token count
 *
 * Falls back to relative mode (bar = total prompt) when max_context is unknown.
 */
function buildCtxBar(entryId) {
    const fullEntries = cachedSession?.fullEntries;
    if (!fullEntries || fullEntries.length === 0) return '';

    const idx = fullEntries.findIndex(fe => fe.id === entryId);
    if (idx < 0) return '';

    const maxCtx = allDiagLlms[fullEntries[idx].llm_id]?.max_context || 0;
    const barScale = 10000;
    const tokensToScale = (t) => (maxCtx > 0) ? (t / maxCtx * barScale) : t;

    // ── 1. Identify first tool result index (Turn 1) ───────────────

    let firstToolIdx = -1;
    for (let i = 0; i < fullEntries.length; i++) {
        if (i > 0 && (fullEntries[i - 1].tool_calls || 0) > 0) {
            firstToolIdx = i;
            break;
        }
    }
    if (firstToolIdx === -1) firstToolIdx = fullEntries.length;

    // ── 2. Build segments ──────────────────────────────────────────

    let segHtml = '';

    // Turn 0: Merged Setup (SYSTEM/USER split)
    if (firstToolIdx > 0) {
        let setupDelta = 0;
        let sysChars = 0, userChars = 0;
        for (let i = 0; i < firstToolIdx; i++) {
            const pp = fullEntries[i].prompt_cost || 0;
            const prevPp = i > 0 ? (fullEntries[i - 1].prompt_cost || 0) : 0;
            setupDelta += (pp - prevPp);
            
            const msgs = Array.isArray(fullEntries[i].prompt_data) ? fullEntries[i].prompt_data : [];
            const prevMsgs = i > 0 ? (Array.isArray(fullEntries[i-1].prompt_data) ? fullEntries[i-1].prompt_data : []) : [];
            const newMsgs = msgs.slice(prevMsgs.length);
            newMsgs.forEach(m => {
                const len = _msgCharLen(m);
                if (m.role === 'system') sysChars += len;
                else                     userChars += len;
            });
        }

        if (setupDelta > 0) {
            const totalChars = sysChars + userChars || 1;
            const sysDelta = setupDelta * sysChars / totalChars;
            const userDelta = setupDelta * userChars / totalChars;

            const gTotal = Math.max(1, Math.round(tokensToScale(setupDelta)));
            const gSys   = Math.max(1, Math.round(tokensToScale(sysDelta)));
            const gUser  = Math.max(1, Math.round(tokensToScale(userDelta)));

            // Conceptual Segment 0 — highlightable as a single block
            const isSelectedInSetup = idx < firstToolIdx;
            const cur = isSelectedInSetup ? ' ctx-seg-current' : '';
            
            segHtml += `<div class="ctx-seg ${cur} ctx-seg-merged-setup" ` +
                       `style="flex-grow:${gTotal}; display:flex" ` +
                       `data-fe-idx="0" data-merged-end="${firstToolIdx - 1}">` +
                       `<div style="flex-grow:${gSys}; background-color:#5a6370; height:100%" title="Initial Prompt (System)"></div>` +
                       `<div style="flex-grow:${gUser}; background-color:#212529; height:100%" title="Initial Prompt (User)"></div>` +
                       `</div>`;
        }
    }

    // Turns 1 … N: Tool Results
    for (let i = firstToolIdx; i <= idx; i++) {
        const pp = fullEntries[i].prompt_cost || 0;
        const prevPp = i > 0 ? (fullEntries[i - 1].prompt_cost || 0) : 0;
        const delta = pp - prevPp;
        if (delta <= 0) continue;

        const isCurrent = i === idx;
        const cur = isCurrent ? ' ctx-seg-current' : '';

        // Segment i represents the result of the tool call made in Entry i-1.
        let toolCat = 'other';
        try {
            const sourceFe = fullEntries[i - 1];
            const rd = sourceFe?.response_data;
            const tc = (typeof rd === 'string' ? JSON.parse(rd) : rd)?.choices?.[0]?.message?.tool_calls?.[0];
            if (tc) {
                toolCat = labelTool(tc.function?.name || '');
            }
        } catch (_) {}

        const colorHex = TOOL_COLORS[toolCat] || '#94a3b8';
        const g = Math.max(1, Math.round(tokensToScale(delta)));

        segHtml += `<div class="ctx-seg ${cur} tool-cat-${toolCat}" ` +
                   `style="flex-grow:${g}; background-color:${colorHex}" ` +
                   `data-fe-idx="${i}"></div>`;
    }

    // ── 3. Free-space segment ──────────────────────────────────────

    const thisPp = fullEntries[idx].prompt_cost || 0;
    const prevPp = idx > 0 ? (fullEntries[idx - 1].prompt_cost || 0) : 0;
    const delta    = thisPp - prevPp;
    const deltaStr = delta > 0 ? `+${fmtTokens(delta)}`
                   : delta < 0 ? `\u2212${fmtTokens(Math.abs(delta))}`
                   : '\u00b10';

    if (maxCtx > 0) {
        const freeTokens = Math.max(0, maxCtx - thisPp);
        if (freeTokens > 0) {
            const gFree = Math.max(1, Math.round(tokensToScale(freeTokens)));
            const freeTip = `Free: ${freeTokens.toLocaleString()} tokens remaining`;
            segHtml += `<div class="ctx-seg ctx-seg-free" style="flex-grow:${gFree}" ` +
                       `title="${escapeHtml(freeTip)}">` +
                       `<span class="ctx-free-label">${fmtTokens(freeTokens)} free</span></div>`;
        }
    }

    // ── 4. Assemble bar + label ────────────────────────────────────

    let labelHtml;
    if (maxCtx > 0) {
        const pct    = Math.round(thisPp / maxCtx * 100);
        const pctCls = pct >= 90 ? 'ctx-critical' : pct >= 75 ? 'ctx-warn' : pct >= 50 ? 'ctx-caution' : '';
        const title  = `${thisPp.toLocaleString()} / ${maxCtx.toLocaleString()} tokens \u2014 ${pct}%`;
        labelHtml = `<div class="ctx-bar" title="${escapeHtml(title)}">${segHtml}</div>` +
                    `<span class="ctx-bar-label ${pctCls}">${pct}%<span class="ctx-bar-delta"> ${deltaStr}</span></span>`;
    } else {
        const title  = `${thisPp.toLocaleString()} prompt tokens (no max_context set)`;
        labelHtml = `<div class="ctx-bar" title="${escapeHtml(title)}">${segHtml}</div>` +
                    `<span class="ctx-bar-label">${fmtTokens(thisPp)}<span class="ctx-bar-delta"> ${deltaStr}</span></span>`;
    }
    return labelHtml;
}

/**
 * Render a full conversation into the right panel.
 *
 * @param {object}      entry            - Full entry to render (last in session for multi-turn)
 * @param {number|null} highlightFrom    - Message index where the selected entry's turn begins
 * @param {number|null} anchorEntryId   - The entry ID the user actually clicked
 * @param {object|null} selectedFull    - The selected entry's own full data (for its response)
 * @param {Array|null}  sessionBoundaries - Per-entry start message indices
 */
function renderConversation(entry, highlightFrom, anchorEntryId, selectedFull, sessionBoundaries, targetMsgIdx = null) {
    const detail = document.getElementById('conversation-detail');

    const messages = Array.isArray(entry.prompt_data) ? entry.prompt_data : [];
    const respData = entry.response_data || {};
    const choices  = respData.choices || [];
    const respMsg  = choices[0]?.message || {};
    const finish   = choices[0]?.finish_reason || 'unknown';

    // If showing a session snapshot, the selected entry's response is highlighted
    const isSessionView  = highlightFrom !== null && highlightFrom !== undefined;
    const selectedResp   = selectedFull ? (selectedFull.response_data?.choices?.[0]?.message || {}) : respMsg;
    const selectedFinish = selectedFull ? (selectedFull.response_data?.choices?.[0]?.finish_reason || 'unknown') : finish;

    // Detect whether this session genuinely accumulates context (Maestro Loop = yes;
    // separate pipeline stage calls close in time = no). Non-accumulating sessions render
    // the anchor entry's own prompt_data rather than the last entry's.
    const isAccumulating = !isSessionView || (anchorEntryId === entry.id)
        || (messages.length > (highlightFrom ?? messages.length));

    const effectiveMessages   = isAccumulating
        ? messages
        : (Array.isArray(selectedFull?.prompt_data) ? selectedFull.prompt_data : messages);
    const effectiveBoundaries = isAccumulating ? sessionBoundaries : null;
    const effectiveHighlight  = isAccumulating ? highlightFrom : null;

    // Infer type from first system message; fall back to first user message for system-less calls
    const firstSys  = effectiveMessages.find(m => m.role === 'system');
    const firstUser = effectiveMessages.find(m => m.role === 'user');
    const entryType = firstSys
        ? labelEntry(firstSys.content || '')
        : labelEntryFromUser(typeof firstUser?.content === 'string' ? firstUser.content : '');

    // Update the type dot for the selected entry in the middle panel
    const dotId = anchorEntryId || entry.id;
    const dot = document.getElementById(`dot-${dotId}`);
    if (dot) dot.className = `diag-entry-dot type-${entryType}`;

    // Header: for accumulating sessions show "selected → last"; otherwise just selected
    const displayId = (isSessionView && isAccumulating)
        ? `#${anchorEntryId} <span style="color:#adb5bd;font-weight:400">in session → #${entry.id}</span>`
        : `#${anchorEntryId || entry.id}`;

    const headerEntry = selectedFull || entry;

    document.getElementById('detail-header').innerHTML =
        `<span>CONVERSATION</span>
         <span class="diag-conv-id">${displayId}</span>
         <span class="diag-conv-type-label type-${entryType}">${escapeHtml(entryType)}</span>`;

    // Finish badge reflects the SELECTED entry's result, not the last entry's
    const finishForBadge = selectedFinish;
    const finishClass    = `finish-${finishForBadge}`;
    const finishLabel    = finishForBadge === 'length'     ? 'HIT TOKEN LIMIT'
                         : finishForBadge === 'tool_calls' ? 'MADE TOOL CALLS'
                         : finishForBadge === 'stop'       ? 'COMPLETED'
                         : finishForBadge.toUpperCase();

    let html = buildSessionSummary(anchorEntryId || entry.id);

    const llmHeaderName = (allDiagLlms[headerEntry.llm_id]?.name) || headerEntry.llm_id || null;
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

    const groupEntries = cachedSession?.fullEntries || [headerEntry];
    const conceptualTurns = getConceptualTurns(groupEntries);

    // Parallel session: all entries share the same prompt (no accumulating context).
    // Each entry is an independent response — render them all side-by-side rather than
    // trying to stitch them into a single accumulating conversation.
    const isParallelSession = isSessionView
        && groupEntries.length > 1
        && (groupEntries[groupEntries.length - 1].prompt_data?.length ?? 0) === (groupEntries[0].prompt_data?.length ?? 0)
        && (groupEntries[0].prompt_data?.length ?? 0) > 0;

    if (isParallelSession) {
        // Shared prompt (system + user messages, identical for every entry)
        const sharedMsgs = Array.isArray(groupEntries[0].prompt_data) ? groupEntries[0].prompt_data : effectiveMessages;
        html += `<div class="diag-turn-divider diag-turn-divider-other" data-entry-id="${groupEntries[0].id}" data-msg-idx="0">` +
                `<span class="diag-divider-label">── SYSTEM Prompt (shared · ${groupEntries.length} parallel requests) ──</span>` +
                `</div>`;
        const sharedGrouped = groupMessages(sharedMsgs, []);
        let msgIdx = 0;
        sharedGrouped.forEach(g => {
            if (g.type === 'tool_group') {
                html += renderToolGroup(g.messages, msgIdx, false);
                msgIdx += g.messages.length;
            } else {
                html += renderMessage(g.message, msgIdx, false);
                msgIdx++;
            }
        });

        // One labeled response block per entry
        groupEntries.forEach((fe, i) => {
            const isAnchor = fe.id === anchorEntryId;
            const divClass = isAnchor
                ? 'diag-turn-divider diag-turn-divider-anchor'
                : 'diag-turn-divider diag-turn-divider-other';
            const anchorAttr = isAnchor ? ' id="turn-anchor"' : '';
            const ctxBar = buildCtxBar(fe.id);
            html += `<div class="${divClass}${ctxBar ? ' has-ctx-bar' : ''}"${anchorAttr} data-entry-id="${fe.id}">` +
                    `<span class="diag-divider-label">── Parallel Request ${i + 1} of ${groupEntries.length} (#${fe.id}) ──</span>` +
                    ctxBar +
                    `</div>`;

            const feMsg       = fe.response_data?.choices?.[0]?.message || {};
            const feFinish    = fe.response_data?.choices?.[0]?.finish_reason || 'unknown';
            const feContent   = feMsg.content || '';
            const feToolCalls = feMsg.tool_calls || [];
            const feReasoning = feMsg.reasoning_content || '';

            if (feContent || feToolCalls.length > 0 || feReasoning) {
                const hlClass = isAnchor ? ' msg-highlighted' : '';
                const feFinishLabel = feFinish === 'stop'   ? 'COMPLETED'
                                    : feFinish === 'length' ? 'HIT TOKEN LIMIT'
                                    : feFinish.toUpperCase();
                html += `<div class="diag-msg msg-assistant${hlClass}">
                    <div class="diag-msg-header">
                        <span class="diag-msg-idx">[REQUEST ${i + 1}]</span>
                        <span>ASSISTANT</span>
                        <span class="diag-finish-badge finish-${feFinish}" style="font-size:0.75em">${escapeHtml(feFinishLabel)}</span>
                        ${feToolCalls.length > 0 ? `<span>&#8595; ${feToolCalls.length} tool call(s)</span>` : ''}
                    </div>
                    <div class="diag-msg-body">`;
                if (feReasoning) {
                    html += `<div class="diag-reasoning-toggle" onclick="toggleReasoning(this)">` +
                            `&#9658; Reasoning (${feReasoning.length.toLocaleString()} chars) — click to expand` +
                            `</div><div class="diag-reasoning-body" style="display:none">${escapeHtml(feReasoning)}</div>`;
                }
                if (feToolCalls.length > 0) feToolCalls.forEach(tc => { html += renderToolCall(tc); });
                if (feContent) {
                    html += `<div class="diag-msg-text"${(feReasoning || feToolCalls.length) ? ' style="margin-top:0.5rem"' : ''}>${escapeHtml(feContent)}</div>`;
                }
                html += `</div></div>`;
            }
        });

        html += `<div class="diag-turn-divider diag-turn-divider-end">` +
                `<span>── end of parallel session (${groupEntries.length} parallel requests) ──</span>` +
                `</div>`;

    } else {

    // Precise msgIdx for all conceptual turns
    conceptualTurns.forEach(t => {
        const b = (effectiveBoundaries || []).find(b => b.entryId === t.entryId);
        if (!b) return;
        if (t.type === 'system' || t.type === 'turn') {
            t.msgIdx = b.startMsgIdx;
        } else if (t.type === 'user') {
            const firstUserIdx = effectiveMessages.slice(b.startMsgIdx).findIndex(m => m.role === 'user');
            t.msgIdx = (firstUserIdx !== -1) ? b.startMsgIdx + firstUserIdx : b.startMsgIdx;
        }
    });

    // Ensure groupMessages breaks at EVERY conceptual turn boundary
    const allBoundaries = conceptualTurns.map(t => ({ startMsgIdx: t.msgIdx, entryId: t.entryId }));
    const grouped = groupMessages(effectiveMessages, allBoundaries);

    let msgIdx = 0;
    grouped.forEach(g => {
        // Render any turn dividers that start at this msgIdx
        const turnsToRender = conceptualTurns.filter(t => t.msgIdx === msgIdx);
        turnsToRender.forEach(turn => {
            const isAnchor = turn.entryId === anchorEntryId &&
                (targetMsgIdx === null || targetMsgIdx === turn.msgIdx || (turn.type === 'user' && targetMsgIdx === -1));

            const divClass = isAnchor
                ? 'diag-turn-divider diag-turn-divider-anchor'
                : 'diag-turn-divider diag-turn-divider-other';
            const anchorAttr = isAnchor ? ' id="turn-anchor"' : '';

            const ctxBar = buildCtxBar(turn.entryId);
            const dataMsgIdx = turn.msgIdx !== undefined ? ` data-msg-idx="${turn.msgIdx}"` : '';
            html += `<div class="${divClass}${ctxBar ? ' has-ctx-bar' : ''}"${anchorAttr} data-entry-id="${turn.entryId}"${dataMsgIdx}>` +
                    `<span class="diag-divider-label">── ${escapeHtml(turn.label)} ──</span>` +
                    ctxBar +
                    `</div>`;
        });

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
    // (i.e. the next entry's prompt already carries the response forward).
    const responseInContext = isAccumulating && isSessionView
        && (anchorEntryId !== entry.id) && messages.length > highlightFrom;
    const isLastEntry = !responseInContext;
    const reasoning   = selectedResp.reasoning_content || '';
    const content     = selectedResp.content || '';
    const toolCalls   = selectedResp.tool_calls || [];

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

    // Abrupt end: the last entry's LLM response ended with tool_calls but no tool result was ever received.
    // This means the session was interrupted mid-flight (context exhausted, loop stopped, crash, etc.).
    const isActualLastEntry = !selectedFull || selectedFull.id === entry.id;
    const isAbruptEnd = isLastEntry && isActualLastEntry && selectedFinish === 'tool_calls';
    if (isAbruptEnd) {
        html += `<div class="diag-abrupt-end">
            <span class="diag-abrupt-icon">&#9888;</span>
            <strong>ABRUPT END</strong> — session terminated while tool call was pending;
            no tool result was ever received by the LLM.
        </div>`;
    }

    if (isSessionView && anchorEntryId === entry.id) {
        const endLabel  = isAbruptEnd ? '── SESSION ABORTED ──' : '── end of session ──';
        const endClass  = isAbruptEnd ? 'diag-turn-divider-abort' : 'diag-turn-divider-end';
        html += `<div class="diag-turn-divider ${endClass}">
            <span>${endLabel}</span>
        </div>`;
    }

    } // end !isParallelSession

    html += '</div>'; // .diag-messages
    detail.innerHTML = html;

    if (isSessionView) {
        requestAnimationFrame(() => {
            const anchor = detail.querySelector('#turn-anchor');
            if (anchor) anchor.scrollIntoView({ behavior: 'smooth', block: 'start' });
        });
    }
}

// ── Individual message rendering ──────────────────────────────

function renderMessage(msg, index, highlighted) {
    const role    = msg.role || 'unknown';
    const content = typeof msg.content === 'string' ? msg.content
                  : Array.isArray(msg.content)
                    ? msg.content.filter(p => p.type === 'text').map(p => p.text).join('\n')
                    : '';
    const toolCalls     = msg.tool_calls || [];
    const reasoningCont = msg.reasoning_content || '';

    // [SYSTEM] injected warnings — render as banner instead of a user message bubble
    if (role === 'user' && content.startsWith('[SYSTEM]')) {
        return renderSystemWarning(content);
    }

    const roleClass = `msg-${role}${highlighted ? ' msg-highlighted' : ''}`;
    let label = role.toUpperCase();
    if (role === 'tool') label = 'TOOL RESULT';
    if (toolCalls.length > 0) label += ` ↓ ${toolCalls.length} tool call(s)`;

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

    // Tool results: collapsible, no bodyHtml wrapping
    if (role === 'tool') {
        const bodyId    = `tr-body-${index}`;
        const lineCount = content ? content.split('\n').length : 0;
        const sizeHint  = lineCount > 0 ? ` (${lineCount} line${lineCount !== 1 ? 's' : ''})` : '';
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
    if (/CRITICAL|forced/i.test(content))       cls = 'warn-critical';
    else if (/context.?window/i.test(content))  cls = 'warn-context';
    else if (/turns.?remaining/i.test(content)) cls = 'warn-turns';
    return `<div class="diag-system-warn ${cls}">&#9888; ${escapeHtml(content)}</div>`;
}

// ── Interactive toggles ───────────────────────────────────────

function toggleToolResult(bodyId, header) {
    const body   = document.getElementById(bodyId);
    const toggle = header.querySelector('.diag-tool-result-toggle');
    const hint   = header.querySelector('.diag-tool-result-hint');
    if (!body || !toggle) return;
    const collapsed    = body.style.display === 'none';
    body.style.display = collapsed ? 'block' : 'none';
    toggle.textContent = collapsed ? '▾' : '▸';
    if (hint) hint.textContent = hint.textContent.replace(
        collapsed ? 'expand' : 'collapse',
        collapsed ? 'collapse' : 'expand'
    );
}

function toggleReasoning(el) {
    const body      = el.nextElementSibling;
    const collapsed = body.style.display === 'none';
    body.style.display = collapsed ? 'block' : 'none';
    const chars = body.textContent.length;
    el.innerHTML = collapsed
        ? `&#9660; Reasoning (${chars.toLocaleString()} chars) — click to collapse`
        : `&#9658; Reasoning (${chars.toLocaleString()} chars) — click to expand`;
}

// ── Context-bar segment hover tooltip ────────────────────────
//
// JS-driven tooltip (not CSS ::after) so it renders at a fixed font
// size regardless of any scaleY transform on the segment.
// Shows: agent-type badge (colour-coded) + context % + tool call detail.

(function _initCtxTooltip() {
    const tip = document.createElement('div');
    tip.id = 'ctx-tooltip';
    document.body.appendChild(tip);
    let _lastSeg = null;

    function _show(seg, mouseX) {
        const feIdx = parseInt(seg.dataset.feIdx, 10);
        if (isNaN(feIdx)) return;
        const fullEntries = cachedSession?.fullEntries;
        if (!fullEntries) return;

        // Merged Segment 0 (Initial Prompt)
        if (feIdx === 0 && seg.classList.contains('ctx-seg-merged-setup')) {
            const mergedEndIdx = parseInt(seg.dataset.mergedEnd, 10) || 0;
            const fe = fullEntries[mergedEndIdx];
            const pp = fe.prompt_cost || 0;
            const total = pp + (fe.generation_cost || 0);
            const llmInfo = allDiagLlms[fe.llm_id] || {};
            const maxCtx = llmInfo.max_context || 0;
            const pctTotal = maxCtx > 0 ? `${(total / maxCtx * 100).toFixed(1)}% of total context` : '';
            const stats = pctTotal ? `${pctTotal} (${fmtTokens(total)})` : fmtTokens(total);

            tip.innerHTML = `<div><span class="ctx-tip-agent" style="background:#6c757d;color:#fff">SETUP</span>` +
                            `<span class="ctx-tip-stats">${escapeHtml(stats)}</span></div>` +
                            `<div class="ctx-tip-tool">Initial Prompt</div>`;
        } else {
            const fe = fullEntries[feIdx];
            if (!fe) return;

            // Token counts
            const pp     = fe.prompt_cost || 0;
            const tg     = fe.generation_cost || 0;
            const prevPp = feIdx > 0 ? (fullEntries[feIdx - 1]?.prompt_cost || 0) : 0;
            const delta  = feIdx > 0 ? pp - prevPp : pp;

            // Agent type (Tooltip uses the agent color from the source call)
            const sourceFe = feIdx > 0 ? fullEntries[feIdx - 1] : fe;
            const labelMsgs = Array.isArray(sourceFe.prompt_data) ? sourceFe.prompt_data : [];
            const labelSys  = labelMsgs.find(m => m.role === 'system');
            const labelUser = labelMsgs.find(m => m.role === 'user');
            const agentType = labelSys
                ? labelEntry(typeof labelSys.content === 'string' ? labelSys.content : '')
                : labelEntryFromUser(typeof labelUser?.content === 'string' ? labelUser.content : '');
            const agentColor = TYPE_COLORS[agentType] || '#6c757d';
            const agentText  = agentType === 'research' ? '#212529' : '#fff';

            // Context stats
            const llmInfo  = allDiagLlms[fe.llm_id] || {};
            const maxCtx   = llmInfo.max_context || 0;
            const total    = pp + tg;
            const pctTotal = maxCtx > 0 ? `${(total / maxCtx * 100).toFixed(1)}% of total context` : '';
            const line1Stats = pctTotal ? `${pctTotal} (${fmtTokens(total)})` : fmtTokens(total);

            const sign = v => (v >= 0 ? '+' : '') + fmtTokens(v);
            let toolName = '';
            const argLines = [];
            try {
                // Segment feIdx represents Entry feIdx. Prompt feIdx contains result of call from feIdx-1.
                const rd = sourceFe.response_data;
                const tc = (typeof rd === 'string' ? JSON.parse(rd) : rd)?.choices?.[0]?.message?.tool_calls?.[0];
                if (tc) {
                    toolName = `${tc.function?.name || 'tool'}()`;
                    const rawArgs = tc.function?.arguments;
                    const args = (rawArgs && typeof rawArgs === 'object') ? rawArgs : JSON.parse(rawArgs || '{}');
                    Object.keys(args).forEach(k => {
                        argLines.push(`ARG "${k}": "${String(args[k]).slice(0, 40)}"`);
                    });
                }
            } catch (_) {}

            const line2 = `#${fe.id} ${toolName}`;

            let html =
                `<div><span class="ctx-tip-agent" style="background:${agentColor};color:${agentText}">${escapeHtml(agentType)}</span>` +
                `<span class="ctx-tip-stats">${escapeHtml(line1Stats)}</span></div>` +
                `<div class="ctx-tip-tool">${escapeHtml(line2)}</div>`;
            argLines.forEach(al => {
                html += `<div class="ctx-tip-tool ctx-tip-arg">${escapeHtml(al)}</div>`;
            });
            tip.innerHTML = html;
        }

        // Position above the bar, horizontally centred on cursor (clamped to viewport)
        const bar     = seg.closest('.ctx-bar');
        const barRect = bar.getBoundingClientRect();
        tip.style.display = 'block';
        const tipW = tip.offsetWidth;
        const tipH = tip.offsetHeight;
        const left   = Math.max(4, Math.min(window.innerWidth - tipW - 4, mouseX - tipW / 2));
        const topAbove = barRect.top - tipH - 6;
        const top    = topAbove < 4 ? barRect.bottom + 6 : topAbove;
        tip.style.left = `${left}px`;
        tip.style.top  = `${top}px`;
    }

    function _hide() {
        if (!_lastSeg) return;
        _lastSeg = null;
        tip.style.display = 'none';
    }

    document.addEventListener('mousemove', e => {
        const seg = e.target.closest('.ctx-seg:not(.ctx-seg-free)');
        if (seg) { _lastSeg = seg; _show(seg, e.clientX); }
        else     _hide();
    });

    // Click any context-bar segment to jump to that entry (same as picking it from the list)
    document.addEventListener('click', e => {
        const seg = e.target.closest('.ctx-seg:not(.ctx-seg-free)');
        if (!seg) return;
        const feIdx = parseInt(seg.dataset.feIdx, 10);
        if (isNaN(feIdx)) return;
        const fe = cachedSession?.fullEntries?.[feIdx];
        if (fe) selectEntry(fe.id);
    });
})();

// ── Init ─────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', loadTasks);
