/* ============================================================
   story.js — Card Story page (/story?task=<id>)
   ============================================================ */

'use strict';

// ── Constants ────────────────────────────────────────────────

const AGENT_TYPE_COLORS = {
    intake:            '#0d6efd',
    planning:          '#6f42c1',
    maestro_loop:      '#198754',
    dev_orchestrator:  '#20c997',
    conceptual_review: '#fd7e14',
    optimization:      '#f59f00',
    security:          '#dc3545',
    full_review:       '#0ca678',
    pip_preflight:     '#e83e8c',
    pip_research:      '#ffc107',
    pip_resolution:    '#e03131',
    subdivision:       '#6610f2',
    arch_gen:          '#1971c2',
};

const AGENT_TYPE_LABELS = {
    intake:            'Intake',
    planning:          'Planning',
    maestro_loop:      'Maestro Loop',
    dev_orchestrator:  'Dev Orchestrator',
    conceptual_review: 'Conceptual Review',
    optimization:      'Optimization',
    security:          'Security',
    full_review:       'Full Review',
    pip_preflight:     'PIP Pre-flight',
    pip_research:      'PIP Research',
    pip_resolution:    'PIP Resolution',
    subdivision:       'Subdivision',
    arch_gen:          'Arch Gen',
};

const STAGE_LABELS = {
    idea:               'IDEA',
    planning:           'PLANNING',
    indev:              'IN DEV',
    conceptual_review:  'REVIEW',
    optimization:       'OPTIM',
    security:           'SECURITY',
    full_review:        'FULL REVIEW',
    completed:          'COMPLETED',
    architecture:       'ARCH',
};

// ── State ────────────────────────────────────────────────────

let _taskId      = null;
let _sessions    = [];
let _task        = null;
let _pips        = [];
let _transitions = [];
let _refreshTimer = null;

// ── Init ─────────────────────────────────────────────────────

document.addEventListener('DOMContentLoaded', () => {
    const params = new URLSearchParams(window.location.search);
    _taskId = params.get('task');
    if (!_taskId) {
        document.getElementById('story-timeline').innerHTML =
            '<p class="story-error">No task ID specified. Use /story?task=&lt;id&gt;</p>';
        return;
    }
    // Set diagnostics link
    document.getElementById('story-diag-link').href =
        `/diagnostics?task=${encodeURIComponent(_taskId)}`;
    loadStory();
});

async function loadStory() {
    clearTimeout(_refreshTimer);
    try {
        // Parallel fetch: task metadata, sessions, PIPs, transition history
        const [taskRes, sessionsRes, pipsRes, transRes] = await Promise.all([
            fetch(`/api/tasks/${encodeURIComponent(_taskId)}`),
            fetch(`/api/tasks/${encodeURIComponent(_taskId)}/agent-sessions`),
            fetch(`/api/tasks/${encodeURIComponent(_taskId)}/pips`),
            fetch(`/api/tasks/${encodeURIComponent(_taskId)}/transition-history`),
        ]);

        if (!taskRes.ok) {
            const msg = taskRes.status === 404 ? 'Task not found.' : `Error ${taskRes.status}`;
            document.getElementById('story-timeline').innerHTML =
                `<p class="story-error">${escHtml(msg)}</p>`;
            return;
        }

        _task        = await taskRes.json();
        _sessions    = sessionsRes.ok ? await sessionsRes.json() : [];
        _pips        = pipsRes.ok     ? await pipsRes.json()     : [];
        _transitions = transRes.ok    ? await transRes.json()    : [];

        renderHeader();
        renderTimeline();
        renderDecisionLedger();

        // Auto-refresh while any session is still running
        const anyRunning = _sessions.some(s => !s.ended_at);
        if (anyRunning) {
            _refreshTimer = setTimeout(loadStory, 10000);
        }
    } catch (err) {
        document.getElementById('story-timeline').innerHTML =
            `<p class="story-error">Failed to load: ${escHtml(String(err))}</p>`;
    }
}

// ── Header ───────────────────────────────────────────────────

