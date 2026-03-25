/* ============================================================
   diag-render.js — Right panel: conversation and message rendering
   Depends on: diag-utils.js, diag-session.js (buildSessionSummary, groupMessages, renderToolGroup)
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

    const thisPp = fullEntries[idx].prompt_cost || 0;
    if (thisPp <= 0) return '';

    const maxCtx = allDiagLlms[fullEntries[idx].llm_id]?.max_context || 0;

    // ── Build coloured segments for each turn 0 … idx ──────────────

    let segHtml = '';
    for (let i = 0; i <= idx; i++) {
        const pp     = fullEntries[i].prompt_cost || 0;
        const prevPp = i > 0 ? (fullEntries[i - 1].prompt_cost || 0) : 0;
        const delta  = i > 0 ? pp - prevPp : pp;
        if (delta <= 0) continue;

        const isCurrent = i === idx;

        // Split delta by content type via character-count proportioning.
        let baseTokens = 0, asstTokens = 0, toolTokens = 0;
        if (i === 0) {
            baseTokens = delta;
        } else {
            const prevMsgs = Array.isArray(fullEntries[i - 1].prompt_data) ? fullEntries[i - 1].prompt_data : [];
            const currMsgs = Array.isArray(fullEntries[i].prompt_data)     ? fullEntries[i].prompt_data     : [];
            const newMsgs  = currMsgs.slice(prevMsgs.length);
            if (newMsgs.length === 0) {
                toolTokens = delta;
            } else {
                let asstChars = 0, toolChars = 0;
                newMsgs.forEach(m => {
                    const len = _msgCharLen(m);
                    if (m.role === 'assistant') asstChars += len;
                    else                        toolChars += len;
                });
                const total = asstChars + toolChars || 1;
                asstTokens = delta * asstChars / total;
                toolTokens = delta * toolChars / total;
            }
        }

        // One segment per entry — total delta tokens, colour by dominant content type.
        // Previously split into base/asst/tool sub-segments, which produced multiple
        // clickable elements with the same entry ID and confused the hover tooltip.
        const cur      = isCurrent ? ' ctx-seg-current' : '';
        const rawTotal = baseTokens + asstTokens + toolTokens;
        const g        = Math.max(1, Math.round(rawTotal));
        const colorCls = i === 0                  ? 'ctx-seg-base'
                       : asstTokens >= toolTokens ? 'ctx-seg-asst'
                       :                            'ctx-seg-tool';
        segHtml += `<div class="ctx-seg ${colorCls}${cur}" style="flex-grow:${g}" ` +
                   `data-fe-idx="${i}" data-orig-grow="${g}"></div>`;
    }

    // ── Free-space segment (remaining context capacity) ────────────

    const prevPp   = idx > 0 ? (fullEntries[idx - 1].prompt_cost || 0) : 0;
    const delta    = thisPp - prevPp;
    const deltaStr = delta > 0 ? `+${fmtTokens(delta)}`
                   : delta < 0 ? `\u2212${fmtTokens(Math.abs(delta))}`
                   : '\u00b10';

    if (maxCtx > 0) {
        const freeTokens = Math.max(0, maxCtx - thisPp);
        if (freeTokens > 0) {
            const freeTip = `Free: ${freeTokens.toLocaleString()} tokens remaining`;
            segHtml += `<div class="ctx-seg ctx-seg-free" style="flex-grow:${Math.round(freeTokens)}" ` +
                       `title="${escapeHtml(freeTip)}">` +
                       `<span class="ctx-free-label">${fmtTokens(freeTokens)} free</span></div>`;
        }
    }

    // ── Assemble bar + label ───────────────────────────────────────

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
function renderConversation(entry, highlightFrom, anchorEntryId, selectedFull, sessionBoundaries) {
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

    // Infer type from first system message of the anchor entry's context
    const firstSys  = effectiveMessages.find(m => m.role === 'system');
    const entryType = labelEntry(firstSys?.content || '');

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
                const ctxBar = buildCtxBar(boundary.entryId);
                html += `<div class="${divClass}${ctxBar ? ' has-ctx-bar' : ''}"${anchorAttr} data-entry-id="${boundary.entryId}">` +
                        `<span class="diag-divider-label">── Entry #${boundary.entryId} ──</span>` +
                        ctxBar +
                        `</div>`;
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

    if (isSessionView && anchorEntryId === entry.id) {
        html += `<div class="diag-turn-divider diag-turn-divider-end">
            <span>── end of session ──</span>
        </div>`;
    }

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

// ── macOS Dock-style magnification on context-bar segments ───
//
// Horizontal growth is applied via flex-grow (not scaleX) so segments
// actually displace their neighbours — a true lens effect.  Vertical
// growth uses scaleY with transform-origin:center so segments grow
// symmetrically (equal up/down), capped at 112.5% to keep the bar tidy.

(function _initDockZoom() {
    const MAX_SCALE_X  = 5;     // peak horizontal factor — applied as flex-grow multiplier
    const MAX_SCALE_Y  = 1.0; // peak vertical factor   — applied as scaleY (±6.25% each way)
    const INFLUENCE_PX = 80;    // ~¼ inch at 96 CSS-DPI

    let _activeBar = null;
    // Natural (pre-zoom) widths per bar, measured before the first zoom frame.
    // Using the live r.width for the large-segment threshold causes oscillation:
    // neighbouring small segs magnify → large seg gets squeezed below the threshold
    // → large seg gets zoomed → it grows → back above threshold → reset → repeat.
    const _naturalWidths = new WeakMap(); // bar → Map<seg, naturalWidthPx>

    function _cacheNaturalWidths(bar) {
        const cache = new Map();
        bar.querySelectorAll('.ctx-seg:not(.ctx-seg-free)').forEach(seg => {
            cache.set(seg, seg.getBoundingClientRect().width);
        });
        _naturalWidths.set(bar, cache);
    }

    function _apply(bar, mouseX) {
        const barRect  = bar.getBoundingClientRect();
        const relX     = mouseX - barRect.left;
        const segs     = bar.querySelectorAll('.ctx-seg:not(.ctx-seg-free)');
        const natCache = _naturalWidths.get(bar);

        bar.classList.add('dock-zooming');

        segs.forEach(seg => {
            const r        = seg.getBoundingClientRect();
            const origGrow = parseFloat(seg.dataset.origGrow) || 1;
            // Use the stable pre-zoom width for the threshold (live r.width oscillates
            // when neighbouring segs are magnified, causing flicker on large segments).
            const natWidth = natCache ? (natCache.get(seg) ?? r.width) : r.width;

            // Skip magnification for segments already wider than the influence diameter —
            // zooming a large segment further adds no usability value.
            if (natWidth >= INFLUENCE_PX * 2) {
                seg.style.flexGrow  = origGrow;
                seg.style.transform = '';
                seg.style.zIndex    = '';
                return;
            }

            const cx     = r.left + r.width / 2 - barRect.left;
            // If cursor is inside this segment clamp dist=0 so it always gets max zoom.
            // Neighbours use normal mouse-to-centre distance for the cosine falloff.
            const inside = mouseX >= r.left && mouseX <= r.right;
            const dist   = inside ? 0 : Math.abs(relX - cx);

            if (dist >= INFLUENCE_PX) {
                seg.style.flexGrow  = origGrow;
                seg.style.transform = '';
                seg.style.zIndex    = '';
            } else {
                // Cosine falloff: 1.0 at dist=0, 0.0 at dist=INFLUENCE_PX
                const t            = Math.cos((Math.PI / 2) * (dist / INFLUENCE_PX));
                const factor_x_raw = 1 + (MAX_SCALE_X - 1) * t;
                // Cap: no segment zooms wider than INFLUENCE_PX*2 px regardless of scale factor.
                // Without this, medium-large segments balloon while truly large ones are skipped,
                // producing an inverted size relationship under the cursor.
                const factor_x = natWidth > 0
                    ? Math.min(factor_x_raw, (INFLUENCE_PX * 2) / natWidth)
                    : factor_x_raw;
                const factor_y = 1 + (MAX_SCALE_Y - 1) * t;
                seg.style.flexGrow  = origGrow * factor_x;
                seg.style.transform = `scaleY(${factor_y.toFixed(3)})`;
                seg.style.zIndex    = Math.round(factor_x * 10);
            }
        });
    }

    function _reset(bar) {
        bar.classList.remove('dock-zooming');
        bar.querySelectorAll('.ctx-seg:not(.ctx-seg-free)').forEach(seg => {
            seg.style.flexGrow  = seg.dataset.origGrow || 1;
            seg.style.transform = '';
            seg.style.zIndex    = '';
        });
    }

    document.addEventListener('mousemove', e => {
        const bar = e.target.closest('.ctx-bar');
        if (_activeBar && _activeBar !== bar) { _reset(_activeBar); _activeBar = null; }
        if (bar) {
            // Measure natural widths before the first zoom frame so the threshold
            // check in _apply sees stable values regardless of neighbour zoom state.
            if (_activeBar !== bar) _cacheNaturalWidths(bar);
            _activeBar = bar;
            _apply(bar, e.clientX);
        }
    });

    document.addEventListener('mouseleave', () => {
        if (_activeBar) { _reset(_activeBar); _activeBar = null; }
    });
})();

// ── Context-bar segment hover tooltip ────────────────────────
//
// JS-driven tooltip (not CSS ::after) so it renders at a fixed font
// size regardless of any scaleY transform on the segment.
// Shows: agent-type badge (colour-coded) + context % + tool call detail.

(function _initCtxTooltip() {
    const TYPE_COLORS = {
        surveyor:     '#0d6efd',
        designer:     '#6f42c1',
        reviewer:     '#20c997',
        judge:        '#fd7e14',
        research:     '#ffc107',
        pitfall:      '#e83e8c',
        maestro_loop: '#198754',
        unknown:      '#6c757d',
    };

    const tip = document.createElement('div');
    tip.id = 'ctx-tooltip';
    document.body.appendChild(tip);
    let _lastSeg = null;

    function _show(seg, mouseX) {
        const feIdx = parseInt(seg.dataset.feIdx, 10);
        if (isNaN(feIdx)) return;
        const fullEntries = cachedSession?.fullEntries;
        if (!fullEntries) return;
        const fe = fullEntries[feIdx];
        if (!fe) return;

        // Token counts
        const pp     = fe.prompt_cost || 0;
        const tg     = fe.generation_cost || 0;
        const prevPp = feIdx > 0 ? (fullEntries[feIdx - 1]?.prompt_cost || 0) : 0;
        const delta  = feIdx > 0 ? pp - prevPp : pp;

        // Agent type — for turn 0 the system message belongs to the orchestrator that
        // launched the session, not the agent running it.  Borrow turn 1's label instead
        // so the badge reflects the actual session type (e.g. RESEARCH not MAESTRO_LOOP).
        const labelFe   = (feIdx === 0 && fullEntries.length > 1) ? fullEntries[1] : fe;
        const labelMsgs = Array.isArray(labelFe.prompt_data) ? labelFe.prompt_data : [];
        const labelSys  = labelMsgs.find(m => m.role === 'system');
        const rawSys    = labelSys
            ? (typeof labelSys.content === 'string'
                ? labelSys.content
                : (Array.isArray(labelSys.content) ? (labelSys.content[0]?.text || '') : ''))
            : '';
        const agentType  = labelEntry(rawSys);
        const agentColor = TYPE_COLORS[agentType] || '#6c757d';
        const agentText  = agentType === 'research' ? '#212529' : '#fff';

        // Line 1: (pp+tg) / max_context — full context footprint of this call
        const llmInfo  = allDiagLlms[fe.llm_id] || {};
        const maxCtx   = llmInfo.max_context || 0;
        const total    = pp + tg;
        const pctTotal = maxCtx > 0 ? `${(total / maxCtx * 100).toFixed(1)}% of total context` : '';
        const line1Stats = pctTotal ? `${pctTotal} (${fmtTokens(total)})` : fmtTokens(total);

        // Line 2: #id  toolName  cost(+tg △ TG): +delta △ PP
        // Lines 3+: one ARG per parameter — all args consistent on their own lines
        const sign = v => (v >= 0 ? '+' : '') + fmtTokens(v);
        let toolPart = '';
        const argLines = [];
        try {
            // response_data may arrive as a string (TEXT column) — parse defensively.
            let rd = fe.response_data;
            if (typeof rd === 'string') rd = JSON.parse(rd);
            const tc = rd?.choices?.[0]?.message?.tool_calls?.[0];
            if (tc) {
                const toolName = tc.function?.name || 'tool';
                // Set toolPart BEFORE parsing args — if args parsing throws the name still shows.
                toolPart = ` ${toolName}`;
                // llama.cpp may return arguments as an already-parsed object, not a string.
                const rawArgs = tc.function?.arguments;
                const args = (rawArgs && typeof rawArgs === 'object')
                    ? rawArgs
                    : JSON.parse(rawArgs || '{}');
                Object.keys(args).forEach(k => {
                    argLines.push(`ARG "${k}": "${String(args[k]).slice(0, 40)}"`);
                });
            } else if (feIdx === 0) {
                // Turn 0 = initial session context.  Clean label on line 2; recursively
                // expand the response JSON so arrays and objects each get their own lines.
                toolPart = `  Initial Prompt`;
                const content = rd?.choices?.[0]?.message?.content;
                if (content && typeof content === 'string') {
                    // Recursive line builder.
                    // Strings/numbers/bools → one line: [pad]"key": "value"
                    // Arrays               → header line [pad]"key":
                    //                        then each item indented one level
                    // Objects              → header line [pad]"key":
                    //                        then each sub-pair indented one level
                    const NB  = '\u00a0'; // non-breaking space — survives escapeHtml
                    const ELL = '\u2026';
                    const MAX = 55;
                    function fmtScalar(v) {
                        const s = v === null ? 'null' : String(v);
                        return `"${s.slice(0, MAX)}${s.length > MAX ? ELL : ''}"`;
                    }
                    function pushVal(lines, key, val, depth) {
                        if (depth > 3) return; // safety cap
                        const pad = NB.repeat(depth * 2);
                        const keyStr = key !== null ? `"${key}": ` : '';
                        if (Array.isArray(val)) {
                            if (val.length === 0) {
                                lines.push(`${pad}${keyStr}[]`);
                            } else {
                                lines.push(`${pad}${keyStr}`);
                                val.forEach(item => {
                                    if (item !== null && typeof item === 'object') {
                                        Object.entries(item).forEach(([k, v]) => pushVal(lines, k, v, depth + 1));
                                    } else {
                                        lines.push(`${NB.repeat((depth + 1) * 2)}${fmtScalar(item)}`);
                                    }
                                });
                            }
                        } else if (val !== null && typeof val === 'object') {
                            lines.push(`${pad}${keyStr}`);
                            Object.entries(val).forEach(([k, v]) => pushVal(lines, k, v, depth + 1));
                        } else {
                            lines.push(`${pad}${keyStr}${fmtScalar(val)}`);
                        }
                    }
                    try {
                        const raw    = content.trim().replace(/^```[a-z]*\n?|\n?```$/g, '');
                        const parsed = JSON.parse(raw);
                        if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
                            Object.entries(parsed).forEach(([k, v]) => pushVal(argLines, k, v, 0));
                        }
                    } catch (_) {
                        const trimmed = content.trim();
                        if (trimmed) argLines.push(`"${trimmed.slice(0, MAX)}${trimmed.length > MAX ? ELL : ''}"`);
                    }
                }
            } else {
                // Non-tool-call turn — show first ~50 chars of text content as context
                const content = rd?.choices?.[0]?.message?.content;
                if (content && typeof content === 'string' && content.trim()) {
                    toolPart = `  "${content.trim().slice(0, 50)}${content.trim().length > 50 ? '\u2026' : ''}"`;
                }
            }
        } catch (_) {}
        const line2 = `#${fe.id}${toolPart}  cost(${sign(tg)} \u25b3 TG): ${sign(delta)} \u25b3 PP`;

        let html =
            `<div><span class="ctx-tip-agent" style="background:${agentColor};color:${agentText}">${escapeHtml(agentType)}</span>` +
            `<span class="ctx-tip-stats">${escapeHtml(line1Stats)}</span></div>` +
            `<div class="ctx-tip-tool">${escapeHtml(line2)}</div>`;
        argLines.forEach(al => {
            html += `<div class="ctx-tip-tool ctx-tip-arg">${escapeHtml(al)}</div>`;
        });
        tip.innerHTML = html;

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
