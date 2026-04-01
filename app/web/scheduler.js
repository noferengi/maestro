/* scheduler.js — live Scheduler Status page
   Auto-refreshes every 3 seconds. */

const REFRESH_MS = 3000;
let _lastFetch = 0;
let _timer = null;

// ── Helpers ──────────────────────────────────────────────────────────────────

function esc(str) {
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}

function typeBadgeClass(type) {
    const known = ['idea','planning','indev','conceptual_review','optimization','full_review','file_summary'];
    return known.includes(type) ? `type-${type}` : 'type-other';
}

function reasonBadgeClass(reason) {
    if (!reason) return '';
    if (reason.startsWith('cooldown')) return 'reason-cooldown';
    if (reason === 'at_capacity') return 'reason-at_capacity';
    if (reason === 'no_llm') return 'reason-no_llm';
    return 'reason-pending';
}

function reasonLabel(reason) {
    if (!reason || reason === 'pending') return 'ready';
    if (reason === 'at_capacity') return 'at capacity';
    if (reason === 'no_llm') return 'no LLM';
    return reason; // e.g. "cooldown (42s)"
}

// ── Card builders ─────────────────────────────────────────────────────────────

function buildTaskCard(task, variant) {
    const typeClass = typeBadgeClass(task.type);
    const diagLink = `/diagnostics?task=${esc(task.id)}`;

    let extra = '';

    if (variant === 'queued' && task.reason) {
        const rc = reasonBadgeClass(task.reason);
        extra = `<span class="sched-badge ${rc}">${esc(reasonLabel(task.reason))}</span>`;
    }

    if (variant === 'blocked' && task.blocking_titles && task.blocking_titles.length) {
        const pills = task.blocking_titles
            .map(t => `<span class="sched-prereq-pill" title="${esc(t)}">${esc(t)}</span>`)
            .join('');
        extra = `<div class="sched-task-prereqs">${pills}</div>`;
    }

    return `
        <div class="sched-task is-${variant}">
            <div class="sched-task-title">
                <a href="${diagLink}" target="_blank" style="color:inherit;text-decoration:none;"
                   title="Open in diagnostics">${esc(task.title)}</a>
            </div>
            <div class="sched-task-meta">
                <span class="sched-badge ${typeClass}">${esc(task.type)}</span>
                ${task.project ? `<span class="sched-task-project" title="${esc(task.project)}">${esc(task.project)}</span>` : ''}
                ${task.llm_name ? `<span class="sched-task-llm" title="${esc(task.llm_name)}">${esc(task.llm_name)}</span>` : ''}
                ${extra && variant === 'queued' ? extra : ''}
            </div>
            ${extra && variant === 'blocked' ? extra : ''}
        </div>`;
}

// ── Render ────────────────────────────────────────────────────────────────────

function render(data) {
    // Status bar
    const dot   = document.getElementById('sched-dot');
    const label = document.getElementById('sched-label');
    if (data.running) {
        dot.className = 'sched-dot running';
        label.textContent = 'Running';
    } else {
        dot.className = 'sched-dot stopped';
        label.textContent = 'Stopped';
    }

    document.getElementById('tick-interval').textContent    = data.tick_interval ?? '—';
    document.getElementById('pending-research').textContent = data.pending_research_jobs ?? 0;
    document.getElementById('pending-summaries').textContent = data.pending_file_summary_jobs ?? 0;

    const pillR = document.getElementById('pill-research');
    const pillS = document.getElementById('pill-summaries');
    pillR.className = 'sched-pill' + (data.pending_research_jobs > 0 ? ' alert' : '');
    pillS.className = 'sched-pill' + (data.pending_file_summary_jobs > 0 ? ' alert' : '');

    // Pinned LLM
    const pinnedEl = document.getElementById('pinned-llm');
    if (data.pinned_llm_id != null && data.llm_capacities) {
        const cap = data.llm_capacities[String(data.pinned_llm_id)];
        pinnedEl.textContent = cap ? cap.name : `LLM ${data.pinned_llm_id}`;
    } else {
        pinnedEl.textContent = 'none';
    }

    // LLM capacity bars
    const grid = document.getElementById('llm-grid');
    const caps = data.llm_capacities || {};
    const capEntries = Object.entries(caps);
    if (capEntries.length === 0) {
        grid.innerHTML = '<span class="sched-empty">No LLMs in use.</span>';
    } else {
        grid.innerHTML = capEntries.map(([lid, info]) => {
            const pct = info.max > 0 ? Math.round((info.current / info.max) * 100) : 0;
            const fillClass = pct >= 100 ? 'full' : pct >= 70 ? 'busy' : '';
            return `
                <div class="sched-llm-card">
                    <div class="sched-llm-name" title="${esc(info.name)}">${esc(info.name)}</div>
                    <div class="sched-llm-bar-wrap">
                        <div class="sched-llm-bar-fill ${fillClass}" style="width:${pct}%"></div>
                    </div>
                    <div class="sched-llm-counts"><span>${info.current}</span> / ${info.max} sessions</div>
                </div>`;
        }).join('');
    }

    // Task columns
    function renderCol(colId, countId, tasks, variant) {
        const body  = document.getElementById(colId);
        const count = document.getElementById(countId);
        count.textContent = tasks.length;
        if (tasks.length === 0) {
            const msgs = { active: 'No active sessions.', queued: 'Queue is empty.', blocked: 'No blocked tasks.' };
            body.innerHTML = `<p class="sched-empty">${msgs[variant]}</p>`;
        } else {
            body.innerHTML = tasks.map(t => buildTaskCard(t, variant)).join('');
        }
    }

    renderCol('col-active',  'count-active',  data.active  || [], 'active');
    renderCol('col-queued',  'count-queued',  data.queued  || [], 'queued');
    renderCol('col-blocked', 'count-blocked', data.blocked || [], 'blocked');
}

// ── Fetch & schedule ──────────────────────────────────────────────────────────

async function fetchStatus() {
    const ageEl = document.getElementById('refresh-age');
    ageEl.className = 'sched-refresh-age sched-refreshing';
    ageEl.textContent = 'refreshing…';

    try {
        const resp = await fetch('/api/scheduler/status');
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        render(data);
        _lastFetch = Date.now();
    } catch (e) {
        console.error('Scheduler status fetch failed:', e);
        document.getElementById('sched-label').textContent = 'Error fetching status';
    }

    ageEl.className = 'sched-refresh-age';
    ageEl.textContent = 'just now';
}

function _updateAge() {
    if (_lastFetch === 0) return;
    const s = Math.round((Date.now() - _lastFetch) / 1000);
    const el = document.getElementById('refresh-age');
    if (el && !el.classList.contains('sched-refreshing')) {
        el.textContent = s <= 1 ? 'just now' : `${s}s ago`;
    }
}

// Start
fetchStatus();
_timer = setInterval(fetchStatus, REFRESH_MS);
setInterval(_updateAge, 1000);