function renderHeader() {
    document.title = `Story — ${_task.title || _taskId}`;

    document.getElementById('story-task-title').textContent =
        _task.title || _taskId;

    const stageBadge = document.getElementById('story-stage-badge');
    const stageLabel = STAGE_LABELS[_task.type] || (_task.type || '').toUpperCase();
    stageBadge.textContent = stageLabel;

    const projectBadge = document.getElementById('story-project-badge');
    if (_task.project) {
        projectBadge.textContent = _task.project;
        projectBadge.style.display = '';
    } else {
        projectBadge.style.display = 'none';
    }

    // Meta line: demotions, PIPs
    const metaEl = document.getElementById('story-meta');
    const parts = [];
    const demotions = (_task.history || []).filter(h => h.event === 'demotion').length;
    if (demotions > 0) parts.push(`Demotions: ${demotions}`);
    if (_pips.length > 0) {
        const unsatisfied = _pips.filter(p => p.status === 'unsatisfied').length;
        const pipStr = `PIPs: ${_pips.length}` + (unsatisfied > 0 ? ` (${unsatisfied} unsatisfied)` : '');
        parts.push(pipStr);
    }
    if (_task.created_at) {
        parts.push(`Created: ${fmtDate(_task.created_at)}`);
    }
    metaEl.innerHTML = parts.map(escHtml).join('<span class="story-meta-sep"> · </span>');
}

// ── Timeline ─────────────────────────────────────────────────

function renderTimeline() {
    const container = document.getElementById('story-timeline');

    if (_sessions.length === 0) {
        container.innerHTML = '<p class="story-empty">No agent sessions recorded for this card yet.</p>';
        return;
    }

    container.innerHTML = '';

    const label = document.createElement('div');
    label.className = 'story-section-label';
    label.textContent = `${_sessions.length} Session${_sessions.length !== 1 ? 's' : ''}`;
    container.appendChild(label);

    for (const s of _sessions) {
        container.appendChild(buildSessionCard(s));
    }
}

function buildSessionCard(s) {
    const running = !s.ended_at;
    const agentKey = s.agent_type || 'unknown';
    const color = AGENT_TYPE_COLORS[agentKey] || '#6c757d';

    const card = document.createElement('div');
    card.className = 'story-session';

    // Colour gutter
    const gutter = document.createElement('div');
    gutter.className = 'story-session-gutter';
    gutter.style.background = color;
    card.appendChild(gutter);

    // Inner content
    const inner = document.createElement('div');
    inner.className = 'story-session-inner';

    // ── Header line ──────────────────────────────────────────
    const header = document.createElement('div');
    header.className = 'story-session-header';

    const agentBadge = document.createElement('span');
    agentBadge.className = `story-agent-badge agent-${agentKey}`;
    agentBadge.style.background = color;
    if (agentKey === 'pip_research') agentBadge.style.color = '#212529';
    agentBadge.textContent = AGENT_TYPE_LABELS[agentKey] || agentKey;
    header.appendChild(agentBadge);

    const reasonTag = document.createElement('span');
    reasonTag.className = 'story-reason-tag' +
        (s.scheduler_reason === 'user_triggered' ? ' user-triggered' : '');
    reasonTag.textContent = s.scheduler_reason === 'user_triggered' ? 'User' : 'Scheduler';
    header.appendChild(reasonTag);

    if (s.started_at) {
        const ts = document.createElement('span');
        ts.className = 'story-timestamp';
        ts.textContent = fmtDate(s.started_at);
        header.appendChild(ts);
    }

    const dur = document.createElement('span');
    dur.className = 'story-duration';
    if (running) {
        dur.innerHTML = '<span class="story-running-dot"></span>running…';
    } else if (s.duration_seconds != null) {
        dur.textContent = fmtDuration(s.duration_seconds);
    }
    header.appendChild(dur);

    inner.appendChild(header);

    // ── Turn bar ─────────────────────────────────────────────
    if (s.max_turns != null && s.max_turns > 0) {
        const barRow = document.createElement('div');
        barRow.className = 'story-turn-bar-row';

        const bar = document.createElement('div');
        bar.className = 'story-turn-bar';

        const fill = document.createElement('div');
        fill.className = 'story-turn-fill';
        const fillClass = running ? 'fill-running' : `fill-${s.exit_reason || 'unknown'}`;
        fill.className += ' ' + fillClass;
        const pct = running
            ? (s.turn_count != null ? Math.min(100, (s.turn_count / s.max_turns) * 100) : 0)
            : Math.min(100, ((s.turn_count || 0) / s.max_turns) * 100);
        fill.style.width = pct.toFixed(1) + '%';
        bar.appendChild(fill);
        barRow.appendChild(bar);

        const turnLabel = document.createElement('span');
        turnLabel.className = 'story-turn-label';
        turnLabel.textContent = running
            ? `${s.turn_count != null ? s.turn_count : '?'} / ${s.max_turns}`
            : `${s.turn_count != null ? s.turn_count : '—'} / ${s.max_turns}`;
        barRow.appendChild(turnLabel);

        inner.appendChild(barRow);
    }

    // ── Exit badge + summary ─────────────────────────────────
    if (!running) {
        const exitRow = document.createElement('div');
        exitRow.className = 'story-exit-row';

        if (s.exit_reason) {
            const exitBadge = document.createElement('span');
            exitBadge.className = `story-exit-badge exit-${s.exit_reason}`;
            exitBadge.textContent = s.exit_reason.replace('_', ' ');
            exitRow.appendChild(exitBadge);
        }

        if (s.exit_summary) {
            const summaryEl = document.createElement('span');
            summaryEl.className = 'story-exit-summary';

            const shortText = s.exit_summary.length > 160
                ? s.exit_summary.slice(0, 160) + '…'
                : s.exit_summary;
            const shortSpan = document.createElement('span');
            shortSpan.className = 'story-exit-summary-short';
            shortSpan.textContent = shortText;
            summaryEl.appendChild(shortSpan);

            if (s.exit_summary.length > 160) {
                const fullSpan = document.createElement('span');
                fullSpan.className = 'story-exit-summary-full';
                fullSpan.textContent = s.exit_summary;

                const expandBtn = document.createElement('button');
                expandBtn.className = 'story-expand-btn';
                expandBtn.textContent = 'Show more';
                expandBtn.onclick = () => {
                    const expanded = fullSpan.style.display !== 'none';
                    fullSpan.style.display = expanded ? 'none' : 'inline';
                    shortSpan.style.display = expanded ? 'inline' : 'none';
                    expandBtn.textContent = expanded ? 'Show more' : 'Show less';
                };
                summaryEl.appendChild(fullSpan);
                summaryEl.appendChild(expandBtn);
            }

            exitRow.appendChild(summaryEl);
        }

        if (exitRow.children.length > 0) inner.appendChild(exitRow);
    }

    // ── Token summary ────────────────────────────────────────
    const totalTok = (s.prompt_tokens || 0) + (s.completion_tokens || 0);
    if (totalTok > 0) {
        const tokLine = document.createElement('div');
        tokLine.className = 'story-token-line';
        tokLine.textContent =
            `${fmtTokens(s.prompt_tokens || 0)} prompt + ${fmtTokens(s.completion_tokens || 0)} completion`;
        inner.appendChild(tokLine);
    }

    // ── Diagnostics link ─────────────────────────────────────
    const diagLink = document.createElement('a');
    diagLink.className = 'story-session-diag';
    diagLink.href = `/diagnostics?task=${encodeURIComponent(s.task_id)}`;
    diagLink.target = '_blank';
    diagLink.textContent = '→ Diagnostics';
    inner.appendChild(diagLink);

    card.appendChild(inner);
    return card;
}

// ── Helpers ──────────────────────────────────────────────────

function escHtml(str) {
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}

function fmtDate(isoStr) {
    if (!isoStr) return '';
    try {
        return new Date(isoStr).toLocaleString(undefined, {
            month: 'short', day: 'numeric',
            hour: '2-digit', minute: '2-digit',
        });
    } catch { return isoStr; }
}

function fmtDuration(secs) {
    if (secs < 60) return `${secs.toFixed(0)}s`;
    const m = Math.floor(secs / 60);
    const s = Math.round(secs % 60);
    return s > 0 ? `${m}m ${s}s` : `${m}m`;
}

function fmtTokens(n) {
    if (n == null || n === 0) return '0';
    if (n >= 1048576) return (n / 1048576).toFixed(1) + 'M';
    if (n >= 1024)    return (n / 1024).toFixed(1) + 'K';
    return String(n);
}

// ── Decision Ledger ──────────────────────────────────────────

const VERDICT_CHIP_CLASS = {
    LIKELY:         'vote-chip--likely',
    POSSIBLE:       'vote-chip--likely',
    CONDITIONAL_PASS: 'vote-chip--likely',
    NEEDS_RESEARCH: 'vote-chip--needs-research',
    NOT_SUITABLE:   'vote-chip--not-suitable',
    REJECTED:       'vote-chip--rejected',
    SUBDIVIDE_IDEA: 'vote-chip--subdivide',
    TOO_LARGE:      'vote-chip--rejected',
};

const OUTCOME_LABEL = {
    passed:           'PASSED',
    conditional_pass: 'COND. PASS',
    rejected:         'REJECTED',
    needs_research:   'RESEARCH',
    subdivide:        'SUBDIVIDE',
    tie:              'TIE',
};

function renderDecisionLedger() {
    const container = document.getElementById('story-timeline');
    if (!_transitions || _transitions.length === 0) return;

    const section = document.createElement('div');
    section.className = 'story-section-label';
    section.textContent = `Decision Ledger — ${_transitions.length} Intake Run${_transitions.length !== 1 ? 's' : ''}`;
    container.appendChild(section);

    // Exhaustion banner (only for idea cards)
    if (_task && _task.intake_exhausted) {
        const banner = document.createElement('div');
        banner.className = 'ledger-exhausted-banner';
        banner.innerHTML = `
            <span>&#9888; Intake exhausted after ${escHtml(String(_task.intake_rejection_count))} rejection(s) — scheduler has stopped retrying.</span>
            <button class="ledger-reset-btn" onclick="resetIntake()">Reset Intake</button>
        `;
        container.appendChild(banner);
    }

    const ledger = document.createElement('div');
    ledger.className = 'decision-ledger';

    for (const run of _transitions) {
        ledger.appendChild(buildLedgerRunCard(run));
    }

    container.appendChild(ledger);
}

function buildLedgerRunCard(run) {
    const outcomeKey = run.outcome || 'unknown';
    const outcomeLabel = OUTCOME_LABEL[outcomeKey] || outcomeKey.toUpperCase();

    const card = document.createElement('div');
    card.className = `ledger-run-card ledger-outcome--${outcomeKey}`;

    // Header
    const header = document.createElement('div');
    header.className = 'ledger-run-header';

    const runNum = document.createElement('span');
    runNum.className = 'ledger-run-num';
    runNum.textContent = `Run #${run.run}`;
    header.appendChild(runNum);

    if (run.created_at) {
        const ts = document.createElement('span');
        ts.className = 'story-timestamp';
        ts.textContent = fmtDate(run.created_at);
        header.appendChild(ts);
    }

    const triggerTag = document.createElement('span');
    triggerTag.className = `story-reason-tag${run.trigger === 'user' ? ' user-triggered' : ''}`;
    triggerTag.textContent = run.trigger === 'user' ? 'User' : 'Scheduler';
    header.appendChild(triggerTag);

    const outcomeBadge = document.createElement('span');
    outcomeBadge.className = `ledger-outcome-badge ledger-outcome-badge--${outcomeKey}`;
    outcomeBadge.textContent = outcomeLabel;
    if (run.forced) outcomeBadge.title = 'Forced (no LLM votes)';
    header.appendChild(outcomeBadge);

    if (run.total_prompt_tokens > 0) {
        const tok = document.createElement('span');
        tok.className = 'story-token-line';
        tok.style.marginLeft = 'auto';
        tok.textContent = `${fmtTokens(run.total_prompt_tokens)}p + ${fmtTokens(run.total_completion_tokens)}c`;
        header.appendChild(tok);
    }

    card.appendChild(header);

    // Tally narrative
    if (run.tally_narrative) {
        const narrative = document.createElement('div');
        narrative.className = 'ledger-tally-narrative';
        narrative.textContent = run.tally_narrative;
        card.appendChild(narrative);
    }

    // Vote chips
    if (run.votes && run.votes.length > 0) {
        const chips = document.createElement('div');
        chips.className = 'ledger-vote-chips';

        for (const v of run.votes) {
            const chip = document.createElement('div');
            const isStatic = v.stage === 'static_analysis';
            const chipClass = isStatic ? 'vote-chip--static' : (VERDICT_CHIP_CLASS[v.verdict] || 'vote-chip--static');
            chip.className = `vote-chip ${chipClass}`;
            chip.dataset.justification = v.justification || '';

            const stageName = document.createElement('span');
            stageName.className = 'vote-chip-stage';
            stageName.textContent = v.stage.replace(/_/g, ' ');

            const verdictName = document.createElement('span');
            verdictName.className = 'vote-chip-verdict';
            verdictName.textContent = v.verdict.replace(/_/g, ' ');

            const confName = document.createElement('span');
            confName.className = 'vote-chip-conf';
            confName.textContent = `${v.confidence}%`;

            chip.appendChild(stageName);
            chip.appendChild(verdictName);
            chip.appendChild(confName);

            // Click to expand justification
            if (v.justification) {
                chip.style.cursor = 'pointer';
                chip.title = 'Click to expand justification';

                const justEl = document.createElement('div');
                justEl.className = 'ledger-justification';
                justEl.textContent = v.justification;
                justEl.style.display = 'none';

                chip.appendChild(justEl);
                chip.addEventListener('click', () => {
                    const expanded = justEl.style.display !== 'none';
                    justEl.style.display = expanded ? 'none' : 'block';
                    chip.classList.toggle('vote-chip--expanded', !expanded);
                });
            }

            chips.appendChild(chip);
        }

        card.appendChild(chips);
    }

    return card;
}

async function resetIntake() {
    if (!_taskId) return;
    try {
        const res = await fetch(`/api/tasks/${encodeURIComponent(_taskId)}/reset-intake`, { method: 'POST' });
        if (res.ok) {
            loadStory();
        } else {
            alert(`Reset failed: ${res.status}`);
        }
    } catch (e) {
        alert(`Reset failed: ${e}`);
    }
}
