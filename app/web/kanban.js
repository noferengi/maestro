// Kanban Board JavaScript - Extracted from kanban.html
// Includes drag-and-drop functionality for reordering within columns

// API Configuration
const API_BASE = '/api';

// Track where each mousedown originated so that click-outside-to-close modals
// are not triggered by a drag that started inside the modal content and ended
// on the backdrop.  Only close when both mousedown AND click land on the backdrop.
let _modalMousedownTarget = null;
document.addEventListener('mousedown', function(e) { _modalMousedownTarget = e.target; });

// Category colours for architecture cards (displayed as badge text colour)
const ARCH_CATEGORY_COLORS = {
    Platform:      '#17a2b8',
    Design:        '#a78bfa',
    Testing:       '#20c997',
    Security:      '#f87171',
    Performance:   '#fb923c',
    API:           '#60a5fa',
    Tooling:       '#fbbf24',
    Data:          '#818cf8',
    UX:            '#f472b6',
    Accessibility: '#34d399',
    Compliance:    '#e879f9',
    Deployment:    '#4ade80',
    Observability: '#38bdf8',
    General:       '#6c757d',
};

// Whether the arch bar is collapsed (persisted in localStorage)
let _archBarCollapsed = localStorage.getItem('archBarCollapsed') === '1';

// WIP Limits configuration - maximum cards allowed per column
const WIP_LIMITS = {
    'idea': 15,
    'planning': 10,
    'indev': 5,
    'conceptual_review': 5,
    'optimization': 5,
    'security': 5,
    'full_review': 5,
    'completed': 15
};

// Task data storage with history tracking - loaded from database
let taskData = {};
let allTasks = [];

// Global LLM, Budget, and Compute Node caches
let allLlms = [];
let allProjects = [];  // [{name, path, description, llm_id, budget_id}] — kept in sync with loadProjects()
let allBudgets = [];
let allComputeNodes = [];  // [{id, name, description, max_parallel_sessions, max_loaded_models}]

// Transition status cache: taskId -> { status, data, rejectionCount }
let transitionCache = {};

// Active polling timers: taskId -> intervalId
let transitionPollers = {};

// Inbox
let inboxMessages = [];
let _inboxUnreadCount = 0;
let _inboxPollInterval = null;

// Big Idea zoom state
let currentBigIdeaFilter = null;  // task ID or null for root view
let breadcrumbStack = [];         // array of {id, title} for nested zoom

// Descendant index: parentId -> [childId, ...]
let childIndex = {};

// State for the "View Children" subdivision cycling modal
let _viewChildrenState = null;   // { taskId, records, childMap, idx }
let _childrenPollerTimer = null; // interval ID while waiting for regeneration to complete
// Full descendant index: taskId -> [all descendant IDs recursively]
let descendantIndex = {};

// ============================================
// Toast Notifications
// ============================================

function showToast(message, type = 'info', duration = 4500) {
    const container = document.getElementById('toast-container');
    if (!container) return;
    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;
    toast.innerHTML = `<span class="toast-body">${message}</span><button class="toast-close" onclick="this.parentElement.remove()">&times;</button>`;
    container.appendChild(toast);
    setTimeout(() => {
        toast.classList.add('toast-fade-out');
        toast.addEventListener('animationend', () => toast.remove());
    }, duration);
}

// ============================================
// Confirm Modal (replaces confirm())
// ============================================

let _confirmResolveCallback = null;

function showConfirm(title, message, okLabel = 'Confirm') {
    return new Promise(resolve => {
        _confirmResolveCallback = resolve;
        document.getElementById('confirm-modal-title').textContent = title;
        document.getElementById('confirm-modal-message').textContent = message;
        document.getElementById('confirm-modal-ok').textContent = okLabel;
        document.getElementById('confirm-modal').classList.add('active');
    });
}

function _confirmResolve(result) {
    document.getElementById('confirm-modal').classList.remove('active');
    if (_confirmResolveCallback) {
        _confirmResolveCallback(result);
        _confirmResolveCallback = null;
    }
}

// Backdrop click for confirm modal
document.addEventListener('DOMContentLoaded', function() {
    const overlay = document.getElementById('confirm-modal');
    if (overlay) {
        overlay.addEventListener('click', function(e) {
            if (e.target === this && _modalMousedownTarget === this) _confirmResolve(false);
        });
    }
    const rdOverlay = document.getElementById('research-dialog-modal');
    if (rdOverlay) {
        rdOverlay.addEventListener('click', function(e) {
            if (e.target === this && _modalMousedownTarget === this) closeResearchDialog();
        });
    }
});

// ============================================
// Research / Investigation Dialog Modal
// ============================================

let _researchDialogTaskId = null;

function closeResearchDialog() {
    _researchDialogTaskId = null;
    document.getElementById('research-dialog-modal').classList.remove('active');
    document.getElementById('research-dialog-question').value = '';
}

async function submitResearchDialog() {
    const question = document.getElementById('research-dialog-question').value.trim();
    if (!question) { showToast('Enter a question first.', 'warning'); return; }
    const taskId = _researchDialogTaskId;
    closeResearchDialog();
    try {
        const resp = await fetch(`${API_BASE}/agent/investigate/${taskId}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ question }),
        });
        const data = await resp.json();
        if (!resp.ok) { showToast(data.detail || 'Agent failed to start', 'error'); return; }
        _pollInvestigationJob(taskId, data.job_id);
        showToast(`Investigation job #${data.job_id} queued.`, 'info');
    } catch (e) {
        showToast('Error starting agent: ' + e.message, 'error');
    }
}

// Highlighted cards (localStorage-backed)
let _highlightedCards = new Set(JSON.parse(localStorage.getItem('maestro_highlights') || '[]'));

function _saveHighlights() {
    try { localStorage.setItem('maestro_highlights', JSON.stringify([..._highlightedCards])); } catch (_) {}
}

function _applyHighlightState(el, taskId) {
    el.classList.toggle('highlighted', _highlightedCards.has(taskId));
    const btn = el.querySelector('.card-highlight-btn');
    if (btn) btn.textContent = _highlightedCards.has(taskId) ? '★' : '☆';
}

function toggleHighlight(taskId) {
    if (_highlightedCards.has(taskId)) { _highlightedCards.delete(taskId); }
    else { _highlightedCards.add(taskId); }
    _saveHighlights();
    const card = cardCache[taskId];
    if (card) _applyHighlightState(card, taskId);
    const mapNode = document.getElementById(`map-node-${taskId}`);
    if (mapNode) _applyHighlightState(mapNode, taskId);
}

// Grouped drag state
let isDraggingGroup = false;
let dragGroupDescendants = [];  // [{id, column, positionOffset}]
let dragGroupOldParentPos = 0;

// Card DOM cache: taskId -> element, built once and reused across renders
const cardCache = {};
// Render fingerprint cache: taskId -> string, detects which cards need updating
const fingerprintCache = {};

// Load tasks from database on startup (scoped to currentProject)
async function loadTasksFromDatabase() {
    try {
        const response = await fetch(`${API_BASE}/projects/${encodeURIComponent(currentProject)}/tasks`);
        if (!response.ok) {
            throw new Error('Failed to load tasks');
        }
        const projectTasks = await response.json();

        // Rebuild stores with only this project's tasks
        taskData = {};
        allTasks = projectTasks;
        allTasks.forEach(task => {
            taskData[task.id] = task;
        });

        console.log(`Loaded ${allTasks.length} tasks from database for project "${currentProject}"`);
        buildDescendantIndex();
        return true;
    } catch (error) {
        console.error('Error loading tasks from database:', error);
        // Fallback to empty state
        return false;
    }
}

// Build child and descendant indexes from taskData
function buildDescendantIndex() {
    childIndex = {};
    descendantIndex = {};

    // Build childIndex: parentId -> [childId, ...]
    for (const task of allTasks) {
        if (task.parent_task_id) {
            if (!childIndex[task.parent_task_id]) {
                childIndex[task.parent_task_id] = [];
            }
            childIndex[task.parent_task_id].push(task.id);
        }
    }

    // Build descendantIndex recursively
    function getDescendants(taskId) {
        if (descendantIndex[taskId] !== undefined) return descendantIndex[taskId];
        const children = childIndex[taskId] || [];
        let all = [...children];
        for (const cid of children) {
            all = all.concat(getDescendants(cid));
        }
        descendantIndex[taskId] = all;
        return all;
    }

    for (const task of allTasks) {
        getDescendants(task.id);
    }
}

// Cheap fingerprint of the fields that affect card appearance.
// Only recompute/rebuild a card when this string changes.
function taskFingerprint(task) {
    return [
        task.type,
        task.position,
        task.title,
        task.owner || '',
        task.parent_task_id || '',
        task.subdivision_generation || 0,
        task.is_big_idea ? '1' : '0',
        task.interface_contracts ? '1' : '0',
        (task.tags || []).join(','),
        (task.pips || []).map(p => `${p.id}:${p.status}:${p.last_checked||''}`).join(','),
    ].join('|');
}

// Load global LLMs, Budgets, and Compute Nodes
async function loadLlmsAndBudgets() {
    try {
        const [llmRes, budgetRes, cnRes] = await Promise.all([
            fetch(`${API_BASE}/llms`),
            fetch(`${API_BASE}/budgets`),
            fetch(`${API_BASE}/compute-nodes`),
        ]);
        if (llmRes.ok) allLlms = await llmRes.json();
        if (budgetRes.ok) allBudgets = await budgetRes.json();
        if (cnRes.ok) allComputeNodes = await cnRes.json();
    } catch (e) {
        console.error('Failed to load LLMs/Budgets/ComputeNodes:', e);
    }
}

function populateLlmSelect(selectedId) {
    const sel = document.getElementById('task-llm-select');
    sel.innerHTML = '<option value="">(none)</option>';
    allLlms.forEach(l => {
        const opt = document.createElement('option');
        opt.value = l.id;
        opt.textContent = l.label;
        if (l.id === selectedId) opt.selected = true;
        sel.appendChild(opt);
    });
}

function populateBudgetSelect(selectedId) {
    const sel = document.getElementById('task-budget-select');
    sel.innerHTML = '<option value="">(none)</option>';
    allBudgets.forEach(b => {
        const opt = document.createElement('option');
        opt.value = b.id;
        opt.textContent = b.name;
        if (b.id === selectedId) opt.selected = true;
        sel.appendChild(opt);
    });
}

// Populate a project-level LLM dropdown (elementId) with allLlms.
// Includes a "(none)" option — selecting it means no default is set.
function populateProjectLlmSelect(elementId, selectedId) {
    const sel = document.getElementById(elementId);
    if (!sel) return;
    sel.innerHTML = '<option value="">(none)</option>';
    allLlms.forEach(l => {
        const opt = document.createElement('option');
        opt.value = l.id;
        opt.textContent = l.label;
        if (l.id === selectedId) opt.selected = true;
        sel.appendChild(opt);
    });
}

// Populate a project-level Budget dropdown (elementId) with allBudgets.
// Includes a "(none)" option — selecting it means no budget is set.
function populateProjectBudgetSelect(elementId, selectedId) {
    const sel = document.getElementById(elementId);
    if (!sel) return;
    sel.innerHTML = '<option value="">(none)</option>';
    allBudgets.forEach(b => {
        const opt = document.createElement('option');
        opt.value = b.id;
        opt.textContent = b.name;
        if (b.id === selectedId) opt.selected = true;
        sel.appendChild(opt);
    });
}

// Populate a compute node dropdown (elementId) with allComputeNodes.
// Includes a "(none)" option.
function populateComputeNodeSelect(elementId, selectedId) {
    const sel = document.getElementById(elementId);
    if (!sel) return;
    sel.innerHTML = '<option value="">(none)</option>';
    allComputeNodes.forEach(n => {
        const opt = document.createElement('option');
        opt.value = n.id;
        opt.textContent = n.name;
        if (n.id === selectedId) opt.selected = true;
        sel.appendChild(opt);
    });
}

// Refresh tasks from database
async function refreshTasks() {
    console.log('Refreshing tasks from database...');
    const success = await loadTasksFromDatabase();
    if (success) {
        await loadTransitionStatuses();
        renderTasksFromDatabase();
        console.log('Tasks refreshed successfully');
    } else {
        console.error('Failed to refresh tasks');
    }
}

// Current modal state
let currentTaskId = null;
let currentTargetStatus = null;
let currentProject = 'TheMaestro';

// Drag and drop state
let draggedElement = null;
let draggedTaskId = null;
let dragSourceContainer = null;
let draggedOriginalIndex = -1;
let insertIndicator = null;
let currentInsertIndex = -1;
let currentInsertContainer = null;

// Column progression for drag-and-drop validation
const COLUMN_NEXT = {
    'architecture': 'idea',
    'idea': 'planning',
    'subdividing': 'planning',   // transient state within idea column
    'planning': 'indev',
    'indev': 'conceptual_review',
    'conceptual_review': 'optimization',
    'optimization': 'security',
    'security': 'full_review',
    'full_review': 'completed'
};

const COLUMN_DISPLAY = {
    'architecture': 'Architecture',
    'idea': 'Ideas',
    'subdividing': 'Ideas',
    'planning': 'Planning',
    'indev': 'In Development',
    'conceptual_review': 'Concept Review',
    'optimization': 'Optimization',
    'security': 'Security',
    'full_review': 'Full Review',
    'completed': 'Completed',
};

// Returns the label for an advance button given a task's current type.
function _advanceBtnLabel(taskType, hasRejections) {
    if (hasRejections) return 'Retry Pipeline';
    if (taskType === 'full_review') return 'Merge to Completed';
    return 'Run Pipeline';
}

function isValidDropTarget(sourceContainer, targetContainer) {
    const sourceCol = sourceContainer.id.replace('tasks-', '');
    const targetCol = targetContainer.id.replace('tasks-', '');
    
    // Always allow reorder within the same column
    if (sourceCol === targetCol) return true;

    // The user shouldn't really be able to move the card on the Kanban board unless they are demoting it themselves.
    // They can't really promote through tasks that need to be completed.
    const sourceIdx = PIPELINE_COLUMN_ORDER.indexOf(sourceCol);
    const targetIdx = PIPELINE_COLUMN_ORDER.indexOf(targetCol);
    
    // If target index is less than source index, it's a demotion - ALLOW.
    if (targetIdx !== -1 && sourceIdx !== -1 && targetIdx < sourceIdx) {
        return true;
    }

    // Deny all other cross-column moves (no manual promotion).
    return false;
}

// ============================================
// PIP Card Helpers
// ============================================

// Status label map for pip-card badges
const PIP_STATUS_LABELS = {
    satisfied:   '✓ Satisfied',
    unsatisfied: '✗ Unsatisfied',
    unverified:  '◌ Unverified',
    checking:    '⟳ Checking',
};

/**
 * Build a single .pip-card DOM element for one PIP at the given pipeline stage.
 * @param {object} pip  — pip object from task.pips (id, origin_stage, requirements, status, last_summary)
 * @param {string} taskId
 * @param {number} index  — 1-based ordinal shown on the label
 */
function buildPipCard(pip, taskId, index) {
    const el = document.createElement('div');
    const status = pip.status || 'unverified';
    el.className = `pip-card pip-${status}`;
    el.dataset.pipId = pip.id;
    el.dataset.taskId = taskId;

    const statusLabel = PIP_STATUS_LABELS[status] || '◌ Unverified';
    const statusClass = `pip-status--${status}`;

    // Body: first requirement truncated; "+N more" if multiple
    const reqs = pip.requirements || [];
    const firstReq = reqs[0] ? reqs[0].slice(0, 80) : '(no requirements)';
    const moreCount = reqs.length - 1;
    const moreHtml = moreCount > 0
        ? `<span class="pip-req-count">+${moreCount} more</span>`
        : '';

    el.innerHTML = `
        <div class="pip-card-header">
            <span class="pip-label">PIP ${index}</span>
            <span class="pip-origin">demoted from ${pip.origin_stage}</span>
            <span class="pip-status ${statusClass}">${statusLabel}</span>
        </div>
        <div class="pip-card-body">${firstReq}${moreHtml}</div>
        <div class="pip-card-toolbar">
            <button class="pip-toolbar-btn" title="Run pre-flight verification" onclick="event.stopPropagation();pipVerify('${taskId}',${pip.id})">🔍 Verify</button>
            <button class="pip-toolbar-btn" title="Run Resolution Agent" onclick="event.stopPropagation();pipResolve('${taskId}',${pip.id})">🔧 Resolve</button>
            <button class="pip-toolbar-btn" title="Verification history" onclick="event.stopPropagation();pipHistory('${taskId}',${pip.id})">📋 History</button>
        </div>
    `;
    return el;
}

/**
 * Wrap a bare .task-card in a .task-card-group with pip-card segments if the
 * task has PIPs.  If the task has no PIPs, returns the bare card unchanged.
 * @param {HTMLElement} card   — a .task-card element
 * @param {object}      task   — full task object including task.pips array
 * @returns {HTMLElement}      — .task-card-group or the original .task-card
 */
function wrapWithPipGroup(card, task) {
    const pips = (task && task.pips) || [];
    if (pips.length === 0) return card;

    const group = document.createElement('div');
    group.className = 'task-card-group';
    group.dataset.taskId = task.id;

    // Move draggable + drag listeners from the inner card to the group so the
    // browser's native ghost captures the full card+pip stack, not just the card.
    card.removeAttribute('draggable');
    card.removeEventListener('dragstart', handleDragStart);
    card.removeEventListener('dragend', handleDragEnd);
    group.setAttribute('draggable', 'true');
    group.addEventListener('dragstart', handleDragStart);
    group.addEventListener('dragend', handleDragEnd);

    group.appendChild(card);
    pips.forEach((pip, i) => {
        group.appendChild(buildPipCard(pip, task.id, i + 1));
    });
    return group;
}

// ============================================
// PIP toolbar actions (card-level)
// ============================================

async function pipVerify(taskId, pipId) {
    if (!confirm('Run pre-flight verification for this PIP now?')) return;
    try {
        const resp = await fetch(`${API_BASE}/tasks/${taskId}/pips/${pipId}/verify`, { method: 'POST' });
        if (!resp.ok) { alert('Verification request failed: ' + resp.status); return; }
        const result = await resp.json();
        const icon = result.outcome === 'passed' ? '✓' : '✗';
        alert(`${icon} ${result.outcome.toUpperCase()}\n\n${result.summary || ''}`);
        await refreshTasks();
    } catch (err) {
        console.error('[PIP] verify failed:', err);
        alert('Verification failed — check the console.');
    }
}

async function pipResolve(taskId, pipId) {
    if (!confirm('Queue a PIP Resolution Agent for this PIP?')) return;
    try {
        const resp = await fetch(`${API_BASE}/tasks/${taskId}/run-pip-resolution/${pipId}`, { method: 'POST' });
        if (resp.status === 202) {
            alert('Resolution agent queued. The scheduler will dispatch it shortly.');
        } else {
            const data = await resp.json().catch(() => ({}));
            alert('Failed to queue resolution: ' + JSON.stringify(data));
        }
    } catch (err) {
        console.error('[PIP] resolve failed:', err);
    }
}

function pipHistory(taskId, pipId) {
    openPipDetailModal(taskId, pipId);
}

// ============================================
// PIP detail modal
// ============================================

let _pipDetailCtx = { taskId: null, pipId: null };

function _escHtml(str) {
    return String(str || '')
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}

async function openPipDetailModal(taskId, pipId) {
    _pipDetailCtx = { taskId, pipId };
    const modal = document.getElementById('pip-detail-modal');
    if (!modal) { console.error('pip-detail-modal not found'); return; }

    document.getElementById('pip-detail-title').textContent = 'Loading…';
    document.getElementById('pip-detail-meta').textContent = '';
    document.getElementById('pip-detail-requirements').innerHTML = '';
    document.getElementById('pip-detail-history-body').innerHTML = '';
    document.getElementById('pip-detail-history-empty').style.display = 'none';
    modal.style.display = 'flex';

    try {
        const [pipsResp, verifResp] = await Promise.all([
            fetch(`${API_BASE}/tasks/${taskId}/pips`),
            fetch(`${API_BASE}/tasks/${taskId}/pips/${pipId}/verifications`),
        ]);
        const pips = await pipsResp.json();
        const verifications = await verifResp.json();

        const pip = pips.find(p => p.id === pipId);
        if (!pip) { alert('PIP not found'); closePipDetailModal(); return; }

        const idx = pips.indexOf(pip) + 1;
        document.getElementById('pip-detail-title').textContent =
            `PIP ${idx} — demoted from ${pip.origin_stage}`;
        document.getElementById('pip-detail-meta').textContent =
            `Created: ${pip.created_at || '—'}  |  Current status: ${pip.status}`;

        // Requirements
        const ul = document.getElementById('pip-detail-requirements');
        (pip.requirements || []).forEach(req => {
            const li = document.createElement('li');
            li.textContent = req;
            ul.appendChild(li);
        });

        // Verification history
        const tbody = document.getElementById('pip-detail-history-body');
        const empty = document.getElementById('pip-detail-history-empty');
        if (!verifications.length) {
            empty.style.display = 'block';
        } else {
            verifications.forEach(v => {
                const outcomeColor = v.outcome === 'passed' ? '#198754'
                    : v.outcome === 'failed' ? '#dc3545' : '#fd7e14';
                const tr = document.createElement('tr');
                tr.innerHTML =
                    `<td class="pip-hist-cell">${_escHtml(v.checked_at_stage)}</td>` +
                    `<td class="pip-hist-cell" style="color:${outcomeColor};font-weight:700">${_escHtml(v.outcome)}</td>` +
                    `<td class="pip-hist-cell">${_escHtml(v.summary)}</td>` +
                    `<td class="pip-hist-cell pip-hist-when">${_escHtml(v.created_at)}</td>`;
                tbody.appendChild(tr);
            });
        }
    } catch (err) {
        console.error('[PIP] modal load failed:', err);
        document.getElementById('pip-detail-title').textContent = 'Error loading PIP';
    }
}

function closePipDetailModal() {
    const modal = document.getElementById('pip-detail-modal');
    if (modal) modal.style.display = 'none';
    _pipDetailCtx = { taskId: null, pipId: null };
}

async function pipDetailVerify() {
    const { taskId, pipId } = _pipDetailCtx;
    if (!taskId || !pipId) return;
    const btn = document.getElementById('pip-detail-verify-btn');
    btn.disabled = true;
    btn.textContent = '⏳ Verifying…';
    try {
        const resp = await fetch(`${API_BASE}/tasks/${taskId}/pips/${pipId}/verify`, { method: 'POST' });
        if (!resp.ok) { alert('Verification failed: ' + resp.status); return; }
        const result = await resp.json();
        alert(`${result.outcome === 'passed' ? '✓' : '✗'} ${result.outcome.toUpperCase()}\n\n${result.summary || ''}`);
        await openPipDetailModal(taskId, pipId);  // reload with fresh data
        await refreshTasks();
    } catch (err) {
        console.error('[PIP] detail verify failed:', err);
    } finally {
        btn.disabled = false;
        btn.textContent = '🔍 Run Verification';
    }
}

async function pipDetailResolve() {
    const { taskId, pipId } = _pipDetailCtx;
    if (!taskId || !pipId) return;
    const btn = document.getElementById('pip-detail-resolve-btn');
    btn.disabled = true;
    try {
        const resp = await fetch(`${API_BASE}/tasks/${taskId}/run-pip-resolution/${pipId}`, { method: 'POST' });
        if (resp.status === 202) {
            alert('Resolution agent queued. The scheduler will dispatch it shortly.');
        } else {
            const data = await resp.json().catch(() => ({}));
            alert('Failed to queue: ' + JSON.stringify(data));
        }
    } catch (err) {
        console.error('[PIP] detail resolve failed:', err);
    } finally {
        btn.disabled = false;
    }
}

// ============================================
// Task Rendering Functions
// ============================================

function renderTasksFromDatabase() {
    console.log('Rendering tasks from database...');

    // Reset caches — full rebuild from scratch
    Object.keys(cardCache).forEach(id => delete cardCache[id]);
    Object.keys(fingerprintCache).forEach(id => delete fingerprintCache[id]);

    // Clear ALL existing task cards from ALL columns
    const columns = ['idea', 'planning', 'indev', 'conceptual_review', 'optimization', 'security', 'full_review', 'completed'];

    columns.forEach(columnType => {
        const container = document.getElementById(`tasks-${columnType}`);
        if (container) {
            while (container.firstChild) {
                container.removeChild(container.firstChild);
            }
        }
    });

    // Create task cards from taskData, sorted by position within each column.
    // Group tasks by type first so the sort is per-column, not global.
    // "subdividing" tasks render in the idea column; "cancelled" tasks are hidden.
    //
    // If currentBigIdeaFilter is set, only show descendants of that Big Idea
    // plus the Big Idea itself.
    const allVisible = Object.values(taskData).filter(t => t && t.type && t.type !== 'cancelled');
    const filteredTasks = currentBigIdeaFilter
        ? allVisible.filter(t => {
            if (t.id === currentBigIdeaFilter) return true;
            const descendants = descendantIndex[currentBigIdeaFilter] || [];
            return descendants.includes(t.id);
        })
        : allVisible;
    const hiddenCount = currentBigIdeaFilter ? allVisible.length - filteredTasks.length : 0;

    const tasksByType = {};
    filteredTasks.forEach(task => {
        const renderCol = task.type === 'subdividing' ? 'idea' : task.type;
        if (!tasksByType[renderCol]) tasksByType[renderCol] = [];
        tasksByType[renderCol].push(task);
    });

    // Update breadcrumb bar
    updateBreadcrumbBar(hiddenCount);

    columns.forEach(colType => {
        const tasks = (tasksByType[colType] || []).sort((a, b) => (a.position ?? 0) - (b.position ?? 0));
        tasks.forEach(task => {
            const container = document.getElementById(`tasks-${colType}`);
            if (container) {
                const rawCard = createTaskCard(task.id, task.title, task.tags, task.owner, task.type);
                const element = wrapWithPipGroup(rawCard, task);
                container.appendChild(element);
                cardCache[task.id] = element;
                fingerprintCache[task.id] = taskFingerprint(task);
            }
        });
    });

    console.log(`Rendered ${Object.values(taskData).filter(t => t && t.type).length} task cards from database`);

    // Update task counts and arch bar
    updateTaskCounts();
    renderArchBar();
    _scheduleStageFooterBatch();
}

function updateTaskCounts() {
    const columns = ['idea', 'planning', 'indev', 'conceptual_review', 'optimization', 'security', 'full_review', 'completed'];

    columns.forEach(columnType => {
        const container = document.getElementById(`tasks-${columnType}`);
        const countElement = document.getElementById(`count-${columnType}`);

        if (container && countElement) {
            const count = container.querySelectorAll('.task-card').length;
            countElement.textContent = count;
        }
    });
}

// ============================================
// PROJECT ARCHITECTURE horizontal bar
// ============================================

function toggleArchBar() {
    _archBarCollapsed = !_archBarCollapsed;
    localStorage.setItem('archBarCollapsed', _archBarCollapsed ? '1' : '0');
    const bar = document.getElementById('arch-bar');
    if (bar) bar.classList.toggle('collapsed', _archBarCollapsed);
    const btn = document.getElementById('arch-bar-toggle');
    if (btn) btn.textContent = _archBarCollapsed ? '\u25BC' : '\u25B2';
}

async function populateArchBar() {
    if (!currentProject) return;
    const btn = document.getElementById('arch-bar-populate');
    if (!btn || btn.disabled) return;
    btn.disabled = true;
    const orig = btn.textContent;
    btn.textContent = '…';
    try {
        const r = await fetch(`/api/projects/${encodeURIComponent(currentProject)}/populate-arch`, { method: 'POST' });
        const data = await r.json();
        if (!r.ok) throw new Error(data.detail || 'Error');
        if (data.queued === 0) {
            btn.textContent = 'All done';
        } else {
            btn.textContent = `Queued ${data.queued}`;
        }
    } catch (e) {
        btn.textContent = 'Error';
        console.error('populateArchBar:', e);
    } finally {
        setTimeout(() => {
            btn.disabled = false;
            btn.textContent = orig;
        }, 3000);
    }
}

function renderArchBar() {
    const container = document.getElementById('arch-cards');
    const countEl   = document.getElementById('arch-bar-count');
    if (!container) return;

    // Gather and sort architecture tasks: critical first, then high, normal, low; then by position
    const priorityOrder = { critical: 0, high: 1, normal: 2, low: 3 };
    const archTasks = Object.values(taskData)
        .filter(t => t && t.type === 'architecture')
        .sort((a, b) => {
            const pa = priorityOrder[(a.content || {}).priority] ?? 2;
            const pb = priorityOrder[(b.content || {}).priority] ?? 2;
            if (pa !== pb) return pa - pb;
            return (a.position ?? 0) - (b.position ?? 0);
        });

    // Update count badge and subtitle progress
    if (countEl) countEl.textContent = archTasks.length > 0 ? `${archTasks.length} card${archTasks.length !== 1 ? 's' : ''}` : '';
    const subtitleEl = document.getElementById('arch-bar-subtitle');
    if (subtitleEl) {
        const running = _archGenJobs.filter(j => j.status === 'running').length;
        const pending = _archGenJobs.filter(j => j.status === 'pending').length;
        if (running > 0 || pending > 0) {
            subtitleEl.innerHTML = `<span style="color:#ffc107;font-weight:bold">Generating architecture cards\u2026</span> (${running} running, ${pending} queued)`;
        } else {
            subtitleEl.textContent = 'Global constraints \u0026 context \u2014 injected into all agents';
        }
    }

    // Rebuild cards
    container.innerHTML = '';
    archTasks.forEach(task => {
        const content  = task.content || {};
        const category = content.category || 'General';
        const priority = content.priority || 'normal';
        const color    = ARCH_CATEGORY_COLORS[category] || ARCH_CATEGORY_COLORS.General;
        const body     = (task.description || '').trim();

        const card = document.createElement('div');
        card.className = `arch-card prio-${priority}`;
        card.dataset.id = task.id;
        card.innerHTML = `
            <div class="arch-card-category" style="color:${color}">${escapeHtml(category)}</div>
            <div class="arch-card-title">${escapeHtml(task.title || '')}</div>
            ${body ? `<div class="arch-card-body">${escapeHtml(body)}</div>` : ''}
            <div class="arch-card-prio-badge prio-${priority}">${priority === 'critical' ? 'CRITICAL' : priority === 'high' ? 'HIGH' : priority === 'low' ? 'low' : ''}</div>
            <div class="arch-card-toolbar">
                <button class="arch-card-btn" onclick="event.stopPropagation();editArchitectureTask('${task.id}')">Edit</button>
                <button class="arch-card-btn danger" onclick="event.stopPropagation();deleteTask('${task.id}')">Del</button>
            </div>`;
        card.addEventListener('click', () => editArchitectureTask(task.id));
        container.appendChild(card);
    });

    // Render ghost cards for pending/running arch gen jobs where the category
    // doesn't already have a real card.
    const existingCategories = new Set(archTasks.map(t => (t.content || {}).category || 'General'));
    _archGenJobs.forEach(job => {
        if (existingCategories.has(job.category)) return;
        const color   = ARCH_CATEGORY_COLORS[job.category] || ARCH_CATEGORY_COLORS.General;
        const isRunning = job.status === 'running';
        const ghost = document.createElement('div');
        ghost.className = 'arch-card ghost';
        ghost.dataset.archGenId = job.id;
        ghost.innerHTML = `
            <div class="arch-card-category" style="color:${color}">${escapeHtml(job.category)}</div>
            <div class="arch-ghost-label">
                <span class="arch-ghost-dot${isRunning ? ' running' : ''}"></span>
                ${isRunning ? 'Generating\u2026' : 'Queued\u2026'}
            </div>`;
        container.appendChild(ghost);
    });

    // Apply collapsed state
    const bar = document.getElementById('arch-bar');
    if (bar) bar.classList.toggle('collapsed', _archBarCollapsed);
    const btn = document.getElementById('arch-bar-toggle');
    if (btn) btn.textContent = _archBarCollapsed ? '\u25BC' : '\u25B2';
}

// Sort a single column's cards into position order using the cache.
// appendChild on an existing child moves it — no DOM nodes are created or destroyed.
function sortColumn(colType) {
    const container = document.getElementById(`tasks-${colType}`);
    if (!container) return;
    const tasks = Object.values(taskData)
        .filter(t => t && t.type !== 'cancelled' && (t.type === 'subdividing' ? 'idea' : t.type) === colType)
        .sort((a, b) => (a.position ?? 0) - (b.position ?? 0));
    tasks.forEach(task => {
        const card = cardCache[task.id];
        if (card) container.appendChild(card);
    });
}

// Diff newTasks against the current DOM/cache state and apply only the changes.
// Called by the auto-refresh loop instead of a full renderTasksFromDatabase().
function reconcile(newTasks) {
    // Map view is open — just keep task data fresh; re-render on close
    if (columnMapActive) {
        allTasks = newTasks;
        taskData = {};
        allTasks.forEach(t => { taskData[t.id] = t; });
        buildDescendantIndex();
        return;
    }
    // Zoom view has filtered visibility logic — fall back to full render
    if (currentBigIdeaFilter) {
        allTasks = newTasks;
        taskData = {};
        allTasks.forEach(t => { taskData[t.id] = t; });
        buildDescendantIndex();
        renderTasksFromDatabase();
        return;
    }

    const columnsToSort = new Set();

    // 1. Remove tasks that no longer exist
    for (const id of Object.keys(cardCache)) {
        if (!newTasks.find(t => t.id === id)) {
            const card = cardCache[id];
            if (card.parentNode) card.parentNode.removeChild(card);
            delete cardCache[id];
            delete fingerprintCache[id];
        }
    }

    // 2. Create new cards and rebuild changed ones; architecture tasks are handled
    //    by renderArchBar() — skip them here entirely.
    let archChanged = false;
    for (const task of newTasks) {
        if (task.type === 'cancelled') continue;
        if (task.type === 'architecture') {
            // Track whether any arch card changed so we know to re-render the bar
            const newFp = taskFingerprint(task);
            if (!fingerprintCache[task.id] || fingerprintCache[task.id] !== newFp) {
                archChanged = true;
                fingerprintCache[task.id] = newFp;
            }
            continue;
        }
        const renderCol = task.type === 'subdividing' ? 'idea' : task.type;
        const newFp = taskFingerprint(task);

        if (!cardCache[task.id]) {
            // New task — create and insert
            const rawCard = createTaskCard(task.id, task.title, task.tags, task.owner, task.type);
            _applyHighlightState(rawCard, task.id);
            const element = wrapWithPipGroup(rawCard, task);
            cardCache[task.id] = element;
            fingerprintCache[task.id] = newFp;
            columnsToSort.add(renderCol);
            if (task.type !== 'idea' && task.type !== 'architecture' && task.type !== 'subdividing') {
                setTimeout(() => _loadStageFooter(task.id), 200);
            }
        } else if (fingerprintCache[task.id] !== newFp) {
            // Changed — rebuild the card element in-place
            const old = cardCache[task.id];
            const oldTask = taskData[task.id];
            if (oldTask) {
                columnsToSort.add(oldTask.type === 'subdividing' ? 'idea' : oldTask.type);
            }
            const rawCard = createTaskCard(task.id, task.title, task.tags, task.owner, task.type);
            _applyHighlightState(rawCard, task.id);
            const newElement = wrapWithPipGroup(rawCard, task);
            if (old.parentNode) old.parentNode.replaceChild(newElement, old);
            cardCache[task.id] = newElement;
            fingerprintCache[task.id] = newFp;
            columnsToSort.add(renderCol);
            // Reload footer — stage or component status may have changed
            if (task.type !== 'idea' && task.type !== 'architecture' && task.type !== 'subdividing') {
                delete _stageSummaryCache[task.id];
                setTimeout(() => _loadStageFooter(task.id), 200);
            }
        }
    }

    // 3. Commit new global state
    allTasks = newTasks;
    taskData = {};
    allTasks.forEach(t => { taskData[t.id] = t; });
    buildDescendantIndex();

    // 4. Re-sort only the columns that had changes
    for (const col of columnsToSort) {
        sortColumn(col);
    }

    // 5. Re-render arch bar if anything changed there
    if (archChanged) renderArchBar();

    updateBreadcrumbBar();
    updateTaskCounts();
}

// Rebuild a single card — used when only transition/processing state changes
// (those aren't in the fingerprint since they're client-side state).
function refreshCard(taskId) {
    const task = taskData[taskId];
    if (!task) return;
    const rawCard = createTaskCard(task.id, task.title, task.tags, task.owner, task.type);
    _applyHighlightState(rawCard, taskId);
    const newElement = wrapWithPipGroup(rawCard, task);
    const old = cardCache[taskId];
    if (old && old.parentNode) {
        old.parentNode.replaceChild(newElement, old);
    } else {
        const renderCol = task.type === 'subdividing' ? 'idea' : task.type;
        const container = document.getElementById(`tasks-${renderCol}`);
        if (container) container.appendChild(newElement);
    }
    cardCache[taskId] = newElement;
    fingerprintCache[taskId] = taskFingerprint(task);
}

// ============================================
// Stage Footer + Stage Journal
// ============================================

const _stageSummaryCache = {};  // taskId -> summary object
let _stageFooterBatchTimer = null;

function _scheduleStageFooterBatch() {
    if (_stageFooterBatchTimer) clearTimeout(_stageFooterBatchTimer);
    _stageFooterBatchTimer = setTimeout(() => {
        const stages = ['planning','indev','conceptual_review','optimization','security','full_review','completed'];
        const ids = Object.values(taskData)
            .filter(t => t && stages.includes(t.type))
            .map(t => t.id);
        // Load in chunks of 6 with 80ms spacing to avoid burst
        let offset = 0;
        const chunk = 6;
        function loadChunk() {
            const batch = ids.slice(offset, offset + chunk);
            batch.forEach(id => _loadStageFooter(id));
            offset += chunk;
            if (offset < ids.length) setTimeout(loadChunk, 100);
        }
        loadChunk();
    }, 300);
}

async function _loadStageFooter(taskId) {
    const el = document.getElementById(`csf-${taskId}`);
    if (!el) return;
    try {
        const resp = await fetch(`/api/tasks/${taskId}/stage-summary`);
        if (!resp.ok) { el.className = 'card-stage-footer csf-empty'; return; }
        const s = await resp.json();
        _stageSummaryCache[taskId] = s;
        _renderStageFooter(el, s);
    } catch (_) {
        el.className = 'card-stage-footer csf-empty';
    }
}

function _renderStageFooter(el, s) {
    const stage = s.current_stage;
    const parts = [];

    if (s.blocking_issue) {
        parts.push(`<span class="csf-block">&#9888; ${escHtml(s.blocking_issue)}</span>`);
    }

    if (stage === 'planning' && s.planning.has_result) {
        const fc = s.planning.file_count;
        const sc = s.planning.step_count;
        parts.push(`<span class="csf-chip csf-muted">&#128196; ${fc} file${fc===1?'':'s'} &middot; ${sc} step${sc===1?'':'s'}</span>`);
        if (!s.blocking_issue) {
            if (s.planning.gate_passed === true)  parts.push(`<span class="csf-chip csf-ok">&#10003; gate</span>`);
            if (s.planning.gate_passed === false) parts.push(`<span class="csf-chip csf-warn">&#10007; gate</span>`);
            if (s.planning.gate_passed === null)  parts.push(`<span class="csf-chip csf-muted">gate pending</span>`);
        }
    } else if (stage === 'indev') {
        const c = s.components;
        if (c.total > 0) {
            parts.push(`<span class="csf-chip csf-muted">&#9881; ${c.done}/${c.total}</span>`);
            if (c.files_changed > 0)
                parts.push(`<span class="csf-chip csf-muted">${c.files_changed} file${c.files_changed===1?'':'s'}</span>`);
        } else if (s.planning.has_result) {
            parts.push(`<span class="csf-chip csf-muted">&#128196; ${s.planning.file_count} files planned</span>`);
        }
    } else if (stage === 'conceptual_review') {
        if (s.planning.has_result)
            parts.push(`<span class="csf-chip csf-muted">&#128196; ${s.planning.file_count} files</span>`);
        if (s.components.total > 0)
            parts.push(`<span class="csf-chip csf-muted">&#9881; ${s.components.done}/${s.components.total}</span>`);
    } else if (stage === 'optimization') {
        if (s.optimization.has_result) {
            const oc = s.optimization.outcome || '?';
            parts.push(`<span class="csf-chip ${oc==='improved'?'csf-ok':'csf-muted'}">&#9889; ${escHtml(oc)}</span>`);
        }
    } else if (stage === 'security') {
        if (s.security.has_result) {
            const v = s.security.worst_verdict || '';
            const cls = (v==='REJECTED'||v==='NOT_SUITABLE') ? 'csf-warn' : (v==='LIKELY'?'csf-ok':'csf-muted');
            parts.push(`<span class="csf-chip ${cls}">&#128274; ${escHtml(v)}</span>`);
            const c = s.security.critical_count;
            if (c > 0) parts.push(`<span class="csf-chip csf-warn">${c} critical</span>`);
        }
    } else if (stage === 'full_review') {
        if (s.full_review.has_result) {
            const v = s.full_review.worst_verdict || '';
            const cls = (v==='REJECTED'||v==='NOT_SUITABLE') ? 'csf-warn' : (v==='LIKELY'?'csf-ok':'csf-muted');
            parts.push(`<span class="csf-chip ${cls}">&#128065; ${escHtml(v)}</span>`);
        }
    } else if (stage === 'completed') {
        const ms = s.merge.status;
        if (ms === 'merged') parts.push(`<span class="csf-chip csf-ok">&#10003; merged</span>`);
        else if (s.components.total > 0) parts.push(`<span class="csf-chip csf-ok">&#10003; ${s.components.done} components</span>`);
        else parts.push(`<span class="csf-chip csf-ok">&#10003; complete</span>`);
    }

    if (parts.length === 0) {
        el.className = 'card-stage-footer csf-empty';
        return;
    }
    el.className = 'card-stage-footer';
    el.innerHTML = parts.join('<span class="csf-sep">·</span>');
}

function escHtml(s) {
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// ---- Stage Journal Modal ----

window._sjTaskId = null;

function openStageJournal(taskId, section) {
    window._sjTaskId = taskId;
    window._sjScrollTo = section || null;
    const task = taskData[taskId];
    const title = task ? task.title : taskId;
    document.getElementById('sj-title').textContent = `Stage Journal — ${title}`;
    document.getElementById('sj-body').innerHTML = '<div style="color:#6c757d;font-size:0.9rem">Loading…</div>';
    document.getElementById('sj-diag-btn').style.display = 'inline-block';
    document.getElementById('stage-journal-modal').style.display = 'flex';
    _buildStageJournal(taskId);
}

function openStageDiff(taskId) {
    openStageJournal(taskId, 'diff');
}

function closeStageJournal() {
    document.getElementById('stage-journal-modal').style.display = 'none';
    window._sjTaskId = null;
}

async function _buildStageJournal(taskId) {
    const body = document.getElementById('sj-body');
    try {
        const [summaryResp, planResp, compResp, optResp, secResp, frResp, mrResp, diffResp, rjResp] = await Promise.all([
            fetch(`/api/tasks/${taskId}/stage-summary`),
            fetch(`/api/tasks/${taskId}/planning-result`),
            fetch(`/api/tasks/${taskId}/component-status`),
            fetch(`/api/tasks/${taskId}/optimization-status`),
            fetch(`/api/tasks/${taskId}/security-status`),
            fetch(`/api/tasks/${taskId}/full-review-status`),
            fetch(`/api/tasks/${taskId}/merge-status`),
            fetch(`/api/tasks/${taskId}/diff`),
            fetch(`/api/tasks/${taskId}/research-jobs`),
        ]);

        const summary  = summaryResp.ok  ? await summaryResp.json()  : null;
        const plan     = planResp.ok     ? await planResp.json()     : null;
        const comps    = compResp.ok     ? await compResp.json()     : [];
        const opt      = optResp.ok      ? await optResp.json()      : null;
        const secList  = secResp.ok      ? await secResp.json()      : [];
        const frList   = frResp.ok       ? await frResp.json()       : [];
        const merge    = mrResp.ok       ? await mrResp.json()       : null;
        const diffData = diffResp.ok     ? await diffResp.json()     : null;
        const rjList   = rjResp.ok       ? await rjResp.json()       : [];

        let html = '';

        // ---- Planning section ----
        if (plan) {
            if (plan.status === 'in_progress') {
                html += `<div class="sj-section">
                    <div class="sj-section-title">&#128196; Planning</div>
                    <div style="color:#fd7e14;font-size:0.85rem">Pipeline running&#8230;</div>
                </div>`;
            } else if (plan.status === 'failed') {
                html += `<div class="sj-section">
                    <div class="sj-section-title">&#128196; Planning <span class="sj-badge warn">run failed</span></div>
                    <div style="color:#dc3545;font-size:0.85rem;margin-top:0.4rem">${escHtml(plan.error_message || 'Unknown error')}</div>
                </div>`;
            } else {
            const gatePassed = plan.gate_passed;
            const gateLabel  = gatePassed === true ? '<span class="sj-badge ok">gate ✓</span>'
                             : gatePassed === false ? '<span class="sj-badge warn">gate ✗</span>'
                             : '';
            html += `<div class="sj-section">
                <div class="sj-section-title">&#128196; Planning ${gateLabel}</div>`;

            if (plan.design_rationale && typeof plan.design_rationale === 'string') {
                html += `<div class="sj-rationale">${escHtml(plan.design_rationale)}</div>`;
            }

            if (plan.file_manifest && plan.file_manifest.length) {
                html += `<table class="sj-table">
                    <thead><tr><th>File</th><th>Action</th><th>Purpose</th><th>~Lines</th></tr></thead><tbody>`;
                for (const f of plan.file_manifest) {
                    const ac = f.action || '';
                    const acCls = ac === 'create' ? 'action-create' : ac === 'modify' ? 'action-modify' : ac === 'delete' ? 'action-delete' : '';
                    html += `<tr>
                        <td><code style="font-size:0.75rem">${escHtml(f.path||'')}</code></td>
                        <td class="col-action ${acCls}">${escHtml(ac)}</td>
                        <td>${escHtml(f.purpose||'')}</td>
                        <td>${f.estimated_lines||''}</td>
                    </tr>`;
                }
                html += '</tbody></table>';
            }

            if (plan.implementation_steps && plan.implementation_steps.length) {
                html += `<div style="font-size:0.75rem;font-weight:700;color:#6c757d;margin:0.6rem 0 0.3rem">Implementation Steps</div>
                    <table class="sj-table"><thead><tr><th>#</th><th>Component</th><th>Description</th><th>Files</th></tr></thead><tbody>`;
                for (const st of plan.implementation_steps) {
                    html += `<tr>
                        <td>${st.order}</td>
                        <td><strong>${escHtml(st.component||'')}</strong></td>
                        <td>${escHtml(st.description||'')}</td>
                        <td style="font-size:0.72rem;color:#6c757d">${(st.files||[]).map(f=>escHtml(f)).join('<br>')}</td>
                    </tr>`;
                }
                html += '</tbody></table>';
            }

            if (plan.gate_checks && plan.gate_checks.length) {
                html += `<div style="font-size:0.75rem;font-weight:700;color:#6c757d;margin:0.6rem 0 0.3rem">Gate Checks</div>`;
                for (const c of plan.gate_checks) {
                    const icon = c.passed ? '&#10003;' : (c.hard_fail ? '&#10007;' : '&#9888;');
                    const color = c.passed ? '#198754' : (c.hard_fail ? '#dc3545' : '#fd7e14');
                    html += `<div class="sj-check-row">
                        <span class="sj-check-icon" style="color:${color}">${icon}</span>
                        <div><strong>${escHtml(c.name)}</strong><div class="sj-check-detail">${escHtml(c.detail||'')}</div></div>
                    </div>`;
                }
            }

            if (plan.review_votes && plan.review_votes.length) {
                html += `<div style="font-size:0.75rem;font-weight:700;color:#6c757d;margin:0.6rem 0 0.3rem">Design Review Votes</div>`;
                for (const v of plan.review_votes) {
                    const vKey = (v.verdict||'').toUpperCase();
                    html += `<div class="sj-vote-row">
                        <span class="sj-vote-badge vote-${vKey}">${escHtml(vKey)}</span>
                        <div><strong>${escHtml(v.stage||'')}</strong>
                            <div class="sj-check-detail">${escHtml((v.justification||'').slice(0,300))}</div>
                        </div>
                    </div>`;
                }
            }

            if (plan.pitfalls_identified && plan.pitfalls_identified.length) {
                html += `<div style="font-size:0.75rem;font-weight:700;color:#6c757d;margin:0.6rem 0 0.3rem">Pitfalls</div>`;
                for (const p of plan.pitfalls_identified) {
                    html += `<div class="sj-pitfall pitfall-${p.severity||'low'}">
                        <span style="font-weight:700;font-size:0.72rem;text-transform:uppercase;color:#6c757d">${escHtml(p.severity||'')}</span>
                        <span>${escHtml(p.detail||p.type||'')}</span>
                    </div>`;
                }
            }

            html += '</div>';
            } // end else (active/superseded plan)
        }

        // ---- Dev / Components section ----
        if (comps && comps.length) {
            const done = comps.filter(c=>c.status==='done').length;
            const failed = comps.filter(c=>c.status==='failed').length;
            const badge = failed > 0 ? `<span class="sj-badge warn">${failed} failed</span>`
                        : done===comps.length ? `<span class="sj-badge ok">all done</span>`
                        : `<span class="sj-badge info">${done}/${comps.length}</span>`;
            html += `<div class="sj-section">
                <div class="sj-section-title">&#9881; Development ${badge}</div>
                <table class="sj-table"><thead><tr><th>#</th><th>Component</th><th>Status</th><th>Turns</th><th>Files changed</th></tr></thead><tbody>`;
            for (const c of comps) {
                const sc = `status-${c.status}`;
                const files = (c.files_changed||[]).map(f=>`<code style="font-size:0.72rem">${escHtml(f)}</code>`).join('<br>');
                html += `<tr>
                    <td>${c.step_order}</td>
                    <td><strong>${escHtml(c.component_name)}</strong>${c.error_detail?`<div style="font-size:0.72rem;color:#dc3545">${escHtml(c.error_detail.slice(0,120))}</div>`:''}</td>
                    <td class="${sc}">${escHtml(c.status)}</td>
                    <td>${c.turns_used||0}</td>
                    <td>${files||'<span style="color:#adb5bd">—</span>'}</td>
                </tr>`;
            }
            html += '</tbody></table></div>';
        }

        // ---- Optimization section ----
        if (opt && opt.outcome && opt.outcome !== 'not_run') {
            const ok = opt.outcome === 'improved';
            html += `<div class="sj-section">
                <div class="sj-section-title">&#9889; Optimization <span class="sj-badge ${ok?'ok':'muted'}">${escHtml(opt.outcome)}</span></div>`;
            if (opt.improvement_summary)
                html += `<div class="sj-rationale">${escHtml(opt.improvement_summary)}</div>`;
            html += '</div>';
        }

        // ---- Security section ----
        if (secList && secList.length) {
            const hasCrit = secList.some(s=>(s.critical_count||0)>0||(s.verdict||'').includes('REJECT'));
            html += `<div class="sj-section">
                <div class="sj-section-title">&#128274; Security ${hasCrit?'<span class="sj-badge warn">issues</span>':'<span class="sj-badge ok">ok</span>'}</div>`;
            for (const s of secList) {
                const vKey = (s.verdict||'').toUpperCase();
                html += `<div class="sj-vote-row">
                    <span class="sj-vote-badge vote-${vKey}">${escHtml(vKey)}</span>
                    <div>
                        <strong>${escHtml(s.reviewer_type||'')}</strong>
                        ${s.critical_count||s.high_count ? `<span style="font-size:0.72rem;color:#dc3545;margin-left:0.4rem">${s.critical_count||0} critical &middot; ${s.high_count||0} high</span>` : ''}
                        <div class="sj-check-detail">${escHtml((s.justification||'').slice(0,300))}</div>
                    </div>
                </div>`;
            }
            html += '</div>';
        }

        // ---- Full Review section ----
        if (frList && frList.length) {
            const worst = frList.map(r=>r.verdict).sort((a,b)=>{
                const o=['REJECTED','NOT_SUITABLE','NEEDS_RESEARCH','POSSIBLE','LIKELY'];
                return o.indexOf(a)-o.indexOf(b);
            })[0]||'';
            const pass = (worst==='LIKELY'||worst==='POSSIBLE');
            html += `<div class="sj-section">
                <div class="sj-section-title">&#128065; Full Review <span class="sj-badge ${pass?'ok':'warn'}">${escHtml(worst)}</span></div>`;
            for (const r of frList) {
                const vKey = (r.verdict||'').toUpperCase();
                html += `<div class="sj-vote-row">
                    <span class="sj-vote-badge vote-${vKey}">${escHtml(vKey)}</span>
                    <div><strong>${escHtml(r.reviewer_type||'')}</strong>
                        <div class="sj-check-detail">${escHtml((r.justification||'').slice(0,300))}</div>
                    </div>
                </div>`;
            }
            html += '</div>';
        }

        // ---- Merge section ----
        if (merge && merge.status && merge.status !== 'not_merged') {
            const ok = merge.status === 'merged';
            html += `<div class="sj-section">
                <div class="sj-section-title">&#128256; Merge <span class="sj-badge ${ok?'ok':'warn'}">${escHtml(merge.status)}</span></div>
                <table class="sj-table"><tbody>
                    <tr><td><strong>Branch</strong></td><td><code>${escHtml(merge.branch_name||'—')}</code></td></tr>
                    ${merge.merge_commit_sha?`<tr><td><strong>Commit</strong></td><td><code>${escHtml(merge.merge_commit_sha)}</code></td></tr>`:''}
                </tbody></table>
            </div>`;
        }

        // ---- Code Diff section ----
        if (diffData && diffData.method) {
            const branch = diffData.branch || '';
            const method = diffData.method;
            const hasDiff = diffData.diff && diffData.diff.trim().length > 0;
            const methodLabel = method === 'merge_commit'
                ? `<span class="sj-badge muted">merge commit ${escHtml((diffData.head_ref||'').slice(0,8))}</span>`
                : `<span class="sj-badge info">${escHtml(branch)}</span>`;
            html += `<div class="sj-section" id="sj-diff-section">
                <div class="sj-section-title">&#9998; Code Diff ${methodLabel}</div>`;
            if (diffData.stat) {
                html += `<div class="diff-stat">${escHtml(diffData.stat)}</div>`;
            }
            if (hasDiff) {
                html += `<div class="diff-viewer">${_renderDiff(diffData.diff)}</div>`;
                if (diffData.truncated) {
                    html += `<div class="diff-truncated-note">&#8230; diff truncated at 64 KiB — use git diff locally for the full output</div>`;
                }
            } else {
                html += `<div style="color:#6c757d;font-size:0.82rem;padding:0.4rem 0">No changes recorded on this branch yet.</div>`;
            }
            html += '</div>';
        } else if (diffData && diffData.error) {
            html += `<div class="sj-section" id="sj-diff-section">
                <div class="sj-section-title">&#9998; Code Diff</div>
                <div style="color:#6c757d;font-size:0.82rem">${escHtml(diffData.error)}</div>
            </div>`;
        }

        // ---- Research Jobs section ----
        {
            const completed = rjList.filter(j=>j.status==='completed').length;
            const failed    = rjList.filter(j=>j.status==='failed').length;
            const pending   = rjList.filter(j=>j.status==='pending'||j.status==='running').length;
            let chips = '';
            if (completed) chips += `<span class="sj-rj-chip chip-completed">${completed} completed</span>`;
            if (pending)   chips += `<span class="sj-rj-chip chip-pending">${pending} pending</span>`;
            if (failed)    chips += `<span class="sj-rj-chip chip-failed">${failed} failed</span>`;
            html += `<details class="sj-section sj-rj-details">
                <summary class="sj-section-title">&#128202; Research Jobs (${rjList.length}) ${chips}</summary>`;
            if (rjList.length === 0) {
                html += `<div class="sj-rj-empty">No research jobs recorded for this task.</div>`;
            } else {
                for (const j of rjList) { html += _renderResearchJobCard(j); }
            }
            html += '</details>';
        }

        if (!html) {
            html = '<div style="color:#6c757d;font-size:0.9rem;padding:1rem 0">No pipeline artifacts yet for this card.</div>';
        }

        body.innerHTML = html;

        // Scroll to requested section
        if (window._sjScrollTo === 'diff') {
            const el = body.querySelector('#sj-diff-section');
            if (el) setTimeout(() => el.scrollIntoView({behavior:'smooth', block:'start'}), 50);
            window._sjScrollTo = null;
        }

        // Update cache
        if (summaryResp.ok) _stageSummaryCache[taskId] = summary;
    } catch (err) {
        body.innerHTML = `<div style="color:#dc3545">Failed to load stage journal: ${escHtml(String(err))}</div>`;
    }
}

function _renderDiff(diffText) {
    // Parse unified diff into colored HTML
    const lines = diffText.split('\n');
    let html = '';
    let lineNum = 0;

    for (const line of lines) {
        if (line.startsWith('diff --git') || line.startsWith('index ') ||
            line.startsWith('--- ') || line.startsWith('+++ ')) {
            if (line.startsWith('diff --git')) {
                const fname = line.replace('diff --git ', '').split(' b/').pop() || line;
                html += `<div class="diff-file-header">&#128196; ${escHtml(fname)}</div>`;
            }
            continue;
        }
        if (line.startsWith('@@')) {
            // Parse hunk header for starting line number
            const m = line.match(/@@ -\d+(?:,\d+)? \+(\d+)/);
            lineNum = m ? parseInt(m[1], 10) - 1 : lineNum;
            html += `<div class="diff-hunk-header">${escHtml(line)}</div>`;
            continue;
        }
        if (line.startsWith('+') && !line.startsWith('+++')) {
            lineNum++;
            html += `<div class="diff-line diff-line-add"><span class="diff-gutter">${lineNum}</span><span class="diff-content">${escHtml(line)}</span></div>`;
        } else if (line.startsWith('-') && !line.startsWith('---')) {
            html += `<div class="diff-line diff-line-del"><span class="diff-gutter">-</span><span class="diff-content">${escHtml(line)}</span></div>`;
        } else if (line.startsWith(' ') || line === '') {
            lineNum++;
            html += `<div class="diff-line diff-line-ctx"><span class="diff-gutter">${lineNum}</span><span class="diff-content">${escHtml(line)}</span></div>`;
        }
    }
    return html;
}

// ============================================
// DOM Initialization
// ============================================

let autoRefreshInterval = null;

// Arch gen jobs for the current project (pending/running) — used to render ghost cards
let _archGenJobs = [];
// Last known scheduler state — used to render per-card job indicators
let _schedulerState = { active: [], queued: [] };

document.addEventListener('DOMContentLoaded', async function() {
    // Load projects first so the sidebar is populated before tasks load
    await loadProjects();
    await Promise.all([loadTasksFromDatabase(), loadLlmsAndBudgets()]);

    // Fetch transition statuses for idea tasks before first render
    await loadTransitionStatuses();

    initializeProjectTabs();   // wires the "+ New Project" button only
    initializeTaskCards();
    initializeModals();
    initializeGlobalConfigButtons();

    // Render tasks from database after loading
    renderTasksFromDatabase();

    // Start auto-refresh every 5 seconds
    startAutoRefresh();

    // Load inbox and start badge polling
    await loadInbox();
    _inboxPollInterval = setInterval(refreshInboxBadge, 60_000);
});

// Start automatic polling for database changes
function startAutoRefresh() {
    autoRefreshInterval = setInterval(async () => {
        if (!currentProject) return;
        try {
            const response = await fetch(`${API_BASE}/projects/${encodeURIComponent(currentProject)}/tasks`);
            if (response.ok) {
                reconcile(await response.json());
            }
        } catch (error) {
            console.error('Auto-refresh error:', error);
        }
        // Refresh arch gen ghost cards (fire-and-forget)
        loadArchGenJobs().catch(() => {});

        // Update queue button label + card job indicators (fire-and-forget)
        if (!_schedulerModalPoller) {
            fetch(`${API_BASE}/scheduler/status`).then(r => r.ok ? r.json() : null).then(data => {
                if (!data) return;
                const queueBtn = document.getElementById('scheduler-queue-btn');
                if (queueBtn) {
                    const total = (data.active || []).length + (data.queued || []).length;
                    queueBtn.textContent = total > 0 ? `⚙ Queue (${total})` : '⚙ Queue';
                }
                _refreshJobIndicators(data);
            }).catch(() => {});
        }
    }, 5000);
}

// Fetch pending/running arch gen jobs for the current project and re-render ghost cards.
async function loadArchGenJobs() {
    if (!currentProject) return;
    try {
        const r = await fetch(`${API_BASE}/projects/${encodeURIComponent(currentProject)}/arch-gen-jobs`);
        if (r.ok) {
            const data = await r.json();
            // Handle both legacy (array) and new (object with .jobs) formats
            _archGenJobs = data.jobs || data;
            renderArchBar();
        }
    } catch (err) {
        console.error("Failed to load arch gen jobs:", err);
    }
}

// Update the per-card job indicator strips based on the latest scheduler state.
function _refreshJobIndicators(schedulerData) {
    _schedulerState = {
        active: schedulerData.active || [],
        queued: schedulerData.queued || [],
    };

    // Build fast lookup maps: taskId → entry
    const activeMap = {};
    (_schedulerState.active || []).forEach(item => { activeMap[item.id] = item; });
    const queuedMap = {};
    (_schedulerState.queued || []).forEach(item => { queuedMap[item.id] = item; });

    // Walk all cached cards and update their indicator element.
    Object.keys(cardCache).forEach(taskId => {
        const el = document.getElementById(`ji-${taskId}`);
        if (!el) return;

        let html = '';
        if (activeMap[taskId]) {
            const llm = activeMap[taskId].llm_name || '';
            html = `<span class="ji-dot"></span><span class="ji-label">Running${llm ? ' \u00b7 ' + escapeHtml(llm) : ''}</span>`;
            el.className = 'card-job-indicator ji-running';
        } else if (queuedMap[taskId]) {
            const reason = queuedMap[taskId].reason || 'pending';
            html = `<span class="ji-dot"></span><span class="ji-label">Queued \u00b7 ${escapeHtml(reason)}</span>`;
            el.className = 'card-job-indicator ji-queued';
        } else {
            el.className = 'card-job-indicator';
        }
        el.innerHTML = html;
    });
}

// Switch to a different project: update state, fetch its tasks, and re-render
async function switchProject(projectName) {
    currentProject = projectName;

    document.querySelectorAll('.project-tab').forEach(t => t.classList.remove('active'));
    const matchingTab = document.querySelector(`.project-tab[data-project="${projectName}"]`);
    if (matchingTab) matchingTab.classList.add('active');

    document.getElementById('current-project-display').textContent = `Selected: ${projectName}`;
    document.querySelector('.board-title').textContent = projectName;

    console.log(`Project switched to: ${projectName}`);

    // Clear transition cache and pollers for previous project
    transitionCache = {};
    Object.values(transitionPollers).forEach(id => clearInterval(id));
    transitionPollers = {};

    _archGenJobs = [];
    _schedulerState = { active: [], queued: [] };
    await Promise.all([loadTasksFromDatabase(), loadArchGenJobs()]);
    await loadTransitionStatuses();
    renderTasksFromDatabase();

    // If the column map is open, re-render it for the new project's tasks.
    // Update the title so it reflects the new project context.
    if (columnMapActive && columnMapType) {
        const label = MAP_COLUMN_LABELS[columnMapType] || (columnMapType.toUpperCase() + ' MAP');
        document.getElementById('column-map-title').textContent = label;
        mapTransform = { x: 0, y: 0, scale: 1 };
        renderColumnMap(columnMapType);
    }
}

// Load projects from the API and render the sidebar tabs
async function loadProjects() {
    try {
        const resp = await fetch(`${API_BASE}/projects`);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const projects = await resp.json();
        allProjects = projects;

        const container = document.getElementById('project-tabs-container');
        container.innerHTML = '';

        projects.forEach(p => {
            container.appendChild(_buildProjectTab(p.name, p.path, p.description, p.llm_id, p.budget_id));
        });

        // If no project is selected yet, pick the first one
        if (!currentProject && projects.length > 0) {
            switchProject(projects[0].name);
        } else if (currentProject) {
            // Re-apply active class
            const active = container.querySelector(`[data-project="${CSS.escape(currentProject)}"]`);
            if (active) active.classList.add('active');
        }
    } catch (err) {
        console.error('Failed to load projects:', err);
    }
}

function _buildProjectTab(name, path, description, llmId, budgetId) {
    const tab = document.createElement('div');
    tab.className = 'project-tab';
    tab.setAttribute('data-project', name);

    const label = document.createElement('span');
    label.className = 'project-tab-label';
    label.textContent = `📁 ${name}`;
    label.title = path ? `Path: ${path}` : 'No path configured';
    label.addEventListener('click', () => switchProject(name));

    const gear = document.createElement('button');
    gear.className = 'project-tab-gear';
    gear.textContent = '⚙';
    gear.title = 'Edit project settings';
    gear.addEventListener('click', (e) => {
        e.stopPropagation();
        openEditProjectModal(name, path || '', description || '', llmId || null, budgetId || null);
    });

    tab.appendChild(label);
    tab.appendChild(gear);
    return tab;
}

// Initialize project tab selection
function initializeProjectTabs() {
    // Add project button
    document.getElementById('add-project').addEventListener('click', function() {
        openNewProjectModal();
    });
}

// Initialize task cards with click handlers
function initializeTaskCards() {
    document.querySelectorAll('.task-card').forEach(card => {
        card.addEventListener('click', function(e) {
            if (e.target.tagName === 'BUTTON' || e.target.classList.contains('action-btn')) {
                return;
            }
            const taskId = this.getAttribute('data-id');
            const task = taskData[taskId];

            if (task && task.immutable) {
                console.log(`Architecture task clicked (immutable): ${taskId}`);
            } else if (task) {
                console.log(`Task clicked: ${taskId}, Status: ${task.type}`);
            }
        });
    });
}

// Initialize modal close behavior
function initializeModals() {
    document.getElementById('task-modal').addEventListener('click', function(e) {
        if (e.target === this && _modalMousedownTarget === this) closeModal();
    });

    document.getElementById('history-modal').addEventListener('click', function(e) {
        if (e.target === this && _modalMousedownTarget === this) closeHistoryModal();
    });

    document.getElementById('new-project-modal').addEventListener('click', function(e) {
        if (e.target === this && _modalMousedownTarget === this) closeNewProjectModal();
    });

    document.getElementById('new-project-name').addEventListener('keydown', function(e) {
        if (e.key === 'Enter') saveNewProject();
        if (e.key === 'Escape') closeNewProjectModal();
    });

    document.getElementById('edit-project-modal').addEventListener('click', function(e) {
        if (e.target === this && _modalMousedownTarget === this) closeEditProjectModal();
    });

    document.getElementById('transition-modal').addEventListener('click', function(e) {
        if (e.target === this && _modalMousedownTarget === this) closeTransitionModal();
    });

    document.getElementById('scheduler-modal').addEventListener('click', function(e) {
        if (e.target === this && _modalMousedownTarget === this) closeSchedulerModal();
    });

    document.getElementById('stage-journal-modal').addEventListener('click', function(e) {
        if (e.target === this && _modalMousedownTarget === this) closeStageJournal();
    });

    document.getElementById('inbox-detail-modal').addEventListener('click', function(e) {
        if (e.target === this && _modalMousedownTarget === this) closeInboxDetailModal();
    });
}

// ============================================
// Modal Functions
// ============================================

function openAddTaskModal(targetStatus) {
    currentTaskId = null;
    currentTargetStatus = targetStatus;

    document.getElementById('modal-title').textContent = `Add Task: ${targetStatus.toUpperCase()}`;
    document.getElementById('task-title').value = '';
    document.getElementById('task-description').value = '';
    document.getElementById('task-tags').value = '';
    document.getElementById('task-owner').value = 'user';
    showArchContentFields(targetStatus);

    // Default LLM to the current project's configured LLM; fall back to first available.
    const currentProjectData = allProjects.find(p => p.name === currentProject);
    const defaultLlmId = (currentProjectData && currentProjectData.llm_id)
        || (allLlms.length > 0 ? allLlms[0].id : null);
    const defaultBudgetId = allBudgets.length > 0 ? allBudgets[0].id : null;
    populateLlmSelect(defaultLlmId);
    populateBudgetSelect(defaultBudgetId);
    // Also refresh the new-project LLM dropdown in case it was opened before allLlms loaded.
    populateProjectLlmSelect('new-project-llm-select', currentProjectData ? currentProjectData.llm_id : null);

    document.getElementById('task-modal').classList.add('active');
}

function showArchContentFields(targetStatus) {
    const isArch = targetStatus === 'architecture';
    // Show category+priority selects only for architecture cards
    document.getElementById('modal-content-fields').style.display = isArch ? 'block' : 'none';
    // Architecture cards don't need LLM / budget assignment
    document.getElementById('task-llm-group').style.display    = isArch ? 'none' : 'block';
    document.getElementById('task-budget-group').style.display = isArch ? 'none' : 'block';
    // Owner / tags are also not meaningful for arch cards — hide them
    const ownerRow = document.getElementById('task-owner') && document.getElementById('task-owner').closest('.form-group');
    const tagsRow  = document.getElementById('task-tags')  && document.getElementById('task-tags').closest('.form-group');
    if (ownerRow) ownerRow.style.display = isArch ? 'none' : 'block';
    if (tagsRow)  tagsRow.style.display  = isArch ? 'none' : 'block';
    // Relabel description for architecture cards
    const descLabel = document.querySelector('label[for="task-description"]');
    if (descLabel) descLabel.textContent = isArch ? 'Body (the constraint or fact)' : 'Description';
}

function closeModal() {
    document.getElementById('task-modal').classList.remove('active');
    currentTaskId = null;
    currentTargetStatus = null;
    // Restore modal to editable state if it was opened read-only
    _restoreModalEditable();
}

function _restoreModalEditable() {
    const modal = document.getElementById('task-modal');
    if (!modal.dataset.readonly) return;
    delete modal.dataset.readonly;
    modal.querySelectorAll('input, textarea, select').forEach(el => {
        el.removeAttribute('readonly');
        el.removeAttribute('disabled');
    });
    const footer = modal.querySelector('.modal-footer');
    footer.innerHTML = '<button class="btn btn-secondary" onclick="closeModal()">Cancel</button>' +
                       '<button class="btn btn-primary" onclick="saveTask()">Save</button>';
}

function viewTask(taskId) {
    const task = taskData[taskId];
    if (!task) return;
    // Populate via the edit path then lock
    editTask(taskId);
    document.getElementById('modal-title').textContent = `View: ${task.title}`;
    const modal = document.getElementById('task-modal');
    modal.dataset.readonly = '1';
    modal.querySelectorAll('input, textarea').forEach(el => el.setAttribute('readonly', ''));
    modal.querySelectorAll('select').forEach(el => el.setAttribute('disabled', ''));
    const footer = modal.querySelector('.modal-footer');
    footer.innerHTML = '<button class="btn btn-primary" onclick="closeModal()">Done</button>';
}

function openNewProjectModal() {
    document.getElementById('new-project-name').value = '';
    document.getElementById('new-project-path').value = '';
    document.getElementById('new-project-description').value = '';
    document.getElementById('new-project-error').style.display = 'none';
    populateProjectLlmSelect('new-project-llm-select', null);
    populateProjectBudgetSelect('new-project-budget-select', null);
    document.getElementById('new-project-modal').classList.add('active');
    document.getElementById('new-project-name').focus();
}

function closeNewProjectModal() {
    document.getElementById('new-project-modal').classList.remove('active');
}

async function saveNewProject() {
    const name = document.getElementById('new-project-name').value.trim();
    const path = document.getElementById('new-project-path').value.trim();
    const description = document.getElementById('new-project-description').value.trim();
    const llmVal = document.getElementById('new-project-llm-select').value;
    const llm_id = llmVal ? parseInt(llmVal, 10) : null;
    const budgetVal = document.getElementById('new-project-budget-select').value;
    const budget_id = budgetVal ? parseInt(budgetVal, 10) : null;
    const errEl = document.getElementById('new-project-error');

    if (!name) {
        errEl.textContent = 'Project name is required.';
        errEl.style.display = 'block';
        document.getElementById('new-project-name').focus();
        return;
    }

    try {
        const resp = await fetch(`${API_BASE}/projects`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name, path, description, llm_id, budget_id }),
        });
        if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            errEl.textContent = err.detail || `Error ${resp.status}`;
            errEl.style.display = 'block';
            return;
        }
        closeNewProjectModal();
        await loadProjects();
        switchProject(name);
    } catch (err) {
        errEl.textContent = `Network error: ${err.message}`;
        errEl.style.display = 'block';
    }
}

function openEditProjectModal(name, path, description, llmId, budgetId) {
    document.getElementById('edit-project-original-name').value = name;
    document.getElementById('edit-project-modal-title').textContent = `Edit: ${name}`;
    document.getElementById('edit-project-name-display').textContent = name;
    document.getElementById('edit-project-path').value = path;
    document.getElementById('edit-project-description').value = description;
    document.getElementById('edit-project-error').style.display = 'none';
    populateProjectLlmSelect('edit-project-llm-select', llmId || null);
    populateProjectBudgetSelect('edit-project-budget-select', budgetId || null);
    document.getElementById('edit-project-modal').classList.add('active');
    document.getElementById('edit-project-path').focus();
}

function closeEditProjectModal() {
    document.getElementById('edit-project-modal').classList.remove('active');
}

async function saveEditProject() {
    const name = document.getElementById('edit-project-original-name').value;
    const path = document.getElementById('edit-project-path').value.trim();
    const description = document.getElementById('edit-project-description').value.trim();
    const llmVal = document.getElementById('edit-project-llm-select').value;
    const llm_id = llmVal ? parseInt(llmVal, 10) : null;
    const budgetVal = document.getElementById('edit-project-budget-select').value;
    const budget_id = budgetVal ? parseInt(budgetVal, 10) : null;
    const errEl = document.getElementById('edit-project-error');

    try {
        const resp = await fetch(`${API_BASE}/projects/${encodeURIComponent(name)}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path, description, llm_id, budget_id }),
        });
        if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            errEl.textContent = err.detail || `Error ${resp.status}`;
            errEl.style.display = 'block';
            return;
        }
        closeEditProjectModal();
        await loadProjects();
    } catch (err) {
        errEl.textContent = `Network error: ${err.message}`;
        errEl.style.display = 'block';
    }
}

async function deleteProjectFromModal() {
    const name = document.getElementById('edit-project-original-name').value;
    if (!await showConfirm('Delete Project', `Delete project "${name}"? This does not delete its tasks.`, 'Delete')) return;

    const errEl = document.getElementById('edit-project-error');
    try {
        const resp = await fetch(`${API_BASE}/projects/${encodeURIComponent(name)}`, { method: 'DELETE' });
        if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            errEl.textContent = err.detail || `Error ${resp.status}`;
            errEl.style.display = 'block';
            return;
        }
        closeEditProjectModal();
        await loadProjects();
        // If the deleted project was active, switch to first available
        if (currentProject === name) {
            const first = document.querySelector('#project-tabs-container .project-tab');
            if (first) switchProject(first.getAttribute('data-project'));
        }
    } catch (err) {
        errEl.textContent = `Network error: ${err.message}`;
        errEl.style.display = 'block';
    }
}

async function saveTask() {
    const title = document.getElementById('task-title').value.trim();
    const description = document.getElementById('task-description').value.trim();
    const tagsInput = document.getElementById('task-tags').value.trim();
    const owner = document.getElementById('task-owner').value.trim() || 'user';

    if (!title) {
        showToast('Task title is required.', 'warning');
        return;
    }

    const tags = tagsInput.split(',').map(t => t.trim()).filter(t => t);

    // Build content object for architecture tasks
    const content = currentTargetStatus === 'architecture' ? {
        category: document.getElementById('arch-category').value || 'General',
        priority: document.getElementById('arch-priority').value || 'normal',
    } : null;

    const isArch = currentTargetStatus === 'architecture';
    const llmVal = !isArch ? document.getElementById('task-llm-select').value : '';
    const budgetVal = !isArch ? document.getElementById('task-budget-select').value : '';
    const llm_id = llmVal ? parseInt(llmVal) : null;
    const budget_id = budgetVal ? parseInt(budgetVal) : null;

    if (currentTaskId) {
        // Update existing task via PUT request
        const taskDataPayload = {
            title,
            description,
            ...(isArch ? {} : { owner, tags, llm_id, budget_id }),
            ...(content && { content })
        };

        const response = await fetch(`${API_BASE}/tasks/${currentTaskId}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(taskDataPayload)
        });

        if (!response.ok) {
            showToast('Failed to update task', 'error');
            return;
        }

        const updatedTask = await response.json();
        taskData[currentTaskId] = updatedTask;
        console.log(`Task updated: ${currentTaskId}`);
    } else if (currentTargetStatus) {
        if (!canAddTaskToColumn(currentTargetStatus)) {
            return;
        }

        const newTaskData = {
            title,
            type: currentTargetStatus,
            description,
            ...(isArch ? {} : { owner, tags, llm_id, budget_id }),
            project: currentProject,
            ...(content && { content })
        };

        const response = await fetch(`${API_BASE}/tasks`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(newTaskData)
        });

        if (!response.ok) {
            showToast('Failed to create task', 'error');
            return;
        }

        const newTask = await response.json();
        taskData[newTask.id] = newTask;
        allTasks.push(newTask);
        console.log(`New task created: ${newTask.id}`);
    }

    closeModal();
    if (isArch) {
        renderArchBar();
    } else {
        renderTasksFromDatabase();
    }
}

function canAddTaskToColumn(status) {
    if (!status || status === 'architecture') return true;
    const check = checkWipLimit(status);
    if (!check.allowed) {
        showToast(`WIP limit reached — column ${status.toUpperCase()} is at ${check.current}/${check.limit} tasks.`, 'warning');
        return false;
    }
    return true;
}

function checkWipLimit(status) {
    const container = document.getElementById(`tasks-${status}`);
    if (container) {
        const currentCount = container.querySelectorAll('.task-card').length;
        const limit = WIP_LIMITS[status];
        return { allowed: currentCount < limit, current: currentCount, limit: limit };
    }
    return { allowed: true, current: 0, limit: WIP_LIMITS[status] || 10 };
}

// ============================================
// Task Card Creation
// ============================================

function canTaskAdvance(id) {
    const task = taskData[id];
    if (!task) return false;
    return !!(task.description && task.llm_id && task.budget_id);
}

function scrollToTask(taskId) {
    const card = document.querySelector(`.task-card[data-id="${taskId}"]`);
    if (card) {
        card.scrollIntoView({ behavior: 'smooth', block: 'center' });
        card.style.outline = '2px solid #0d6efd';
        setTimeout(() => { card.style.outline = ''; }, 2000);
    }
}

// ============================================
// Big Idea Zoom View
// ============================================

function zoomIntoBigIdea(taskId) {
    const task = taskData[taskId];
    if (!task) return;
    if (currentBigIdeaFilter === taskId) return;  // already filtered to this — clicking again is a no-op

    breadcrumbStack.push({ id: taskId, title: task.title });
    currentBigIdeaFilter = taskId;
    renderTasksFromDatabase();
}

function zoomToRoot() {
    currentBigIdeaFilter = null;
    breadcrumbStack = [];
    renderTasksFromDatabase();
}

function zoomToBreadcrumb(index) {
    // Zoom to a specific level in the breadcrumb stack
    if (index < 0) {
        zoomToRoot();
        return;
    }
    breadcrumbStack = breadcrumbStack.slice(0, index + 1);
    currentBigIdeaFilter = breadcrumbStack[breadcrumbStack.length - 1].id;
    renderTasksFromDatabase();
}

function updateBreadcrumbBar(hiddenCount = 0) {
    const bar = document.getElementById('breadcrumb-bar');
    const trail = document.getElementById('breadcrumb-trail');
    const countEl = document.getElementById('breadcrumb-filter-count');
    if (!bar || !trail) return;

    if (!currentBigIdeaFilter) {
        bar.style.display = 'none';
        return;
    }

    bar.style.display = 'flex';
    let html = '';
    breadcrumbStack.forEach((crumb, i) => {
        html += '<span class="breadcrumb-separator">&gt;</span>';
        if (i < breadcrumbStack.length - 1) {
            html += `<span class="breadcrumb-segment" onclick="zoomToBreadcrumb(${i})" style="cursor:pointer">${crumb.title}</span>`;
        } else {
            html += `<span class="breadcrumb-current">${crumb.title}</span>`;
        }
    });
    trail.innerHTML = html;

    if (countEl) {
        if (hiddenCount > 0) {
            countEl.textContent = `\u2014 ${hiddenCount} card${hiddenCount === 1 ? '' : 's'} hidden by this filter`;
            countEl.style.display = 'inline';
        } else {
            countEl.style.display = 'none';
        }
    }
}

function buildPrereqLabels(taskId) {
    const task = taskData[taskId];
    if (!task) return '';

    const prereqs = task.prerequisites || [];
    let parts = [];

    // "Blocked by" labels
    if (prereqs.length > 0) {
        const blockers = prereqs.map(pid => {
            const prereqTask = taskData[pid];
            if (!prereqTask) return null;
            const done = ['completed', 'accepted'].includes((prereqTask.type || '').toLowerCase());
            const cls = done ? 'prereq-met' : 'prereq-unmet';
            const symbol = done ? '\u2713' : '\u2717';
            return `<span class="${cls}">${symbol} ${prereqTask.title || pid}</span>`;
        }).filter(Boolean);
        if (blockers.length > 0) {
            parts.push(`<div class="prereq-label">Blocked by: ${blockers.join(', ')}</div>`);
        }
    }

    // "Blocks" labels — find tasks that have this task as a prerequisite
    const blocksIds = [];
    const descendants = currentBigIdeaFilter ? (descendantIndex[currentBigIdeaFilter] || []) : [];
    const scopeIds = currentBigIdeaFilter ? [currentBigIdeaFilter, ...descendants] : Object.keys(taskData);
    for (const otherId of scopeIds) {
        const other = taskData[otherId];
        if (other && (other.prerequisites || []).includes(taskId)) {
            blocksIds.push(other.title || otherId);
        }
    }
    if (blocksIds.length > 0) {
        parts.push(`<div class="prereq-label prereq-blocks">Blocks: ${blocksIds.join(', ')}</div>`);
    }

    return parts.join('');
}

function buildContractPills(contracts) {
    if (!contracts) return '';
    const provides = contracts.provides || [];
    const consumes = contracts.consumes || [];
    let html = '<div style="margin-top:0.35rem;display:flex;flex-wrap:wrap;gap:0.25rem">';
    provides.forEach(p => {
        const name = typeof p === 'string' ? p : (p.name || '?');
        html += `<span style="font-size:0.6rem;padding:0.1rem 0.35rem;border-radius:3px;background:#d1e7dd;color:#0f5132">provides: ${name}</span>`;
    });
    consumes.forEach(c => {
        const name = typeof c === 'string' ? c : (c.name || '?');
        html += `<span style="font-size:0.6rem;padding:0.1rem 0.35rem;border-radius:3px;background:#cff4fc;color:#055160">consumes: ${name}</span>`;
    });
    html += '</div>';
    return (provides.length + consumes.length) > 0 ? html : '';
}

async function viewChildren(taskId) {
    try {
        const [childResp, recResp] = await Promise.all([
            fetch(`${API_BASE}/tasks/${taskId}/children`),
            fetch(`${API_BASE}/tasks/${taskId}/subdivision-records`),
        ]);
        if (!childResp.ok) return;

        const children = await childResp.json();
        const records  = recResp.ok ? await recResp.json() : [];

        // childMap: id -> child object (includes cancelled children from all batches)
        const childMap = {};
        children.forEach(c => { childMap[c.id] = c; });

        // Default to the active record; fall back to most-recent (index 0)
        const activeIdx = records.findIndex(r => r.status === 'active');
        const startIdx  = activeIdx >= 0 ? activeIdx : 0;

        _viewChildrenState = { taskId, records, childMap, idx: startIdx };

        document.getElementById('transition-modal-title').textContent = 'Subdivision Details';
        document.getElementById('transition-modal').classList.add('active');
        _renderChildrenView();
    } catch (err) {
        console.error('Error viewing children:', err);
    }
}

function _renderChildrenView() {
    if (!_viewChildrenState) return;
    const { taskId, records, childMap, idx } = _viewChildrenState;
    const task = taskData[taskId] || {};

    let html = `<h3 style="margin-bottom:1rem">Children of: ${task.title || taskId}</h3>`;

    if (records.length === 0) {
        const all = Object.values(childMap);
        html += all.length === 0
            ? '<p style="color:#6c757d">No children found.</p>'
            : all.map(_childCard).join('');
    } else {
        const rec = records[idx];
        if (rec.status === 'generating') {
            html += `<div style="text-align:center;padding:2.5rem 1rem;color:#6c757d">
                <div style="font-size:2rem;margin-bottom:0.75rem">⏳</div>
                <strong style="font-size:1rem">Generating new subdivision set…</strong>
                <div style="margin-top:0.5rem;font-size:0.85rem">The LLM is working. This panel updates automatically.</div>
            </div>`;
        } else {
            const recChildren = (rec.child_task_ids || []).map(id => childMap[id]).filter(Boolean);
            html += recChildren.length === 0
                ? '<p style="color:#6c757d">No children in this set.</p>'
                : recChildren.map(_childCard).join('');
        }
    }

    document.getElementById('transition-modal-body').innerHTML = html;
    _renderChildrenFooter();
}

function _childCard(c) {
    const statusColor  = c.type === 'cancelled' ? '#6c757d' :
                         c.type === 'completed'  ? '#198754' :
                         c.type === 'planning'   ? '#ffc107' : '#0d6efd';
    const dimStyle = c.type === 'cancelled' ? 'opacity:0.5;' : '';
    return `
        <div style="border:1px solid #dee2e6;border-radius:6px;padding:0.75rem;margin-bottom:0.5rem;border-left:4px solid ${statusColor};${dimStyle}">
            <strong>${c.title}</strong>
            <span style="float:right;font-size:0.75rem;text-transform:uppercase;color:${statusColor};font-weight:600">${c.type}</span>
            <div style="font-size:0.85rem;color:#6c757d;margin-top:0.25rem">${c.description || ''}</div>
            ${c.subdivision_generation > 0 ? `<span class="subdivision-badge gen" style="margin-top:0.35rem;display:inline-block">Gen ${c.subdivision_generation}</span>` : ''}
            ${c.is_big_idea ? '<span class="big-idea-badge" style="margin-left:0.35rem">Big Idea</span>' : ''}
            ${c.interface_contracts ? buildContractPills(c.interface_contracts) : ''}
        </div>`;
}

function _fmtTok(n) {
    if (n >= 1048576) return (n / 1048576).toFixed(1) + 'M';
    if (n >= 1024)    return Math.round(n / 1024) + 'K';
    return String(n || 0);
}

function _renderChildrenFooter() {
    if (!_viewChildrenState) return;
    const { taskId, records, idx } = _viewChildrenState;
    const footerLeft = document.getElementById('transition-modal-footer-left');
    if (!footerLeft) return;

    const btnStyle = 'font-size:0.78rem;padding:0.2rem 0.5rem;line-height:1.4';
    let html = '';

    if (records.length > 0) {
        const rec      = records[idx];
        const n        = records.length;
        const pp       = rec.prompt_tokens || 0;
        const tg       = rec.completion_tokens || 0;
        const isGenerating = rec.status === 'generating';
        const isActive     = rec.status === 'active';
        const statusCol    = isGenerating ? '#fd7e14' : isActive ? '#198754' : '#6c757d';

        // ← older  N of M  newer →  ·  status  ·  tokens
        const olderDis = idx >= n - 1 ? 'disabled' : '';
        const newerDis = idx <= 0     ? 'disabled' : '';

        html += `
          <button class="btn btn-secondary" style="${btnStyle}" onclick="prevChildRecord()" ${olderDis}>&#8592;</button>
          <span style="font-size:0.8rem;color:#adb5bd;white-space:nowrap">
            Set ${idx + 1}&thinsp;/&thinsp;${n}
            &nbsp;&middot;&nbsp;
            <span style="color:${statusCol};font-weight:600">${isGenerating ? 'generating\u2026' : rec.status}</span>
            ${!isGenerating ? `&nbsp;&middot;&nbsp;${_fmtTok(pp)}&thinsp;pp&thinsp;/&thinsp;${_fmtTok(tg)}&thinsp;tg` : ''}
          </span>
          <button class="btn btn-secondary" style="${btnStyle}" onclick="nextChildRecord()" ${newerDis}>&#8594;</button>`;

        if (!isActive && !isGenerating) {
            html += `<button class="btn btn-primary" style="${btnStyle};margin-left:0.4rem"
                             onclick="activateSubdivisionRecord('${taskId}', ${rec.id})">Activate this set</button>`;
        }
    }

    // Regenerate — disabled only while a regeneration is actively in progress (status='generating').
    // An 'active' record means subdivision completed successfully — Regenerate should be available then.
    const busy  = records.some(r => r.status === 'generating');
    html += `<button class="btn btn-warning" style="${btnStyle};margin-left:${records.length > 0 ? '0.75rem' : '0'}"
                     onclick="regenerateSubdivision('${taskId}')"
                     ${busy ? 'disabled title="Already regenerating"' : ''}>&#x21BA; Regenerate</button>`;

    footerLeft.innerHTML = html;
}

function prevChildRecord() {
    if (!_viewChildrenState) return;
    const max = _viewChildrenState.records.length - 1;
    if (_viewChildrenState.idx < max) {
        _viewChildrenState.idx++;
        _renderChildrenView();
    }
}

function nextChildRecord() {
    if (!_viewChildrenState) return;
    if (_viewChildrenState.idx > 0) {
        _viewChildrenState.idx--;
        _renderChildrenView();
    }
}

async function activateSubdivisionRecord(taskId, recordId) {
    try {
        const resp = await fetch(`${API_BASE}/tasks/${taskId}/subdivision-records/${recordId}/activate`, { method: 'POST' });
        if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            showToast('Failed to activate: ' + (err.detail || resp.statusText), 'error');
            return;
        }
        await viewChildren(taskId);   // refresh modal with updated statuses
    } catch (err) {
        console.error('Error activating record:', err);
    }
}

async function regenerateSubdivision(taskId) {
    try {
        const resp = await fetch(`${API_BASE}/tasks/${taskId}/regenerate-subdivision`, { method: 'POST' });
        if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            showToast('Failed to regenerate: ' + (err.detail || resp.statusText), 'error');
            return;
        }
        // Keep the modal open. Inject a synthetic "generating" placeholder as the newest
        // set (index 0) so the user sees "2/2 · generating…" immediately instead of the
        // stale "1/1 · not active" state while the LLM runs.
        if (_viewChildrenState && _viewChildrenState.taskId === taskId) {
            const synth = { id: null, status: 'generating', child_task_ids: [], prompt_tokens: 0, completion_tokens: 0 };
            _viewChildrenState.records.unshift(synth);
            _viewChildrenState.idx = 0;
            _renderChildrenView();
        }
        // Poll every 4 s until the real active record appears in the DB.
        _startChildrenPoller(taskId);
    } catch (err) {
        console.error('Error regenerating:', err);
    }
}

function _startChildrenPoller(taskId) {
    _stopChildrenPoller();
    _childrenPollerTimer = setInterval(async () => {
        if (!_viewChildrenState || _viewChildrenState.taskId !== taskId) {
            _stopChildrenPoller();
            return;
        }
        try {
            const [childResp, recResp] = await Promise.all([
                fetch(`${API_BASE}/tasks/${taskId}/children`),
                fetch(`${API_BASE}/tasks/${taskId}/subdivision-records`),
            ]);
            if (!childResp.ok || !recResp.ok) return;
            const children = await childResp.json();
            const records  = await recResp.json();
            const activeIdx = records.findIndex(r => r.status === 'active');
            if (activeIdx >= 0) {
                _stopChildrenPoller();
                const childMap = {};
                children.forEach(c => { childMap[c.id] = c; });
                _viewChildrenState = { taskId, records, childMap, idx: activeIdx };
                _renderChildrenView();
            }
        } catch (e) { /* keep polling */ }
    }, 4000);
}

function _stopChildrenPoller() {
    if (_childrenPollerTimer) {
        clearInterval(_childrenPollerTimer);
        _childrenPollerTimer = null;
    }
}

function _renderResearchJobCard(j) {
    const statusColor = j.status === 'completed' ? '#198754' :
                        j.status === 'failed'    ? '#dc3545' :
                        j.status === 'cancelled' ? '#fd7e14' : '#6c757d';
    const findings = j.findings
        ? (j.findings.length > 300 ? escHtml(j.findings.slice(0, 300)) + '…' : escHtml(j.findings))
        : '<em style="color:#6c757d">No findings yet.</em>';
    return `<div class="sj-rj-card" style="border-left-color:${statusColor}">
        <div class="sj-rj-header">
            <span class="sj-rj-status" style="color:${statusColor}">${escHtml(j.status)}</span>
            <span class="transition-timestamp">${j.created_at || ''}</span>
        </div>
        <div class="sj-rj-question">${escHtml(j.question || '')}</div>
        <div class="sj-rj-findings">${findings}</div>
        <div class="sj-rj-meta">
            Lives used: ${j.lives_used ?? '—'} &nbsp;|&nbsp;
            Tokens: ${j.prompt_tokens ?? 0} prompt / ${j.completion_tokens ?? 0} completion
            ${j.completed_at ? `&nbsp;|&nbsp; Completed: ${escHtml(j.completed_at)}` : ''}
        </div>
    </div>`;
}

async function viewResearchJobs(taskId) {
    try {
        const resp = await fetch(`${API_BASE}/tasks/${taskId}/research-jobs`);
        if (!resp.ok) return;
        const jobs = await resp.json();

        const task = taskData[taskId] || {};
        const title = task.title || taskId;

        let html = `<h3 style="margin-bottom:1rem">Research Jobs: ${escHtml(title)}</h3>`;
        if (jobs.length === 0) {
            html += '<p style="color:#6c757d">No research jobs for this task.</p>';
        } else {
            jobs.forEach(j => { html += _renderResearchJobCard(j); });
        }

        document.getElementById('transition-modal-title').textContent = 'Research Jobs';
        document.getElementById('transition-modal-body').innerHTML = html;
        document.getElementById('transition-modal').classList.add('active');
    } catch (err) {
        console.error('Error viewing research jobs:', err);
    }
}

async function viewBenchmarks(taskId) {
    try {
        const resp = await fetch(`${API_BASE}/tasks/${taskId}/benchmarks`);
        if (!resp.ok) return;
        const records = await resp.json();

        const task = taskData[taskId] || {};
        const title = task.title || taskId;

        let html = `<h3 style="margin-bottom:1rem">Benchmarks: ${title}</h3>`;
        if (records.length === 0) {
            html += '<p style="color:#6c757d">No benchmark records for this task.</p>';
        } else {
            const byTask = {};
            records.forEach(r => {
                if (!byTask[r.task_id]) byTask[r.task_id] = {};
                byTask[r.task_id][r.benchmark_type] = r;
            });

            Object.entries(byTask).forEach(([subTaskId, pair]) => {
                const before = pair['before'];
                const after  = pair['after'];
                const bm = before ? JSON.parse(before.metrics || '{}') : {};
                const am = after  ? JSON.parse(after.metrics  || '{}') : {};

                const durDelta  = (bm.test_duration_ms != null && am.test_duration_ms != null)
                    ? `${bm.test_duration_ms}ms → ${am.test_duration_ms}ms`
                    : (bm.test_duration_ms != null ? `${bm.test_duration_ms}ms → ?` : '—');
                const memDelta  = (bm.memory_peak_mb != null && am.memory_peak_mb != null)
                    ? `${bm.memory_peak_mb}MB → ${am.memory_peak_mb}MB`
                    : '—';
                const bigODelta = (bm.big_o_class && am.big_o_class)
                    ? `${bm.big_o_class} → ${am.big_o_class}`
                    : (bm.big_o_class || am.big_o_class || '—');
                const readCost  = am.readability_cost != null ? am.readability_cost : '—';
                const premature = am.is_premature ? ' ⚠ premature' : '';
                const debtBadge = am.tech_debt_resolved ? ' ✓ tech-debt' : '';
                const notes     = am.notes || bm.notes || '';
                const scaleN    = am.scale_n || bm.scale_n || '';

                html += `
                    <div style="border:1px solid #dee2e6;border-radius:6px;padding:0.75rem;margin-bottom:0.75rem;border-left:4px solid #6f42c1">
                        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:0.4rem">
                            <span style="font-size:0.75rem;font-weight:600;color:#6f42c1;text-transform:uppercase">Sub-task ${subTaskId}</span>
                            <span class="transition-timestamp">${(after || before || {}).created_at || ''}</span>
                        </div>
                        <div style="display:grid;grid-template-columns:1fr 1fr;gap:0.3rem 1rem;font-size:0.85rem;margin-bottom:0.4rem">
                            <div><span style="color:#6c757d">Duration:</span> ${durDelta}</div>
                            <div><span style="color:#6c757d">Memory:</span> ${memDelta}</div>
                            <div><span style="color:#6c757d">Big O:</span> ${bigODelta}</div>
                            <div><span style="color:#6c757d">Readability cost:</span> ${readCost}</div>
                            ${scaleN ? `<div><span style="color:#6c757d">Scale N:</span> ${scaleN}</div>` : ''}
                        </div>
                        ${(premature || debtBadge) ? `<div style="font-size:0.75rem;color:#6c757d;margin-bottom:0.25rem">${premature}${debtBadge}</div>` : ''}
                        ${notes ? `<div style="font-size:0.8rem;color:#495057;font-style:italic">${notes}</div>` : ''}
                        ${!after ? '<div style="font-size:0.75rem;color:#fd7e14">⚠ No after-record yet</div>' : ''}
                    </div>
                `;
            });
        }

        document.getElementById('transition-modal-title').textContent = 'Optimization Benchmarks';
        document.getElementById('transition-modal-body').innerHTML = html;
        document.getElementById('transition-modal').classList.add('active');
    } catch (err) {
        console.error('Error viewing benchmarks:', err);
    }
}

// ============================================
// Agent Toolbar Functions
// ============================================

function openResearchDialog(taskId) {
    _researchDialogTaskId = taskId;
    const task = taskData[taskId];
    const label = task ? `Task: ${task.title}` : `Task ID: ${taskId}`;
    document.getElementById('research-dialog-task-label').textContent = label;
    document.getElementById('research-dialog-question').value = '';
    document.getElementById('research-dialog-modal').classList.add('active');
    setTimeout(() => document.getElementById('research-dialog-question').focus(), 50);
}

function _pollInvestigationJob(taskId, jobId) {
    const timer = setInterval(async () => {
        try {
            const resp = await fetch(`${API_BASE}/agent/investigate/${taskId}/status?job_id=${jobId}`);
            if (!resp.ok) { clearInterval(timer); return; }
            const data = await resp.json();
            if (data.status === 'completed') {
                clearInterval(timer);
                const answer = data.report && data.report.answer ? ` — ${data.report.answer.slice(0, 80)}` : '';
                showToast(`Investigation #${jobId} complete${answer}… Check inbox for full report.`, 'success', 8000);
            } else if (data.status === 'failed') {
                clearInterval(timer);
                showToast(`Investigation #${jobId} failed: ${data.error || 'unknown error'}`, 'error');
            }
        } catch (_) { clearInterval(timer); }
    }, 3000);
}

function _pollResearchJob(taskId, jobId) {
    const timer = setInterval(async () => {
        try {
            const resp = await fetch(`${API_BASE}/agent/research/${taskId}/status?job_id=${jobId}`);
            if (!resp.ok) { clearInterval(timer); return; }
            const data = await resp.json();
            if (data.status === 'completed') {
                clearInterval(timer);
                const verdict = data.verdict ? ` [${data.verdict}]` : '';
                showToast(`Research #${jobId} complete${verdict} — open Research Jobs on the card for findings.`, 'success', 7000);
            } else if (data.status === 'failed') {
                clearInterval(timer);
                showToast(`Research #${jobId} failed: ${data.error || 'unknown error'}`, 'error');
            }
        } catch (_) { clearInterval(timer); }
    }, 3000);
}

async function toolbarSubdivide(taskId) {
    const task = taskData[taskId];
    const title = task ? `"${task.title}"` : `task ${taskId}`;
    const ok = await showConfirm('Subdivide Task', `Run the subdivision agent on ${title}? Existing children will be cancelled.`, 'Subdivide');
    if (!ok) return;
    try {
        const resp = await fetch(`${API_BASE}/agent/subdivide/${taskId}`, { method: 'POST' });
        const data = await resp.json();
        if (!resp.ok) { showToast(data.detail || 'Subdivide failed', 'error'); return; }
        if (taskData[taskId]) {
            taskData[taskId].type = 'subdividing';
            refreshCard(taskId);
        }
    } catch (e) {
        showToast('Error: ' + e.message, 'error');
    }
}

async function runAgentFromToolbar(taskId) {
    try {
        const resp = await fetch(`${API_BASE}/agent/run/${taskId}`, { method: 'POST' });
        const data = await resp.json();
        if (!resp.ok) { showToast(data.detail || 'Could not start agent', 'error'); return; }
        showToast('MaestroLoop started.', 'success');
    } catch (e) {
        showToast('Error: ' + e.message, 'error');
    }
}

function createTaskCard(id, title, tags, owner, status) {
    const card = document.createElement('div');
    card.className = `task-card ${status}`;
    card.setAttribute('data-id', id);
    card.setAttribute('data-status', status);
    card.setAttribute('draggable', 'true');

    // Check for rejection/processing state from transition cache
    const cached = transitionCache[id];
    const latestOutcome = cached && cached.history.length > 0 ? cached.history[0].outcome : null;
    const rejectionCount = cached ? cached.rejectionCount : 0;

    if (latestOutcome === 'rejected' || latestOutcome === 'failed') {
        card.classList.add('rejected');
    }
    // If we have an active poller, card is processing
    if (transitionPollers[id]) {
        card.classList.add('processing');
    }

    const tagsHtml = tags.map(tag => `<span class="tag">${tag}</span>`).join('') || '<span class="tag">general</span>';
    const ownerHtml = owner ? `<span>${owner}</span>` : '';

    const taskObj = taskData[id] || {};
    const rejBadge = rejectionCount > 0 ? `<span class="rejection-badge" title="${rejectionCount} rejection(s)">${rejectionCount}x</span>` : '';
    const processingSpinner = transitionPollers[id] ? '<span class="processing-indicator">\u25E0</span>' : '';

    // PIP badge — small inline indicator; the full pip-card stack is built by wrapWithPipGroup()
    const pips = taskObj.pips || [];
    const pipBadge = pips.length > 0
        ? `<span class="pip-badge" title="${pips.length} Performance Improvement Plan(s)">PIP</span>`
        : '';
    const pipRequirementsHtml = '';  // requirements now live in .pip-card segments below the card

    // Subdivision badges
    const parentId = taskObj.parent_task_id;
    const generation = taskObj.subdivision_generation || 0;
    const isSubdividing = status === 'subdividing';

    let subdivBadge = '';
    if (isSubdividing) {
        // Only show the animated badge while the agent is actively running.
        // "Active" = no non-cancelled children yet (covers first run AND mid-regeneration
        // where old children are cancelled before new ones are created).
        // Once real children exist the job is done — badge disappears entirely.
        const children = childIndex[id] || [];
        const hasActiveChildren = children.some(cid => taskData[cid] && taskData[cid].type !== 'cancelled');
        if (!hasActiveChildren) {
            subdivBadge = '<span class="subdivision-badge subdividing" title="Subdividing...">Subdividing</span>';
            card.classList.add('subdividing');
        }
    } else if (generation > 0) {
        subdivBadge = `<span class="subdivision-badge gen" title="Generation ${generation} sub-idea">Gen ${generation}</span>`;
    }

    // Big Idea badge and styling
    const isBigIdea = taskObj.is_big_idea;
    let bigIdeaBadge = '';
    let contractIndicator = '';
    if (isBigIdea) {
        bigIdeaBadge = '<span class="big-idea-badge">Big Idea</span>';
        card.classList.add('big-idea-card');
    }
    if (taskObj.interface_contracts) {
        contractIndicator = '<span class="contract-indicator" title="Has interface contracts">&#128196;</span>';
    }

    let parentLink = '';
    if (parentId && taskData[parentId]) {
        const parentTitle = taskData[parentId].title || parentId;
        parentLink = `<div class="parent-link" onclick="scrollToTask('${parentId}')" title="Parent: ${parentTitle}">&#8593; ${parentTitle}</div>`;
    }

    // Prerequisite labels for zoom view
    let prereqHtml = '';
    if (currentBigIdeaFilter) {
        prereqHtml = buildPrereqLabels(id);
    }

    const showFooter = (status !== 'idea' && status !== 'architecture' && status !== 'subdividing' && status !== 'cancelled');
    const footerHtml = showFooter
        ? `<div class="card-stage-footer csf-loading" id="csf-${id}" onclick="event.stopPropagation();openStageJournal('${id}')">…</div>`
        : '';

    card.innerHTML = `
        <button class="card-highlight-btn" title="Highlight card" onclick="event.stopPropagation();toggleHighlight('${id}')">☆</button>
        ${parentLink}
        <div class="task-title"${isBigIdea ? ` onclick="zoomIntoBigIdea('${id}')" style="cursor:pointer"` : ''}>${title}${rejBadge}${processingSpinner}${subdivBadge}${bigIdeaBadge}${contractIndicator}${pipBadge}</div>
        <div class="task-meta">
            ${tagsHtml}
            ${ownerHtml}
        </div>
        ${pipRequirementsHtml}
        ${prereqHtml}
        ${footerHtml}
        <div class="card-job-indicator" id="ji-${id}"></div>
        <div class="card-toolbar">
            <span class="toolbar-sep"></span>
            <button class="toolbar-btn" title="Research — run a research agent on this card" onclick="event.stopPropagation();openResearchDialog('${id}')">🔍</button>
            <button class="toolbar-btn" title="Subdivide — run subdivision agent on this card" onclick="event.stopPropagation();toolbarSubdivide('${id}')">✂</button>
            <button class="toolbar-btn" title="Run Planning pipeline" onclick="event.stopPropagation();toolbarRunPipeline('${id}','planning')">📋</button>
            <button class="toolbar-btn" title="Run Conceptual Review pipeline" onclick="event.stopPropagation();toolbarRunPipeline('${id}','review')">👁</button>
            <button class="toolbar-btn" title="Run Optimization pipeline" onclick="event.stopPropagation();toolbarRunPipeline('${id}','optimization')">⚡</button>
            <button class="toolbar-btn" title="Run Security pipeline" onclick="event.stopPropagation();toolbarRunPipeline('${id}','security')">🔒</button>
            <button class="toolbar-btn" title="Manual Session — drive tool calls yourself" onclick="event.stopPropagation();openManualSession('${id}')">⌨</button>
            <span class="toolbar-sep"></span>
            <button class="toolbar-btn" title="Run Agent — start MaestroLoop" onclick="event.stopPropagation();runAgentFromToolbar('${id}')">▶</button>
            <button class="toolbar-btn" title="Stop Agent — request graceful halt" onclick="event.stopPropagation();toolbarStopAgent('${id}')">⏹</button>
            <button class="toolbar-btn" title="Demote — move one stage backward" onclick="event.stopPropagation();toolbarDemote('${id}')">↩</button>
            <button class="toolbar-btn" title="Set Stage — move to any pipeline stage" onclick="event.stopPropagation();toolbarStagePicker('${id}',this)">⚙</button>
            <span class="toolbar-sep"></span>
            <button class="toolbar-btn" title="Open in Diagnostics" onclick="event.stopPropagation();toolbarOpenDiagnostics('${id}')">📊</button>
            <button class="toolbar-btn" title="Stage Journal — artifacts, gate checks, code diff" onclick="event.stopPropagation();openStageJournal('${id}')">📋</button>
            <button class="toolbar-btn" title="Research Jobs — view research agents run for this card" onclick="event.stopPropagation();viewResearchJobs('${id}')">🗂</button>
            ${['indev','conceptual_review','optimization','security','full_review','completed'].includes(status) ? `<button class="toolbar-btn" title="View code diff" onclick="event.stopPropagation();openStageDiff('${id}')">&#9998;</button>` : ''}
            <button class="toolbar-btn" title="Clone as new Idea" onclick="event.stopPropagation();toolbarClone('${id}')">⧉</button>
            <button class="toolbar-btn" title="Pin to top of column" onclick="event.stopPropagation();toolbarPin('${id}')">📌</button>
            <button class="toolbar-btn" title="Open in Column Map (DAG view)" onclick="event.stopPropagation();toolbarOpenMap('${id}')">🔗</button>
        </div>
        <div class="task-actions">
            <button class="action-btn" onclick="editTask('${id}')">Edit</button>
            <button class="action-btn action-btn-danger" onclick="deleteTask('${id}')">Delete</button>
        </div>
    `;
    _applyHighlightState(card, id);

    // Make rejected/failed cards clickable to open transition detail
    if (rejectionCount > 0) {
        card.style.cursor = 'pointer';
        card.addEventListener('click', (e) => {
            // Don't open overlay if a button was clicked
            if (e.target.closest('.action-btn')) return;
            openTransitionModal(id);
        });
    }

    const ready = canTaskAdvance(id);

    if (status === 'subdividing') {
        // Subdividing — always show View + Edit + View Children + Delete; Advance if ready
        const actionsDiv = card.querySelector('.task-actions');
        actionsDiv.innerHTML = `
            <button class="action-btn" onclick="viewTask('${id}')">View</button>
            <button class="action-btn" onclick="editTask('${id}')">Edit</button>
            <button class="action-btn" onclick="viewChildren('${id}')">View Children</button>
            <button class="action-btn action-btn-danger" onclick="deleteTask('${id}')">Delete</button>
        `;
        if (canTaskAdvance(id)) {
            const advBtn = document.createElement('button');
            advBtn.className = 'action-btn action-btn-advance';
            if (transitionPollers[id]) {
                advBtn.textContent = 'Processing...';
                advBtn.disabled = true;
            } else {
                advBtn.textContent = _advanceBtnLabel(status, rejectionCount > 0);
            }
            advBtn.onclick = (e) => { e.stopPropagation(); advanceTask(id); };
            actionsDiv.appendChild(advBtn);
        }
    } else if (status === 'idea') {
        const actionsDiv = card.querySelector('.task-actions');
        const advanceBtn = document.createElement('button');
        advanceBtn.className = 'action-btn action-btn-advance';
        if (transitionPollers[id]) {
            advanceBtn.textContent = 'Processing...';
            advanceBtn.disabled = true;
        } else {
            advanceBtn.textContent = _advanceBtnLabel(status, rejectionCount > 0);
        }
        advanceBtn.onclick = (e) => {
            e.stopPropagation();
            advanceTask(id);
        };
        actionsDiv.appendChild(advanceBtn);

        // Show View Children if this card is a Big Idea or has non-cancelled children
        const hasChildren = (childIndex[id] || []).some(cid => taskData[cid] && taskData[cid].type !== 'cancelled');
        if (hasChildren || isBigIdea) {
            const childBtn = document.createElement('button');
            childBtn.className = 'action-btn';
            childBtn.textContent = 'View Children';
            childBtn.onclick = (e) => { e.stopPropagation(); viewChildren(id); };
            actionsDiv.appendChild(childBtn);
        }
    } else if (status === 'planning' || status === 'indev' || status === 'conceptual_review' || status === 'optimization' || status === 'security' || status === 'full_review') {
        const actionsDiv = card.querySelector('.task-actions');
        const advanceBtn = document.createElement('button');
        advanceBtn.className = 'action-btn action-btn-advance';
        if (transitionPollers[id]) {
            advanceBtn.textContent = 'Processing...';
            advanceBtn.disabled = true;
        } else {
            advanceBtn.textContent = _advanceBtnLabel(status, rejectionCount > 0);
        }
        advanceBtn.onclick = (e) => {
            e.stopPropagation();
            advanceTask(id);
        };
        actionsDiv.appendChild(advanceBtn);
    } else if (status === 'completed') {
        const viewBtn = card.querySelector('.task-actions');
        viewBtn.innerHTML = `<button class="action-btn" onclick="viewTaskHistory('${id}')">View Proof</button>
                             <button class="action-btn action-btn-danger" onclick="deleteTask('${id}')">Delete</button>`;
    } else if (status === 'architecture') {
        const editBtn = card.querySelector('.task-actions');
        if (editBtn) {
            editBtn.innerHTML = `<button class="action-btn" onclick="editArchitectureTask('${id}')">Edit</button>
                                 <button class="action-btn action-btn-danger" onclick="deleteTask('${id}')">Delete</button>`;
        }
    }

    // Research Jobs button — available on any status that can have research (not idea/subdividing/architecture)
    if (status !== 'idea' && status !== 'subdividing' && status !== 'architecture') {
        const researchBtn = document.createElement('button');
        researchBtn.className = 'action-btn';
        researchBtn.textContent = 'Research Jobs';
        researchBtn.onclick = (e) => { e.stopPropagation(); viewResearchJobs(id); };
        card.querySelector('.task-actions').appendChild(researchBtn);
    }

    // Benchmarks button — visible once optimization stage has run
    if (status === 'optimization' || status === 'security' || status === 'full_review' || status === 'completed') {
        const benchBtn = document.createElement('button');
        benchBtn.className = 'action-btn';
        benchBtn.textContent = 'Benchmarks';
        benchBtn.onclick = (e) => { e.stopPropagation(); viewBenchmarks(id); };
        card.querySelector('.task-actions').appendChild(benchBtn);
    }

    card.addEventListener('dragstart', handleDragStart);
    card.addEventListener('dragend', handleDragEnd);

    return card;
}

// ============================================
// Advance Task (Initiate Pipeline stages)
// ============================================

async function advanceTask(taskId) {
    const task = taskData[taskId];
    if (!task) return;

    const advanceStartedAt = new Date().toISOString();
    
    // Map status to specific API endpoints
    const endpointMap = {
        'idea': 'advance',
        'subdividing': 'advance',
        'planning': 'run-planning',
        'indev': 'run-review', // after indev we want conceptual review
        'conceptual_review': 'run-review',
        'optimization': 'run-security',
        'security': 'run-full-review',
        'full_review': 'merge'
    };

    const action = endpointMap[task.type] || 'advance';
    
    try {
        const response = await fetch(`${API_BASE}/tasks/${taskId}/${action}`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' }
        });
        if (!response.ok) {
            const err = await response.json();
            showToast('Pipeline start failed: ' + (err.detail || 'Unknown error'), 'error');
            return;
        }
        const result = await response.json();
        console.log(`Pipeline ${action} initiated:`, result);

        // Mark card as processing immediately
        setCardProcessing(taskId, true);

        // Start polling for transition status, ignoring any results from before this click
        startTransitionPolling(taskId, advanceStartedAt);
    } catch (error) {
        console.error('Error advancing task:', error);
    }
}

function setCardProcessing(taskId, processing) {
    const card = cardCache[taskId];
    if (!card) return;
    if (processing) {
        card.classList.add('processing');
        card.classList.remove('rejected');
        // Add spinner indicator if not present
        const titleEl = card.querySelector('.task-title');
        if (titleEl && !titleEl.querySelector('.processing-indicator')) {
            const spinner = document.createElement('span');
            spinner.className = 'processing-indicator';
            spinner.textContent = '\u25E0'; // half-circle spinner character
            titleEl.appendChild(spinner);
        }
        // Disable the advance button while processing
        const advBtn = card.querySelector('.action-btn-advance');
        if (advBtn) {
            advBtn.disabled = true;
            advBtn.textContent = 'Processing...';
        }
    } else {
        card.classList.remove('processing');
        const titleEl = card.querySelector('.task-title');
        if (titleEl) {
            const spinner = titleEl.querySelector('.processing-indicator');
            if (spinner) spinner.remove();
        }
    }
}

function startTransitionPolling(taskId, notBefore = null) {
    // Clear any existing poller for this task
    if (transitionPollers[taskId]) {
        clearInterval(transitionPollers[taskId]);
    }

    const pollInterval = setInterval(async () => {
        try {
            const resp = await fetch(`${API_BASE}/tasks/${taskId}/transition-status`);
            if (!resp.ok) return;
            const data = await resp.json();

            console.log(`[poll] Task ${taskId} transition-status:`, data);

            // Still no result yet — keep polling
            if (data.status === 'no_transitions' || !data.outcome) {
                return;
            }

            // Stale result from a previous run — keep polling until we see a fresh one
            if (notBefore && data.created_at && data.created_at < notBefore) {
                return;
            }

            // Pipeline completed — stop polling
            clearInterval(transitionPollers[taskId]);
            delete transitionPollers[taskId];

            // Save every result to inbox (pass or fail) for later review
            const taskTitle = allTasks.find(t => t.id === taskId)?.title || taskId;
            _inboxSaveTransitionResult(taskId, taskTitle, data);

            if (data.outcome === 'passed') {
                // Task promoted — fetch fresh data and reconcile
                await loadTasksFromDatabase();
                await loadTransitionStatuses();
                reconcile(allTasks);
            } else {
                // rejected or failed — update this card immediately, then sync
                setCardProcessing(taskId, false);
                cacheTransitionData(taskId, data);
                refreshCard(taskId);

                // Show the failure overlay
                openTransitionModal(taskId);

                await loadTasksFromDatabase();
                await loadTransitionStatuses();
                reconcile(allTasks);
            }
        } catch (err) {
            console.error(`[poll] Error polling transition for ${taskId}:`, err);
        }
    }, 2500); // Poll every 2.5 seconds

    transitionPollers[taskId] = pollInterval;
}

function cacheTransitionData(taskId, data) {
    if (!transitionCache[taskId]) {
        transitionCache[taskId] = { history: [], rejectionCount: 0 };
    }
    // Avoid duplicates by checking timestamp
    const exists = transitionCache[taskId].history.some(
        h => h.created_at === data.created_at && h.transition === data.transition
    );
    if (!exists) {
        transitionCache[taskId].history.unshift(data);
    }
    // Count rejections/failures
    transitionCache[taskId].rejectionCount = transitionCache[taskId].history.filter(
        h => h.outcome === 'rejected' || h.outcome === 'failed'
    ).length;
}

// ============================================
// Transition Status Loading (for existing tasks)
// ============================================

async function loadTransitionStatuses() {
    // Fetch transition status for all idea-column tasks
    const ideaTasks = allTasks.filter(t => t.type === 'idea');
    const promises = ideaTasks.map(async (task) => {
        try {
            const resp = await fetch(`${API_BASE}/tasks/${task.id}/transition-status`);
            if (!resp.ok) return;
            const data = await resp.json();
            if (data.status === 'no_transitions') return;

            // If the API returns a history array, load all entries
            if (data.history && Array.isArray(data.history)) {
                transitionCache[task.id] = { history: [], rejectionCount: 0 };
                data.history.forEach(entry => cacheTransitionData(task.id, entry));
            } else {
                cacheTransitionData(task.id, data);
            }
        } catch (err) {
            // Silently skip — not critical
        }
    });
    await Promise.all(promises);
}

// ============================================
// Transition Failure Overlay
// ============================================

const TRANSITION_LABELS = {
    'idea_to_planning': 'IDEA \u2192 PLANNING',
    'planning_to_development': 'PLANNING \u2192 DEVELOPMENT',
    'development_to_review': 'DEVELOPMENT \u2192 REVIEW',
    'review_to_completed': 'REVIEW \u2192 COMPLETED',
};

function openTransitionModal(taskId) {
    const cached = transitionCache[taskId];
    if (!cached || cached.history.length === 0) {
        showToast('No transition data available for this task.', 'info');
        return;
    }

    const task = taskData[taskId];
    const taskTitle = task ? task.title : taskId;

    document.getElementById('transition-modal-title').textContent = `Transitions: ${taskTitle}`;
    renderTransitionDetail(taskId, 0);
    document.getElementById('transition-modal').classList.add('active');
}

function renderTransitionDetail(taskId, index) {
    const cached = transitionCache[taskId];
    if (!cached || !cached.history[index]) return;

    const body = document.getElementById('transition-modal-body');
    const history = cached.history;
    const data = history[index];

    let html = '';

    // History navigation if multiple attempts
    if (history.length > 1) {
        html += '<div class="transition-history-nav">';
        history.forEach((h, i) => {
            const activeClass = i === index ? ' active' : '';
            const outcomeClass = ` outcome-${h.outcome}`;
            const label = TRANSITION_LABELS[h.transition] || h.transition;
            const ts = h.created_at ? new Date(h.created_at).toLocaleDateString() : '';
            html += `<button class="transition-history-btn${activeClass}${outcomeClass}" `
                  + `onclick="renderTransitionDetail('${taskId}', ${i})">`
                  + `#${history.length - i} ${h.outcome.toUpperCase()} ${ts}</button>`;
        });
        html += '</div>';
    }

    // Transition header
    const transLabel = TRANSITION_LABELS[data.transition] || data.transition;
    const outcomeClass = `outcome-${data.outcome}`;
    html += `<div class="transition-header ${outcomeClass}">`;
    html += `<span class="transition-label">${transLabel}</span>`;
    html += `<span class="transition-outcome ${data.outcome}">${data.outcome.toUpperCase()}</span>`;
    html += '</div>';

    // Votes
    const votes = data.votes || [];
    if (votes.length > 0) {
        html += '<h3 style="font-size:0.95rem; margin-bottom:0.5rem; color:#495057;">Stage Votes</h3>';
        votes.forEach(v => {
            const verdictClass = `verdict-${v.verdict}`;
            // Confidence may be 0.0-1.0 (float) or 0-100 (int); normalize to percentage
            const rawConf = v.confidence;
            const confPct = rawConf != null
                ? (rawConf <= 1 ? Math.round(rawConf * 100) : Math.round(rawConf))
                : null;
            const confidence = confPct != null ? `${confPct}%` : 'N/A';
            html += `<div class="vote-card">`;
            html += `<div class="vote-card-header">`;
            html += `<span class="vote-stage">${v.stage}</span>`;
            html += `<span class="vote-verdict ${verdictClass}">${v.verdict}</span>`;
            html += `</div>`;
            html += `<div class="vote-confidence">Confidence: ${confidence}</div>`;
            if (v.justification) {
                html += `<div class="vote-justification">${escapeHtml(v.justification)}</div>`;
            }
            html += '</div>';
        });
    }

    // Token usage
    const promptTok = data.total_prompt_tokens || 0;
    const compTok = data.total_completion_tokens || 0;
    if (promptTok || compTok) {
        html += '<div class="transition-tokens">';
        html += `<span>Prompt tokens: <strong>${promptTok.toLocaleString()}</strong></span>`;
        html += `<span>Completion tokens: <strong>${compTok.toLocaleString()}</strong></span>`;
        html += `<span>Total: <strong>${(promptTok + compTok).toLocaleString()}</strong></span>`;
        html += '</div>';
    }

    // Timestamp
    if (data.created_at) {
        const ts = new Date(data.created_at).toLocaleString();
        html += `<div class="transition-timestamp">Evaluated: ${ts}</div>`;
    }

    body.innerHTML = html;
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

function closeTransitionModal() {
    document.getElementById('transition-modal').classList.remove('active');
    _viewChildrenState = null;
    _stopChildrenPoller();
    const fl = document.getElementById('transition-modal-footer-left');
    if (fl) fl.innerHTML = '';
}

// ============================================
// Scheduler Queue Modal
// ============================================

let _schedulerModalPoller = null;

function openSchedulerModal() {
    document.getElementById('scheduler-modal').classList.add('active');
    _fetchAndRenderScheduler();
    _schedulerModalPoller = setInterval(_fetchAndRenderScheduler, 3000);
}

function closeSchedulerModal() {
    document.getElementById('scheduler-modal').classList.remove('active');
    if (_schedulerModalPoller) {
        clearInterval(_schedulerModalPoller);
        _schedulerModalPoller = null;
    }
}

async function _fetchAndRenderScheduler() {
    try {
        const resp = await fetch(`${API_BASE}/scheduler/status`);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = await resp.json();
        _renderSchedulerModal(data);
        const ts = new Date().toLocaleTimeString();
        const statusEl = document.getElementById('scheduler-modal-status');
        if (statusEl) statusEl.textContent = `Updated ${ts} · ${data.running ? 'Scheduler running' : 'Scheduler stopped'}`;
    } catch (e) {
        const body = document.getElementById('scheduler-modal-body');
        if (body) body.innerHTML = `<p style="color:#dc3545">Failed to load: ${e.message}</p>`;
    }
}

function _renderSchedulerModal(data) {
    const body = document.getElementById('scheduler-modal-body');
    if (!body) return;

    const { active = [], queued = [], blocked = [], llm_capacities = {},
            pending_research_jobs = 0, pending_file_summary_jobs = 0 } = data;

    // Update header button label
    const queueBtn = document.getElementById('scheduler-queue-btn');
    if (queueBtn) {
        const total = active.length + queued.length;
        queueBtn.textContent = total > 0 ? `⚙ Queue (${total})` : '⚙ Queue';
    }

    // Collect all LLM IDs that appear in any list
    const allLlmIds = new Set();
    const noLlmTasks = { active: [], queued: [], blocked: [] };

    for (const task of [...active, ...queued, ...blocked]) {
        if (task.llm_id != null) allLlmIds.add(String(task.llm_id));
        else {
            if (active.includes(task)) noLlmTasks.active.push(task);
            else if (queued.includes(task)) noLlmTasks.queued.push(task);
            else noLlmTasks.blocked.push(task);
        }
    }

    // Build a map: llm_id → { active[], queued[], blocked[] }
    const byLlm = {};
    for (const lid of allLlmIds) {
        byLlm[lid] = { active: [], queued: [], blocked: [] };
    }
    for (const t of active) {
        const key = t.llm_id != null ? String(t.llm_id) : null;
        if (key) byLlm[key].active.push(t);
    }
    for (const t of queued) {
        const key = t.llm_id != null ? String(t.llm_id) : null;
        if (key) byLlm[key].queued.push(t);
    }
    for (const t of blocked) {
        const key = t.llm_id != null ? String(t.llm_id) : null;
        if (key) byLlm[key].blocked.push(t);
    }

    const escHtml = s => String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');

    function taskRow(t, bucket) {
        const typeColor = {
            idea:'#6c757d', planning:'#0d6efd', indev:'#198754',
            conceptual_review:'#20c997', optimization:'#fd7e14',
            full_review:'#dc3545'
        }[t.type] || '#6c757d';

        let badge = '';
        if (bucket === 'active') {
            badge = `<span style="background:#198754;color:#fff;padding:1px 6px;border-radius:3px;font-size:0.7rem;font-weight:600">RUNNING</span>`;
        } else if (bucket === 'queued') {
            const reasonColor = t.reason === 'at_capacity' ? '#fd7e14' : t.reason === 'cooldown' ? '#dc3545' : '#6c757d';
            badge = `<span style="background:${reasonColor};color:#fff;padding:1px 6px;border-radius:3px;font-size:0.7rem;font-weight:600">${escHtml(t.reason || 'QUEUED')}</span>`;
        } else {
            badge = `<span style="background:#6c757d;color:#fff;padding:1px 6px;border-radius:3px;font-size:0.7rem;font-weight:600">BLOCKED</span>`;
        }

        let extra = '';
        if (bucket === 'blocked' && t.blocking_titles && t.blocking_titles.length) {
            extra = `<div style="font-size:0.72rem;color:#6c757d;margin-top:2px">Waiting on: ${t.blocking_titles.map(escHtml).join(', ')}</div>`;
        }

        return `<div style="display:flex;align-items:flex-start;gap:0.5rem;padding:5px 0;border-bottom:1px solid #2a2a2a">
            <span style="width:8px;height:8px;border-radius:50%;background:${typeColor};margin-top:5px;flex-shrink:0"></span>
            <div style="flex:1;min-width:0">
                <div style="font-size:0.82rem;font-weight:500;white-space:nowrap;overflow:hidden;text-overflow:ellipsis" title="${escHtml(t.title)}">${escHtml(t.title)}</div>
                <div style="font-size:0.72rem;color:#6c757d">${escHtml(t.type)} · ${escHtml(t.project || '—')}</div>
                ${extra}
            </div>
            <div style="flex-shrink:0">${badge}</div>
        </div>`;
    }

    function llmSection(lid, tasks, cap) {
        const capInfo = cap || { name: tasks.active[0]?.llm_name || tasks.queued[0]?.llm_name || tasks.blocked[0]?.llm_name || `LLM ${lid}`, current: 0, max: '?' };
        const slotText = `${capInfo.current}/${capInfo.max} slots`;
        const totalTasks = tasks.active.length + tasks.queued.length + tasks.blocked.length;
        if (totalTasks === 0) return '';

        let html = `<div style="margin-bottom:1.2rem">
            <div style="display:flex;align-items:center;gap:0.5rem;margin-bottom:0.4rem;padding-bottom:0.3rem;border-bottom:2px solid #333">
                <span style="font-weight:700;font-size:0.9rem">${escHtml(capInfo.name)}</span>
                <span style="font-size:0.75rem;color:#6c757d;margin-left:auto">${slotText}</span>
            </div>`;

        if (tasks.active.length) {
            html += `<div style="font-size:0.72rem;color:#198754;font-weight:600;margin-bottom:2px">ACTIVE (${tasks.active.length})</div>`;
            html += tasks.active.map(t => taskRow(t, 'active')).join('');
        }
        if (tasks.queued.length) {
            html += `<div style="font-size:0.72rem;color:#fd7e14;font-weight:600;margin:6px 0 2px">QUEUED (${tasks.queued.length})</div>`;
            html += tasks.queued.map(t => taskRow(t, 'queued')).join('');
        }
        if (tasks.blocked.length) {
            html += `<div style="font-size:0.72rem;color:#6c757d;font-weight:600;margin:6px 0 2px">BLOCKED (${tasks.blocked.length})</div>`;
            html += tasks.blocked.map(t => taskRow(t, 'blocked')).join('');
        }

        html += '</div>';
        return html;
    }

    let html = '';

    if (allLlmIds.size === 0 && noLlmTasks.active.length === 0 && noLlmTasks.queued.length === 0 && noLlmTasks.blocked.length === 0) {
        html = '<p style="color:#6c757d;text-align:center;padding:2rem 0">No dispatchable tasks in queue.</p>';
    } else {
        // Sort LLM groups: those with active tasks first, then by name
        const sortedLids = Array.from(allLlmIds).sort((a, b) => {
            const aActive = byLlm[a].active.length;
            const bActive = byLlm[b].active.length;
            if (aActive !== bActive) return bActive - aActive;
            const aName = (llm_capacities[a]?.name || '').toLowerCase();
            const bName = (llm_capacities[b]?.name || '').toLowerCase();
            return aName.localeCompare(bName);
        });

        for (const lid of sortedLids) {
            html += llmSection(lid, byLlm[lid], llm_capacities[lid]);
        }

        // Unassigned tasks
        const unassignedTotal = noLlmTasks.active.length + noLlmTasks.queued.length + noLlmTasks.blocked.length;
        if (unassignedTotal > 0) {
            html += llmSection('(none)', noLlmTasks, { name: 'No LLM Assigned', current: 0, max: 0 });
        }
    }

    // Footer summary
    const summaryParts = [];
    if (pending_research_jobs > 0) summaryParts.push(`${pending_research_jobs} research job${pending_research_jobs !== 1 ? 's' : ''} pending`);
    if (pending_file_summary_jobs > 0) summaryParts.push(`${pending_file_summary_jobs} file summary job${pending_file_summary_jobs !== 1 ? 's' : ''} pending`);
    if (summaryParts.length) {
        html += `<div style="font-size:0.78rem;color:#6c757d;border-top:1px solid #333;padding-top:0.5rem;margin-top:0.5rem">${summaryParts.join(' · ')}</div>`;
    }

    body.innerHTML = html;
}

// ============================================
// Inbox
// ============================================

async function loadInbox() {
    try {
        const resp = await fetch(`${API_BASE}/inbox`);
        if (!resp.ok) return;
        inboxMessages = await resp.json();
        _inboxUnreadCount = inboxMessages.filter(m => !m.read).length;
        _updateInboxBadge();
    } catch (e) {
        console.error('[inbox] load failed:', e);
    }
}

async function refreshInboxBadge() {
    try {
        const resp = await fetch(`${API_BASE}/inbox/unread-count`);
        if (!resp.ok) return;
        const { count } = await resp.json();
        _inboxUnreadCount = count;
        _updateInboxBadge();
    } catch (e) { /* silent */ }
}

function _updateInboxBadge() {
    const badge = document.getElementById('inbox-badge');
    if (!badge) return;
    if (_inboxUnreadCount > 0) {
        badge.textContent = _inboxUnreadCount > 99 ? '99+' : String(_inboxUnreadCount);
        badge.style.display = 'flex';
    } else {
        badge.style.display = 'none';
    }
}

async function openInboxModal() {
    await loadInbox();
    _renderInboxList();
    document.getElementById('inbox-modal').classList.add('active');
}

function closeInboxModal() {
    document.getElementById('inbox-modal').classList.remove('active');
}

function openInboxDetailModal(msgId) {
    const msg = inboxMessages.find(m => m.id === msgId);
    if (!msg) return;

    document.getElementById('inbox-detail-title').textContent = msg.subject;
    const body = document.getElementById('inbox-detail-body');

    let html = `<div style="padding:1rem">`;
    if (msg.task_title) {
        html += `<div style="margin-bottom:0.5rem;font-size:0.85rem;color:#6c757d">Task: <strong>${msg.task_title}</strong></div>`;
    }
    html += `<div style="margin-bottom:1rem;font-size:0.8rem;color:#adb5bd">${new Date(msg.created_at).toLocaleString()}</div>`;

    if (msg.data_json) {
        try {
            const data = JSON.parse(msg.data_json);
            if (data.report && data.report.findings) {
                html += `<h3 style="font-size:1rem;margin-bottom:0.5rem">Report Findings</h3>`;
                html += `<div style="background:#f8f9fa;padding:1rem;border-radius:6px;font-size:0.9rem;white-space:pre-wrap;line-height:1.5">${data.report.findings}</div>`;
                if (data.report.answer) {
                    html += `<h3 style="font-size:1rem;margin:1rem 0 0.5rem">Answer</h3>`;
                    html += `<div style="background:#e7f3ff;padding:1rem;border-radius:6px;font-size:0.95rem;font-weight:500;white-space:pre-wrap">${data.report.answer}</div>`;
                }
            } else {
                html += `<h3 style="font-size:1rem;margin-bottom:0.5rem">Data Details</h3>`;
                html += `<pre style="background:#f8f9fa;padding:1rem;border-radius:6px;font-size:0.8rem;overflow-x:auto">${JSON.stringify(data, null, 2)}</pre>`;
            }
        } catch (e) {
            html += `<p style="color:#6c757d">Raw data: ${msg.data_json}</p>`;
        }
    } else {
        html += `<p style="color:#6c757d">No additional details available.</p>`;
    }

    html += `</div>`;
    body.innerHTML = html;
    document.getElementById('inbox-detail-modal').classList.add('active');
}

function closeInboxDetailModal() {
    document.getElementById('inbox-detail-modal').classList.remove('active');
}

function _inboxOutcomeClass(outcome) {
    if (!outcome) return 'inbox-outcome-unknown';
    const o = outcome.toLowerCase();
    if (o === 'rejected') return 'inbox-outcome-rejected';
    if (o === 'failed')   return 'inbox-outcome-failed';
    if (o === 'passed')   return 'inbox-outcome-passed';
    if (o.startsWith('subdivide')) return 'inbox-outcome-subdivide';
    return 'inbox-outcome-unknown';
}

function _inboxRelTime(isoStr) {
    if (!isoStr) return '';
    const d = new Date(isoStr.endsWith('Z') ? isoStr : isoStr + 'Z');
    const diffMs = Date.now() - d.getTime();
    const diffMin = Math.floor(diffMs / 60_000);
    if (diffMin < 1)  return 'just now';
    if (diffMin < 60) return `${diffMin}m ago`;
    const diffHr = Math.floor(diffMin / 60);
    if (diffHr < 24)  return `${diffHr}h ago`;
    return `${Math.floor(diffHr / 24)}d ago`;
}

function _renderInboxList() {
    const body = document.getElementById('inbox-modal-body');
    const unreadLabel = document.getElementById('inbox-modal-unread-label');
    const markAllBtn = document.getElementById('inbox-mark-all-btn');
    const unread = inboxMessages.filter(m => !m.read).length;

    if (unread > 0) {
        unreadLabel.textContent = `${unread} unread`;
        unreadLabel.style.display = 'inline-block';
        markAllBtn.style.display = '';
    } else {
        unreadLabel.style.display = 'none';
        markAllBtn.style.display = 'none';
    }

    if (inboxMessages.length === 0) {
        body.innerHTML = '<div class="inbox-empty">No messages yet.<br>Intake pipeline results will appear here.</div>';
        return;
    }

    body.innerHTML = inboxMessages.map(msg => {
        const outcomeClass = _inboxOutcomeClass(msg.outcome);
        const outcomeLabel = msg.outcome ? msg.outcome.toUpperCase().replace('_', ' ') : '—';
        const taskChip = msg.task_title
            ? `<span class="inbox-item-task" title="${msg.task_title}">${msg.task_title}</span>`
            : '';
        return `
        <div class="inbox-item ${msg.read ? '' : 'unread'}" data-id="${msg.id}" onclick="inboxOpenMessage('${msg.id}')">
            <div class="inbox-unread-dot"></div>
            <div class="inbox-item-body">
                <div class="inbox-item-subject">${msg.subject}</div>
                <div class="inbox-item-meta">
                    ${taskChip}
                    <span class="inbox-outcome-badge ${outcomeClass}">${outcomeLabel}</span>
                    <span class="inbox-item-time">${_inboxRelTime(msg.created_at)}</span>
                </div>
            </div>
            <div class="inbox-item-actions">
                <button class="inbox-delete-btn" title="Delete" onclick="inboxDelete(event, '${msg.id}')">×</button>
            </div>
        </div>`.trim();
    }).join('');
}

async function inboxOpenMessage(msgId) {
    const msg = inboxMessages.find(m => m.id === msgId);
    if (!msg) return;

    // Mark as read
    if (!msg.read) {
        await fetch(`${API_BASE}/inbox/${msgId}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ read: true }),
        });
        msg.read = true;
        _inboxUnreadCount = Math.max(0, _inboxUnreadCount - 1);
        _updateInboxBadge();
        _renderInboxList();
    }

    // Load into transition modal using cached data
    if (msg.data_json && msg.task_id) {
        try {
            const data = JSON.parse(msg.data_json);
            cacheTransitionData(msg.task_id, data);
            closeInboxModal();
            openTransitionModal(msg.task_id);
            return;
        } catch (e) {
            console.error('[inbox] failed to parse data_json:', e);
        }
    }

    // Fallback: just open transition modal if task has cached data
    if (msg.task_id && transitionCache[msg.task_id]) {
        closeInboxModal();
        openTransitionModal(msg.task_id);
    } else {
        openInboxDetailModal(msgId);
    }
}

async function inboxMarkAllRead() {
    await fetch(`${API_BASE}/inbox/mark-all-read`, { method: 'POST' });
    inboxMessages.forEach(m => { m.read = true; });
    _inboxUnreadCount = 0;
    _updateInboxBadge();
    _renderInboxList();
}

async function inboxDelete(event, msgId) {
    event.stopPropagation();
    await fetch(`${API_BASE}/inbox/${msgId}`, { method: 'DELETE' });
    const idx = inboxMessages.findIndex(m => m.id === msgId);
    if (idx !== -1) {
        if (!inboxMessages[idx].read) _inboxUnreadCount = Math.max(0, _inboxUnreadCount - 1);
        inboxMessages.splice(idx, 1);
    }
    _updateInboxBadge();
    _renderInboxList();
}

async function _inboxSaveTransitionResult(taskId, taskTitle, data) {
    const outcome = data.outcome || 'unknown';
    const outcomeLabel = outcome.charAt(0).toUpperCase() + outcome.slice(1).replace('_', ' ');
    const subject = `Intake: ${taskTitle} — ${outcomeLabel}`;
    try {
        const resp = await fetch(`${API_BASE}/inbox`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                subject,
                source_type: 'intake_result',
                task_id: taskId,
                task_title: taskTitle,
                outcome,
                data_json: JSON.stringify(data),
            }),
        });
        if (resp.ok) {
            const msg = await resp.json();
            inboxMessages.unshift(msg);
            _inboxUnreadCount++;
            _updateInboxBadge();
        }
    } catch (e) {
        console.error('[inbox] save failed:', e);
    }
}

// ============================================
// Task Deletion
// ============================================

async function deleteTask(taskId) {
    const task = taskData[taskId];
    const hasChildren = task && task.is_big_idea;
    const msg = hasChildren
        ? 'Hide this task and all its children? They will no longer appear on the board.'
        : 'Hide this task? It will no longer appear on the board.';
    if (!await showConfirm('Hide Task', msg, 'Hide')) return;

    const response = await fetch(`${API_BASE}/tasks/${taskId}`, { method: 'DELETE' });
    if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        showToast(data.detail || 'Failed to hide task', 'error');
        return;
    }

    const data = await response.json();
    const count = data.deactivated || 1;

    const isArch = task && task.type === 'architecture';

    // Remove task and any descendants from local state
    delete taskData[taskId];
    allTasks = allTasks.filter(t => t.id !== taskId);

    if (isArch) {
        // Arch cards live in the arch bar, not the kanban columns
        renderArchBar();
    } else {
        const card = document.querySelector(`.task-card[data-id="${taskId}"]`);
        if (card) {
            const container = card.closest('.tasks-container');
            card.remove();
            if (container) updateTaskCount(container.id.replace('tasks-', ''));
        }
    }
    if (count > 1) showToast(`Hidden ${count} tasks (task + children).`, 'info');
    // Full reload so descendant cards disappear too
    if (count > 1) await loadTasksFromDatabase();
}

// ============================================
// Task Movement
// ============================================

async function moveTask(taskId, newStatus) {
    console.log('=== Move Task Called ===');
    console.log('Task ID:', taskId);
    console.log('New Status:', newStatus);
    console.log('Task Data:', taskData[taskId]);

    const task = taskData[taskId];

    if (!task) {
        console.error('Task not found in taskData:', taskId);
        showToast('Task not found', 'error');
        return;
    }

    if (task.immutable) {
        console.log('Cannot move immutable architecture task');
        return;
    }

    if (!canTaskAdvance(taskId)) {
        showToast('Task cannot advance — it needs a description, LLM, and budget assigned.', 'warning');
        return;
    }

    // Update task in database via PUT request
    const response = await fetch(`${API_BASE}/tasks/${taskId}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ type: newStatus })
    });

    console.log('Response status:', response.status);

    if (!response.ok) {
        const errorText = await response.text();
        console.error('Error response:', errorText);
        showToast('Failed to move task: ' + errorText, 'error');
        return;
    }

    const updatedTask = await response.json();
    console.log('Updated task:', updatedTask);
    taskData[taskId] = updatedTask;

    // Update UI after successful database update
    const currentCard = document.querySelector(`.task-card[data-id="${taskId}"]`);
    console.log('Current card:', currentCard);
    if (currentCard) {
        const currentStatus = currentCard.getAttribute('data-status');
        currentCard.classList.remove(currentStatus);
        currentCard.classList.add(newStatus);
        currentCard.setAttribute('data-status', newStatus);

        const newContainer = document.getElementById(`tasks-${newStatus}`);
        const currentContainer = document.getElementById(`tasks-${currentStatus}`);

        if (currentContainer) {
            currentContainer.removeChild(currentCard);
        }

        const actions = currentCard.querySelector('.task-actions');
        const ready = canTaskAdvance(taskId);
        if (newStatus === 'planning') {
            actions.innerHTML = `<button class="action-btn" onclick="editTask('${taskId}')">Edit</button>
                                 <button class="action-btn action-btn-danger" onclick="deleteTask('${taskId}')">Delete</button>`
                + (ready ? `<button class="action-btn" onclick="moveTask('${taskId}', 'indev')">Move to IN DEVELOPMENT</button>` : '');
        } else if (newStatus === 'indev') {
            actions.innerHTML = `<button class="action-btn" onclick="editTask('${taskId}')">Edit</button>
                                 <button class="action-btn action-btn-danger" onclick="deleteTask('${taskId}')">Delete</button>`
                + (ready ? `<button class="action-btn" onclick="moveTask('${taskId}', 'conceptual_review')">Move to CONCEPTUAL REVIEW</button>` : '');
        } else if (newStatus === 'conceptual_review') {
            actions.innerHTML = `<button class="action-btn" onclick="editTask('${taskId}')">Edit</button>
                                 <button class="action-btn action-btn-danger" onclick="deleteTask('${taskId}')">Delete</button>`
                + (ready ? `<button class="action-btn" onclick="moveTask('${taskId}', 'optimization')">Move to OPTIMIZATION</button>` : '');
        } else if (newStatus === 'optimization') {
            actions.innerHTML = `<button class="action-btn" onclick="editTask('${taskId}')">Edit</button>
                                 <button class="action-btn action-btn-danger" onclick="deleteTask('${taskId}')">Delete</button>`
                + (ready ? `<button class="action-btn" onclick="moveTask('${taskId}', 'security')">Move to SECURITY</button>` : '');
        } else if (newStatus === 'security') {
            actions.innerHTML = `<button class="action-btn" onclick="editTask('${taskId}')">Edit</button>
                                 <button class="action-btn action-btn-danger" onclick="deleteTask('${taskId}')">Delete</button>`
                + (ready ? `<button class="action-btn" onclick="moveTask('${taskId}', 'full_review')">Move to FINAL REVIEW</button>` : '');
        } else if (newStatus === 'full_review') {
            actions.innerHTML = `<button class="action-btn" onclick="editTask('${taskId}')">Edit</button>
                                 <button class="action-btn action-btn-danger" onclick="deleteTask('${taskId}')">Delete</button>`
                + (ready ? `<button class="action-btn" onclick="moveTask('${taskId}', 'completed')">Move to COMPLETED</button>` : '');
        } else if (newStatus === 'completed') {
            actions.innerHTML = `<button class="action-btn" onclick="viewTaskHistory('${taskId}')">View Proof</button>
                                 <button class="action-btn action-btn-danger" onclick="deleteTask('${taskId}')">Delete</button>`;
        }

        if (newContainer) {
            newContainer.appendChild(currentCard);
        }

        updateTaskCount(currentStatus);
        updateTaskCount(newStatus);

        console.log(`Task ${taskId} moved from ${currentStatus} to ${newStatus}`);
    }

    // Auto-move from indev to conceptual_review after 15 seconds
    if (newStatus === 'indev') {
        setTimeout(async () => {
            if (taskData[taskId]) {
                await moveTask(taskId, 'conceptual_review');
                console.log(`Auto-move: Task ${taskId} moved to conceptual_review after 15 seconds`);
            }
        }, 15000);
    }
}

// ============================================
// Task Editing
// ============================================

function editTask(taskId) {
    const task = taskData[taskId];
    if (!task) return;

    currentTaskId = taskId;
    currentTargetStatus = task.type;

    document.getElementById('modal-title').textContent = `Edit Task: ${task.title}`;
    document.getElementById('task-title').value = task.title;
    document.getElementById('task-description').value = task.description || '';
    document.getElementById('task-tags').value = (task.tags || []).join(', ');
    document.getElementById('task-owner').value = task.owner || 'user';
    showArchContentFields(task.type);
    populateLlmSelect(task.llm_id);
    populateBudgetSelect(task.budget_id);

    document.getElementById('task-modal').classList.add('active');
}

async function saveEditTask() {
    const title = document.getElementById('task-title').value.trim();
    const description = document.getElementById('task-description').value.trim();
    const tagsInput = document.getElementById('task-tags').value.trim();
    const owner = document.getElementById('task-owner').value.trim() || 'user';

    if (!title) {
        showToast('Task title is required.', 'warning');
        return;
    }

    const tags = tagsInput.split(',').map(t => t.trim()).filter(t => t);

    // Build content object for architecture tasks
    const content = currentTargetStatus === 'architecture' ? {
        category: document.getElementById('arch-category').value || 'General',
        priority: document.getElementById('arch-priority').value || 'normal',
    } : null;

    const taskDataPayload = {
        title,
        description,
        owner,
        tags,
        ...(content && { content })
    };

    const response = await fetch(`${API_BASE}/tasks/${currentTaskId}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(taskDataPayload)
    });

    if (!response.ok) {
        showToast('Failed to update task', 'error');
        return;
    }

    const updatedTask = await response.json();
    taskData[currentTaskId] = updatedTask;
    console.log(`Task updated: ${currentTaskId}`);

    closeModal();
}

function editArchitectureTask(taskId) {
    const task = taskData[taskId];
    if (!task) return;

    currentTaskId = taskId;
    currentTargetStatus = 'architecture';

    document.getElementById('modal-title').textContent = `Edit Architecture: ${task.title}`;
    document.getElementById('task-title').value = task.title || '';
    document.getElementById('task-description').value = task.description || '';

    const content = task.content || {};
    const catEl  = document.getElementById('arch-category');
    const prioEl = document.getElementById('arch-priority');
    if (catEl)  catEl.value  = content.category || 'General';
    if (prioEl) prioEl.value = content.priority  || 'normal';

    showArchContentFields('architecture');
    document.getElementById('task-modal').classList.add('active');
}

// ============================================
// Task History View
// ============================================

function viewTaskHistory(taskId) {
    const task = taskData[taskId];
    if (!task) return;

    const historyContainer = document.getElementById('history-content');
    let html = `<div class="form-group"><label class="form-label">Task Details</label>`;
    html += `<div style="font-size: 0.9em; color: #666;">`;
    html += `<strong>Title:</strong> ${task.title}<br>`;
    html += `<strong>Owner:</strong> ${task.owner || 'user'}<br>`;
    html += `<strong>Tags:</strong> ${task.tags ? task.tags.join(', ') : 'none'}<br>`;
    html += `<strong>Description:</strong> ${task.description || 'N/A'}`;
    html += `</div></div></div>`;

    html += `<div class="form-group"><label class="form-label">Proof of Work - Task Timeline</label>`;
    html += `<div class="timeline">`;

    task.history.forEach(h => {
        const date = new Date(h.timestamp);
        const formattedDate = date.toLocaleString();
        html += `
            <div class="timeline-item ${h.status}">
                <div class="timeline-status">${h.status}</div>
                ${h.message ? `<div class="timeline-message">${h.message}</div>` : ''}
                <div class="timeline-time">${formattedDate}</div>
            </div>
        `;
    });

    html += `</div></div>`;

    document.getElementById('history-modal-title').textContent = `Task: ${task.title}`;
    historyContainer.innerHTML = html;

    document.getElementById('history-modal').classList.add('active');
}

function closeHistoryModal() {
    document.getElementById('history-modal').classList.remove('active');
}

function updateTaskCount(status) {
    const container = document.getElementById(`tasks-${status}`);
    if (container) {
        const count = container.querySelectorAll('.task-card').length;
        const countElement = document.getElementById(`count-${status}`);
        if (countElement) {
            countElement.textContent = count;
        }
    }
}

// ============================================
// Drag and Drop — Ghost placeholder UX
// ============================================

/**
 * Return the direct droppable children of a .tasks-container: bare .task-card
 * elements and .task-card-group wrappers, excluding ghost placeholders.
 * Used by drag-start and drag-over to keep groups as atomic drag units.
 */
function _draggableChildren(container) {
    return [...container.children].filter(
        el => !el.classList.contains('drop-ghost') &&
              (el.classList.contains('task-card') || el.classList.contains('task-card-group'))
    );
}

function handleDragStart(e) {
    // If this .task-card lives inside a .task-card-group, drag the whole group
    // as a single unit so pip-card segments move with it.
    const group = this.closest('.task-card-group');
    draggedElement = group || this;
    // this may be a .task-card (data-id) or a .task-card-group (data-task-id)
    draggedTaskId = this.dataset.id || this.dataset.taskId;
    dragSourceContainer = draggedElement.closest('.tasks-container');

    // Capture original index among direct droppable children of the container
    const siblings = _draggableChildren(dragSourceContainer);
    draggedOriginalIndex = siblings.indexOf(draggedElement);

    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', draggedTaskId);

    // Defer adding .dragging by one tick so the browser captures the drag image
    // BEFORE opacity/pointer-events take effect.  Applying it synchronously inside
    // dragstart causes some browsers to treat the element as gone and cancel the
    // drag session immediately (symptom: dragend fires right after dragstart with
    // no dragover/drop events in between).
    const _dragTarget = draggedElement;
    setTimeout(() => {
        if (draggedElement) {
            _dragTarget.classList.add('dragging');
            // Collapse the element from layout so it doesn't skew sibling
            // midpoint calculations during dragover.  Done in JS too so
            // it works even if the CSS is cached.
            _dragTarget.style.height = '1px';
            _dragTarget.style.minHeight = '0';
            _dragTarget.style.padding = '0';
            _dragTarget.style.margin = '0';
            _dragTarget.style.border = 'none';
            _dragTarget.style.overflow = 'hidden';
            _dragTarget.style.opacity = '0';
        }
    }, 0);

    // Grouped drag: if this is a Big Idea, dim all descendants
    const taskObj = taskData[draggedTaskId] || {};
    if (taskObj.is_big_idea && descendantIndex[draggedTaskId]) {
        isDraggingGroup = true;
        dragGroupOldParentPos = taskObj.position || 0;
        dragGroupDescendants = [];
        const descendants = descendantIndex[draggedTaskId] || [];
        descendants.forEach(descId => {
            const descTask = taskData[descId];
            if (descTask) {
                dragGroupDescendants.push({
                    id: descId,
                    column: descTask.type === 'subdividing' ? 'idea' : descTask.type,
                    position: descTask.position || 0,
                    positionOffset: (descTask.position || 0) - (taskObj.position || 0),
                });
                const descCard = document.querySelector(`[data-id="${descId}"]`);
                if (descCard) descCard.classList.add('dragging-group');
            }
        });
    }

    // Create a single shared ghost placeholder (not appended yet — inserted into
    // the container DOM during dragover so surrounding cards are pushed apart by
    // normal block layout).
    insertIndicator = document.createElement('div');
    insertIndicator.className = 'drop-ghost';
    insertIndicator.setAttribute('aria-hidden', 'true');

    console.log(`Drag Start: card=${draggedTaskId}${isDraggingGroup ? ' (group)' : ''}`);
}

function handleDragEnd(e) {
    // draggedElement may be a .task-card or a .task-card-group
    const dragTarget = draggedElement || this;
    dragTarget.classList.remove('dragging');
    // Clear inline styles set during dragstart collapse
    dragTarget.style.height = '';
    dragTarget.style.minHeight = '';
    dragTarget.style.padding = '';
    dragTarget.style.margin = '';
    dragTarget.style.border = '';
    dragTarget.style.overflow = '';
    dragTarget.style.opacity = '';

    if (insertIndicator && insertIndicator.parentNode) {
        insertIndicator.parentNode.removeChild(insertIndicator);
    }
    insertIndicator = null;

    // Clean up group drag state
    if (isDraggingGroup) {
        document.querySelectorAll('.dragging-group').forEach(el => el.classList.remove('dragging-group'));
        isDraggingGroup = false;
        dragGroupDescendants = [];
        dragGroupOldParentPos = 0;
    }

    draggedElement = null;
    draggedTaskId = null;
    dragSourceContainer = null;
    draggedOriginalIndex = -1;
    currentInsertIndex = -1;
    currentInsertContainer = null;

    console.log('Drag End');
}

function handleContainerDragOver(e) {
    const container = this;

    // Block ghost + drop for invalid targets
    if (!dragSourceContainer || !isValidDropTarget(dragSourceContainer, container)) {
        e.dataTransfer.dropEffect = 'none';
        return;
    }

    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    // Exclude the dragging element and the ghost from midpoint geometry.
    // Use _draggableChildren so .task-card-group elements are treated as single units.
    const cards = _draggableChildren(container).filter(
        el => !el.classList.contains('dragging')
    );

    // Find insertion point: first card whose vertical midpoint is below the cursor
    let insertIndex = cards.length; // default: append at end
    for (let i = 0; i < cards.length; i++) {
        const rect = cards[i].getBoundingClientRect();
        const midpoint = rect.top + rect.height / 2;
        if (e.clientY < midpoint) {
            insertIndex = i;
            break;
        }
    }

    // Skip DOM manipulation if nothing has changed (avoids animation thrash)
    if (currentInsertContainer === container && currentInsertIndex === insertIndex) {
        return;
    }

    // "Same slot" means: inserting at insertIndex within the SAME container would leave the
    // card in its current position.  insertIndex is in the space of cards *excluding* the
    // dragged element, so the only true no-op is insertIndex === draggedOriginalIndex.
    const isSameContainer = container === dragSourceContainer;
    const isSameSlot = isSameContainer && insertIndex === draggedOriginalIndex;

    if (isSameSlot) {
        // Remove ghost if present
        if (insertIndicator && insertIndicator.parentNode) {
            insertIndicator.parentNode.removeChild(insertIndicator);
        }
        currentInsertIndex = insertIndex;
        currentInsertContainer = container;
        // Restore card opacity — "drop here = no move"
        if (draggedElement) draggedElement.style.opacity = '1';
        return;
    }

    // Different slot — show ghost, grey out card
    if (draggedElement) draggedElement.style.opacity = '0.4';

    // Remove ghost from wherever it currently lives
    if (insertIndicator && insertIndicator.parentNode) {
        insertIndicator.parentNode.removeChild(insertIndicator);
    }

    // Insert ghost into this container at the computed position
    if (insertIndex < cards.length) {
        container.insertBefore(insertIndicator, cards[insertIndex]);
    } else {
        container.appendChild(insertIndicator);
    }

    currentInsertIndex = insertIndex;
    currentInsertContainer = container;
}

function handleContainerDragLeave(e) {
    // Only act when the pointer genuinely leaves this container
    if (!this.contains(e.relatedTarget)) {
        if (insertIndicator && insertIndicator.parentNode === this) {
            this.removeChild(insertIndicator);
        }
        currentInsertIndex = -1;
        currentInsertContainer = null;
    }
}

async function handleContainerDrop(e) {
    e.preventDefault();

    // Reject drops on invalid targets
    if (!dragSourceContainer || !currentInsertContainer || !isValidDropTarget(dragSourceContainer, currentInsertContainer)) {
        return;
    }

    // Capture everything before any await
    const container = currentInsertContainer;
    const insertIndex = currentInsertIndex;
    const taskId = draggedTaskId;

    // Remove ghost immediately
    if (insertIndicator && insertIndicator.parentNode) {
        insertIndicator.parentNode.removeChild(insertIndicator);
    }

    if (!container || !taskId || insertIndex === -1) {
        console.log('Drop: missing state, aborting');
        return;
    }

    const columnType = container.id.replace('tasks-', '');
    console.log(`Drop: taskId=${taskId}, insertIndex=${insertIndex}, column=${columnType}`);

    // POST to API
    let newPosition = insertIndex;
    try {
        const response = await fetch(`${API_BASE}/tasks/${taskId}/reorder`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ position: newPosition, type: columnType })
        });

        if (response.ok) {
            // Grouped drag: update descendant positions
            if (isDraggingGroup && dragGroupDescendants.length > 0) {
                const positionDelta = newPosition - dragGroupOldParentPos;
                const moves = dragGroupDescendants.map(desc => ({
                    task_id: desc.id,
                    position: Math.max(0, desc.position + positionDelta),
                    type: desc.column,
                }));
                try {
                    await fetch(`${API_BASE}/tasks/batch-reorder`, {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ moves }),
                    });
                } catch (batchErr) {
                    console.error('Batch reorder error:', batchErr);
                }
            }

            // Re-fetch this column's tasks from the server to get authoritative positions
            const freshResponse = await fetch(`/api/projects/${encodeURIComponent(currentProject)}/tasks`);
            if (freshResponse.ok) {
                const freshTasks = await freshResponse.json();
                // Replace taskData entries with fresh server data
                freshTasks.forEach(task => { taskData[task.id] = task; });
                allTasks = freshTasks;
                buildDescendantIndex();
            }
            renderTasksFromDatabase();
        } else {
            console.error('Reorder failed:', response.status);
            renderTasksFromDatabase(); // restore visual state
        }
    } catch (err) {
        console.error('Reorder error:', err);
        renderTasksFromDatabase();
    }
}

// ============================================
// Wire up drag-and-drop listeners
// ============================================

function initializeDragAndDrop() {
    // Cards inside a .task-card-group have draggable on the group, not the card.
    document.querySelectorAll('.task-card').forEach(card => {
        if (card.closest('.task-card-group')) return;
        card.setAttribute('draggable', 'true');
        card.addEventListener('dragstart', handleDragStart);
        card.addEventListener('dragend', handleDragEnd);
    });

    document.querySelectorAll('.tasks-container').forEach(container => {
        container.addEventListener('dragover', handleContainerDragOver);
        container.addEventListener('dragleave', handleContainerDragLeave);
        container.addEventListener('drop', handleContainerDrop);
    });
}

// Initialize after DOM is ready
setTimeout(initializeDragAndDrop, 100);

// ============================================
// Global Config: LLM & Budget Management
// ============================================

function initializeGlobalConfigButtons() {
    document.getElementById('manage-llms-btn').addEventListener('click', openLlmModal);
    document.getElementById('manage-budgets-btn').addEventListener('click', openBudgetModal);
    document.getElementById('manage-tools-btn').addEventListener('click', openToolsModal);
    document.getElementById('manage-compute-nodes-btn').addEventListener('click', openComputeNodeModal);

    document.getElementById('llm-modal').addEventListener('click', function(e) {
        if (e.target === this && _modalMousedownTarget === this) closeLlmModal();
    });
    document.getElementById('budget-modal').addEventListener('click', function(e) {
        if (e.target === this && _modalMousedownTarget === this) closeBudgetModal();
    });
    document.getElementById('tools-modal').addEventListener('click', function(e) {
        if (e.target === this && _modalMousedownTarget === this) closeToolsModal();
    });
    document.getElementById('compute-node-modal').addEventListener('click', function(e) {
        if (e.target === this && _modalMousedownTarget === this) closeComputeNodeModal();
    });
}

function showInlineError(elementId, message, duration = 5000) {
    const el = document.getElementById(elementId);
    if (!el) return;
    el.textContent = message;
    el.style.display = 'block';
    // Re-trigger animation
    el.style.animation = 'none';
    el.offsetHeight; // reflow
    el.style.animation = '';
    clearTimeout(el._hideTimer);
    el._hideTimer = setTimeout(() => { el.style.display = 'none'; }, duration);
}

// --- LLM Modal ---

let _llmEditingId = null;  // Currently editing LLM id (null = add mode)

async function openLlmModal() {
    await loadLlmsAndBudgets();
    renderLlmList();
    populateComputeNodeSelect('llm-compute-node', null);
    populateComputeNodeSelect('llm-edit-compute-node', null);
    switchLlmTab('add');
    document.getElementById('llm-modal').classList.add('active');
}

function closeLlmModal() {
    document.getElementById('llm-modal').classList.remove('active');
    _llmEditingId = null;
}

function switchLlmTab(tab) {
    // Toggle tab buttons
    document.getElementById('llm-tab-add').classList.toggle('active', tab === 'add');
    document.getElementById('llm-tab-edit').classList.toggle('active', tab === 'edit');
    // Toggle panes
    document.getElementById('llm-pane-add').classList.toggle('active', tab === 'add');
    document.getElementById('llm-pane-edit').classList.toggle('active', tab === 'edit');
    // Update footer button
    const btn = document.getElementById('llm-submit-btn');
    if (tab === 'add') {
        btn.textContent = 'Add LLM Endpoint';
        btn.onclick = addLlm;
    } else {
        btn.textContent = 'Save Changes';
        btn.onclick = saveLlmEdit;
    }
}

function editLlmEntry(id) {
    const llm = allLlms.find(l => l.id === id);
    if (!llm) return;
    _llmEditingId = id;
    document.getElementById('llm-edit-id').value = id;
    document.getElementById('llm-edit-address').value = llm.address;
    document.getElementById('llm-edit-port').value = llm.port;
    document.getElementById('llm-edit-model').value = llm.model;
    document.getElementById('llm-edit-parallel').value = llm.parallel_sessions;
    document.getElementById('llm-edit-max-context').value = llm.max_context;
    document.getElementById('llm-edit-notes').value = llm.notes || '';
    document.getElementById('llm-edit-cost-prompt').value = llm.cost_per_million_prompt_tokens || 0;
    document.getElementById('llm-edit-cost-completion').value = llm.cost_per_million_completion_tokens || 0;
    populateComputeNodeSelect('llm-edit-compute-node', llm.compute_node_id || null);
    document.getElementById('llm-edit-placeholder').style.display = 'none';
    document.getElementById('llm-edit-form').style.display = 'block';
    document.getElementById('llm-edit-error').style.display = 'none';
    switchLlmTab('edit');
}

function renderLlmList() {
    const container = document.getElementById('llm-list');
    if (allLlms.length === 0) {
        container.innerHTML = '<p style="color:#6c757d;font-size:0.85rem">No LLM endpoints configured.</p>';
        return;
    }
    let html = '<table style="width:100%;font-size:0.85rem;border-collapse:collapse">';
    html += '<tr style="border-bottom:1px solid #dee2e6"><th style="text-align:left;padding:0.4rem">ID</th><th style="text-align:left;padding:0.4rem">Endpoint</th><th style="text-align:left;padding:0.4rem">Model</th><th style="text-align:left;padding:0.4rem">Sessions</th><th style="text-align:left;padding:0.4rem">Context</th><th></th></tr>';
    allLlms.forEach(l => {
        const ctx = l.max_context >= 1024 ? `${Math.round(l.max_context / 1024)}k` : l.max_context;
        html += `<tr style="border-bottom:1px solid #f0f0f0">
            <td style="padding:0.4rem">${l.id}</td>
            <td style="padding:0.4rem">${l.address}:${l.port}</td>
            <td style="padding:0.4rem"><a href="#" onclick="editLlmEntry(${l.id}); return false;" style="color:#0d6efd;text-decoration:none;cursor:pointer">${l.model}</a></td>
            <td style="padding:0.4rem">${l.parallel_sessions}</td>
            <td style="padding:0.4rem">${ctx}</td>
            <td style="padding:0.4rem"><button class="action-btn action-btn-danger" onclick="deleteLlmEntry(${l.id})">Delete</button></td>
        </tr>`;
    });
    html += '</table>';
    container.innerHTML = html;
}

function _validateLlmFields(prefix) {
    const address = document.getElementById(`${prefix}-address`).value.trim();
    const port = parseInt(document.getElementById(`${prefix}-port`).value) || 8008;
    const model = document.getElementById(`${prefix}-model`).value.trim();
    const parallelRaw = parseInt(document.getElementById(`${prefix}-parallel`).value);
    const contextRaw = parseInt(document.getElementById(`${prefix}-max-context`).value);
    const notes = (document.getElementById(`${prefix}-notes`) || {}).value || '';
    const errorId = prefix === 'llm' ? 'llm-error' : 'llm-edit-error';

    if (!address || !model) { showInlineError(errorId, 'Address and model are required.'); return null; }
    if (isNaN(parallelRaw) || parallelRaw < 1 || parallelRaw > 1024) {
        showInlineError(errorId, 'Parallel sessions must be between 1 and 1,024.');
        return null;
    }
    if (isNaN(contextRaw) || contextRaw < 1) {
        showInlineError(errorId, 'Max context must be a non-zero number.');
        return null;
    }
    const costPrompt = parseFloat(document.getElementById(`${prefix}-cost-prompt`)?.value) || 0;
    const costCompletion = parseFloat(document.getElementById(`${prefix}-cost-completion`)?.value) || 0;
    const cnRaw = document.getElementById(`${prefix}-compute-node`)?.value;
    const compute_node_id = cnRaw ? parseInt(cnRaw) : null;
    return { address, port, model, parallel_sessions: parallelRaw, max_context: contextRaw, notes,
             cost_per_million_prompt_tokens: costPrompt,
             cost_per_million_completion_tokens: costCompletion,
             compute_node_id };
}

async function addLlm() {
    const data = _validateLlmFields('llm');
    if (!data) return;

    const res = await fetch(`${API_BASE}/llms`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data)
    });
    if (!res.ok) {
        const err = await res.json();
        showInlineError('llm-error', err.detail || 'Failed to create LLM.');
        return;
    }
    document.getElementById('llm-model').value = '';
    document.getElementById('llm-parallel').value = '1';
    document.getElementById('llm-max-context').value = '4096';
    document.getElementById('llm-notes').value = '';
    await loadLlmsAndBudgets();
    renderLlmList();
}

async function saveLlmEdit() {
    if (!_llmEditingId) return;
    const data = _validateLlmFields('llm-edit');
    if (!data) return;

    const res = await fetch(`${API_BASE}/llms/${_llmEditingId}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data)
    });
    if (!res.ok) {
        const err = await res.json();
        showInlineError('llm-edit-error', err.detail || 'Failed to update LLM.');
        return;
    }
    await loadLlmsAndBudgets();
    renderLlmList();
    // Stay on edit tab, refresh the form with updated data
    editLlmEntry(_llmEditingId);
}

async function deleteLlmEntry(id) {
    if (!await showConfirm('Delete LLM Endpoint', 'Delete this LLM endpoint?', 'Delete')) return;
    await fetch(`${API_BASE}/llms/${id}`, { method: 'DELETE' });
    // If we were editing this one, reset the edit pane
    if (_llmEditingId === id) {
        _llmEditingId = null;
        document.getElementById('llm-edit-form').style.display = 'none';
        document.getElementById('llm-edit-placeholder').style.display = 'block';
        switchLlmTab('add');
    }
    await loadLlmsAndBudgets();
    renderLlmList();
}

// --- Budget Modal ---

let _budgetEditingId = null;  // Currently editing budget id (null = add mode)

async function openBudgetModal() {
    await loadLlmsAndBudgets();
    renderBudgetList();
    switchBudgetTab('add');
    document.getElementById('budget-modal').classList.add('active');
}

function closeBudgetModal() {
    document.getElementById('budget-modal').classList.remove('active');
    _budgetEditingId = null;
}

function switchBudgetTab(tab) {
    document.getElementById('budget-tab-add').classList.toggle('active', tab === 'add');
    document.getElementById('budget-tab-edit').classList.toggle('active', tab === 'edit');
    document.getElementById('budget-pane-add').classList.toggle('active', tab === 'add');
    document.getElementById('budget-pane-edit').classList.toggle('active', tab === 'edit');
    const btn = document.getElementById('budget-submit-btn');
    if (tab === 'add') {
        btn.textContent = 'Add Budget';
        btn.onclick = addBudget;
    } else {
        btn.textContent = 'Save Changes';
        btn.onclick = saveBudgetEdit;
    }
}

function editBudgetEntry(id) {
    const budget = allBudgets.find(b => b.id === id);
    if (!budget) return;
    _budgetEditingId = id;
    document.getElementById('budget-edit-id').value = id;
    document.getElementById('budget-edit-name').value = budget.name;
    document.getElementById('budget-edit-dollar-amount').value = budget.dollar_amount ?? -1;
    document.getElementById('budget-edit-placeholder').style.display = 'none';
    document.getElementById('budget-edit-form').style.display = 'block';
    document.getElementById('budget-edit-error').style.display = 'none';
    switchBudgetTab('edit');
    // Fetch usage summary and remaining
    loadBudgetSummary(id);
    loadBudgetRemaining(id);
}

async function loadBudgetSummary(budgetId) {
    const el = document.getElementById('budget-summary-content');
    el.textContent = 'Loading...';
    try {
        const res = await fetch(`${API_BASE}/budgets/${budgetId}/summary`);
        if (!res.ok) { el.textContent = 'Failed to load summary.'; return; }
        const s = res.json ? await res.json() : {};
        const totalTokens = (s.total_prompt_tokens || 0) + (s.total_generation_tokens || 0);
        const totalDisplay = totalTokens >= 1024 ? `${Math.round(totalTokens / 1024)}k` : totalTokens;
        const promptDisplay = (s.total_prompt_tokens || 0) >= 1024 ? `${Math.round(s.total_prompt_tokens / 1024)}k` : (s.total_prompt_tokens || 0);
        const genDisplay = (s.total_generation_tokens || 0) >= 1024 ? `${Math.round(s.total_generation_tokens / 1024)}k` : (s.total_generation_tokens || 0);
        el.innerHTML = `
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:0.25rem 1rem">
                <span>LLM Calls:</span><span><strong>${s.total_entries || 0}</strong></span>
                <span>Prompt Tokens:</span><span><strong>${promptDisplay}</strong></span>
                <span>Generation Tokens:</span><span><strong>${genDisplay}</strong></span>
                <span>Total Tokens:</span><span><strong>${totalDisplay}</strong></span>
                <span>Tool Calls:</span><span><strong>${s.total_tool_calls || 0}</strong></span>
            </div>`;
    } catch (e) {
        el.textContent = 'Error loading summary.';
    }
}

async function loadBudgetRemaining(budgetId) {
    const box = document.getElementById('budget-remaining-box');
    const txt = document.getElementById('budget-remaining-text');
    box.style.display = 'none';
    try {
        const res = await fetch(`${API_BASE}/budgets/${budgetId}/remaining`);
        if (!res.ok) return;
        const r = await res.json();
        if (r.infinite) {
            box.style.display = 'none';
            return;
        }
        const spent = r.spent_dollars ? `$${r.spent_dollars.toFixed(4)}` : '$0.00';
        const limit = `$${Number(r.dollar_amount).toFixed(2)}`;
        const remaining = r.remaining_dollars != null ? `$${r.remaining_dollars.toFixed(4)}` : '—';
        txt.textContent = `Spent: ${spent} of ${limit} limit — Remaining: ${remaining}`;
        box.style.display = 'block';
    } catch (_) {}
}

function renderBudgetList() {
    const container = document.getElementById('budget-list');
    if (allBudgets.length === 0) {
        container.innerHTML = '<p style="color:#6c757d;font-size:0.85rem">No budgets configured.</p>';
        return;
    }
    let html = '<table style="width:100%;font-size:0.85rem;border-collapse:collapse">';
    html += '<tr style="border-bottom:1px solid #dee2e6"><th style="text-align:left;padding:0.4rem">ID</th><th style="text-align:left;padding:0.4rem">Name</th><th style="text-align:left;padding:0.4rem">Limit</th><th></th></tr>';
    allBudgets.forEach(b => {
        const limitLabel = (b.dollar_amount === -1 || b.dollar_amount == null) ? '∞' : `$${Number(b.dollar_amount).toFixed(2)}`;
        html += `<tr style="border-bottom:1px solid #f0f0f0">
            <td style="padding:0.4rem">${b.id}</td>
            <td style="padding:0.4rem"><a href="#" onclick="editBudgetEntry(${b.id}); return false;" style="color:#0d6efd;text-decoration:none;cursor:pointer">${b.name}</a></td>
            <td style="padding:0.4rem">${limitLabel}</td>
            <td style="padding:0.4rem"><button class="action-btn action-btn-danger" onclick="deleteBudgetEntry(${b.id})">Delete</button></td>
        </tr>`;
    });
    html += '</table>';
    container.innerHTML = html;
}

async function addBudget() {
    const name = document.getElementById('budget-name').value.trim();
    if (!name) { showInlineError('budget-error', 'Budget name is required.'); return; }
    const dollarAmount = parseFloat(document.getElementById('budget-dollar-amount').value);
    const dollar_amount = isNaN(dollarAmount) ? -1 : dollarAmount;

    const res = await fetch(`${API_BASE}/budgets`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, dollar_amount })
    });
    if (!res.ok) {
        const err = await res.json();
        showInlineError('budget-error', err.detail || 'Failed to create budget.');
        return;
    }
    document.getElementById('budget-name').value = '';
    document.getElementById('budget-dollar-amount').value = '-1';
    await loadLlmsAndBudgets();
    renderBudgetList();
}

async function saveBudgetEdit() {
    if (!_budgetEditingId) return;
    const name = document.getElementById('budget-edit-name').value.trim();
    if (!name) { showInlineError('budget-edit-error', 'Budget name is required.'); return; }

    const dollarAmountRaw = parseFloat(document.getElementById('budget-edit-dollar-amount').value);
    const dollar_amount = isNaN(dollarAmountRaw) ? -1 : dollarAmountRaw;

    const res = await fetch(`${API_BASE}/budgets/${_budgetEditingId}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, dollar_amount })
    });
    if (!res.ok) {
        const err = await res.json();
        showInlineError('budget-edit-error', err.detail || 'Failed to update budget.');
        return;
    }
    await loadLlmsAndBudgets();
    renderBudgetList();
    editBudgetEntry(_budgetEditingId);
}

async function deleteBudgetEntry(id) {
    if (!await showConfirm('Delete Budget', 'Delete this budget?', 'Delete')) return;
    await fetch(`${API_BASE}/budgets/${id}`, { method: 'DELETE' });
    if (_budgetEditingId === id) {
        _budgetEditingId = null;
        document.getElementById('budget-edit-form').style.display = 'none';
        document.getElementById('budget-edit-placeholder').style.display = 'block';
        switchBudgetTab('add');
    }
    await loadLlmsAndBudgets();
    renderBudgetList();
}


// ============================================
// Agent Tools Modal
// ============================================

let _toolsData = null;       // Cached response from /api/agent/tools
let _toolsFilterAgent = null; // Currently selected agent filter (null = show all)

// Tool categories inferred from name prefixes
function _toolCategory(name) {
    if (name.startsWith('git_'))    return 'git';
    if (name.startsWith('read_') || name.startsWith('write_') || name.startsWith('append_') ||
        name === 'count_lines' || name === 'list_directory' || name === 'archive_file') return 'file';
    if (name === 'search_files' || name === 'find_files') return 'search';
    if (name === 'run_shell')       return 'shell';
    if (name.startsWith('get_task') || name.startsWith('list_task') ||
        name.startsWith('update_task') || name.startsWith('append_task')) return 'task';
    return 'other';
}

const _CATEGORY_LABELS = {
    file: 'File I/O', search: 'Search', git: 'Git', shell: 'Execution', task: 'Kanban', other: 'Other'
};

const _CATEGORY_ORDER = ['file', 'search', 'git', 'shell', 'task', 'other'];

// --- Compute Node Modal ---

let _cnEditingId = null;  // Currently editing compute node id (null = add mode)

async function openComputeNodeModal() {
    await loadLlmsAndBudgets();
    renderComputeNodeList();
    switchComputeNodeTab('add');
    document.getElementById('compute-node-modal').classList.add('active');
}

function closeComputeNodeModal() {
    document.getElementById('compute-node-modal').classList.remove('active');
    _cnEditingId = null;
}

function switchComputeNodeTab(tab) {
    document.getElementById('cn-tab-add').classList.toggle('active', tab === 'add');
    document.getElementById('cn-tab-edit').classList.toggle('active', tab === 'edit');
    document.getElementById('cn-pane-add').classList.toggle('active', tab === 'add');
    document.getElementById('cn-pane-edit').classList.toggle('active', tab === 'edit');
    const btn = document.getElementById('cn-submit-btn');
    if (tab === 'add') {
        btn.textContent = 'Add Compute Node';
        btn.onclick = addComputeNode;
    } else {
        btn.textContent = 'Save Changes';
        btn.onclick = saveComputeNodeEdit;
    }
}

function editComputeNodeEntry(id) {
    const node = allComputeNodes.find(n => n.id === id);
    if (!node) return;
    _cnEditingId = id;
    document.getElementById('cn-edit-id').value = id;
    document.getElementById('cn-edit-name').value = node.name;
    document.getElementById('cn-edit-description').value = node.description || '';
    document.getElementById('cn-edit-max-sessions').value = node.max_parallel_sessions;
    document.getElementById('cn-edit-max-loaded-models').value = node.max_loaded_models;
    document.getElementById('cn-edit-placeholder').style.display = 'none';
    document.getElementById('cn-edit-form').style.display = 'block';
    document.getElementById('cn-edit-error').style.display = 'none';
    switchComputeNodeTab('edit');
}

function renderComputeNodeList() {
    const container = document.getElementById('compute-node-list');
    if (allComputeNodes.length === 0) {
        container.innerHTML = '<p style="color:#6c757d;font-size:0.85rem">No compute nodes configured.</p>';
        return;
    }
    let html = '<table style="width:100%;font-size:0.85rem;border-collapse:collapse">';
    html += '<tr style="border-bottom:1px solid #dee2e6"><th style="text-align:left;padding:0.4rem">ID</th><th style="text-align:left;padding:0.4rem">Name</th><th style="text-align:left;padding:0.4rem">Sessions</th><th style="text-align:left;padding:0.4rem">Models</th><th style="text-align:left;padding:0.4rem">Description</th><th></th></tr>';
    allComputeNodes.forEach(n => {
        html += `<tr style="border-bottom:1px solid #f0f0f0">
            <td style="padding:0.4rem">${n.id}</td>
            <td style="padding:0.4rem"><a href="#" onclick="editComputeNodeEntry(${n.id}); return false;" style="color:#0d6efd;text-decoration:none;cursor:pointer">${escapeHtml(n.name)}</a></td>
            <td style="padding:0.4rem">${n.max_parallel_sessions}</td>
            <td style="padding:0.4rem">${n.max_loaded_models}</td>
            <td style="padding:0.4rem;color:#6c757d">${escapeHtml(n.description || '')}</td>
            <td style="padding:0.4rem"><button class="action-btn action-btn-danger" onclick="deleteComputeNodeEntry(${n.id})">Delete</button></td>
        </tr>`;
    });
    html += '</table>';
    container.innerHTML = html;
}

async function addComputeNode() {
    const name = document.getElementById('cn-name').value.trim();
    const description = document.getElementById('cn-description').value.trim();
    const mps = parseInt(document.getElementById('cn-max-sessions').value) || 1;
    const mlm = parseInt(document.getElementById('cn-max-loaded-models').value) || 1;
    if (!name) { showInlineError('cn-error', 'Name is required.'); return; }
    if (mps < 1) { showInlineError('cn-error', 'Max sessions must be >= 1.'); return; }
    if (mlm < 1) { showInlineError('cn-error', 'Max loaded models must be >= 1.'); return; }

    const res = await fetch(`${API_BASE}/compute-nodes`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, description: description || null, max_parallel_sessions: mps, max_loaded_models: mlm })
    });
    if (!res.ok) {
        const err = await res.json();
        showInlineError('cn-error', err.detail || 'Failed to create compute node.');
        return;
    }
    document.getElementById('cn-name').value = '';
    document.getElementById('cn-description').value = '';
    document.getElementById('cn-max-sessions').value = '1';
    document.getElementById('cn-max-loaded-models').value = '1';
    await loadLlmsAndBudgets();
    renderComputeNodeList();
}

async function saveComputeNodeEdit() {
    if (!_cnEditingId) return;
    const name = document.getElementById('cn-edit-name').value.trim();
    const description = document.getElementById('cn-edit-description').value.trim();
    const mps = parseInt(document.getElementById('cn-edit-max-sessions').value) || 1;
    const mlm = parseInt(document.getElementById('cn-edit-max-loaded-models').value) || 1;
    if (!name) { showInlineError('cn-edit-error', 'Name is required.'); return; }

    const res = await fetch(`${API_BASE}/compute-nodes/${_cnEditingId}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, description: description || null, max_parallel_sessions: mps, max_loaded_models: mlm })
    });
    if (!res.ok) {
        const err = await res.json();
        showInlineError('cn-edit-error', err.detail || 'Failed to update compute node.');
        return;
    }
    await loadLlmsAndBudgets();
    renderComputeNodeList();
    editComputeNodeEntry(_cnEditingId);
}

async function deleteComputeNodeEntry(id) {
    if (!await showConfirm('Delete Compute Node', 'Delete this compute node? LLM endpoints assigned to it will become unassigned.', 'Delete')) return;
    await fetch(`${API_BASE}/compute-nodes/${id}`, { method: 'DELETE' });
    if (_cnEditingId === id) {
        _cnEditingId = null;
        document.getElementById('cn-edit-form').style.display = 'none';
        document.getElementById('cn-edit-placeholder').style.display = 'block';
        switchComputeNodeTab('add');
    }
    await loadLlmsAndBudgets();
    renderComputeNodeList();
}

async function openToolsModal() {
    document.getElementById('tools-modal').classList.add('active');
    if (!_toolsData) {
        document.getElementById('tools-card-container').innerHTML = '<em>Loading tools...</em>';
        try {
            const res = await fetch(`${API_BASE}/agent/tools`);
            _toolsData = await res.json();
        } catch (err) {
            document.getElementById('tools-card-container').innerHTML = `<em>Error loading tools: ${err}</em>`;
            return;
        }
    }
    _toolsFilterAgent = null;
    renderToolsAgentTree();
    renderToolCards();
}

function closeToolsModal() {
    document.getElementById('tools-modal').classList.remove('active');
}

function renderToolsAgentTree() {
    const container = document.getElementById('tools-agent-tree');
    const access = _toolsData.agent_access;

    let html = '';
    for (const [agentName, info] of Object.entries(access)) {
        const count = info.tools.length;
        const isActive = _toolsFilterAgent === agentName;
        const toolLabel = count === 0 ? 'No direct tools' : `${count} tool${count !== 1 ? 's' : ''}`;
        html += `
            <div class="tools-agent-node${isActive ? ' active' : ''}"
                 onclick="filterToolsByAgent('${agentName}')">
                <div class="tools-agent-name">${agentName}</div>
                <div class="tools-agent-desc">${_escapeHtml(info.description)}</div>
                <div class="tools-agent-count">${toolLabel}</div>
            </div>
        `;
    }
    container.innerHTML = html;
}

function filterToolsByAgent(agentName) {
    if (_toolsFilterAgent === agentName) {
        _toolsFilterAgent = null;  // Toggle off
    } else {
        _toolsFilterAgent = agentName;
    }
    renderToolsAgentTree();
    renderToolCards();
}

function _escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
}

function renderToolCards() {
    const container = document.getElementById('tools-card-container');
    const schemas = _toolsData.tool_schemas;
    const access = _toolsData.agent_access;

    // Build a set of tool names to show (filtered by agent or all)
    let visibleTools = null;
    if (_toolsFilterAgent) {
        const agentTools = access[_toolsFilterAgent]?.tools || [];
        if (agentTools.length === 0) {
            container.innerHTML = `<em style="color:#6c757d">${_toolsFilterAgent} does not dispatch tools directly — it uses structured LLM prompts.</em>`;
            return;
        }
        visibleTools = new Set(agentTools);
    }

    // Build reverse map: tool name -> list of agent names that have it
    const toolAgents = {};
    for (const [agentName, info] of Object.entries(access)) {
        for (const t of info.tools) {
            if (!toolAgents[t]) toolAgents[t] = [];
            toolAgents[t].push(agentName);
        }
    }

    // Group schemas by category
    const grouped = {};
    for (const schema of schemas) {
        const name = schema.function.name;
        if (visibleTools && !visibleTools.has(name)) continue;
        const cat = _toolCategory(name);
        if (!grouped[cat]) grouped[cat] = [];
        grouped[cat].push(schema);
    }

    let html = '';
    for (const cat of _CATEGORY_ORDER) {
        const tools = grouped[cat];
        if (!tools || tools.length === 0) continue;
        html += `<div style="font-size:0.78rem;font-weight:600;color:#6c757d;text-transform:uppercase;letter-spacing:0.5px;margin:0.75rem 0 0.35rem;padding-left:0.25rem">${_CATEGORY_LABELS[cat] || cat}</div>`;
        for (const schema of tools) {
            html += _renderToolCard(schema, toolAgents);
        }
    }

    container.innerHTML = html || '<em>No tools to display.</em>';
}

function _renderToolCard(schema, toolAgents) {
    const fn = schema.function;
    const name = fn.name;
    const desc = fn.description || '';
    const params = fn.parameters?.properties || {};
    const required = new Set(fn.parameters?.required || []);
    const agents = toolAgents[name] || [];

    // Agent badges
    let badges = '';
    for (const a of agents) {
        const cls = a === 'MaestroLoop' ? 'maestro' : 'research';
        badges += `<span class="tool-card-badge ${cls}">${a}</span>`;
    }

    // Parameter list
    let paramHtml = '';
    if (Object.keys(params).length > 0) {
        paramHtml = '<ul class="tool-card-params">';
        for (const [pName, pDef] of Object.entries(params)) {
            const type = pDef.type || 'any';
            const isReq = required.has(pName);
            const pDesc = pDef.description || '';
            const defVal = pDef.default !== undefined ? ` = ${JSON.stringify(pDef.default)}` : '';
            paramHtml += `
                <li>
                    <span class="tool-param-name">${pName}</span>
                    <span class="tool-param-type">${type}${defVal}</span>
                    ${isReq ? '<span class="tool-param-required">required</span>' : ''}
                    ${pDesc ? `<div class="tool-param-desc">${_escapeHtml(pDesc)}</div>` : ''}
                </li>`;
        }
        paramHtml += '</ul>';
    }

    // The "prompt injection" — the description string the LLM sees
    const promptBlock = `<div class="tool-card-prompt-label">LLM Prompt (what the model sees)</div>
        <div class="tool-card-prompt">${_escapeHtml(JSON.stringify(schema, null, 2))}</div>`;

    return `
        <div class="tool-card" id="tool-card-${name}">
            <div class="tool-card-header" onclick="toggleToolCard('${name}')">
                <div style="display:flex;align-items:center;gap:0.5rem;flex-wrap:wrap">
                    <span class="tool-card-name">${name}</span>
                    <div class="tool-card-badges">${badges}</div>
                </div>
                <span class="tool-card-chevron">&#9654;</span>
            </div>
            <div class="tool-card-body">
                <div class="tool-card-desc">${_escapeHtml(desc)}</div>
                ${paramHtml}
                ${promptBlock}
            </div>
        </div>`;
}

function toggleToolCard(name) {
    const card = document.getElementById(`tool-card-${name}`);
    if (card) card.classList.toggle('expanded');
}

// ============================================
// Column Map View — 2D radial layout
// ============================================

let columnMapActive = false;
let columnMapType = null;
let mapTransform = { x: 0, y: 0, scale: 1 };
let mapDragState = { dragging: false, startX: 0, startY: 0, originX: 0, originY: 0 };

// Shared state for the currently-open map — populated by renderColumnMap,
// read by _mapRedrawArrows and the node-drag handlers.
let _mapCurrentEdges = [];
let _mapCurrentNodePositions = {};  // nodeId → {x, y} in layout coords
let _mapCurrentColor = '#6c757d';
let _mapOffsetX = 0;   // canvas = layout + offset
let _mapOffsetY = 0;
const _MAP_CARD_W = 230;
const _MAP_CARD_H = 130;
const _MAP_PIP_CHIP_H = 44;  // height of each PIP chip below a map node

// Node-drag state
let _mapNodeDrag = {
    active: false,
    nodeId: null,                  // the node the user grabbed
    startMouseCanvas: { x: 0, y: 0 },
    groupIds: [],                  // dragged node + all its descendants
    groupStartLayout: {},          // nodeId → {x,y} layout coords at drag start
};

const MAP_COLORS = {
    architecture:      '#6f42c1',
    idea:              '#17a2b8',
    planning:          '#ffc107',
    indev:             '#0d6efd',
    conceptual_review: '#20c997',
    optimization:      '#6610f2',
    security:          '#e83e8c',
    full_review:       '#fd7e14',
    completed:         '#198754',
    subdividing:       '#6f42c1',
};

const MAP_COLUMN_LABELS = {
    architecture:      'ARCHITECTURE MAP',
    idea:              'IDEAS MAP',
    planning:          'PLANNING MAP',
    indev:             'IN DEVELOPMENT MAP',
    conceptual_review: 'CONCEPTUAL REVIEW MAP',
    optimization:      'OPTIMIZATION MAP',
    security:          'SECURITY MAP',
    full_review:       'FINAL REVIEW MAP',
    completed:         'COMPLETED MAP',
};

// Called when user clicks a column header or its whitespace.
// Skips if the click landed on a card/button to avoid accidental triggers.
function handleColumnClick(e, colType) {
    if (e.target.closest('.task-card, button, .add-task-btn, a, input, select, textarea')) return;
    openColumnMap(colType);
}

// Called when user clicks inside a tasks-container (below/around cards).
function handleTasksContainerClick(e, colType) {
    if (e.target.closest('.task-card, button, .add-task-btn, a, input, select, textarea')) return;
    openColumnMap(colType);
}

function openColumnMap(colType, focusNodeId) {
    columnMapActive = true;
    columnMapType = colType;
    mapTransform = { x: 0, y: 0, scale: 1 };

    document.querySelector('.kanban-board').style.display = 'none';
    const container = document.getElementById('column-map-container');
    container.style.display = 'flex';

    const label = MAP_COLUMN_LABELS[colType] || (colType.toUpperCase() + ' MAP');
    document.getElementById('column-map-title').textContent = label;

    renderColumnMap(colType);
    setupMapInteraction();

    // If a specific node was requested, scroll/pan to it and pulse-highlight it
    if (focusNodeId) {
        setTimeout(() => _mapFocusNode(focusNodeId), 80);
    }
}

function _mapFocusNode(nodeId) {
    const node = document.getElementById(`map-node-${nodeId}`);
    if (!node) return;

    // Read the node's layout position from shared state
    const pos = _mapCurrentNodePositions[nodeId];
    if (pos) {
        // Center the viewport on this node
        const wrap = document.getElementById('column-map-scroll-wrap');
        const cx = wrap.clientWidth  / 2;
        const cy = wrap.clientHeight / 2;
        mapTransform.x = cx - pos.cx;
        mapTransform.y = cy - pos.cy;
        const canvas = document.getElementById('column-map-canvas');
        canvas.style.transform = `translate(${mapTransform.x}px,${mapTransform.y}px) scale(${mapTransform.scale})`;
        _mapRedrawArrows();
    }

    // Pulse-highlight the node
    node.classList.add('map-node-focus');
    setTimeout(() => node.classList.remove('map-node-focus'), 2000);
}

function closeColumnMap() {
    columnMapActive = false;
    columnMapType = null;
    teardownMapInteraction();
    document.getElementById('column-map-container').style.display = 'none';
    document.querySelector('.kanban-board').style.display = 'flex';
}

function _mapGetTasksForColumn(colType) {
    return allTasks.filter(t => {
        if (!t || !t.type) return false;
        if (t.type === 'cancelled') return false;
        if (colType === 'idea') return t.type === 'idea' || t.type === 'subdividing';
        return t.type === colType;
    });
}

// Returns { nodes: [{id, x, y, task, newlyPositioned}], edges: [{fromId, toId}] }
//
// Three-phase layout:
//   Phase 1 — load saved map_x / map_y from task data (skip recomputing these)
//   Phase 2 — BFS fan-out: newly-subdivided children of positioned parents get
//              radial positions derived from their parent (handles new sub-ideas)
//   Phase 3 — standard radial subtree layout for anything completely unpositioned
//              (brand-new ideas with no parent yet saved on the board)
function _mapComputeLayout(tasks, colType) {
    const RADII = [320, 240, 180, 140];

    const taskMap = {};
    tasks.forEach(t => { taskMap[t.id] = t; });

    // Build parent→children map and edge list
    const edges = [];
    const childrenOf = {};

    if (colType === 'idea' || colType === 'architecture') {
        tasks.forEach(t => {
            if (t.parent_task_id && taskMap[t.parent_task_id]) {
                edges.push({ fromId: t.parent_task_id, toId: t.id });
                (childrenOf[t.parent_task_id] = childrenOf[t.parent_task_id] || []).push(t.id);
            }
        });
    } else {
        tasks.forEach(t => {
            (t.prerequisites || []).forEach(prereqId => {
                if (taskMap[prereqId]) {
                    edges.push({ fromId: prereqId, toId: t.id });
                    (childrenOf[prereqId] = childrenOf[prereqId] || []).push(t.id);
                }
            });
        });
    }

    // ── Phase 1: load saved positions ──────────────────────────────────────────
    const nodePositions = {};
    tasks.forEach(t => {
        if (t.map_x != null && t.map_y != null) {
            nodePositions[t.id] = { x: t.map_x, y: t.map_y };
        }
    });

    // ── Phase 2: BFS fan-out for new children of positioned parents ────────────
    // Handles the subdivision case: parent already saved, children are new (null coords)
    const bfsQueue = tasks.filter(t => nodePositions[t.id]).map(t => t.id);
    const bfsVisited = new Set(bfsQueue);
    while (bfsQueue.length > 0) {
        const parentId = bfsQueue.shift();
        const parentPos = nodePositions[parentId];
        const unplacedKids = (childrenOf[parentId] || []).filter(k => !nodePositions[k]);
        if (unplacedKids.length === 0) continue;

        const r = RADII[0];
        const span = unplacedKids.length === 1 ? 0 : Math.PI * 1.5;
        unplacedKids.forEach((kidId, i) => {
            const angle = unplacedKids.length === 1
                ? Math.PI / 2
                : Math.PI / 4 + (i / (unplacedKids.length - 1)) * span;
            nodePositions[kidId] = {
                x: parentPos.x + r * Math.cos(angle),
                y: parentPos.y + r * Math.sin(angle),
            };
            if (!bfsVisited.has(kidId)) { bfsVisited.add(kidId); bfsQueue.push(kidId); }
        });
    }

    // ── Phase 3: standard radial layout for completely-unpositioned subtrees ───
    const hasParentInColumn = new Set(edges.map(e => e.toId));
    const unpositionedRoots = tasks.filter(t => !nodePositions[t.id] && !hasParentInColumn.has(t.id));

    if (unpositionedRoots.length > 0) {
        function placeSubtree(nodeId, cx, cy, depth, centerAngle, arcSpan) {
            nodePositions[nodeId] = { x: cx, y: cy };
            const kids = (childrenOf[nodeId] || []).filter(k => !nodePositions[k]);
            if (kids.length === 0) return;
            const r = RADII[Math.min(depth, RADII.length - 1)];
            const span = Math.min(arcSpan, Math.PI * 1.75);
            kids.forEach((kidId, i) => {
                const angle = kids.length === 1
                    ? centerAngle
                    : centerAngle - span / 2 + (i / (kids.length - 1)) * span;
                const subSpan = span / Math.max(1, kids.length);
                placeSubtree(kidId, cx + r * Math.cos(angle), cy + r * Math.sin(angle),
                             depth + 1, angle, subSpan);
            });
        }

        const ROOT_SPACING = Math.max(700, RADII[0] * 2.4);
        const totalW = (unpositionedRoots.length - 1) * ROOT_SPACING;
        unpositionedRoots.forEach((root, i) => {
            const arcSpan = unpositionedRoots.length === 1 ? 2 * Math.PI : Math.PI * 1.3;
            placeSubtree(root.id, -totalW / 2 + i * ROOT_SPACING, 0, 0, Math.PI / 2, arcSpan);
        });
    }

    // ── Fallback: any orphan tasks still unpositioned (shouldn't normally happen)
    let isoX = -(tasks.length * 260) / 2;
    const isoY = -(RADII[0] + 260);
    tasks.forEach(t => {
        if (!nodePositions[t.id]) {
            nodePositions[t.id] = { x: isoX, y: isoY };
            isoX += 260;
        }
    });

    const nodes = tasks.map(t => ({
        id: t.id,
        x: nodePositions[t.id].x,
        y: nodePositions[t.id].y,
        task: t,
        newlyPositioned: t.map_x == null || t.map_y == null,  // flag: needs saving
    }));

    return { nodes, edges };
}

// Batch-save newly computed map positions to the database.
// Fire-and-forget — failures are logged but don't block the UI.
async function _mapSavePositions(toSave) {
    if (!toSave || toSave.length === 0) return;
    try {
        await fetch(`${API_BASE}/tasks/map-positions`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(toSave),
        });
        // Mirror into live taskData so the next reconcile sees them as saved
        toSave.forEach(u => {
            if (taskData[u.id]) {
                taskData[u.id].map_x = u.map_x;
                taskData[u.id].map_y = u.map_y;
            }
        });
    } catch (e) {
        console.warn('[ColumnMap] Failed to save positions:', e);
    }
}

function renderColumnMap(colType) {
    const tasks = _mapGetTasksForColumn(colType);

    const svg       = document.getElementById('column-map-svg');
    const nodesEl   = document.getElementById('column-map-nodes');
    const canvas    = document.getElementById('column-map-canvas');
    const scrollWrap = document.getElementById('column-map-scroll-wrap');

    svg.innerHTML     = '';
    nodesEl.innerHTML = '';
    // Clear any previous empty-state message
    const prevEmpty = scrollWrap.querySelector('.map-empty-msg');
    if (prevEmpty) prevEmpty.remove();

    if (tasks.length === 0) {
        const emptyMsg = document.createElement('div');
        emptyMsg.className = 'map-empty-msg';
        emptyMsg.textContent = 'No tasks in this column';
        scrollWrap.appendChild(emptyMsg);
        canvas.style.width  = '100%';
        canvas.style.height = '100%';
        applyMapTransform();
        return;
    }

    const { nodes, edges } = _mapComputeLayout(tasks, colType);

    const PAD = 120;

    // Compute bounding box → offset everything into positive canvas space
    const xs = nodes.map(n => n.x);
    const ys = nodes.map(n => n.y);
    const minX = Math.min(...xs);
    const minY = Math.min(...ys);
    const maxX = Math.max(...xs) + _MAP_CARD_W;
    // Account for PIP chip stacks that extend below the base card height
    const maxY = nodes.reduce((acc, n) => {
        const pipCount = (n.task && n.task.pips) ? n.task.pips.length : 0;
        return Math.max(acc, n.y + _MAP_CARD_H + pipCount * _MAP_PIP_CHIP_H);
    }, Math.max(...ys) + _MAP_CARD_H);

    const W  = maxX - minX + PAD * 2;
    const H  = maxY - minY + PAD * 2;
    const OX = -minX + PAD;
    const OY = -minY + PAD;

    canvas.style.width  = W + 'px';
    canvas.style.height = H + 'px';
    svg.setAttribute('width',  W);
    svg.setAttribute('height', H);

    // Store shared map state (used by _mapRedrawArrows and drag handlers)
    _mapCurrentEdges = edges;
    _mapCurrentColor = MAP_COLORS[colType] || '#6c757d';
    _mapOffsetX = OX;
    _mapOffsetY = OY;
    _mapCurrentNodePositions = {};
    nodes.forEach(n => { _mapCurrentNodePositions[n.id] = { x: n.x, y: n.y }; });

    // SVG defs: arrowhead marker (drawn once; _mapRedrawArrows adds paths)
    // refX=18 places the tip (x=18) exactly at the path endpoint.
    svg.innerHTML = `
        <defs>
            <marker id="map-arrowhead" markerWidth="18" markerHeight="12"
                    refX="18" refY="6" orient="auto" markerUnits="userSpaceOnUse">
                <polygon points="0 0, 18 6, 0 12" fill="${_mapCurrentColor}" opacity="0.9"/>
            </marker>
        </defs>`;

    _mapRedrawArrows();

    // Render node cards as HTML divs
    nodes.forEach(({ id, x, y, task }) => {
        const node = document.createElement('div');
        node.id        = `map-node-${id}`;
        node.className = `map-node ${task.type || ''}`;
        node.style.left           = (x + OX) + 'px';
        node.style.top            = (y + OY) + 'px';
        node.style.borderLeftColor = _mapCurrentColor;

        // Title + badges
        let badges = '';
        if (task.is_big_idea)
            badges += '<span class="big-idea-badge">BIG IDEA</span>';
        if ((task.subdivision_generation || 0) > 0)
            badges += `<span class="subdivision-badge gen">Gen ${task.subdivision_generation}</span>`;

        // Tags + owner
        const tagHtml   = (task.tags || []).map(tg => `<span class="tag">${tg}</span>`).join('');
        const ownerHtml = task.owner ? `<span class="task-owner">${task.owner}</span>` : '';

        // Action buttons
        let actionHtml = `<button class="map-btn map-btn-secondary" onclick="editTask('${id}')">Edit</button>`;
        if (task.type === 'idea' || task.type === 'subdividing') {
            const hasRejections = (transitionCache[id] && transitionCache[id].rejectionCount > 0);
            actionHtml += ` <button class="map-btn map-btn-primary" onclick="advanceTask('${id}')">${_advanceBtnLabel(task.type, hasRejections)}</button>`;
        }
        if ((childIndex[id] || []).length > 0)
            actionHtml += ` <button class="map-btn map-btn-info" onclick="viewChildren('${id}')">Children</button>`;
        if (task.type === 'planning')
            actionHtml += ` <button class="map-btn map-btn-warning" onclick="moveTask('${id}','indev')">&#8594; Dev</button>`;

        node.innerHTML = `
            <button class="card-highlight-btn" title="Highlight" onclick="event.stopPropagation();toggleHighlight('${id}')">☆</button>
            <div class="map-node-title" onclick="editTask('${id}')">${task.title || '(untitled)'}${badges ? ' ' + badges : ''}</div>
            <div class="map-node-meta">${tagHtml}${ownerHtml}</div>
            <div class="card-toolbar" style="margin-bottom:0.3rem">
                <button class="toolbar-btn" title="Research" onclick="event.stopPropagation();openResearchDialog('${id}')">🔍</button>
                <button class="toolbar-btn" title="Subdivide" onclick="event.stopPropagation();toolbarSubdivide('${id}')">✂</button>
                <button class="toolbar-btn" title="Run Planning pipeline" onclick="event.stopPropagation();toolbarRunPipeline('${id}','planning')">📋</button>
                <button class="toolbar-btn" title="Run Conceptual Review pipeline" onclick="event.stopPropagation();toolbarRunPipeline('${id}','review')">👁</button>
                <button class="toolbar-btn" title="Run Optimization pipeline" onclick="event.stopPropagation();toolbarRunPipeline('${id}','optimization')">⚡</button>
                <button class="toolbar-btn" title="Run Security pipeline" onclick="event.stopPropagation();toolbarRunPipeline('${id}','security')">🔒</button>
                <button class="toolbar-btn" title="Manual Session" onclick="event.stopPropagation();openManualSession('${id}')">⌨</button>
                <button class="toolbar-btn" title="Run Agent" onclick="event.stopPropagation();runAgentFromToolbar('${id}')">▶</button>
                <button class="toolbar-btn" title="Stop Agent" onclick="event.stopPropagation();toolbarStopAgent('${id}')">⏹</button>
                <button class="toolbar-btn" title="Demote one stage" onclick="event.stopPropagation();toolbarDemote('${id}')">↩</button>
                <button class="toolbar-btn" title="Set Stage" onclick="event.stopPropagation();toolbarStagePicker('${id}',this)">⚙</button>
                <button class="toolbar-btn" title="Open in Diagnostics" onclick="event.stopPropagation();toolbarOpenDiagnostics('${id}')">📊</button>
                <button class="toolbar-btn" title="Clone as new Idea" onclick="event.stopPropagation();toolbarClone('${id}')">⧉</button>
                <button class="toolbar-btn" title="Pin to top of column" onclick="event.stopPropagation();toolbarPin('${id}')">📌</button>
            </div>
            <div class="map-node-actions">${actionHtml}</div>`;

        _applyHighlightState(node, id);

        // Drag-to-reposition — mousedown on the card body (not buttons/links)
        node.addEventListener('mousedown', (e) => _mapStartNodeDrag(e, id));

        nodesEl.appendChild(node);

        // PIP chips — stacked vertically below the main node card
        const pips = (task && task.pips) || [];
        pips.forEach((pip, pipIdx) => {
            const status = pip.status || 'unverified';
            const statusLabel = PIP_STATUS_LABELS[status] || status;
            const firstReq = (pip.requirements && pip.requirements[0])
                ? pip.requirements[0].substring(0, 60) + (pip.requirements[0].length > 60 ? '…' : '')
                : '(no requirements)';

            const chip = document.createElement('div');
            chip.className = `map-pip-chip map-pip-chip--${status}`;
            chip.style.left = (x + OX) + 'px';
            chip.style.top  = (y + OY + _MAP_CARD_H + pipIdx * _MAP_PIP_CHIP_H) + 'px';
            chip.innerHTML = `
                <span class="map-pip-chip-label">PIP ${pipIdx + 1}</span>
                <span class="map-pip-chip-status pip-status--${status}">${statusLabel}</span>
                <span class="map-pip-chip-req">${_escHtml(firstReq)}</span>`;
            chip.title = `PIP ${pipIdx + 1} — demoted from ${pip.origin_stage}\nClick to view details`;
            chip.addEventListener('click', (e) => {
                e.stopPropagation();
                openPipDetailModal(id, pip.id);
            });
            nodesEl.appendChild(chip);
        });
    });

    // Center the layout in the viewport on first open
    const wrap = document.getElementById('column-map-scroll-wrap');
    const ww = wrap.clientWidth  || window.innerWidth  - 240;
    const wh = wrap.clientHeight || window.innerHeight;
    mapTransform.x = (ww - W) / 2;
    mapTransform.y = (wh - H) / 2;
    applyMapTransform();

    // Persist any positions that were computed on the fly (map_x / map_y were null)
    const toSave = nodes
        .filter(n => n.newlyPositioned)
        .map(n => ({ id: n.id, map_x: n.x, map_y: n.y }));
    _mapSavePositions(toSave);
}

// Convert screen (viewport) coordinates to canvas-space coordinates,
// accounting for the current pan and zoom transform.
function _mapScreenToCanvas(screenX, screenY) {
    const wrap = document.getElementById('column-map-scroll-wrap');
    const rect = wrap.getBoundingClientRect();
    return {
        x: (screenX - rect.left - mapTransform.x) / mapTransform.scale,
        y: (screenY - rect.top  - mapTransform.y) / mapTransform.scale,
    };
}

// Initiate a node drag. Called from each node card's mousedown listener.
// Dragging a parent node moves all its descendants by the same delta (group drag).
// Dragging a leaf node moves only that node.
function _mapStartNodeDrag(e, nodeId) {
    if (e.button !== 0) return;
    if (e.target.closest('button, a')) return;  // let action buttons fire normally
    e.stopPropagation();                         // prevent canvas pan

    const mouse = _mapScreenToCanvas(e.clientX, e.clientY);

    // Build the group: grabbed node + every descendant
    const descendants = descendantIndex[nodeId] || [];
    const groupIds    = [nodeId, ...descendants];

    // Snapshot layout positions for every node in the group
    const groupStartLayout = {};
    groupIds.forEach(id => {
        const pos = _mapCurrentNodePositions[id];
        if (pos) groupStartLayout[id] = { x: pos.x, y: pos.y };
    });

    _mapNodeDrag.active           = true;
    _mapNodeDrag.nodeId           = nodeId;
    _mapNodeDrag.startMouseCanvas = { x: mouse.x, y: mouse.y };
    _mapNodeDrag.groupIds         = groupIds;
    _mapNodeDrag.groupStartLayout = groupStartLayout;

    document.body.style.cursor = 'grabbing';

    // Visual: grabbed node gets full drag style; descendants get a lighter tint
    const el = document.getElementById(`map-node-${nodeId}`);
    if (el) el.classList.add('map-node-dragging');
    descendants.forEach(id => {
        const cel = document.getElementById(`map-node-${id}`);
        if (cel) cel.classList.add('map-node-dragging-child');
    });
}

// Return the point on the boundary of a card (centered at cx,cy, half-dims HW×HH)
// in the direction from (cx,cy) toward (tx,ty).
function _mapCardEdge(cx, cy, tx, ty) {
    const dx = tx - cx, dy = ty - cy;
    if (!dx && !dy) return { x: cx, y: cy };
    const sx = (_MAP_CARD_W / 2) / Math.abs(dx);
    const sy = (_MAP_CARD_H / 2) / Math.abs(dy);
    const s  = Math.min(sx, sy);
    return { x: cx + dx * s, y: cy + dy * s };
}

// Redraw all SVG arrows from current _mapCurrentNodePositions.
// Called once on initial render and again on every node-drag tick.
// Arrows run edge-to-edge (not center-to-center) so the arrowhead tip lands
// exactly at the target card border and is never hidden behind it.
function _mapRedrawArrows() {
    const svg = document.getElementById('column-map-svg');
    if (!svg) return;
    svg.querySelectorAll('path').forEach(p => p.remove());

    const HW = _MAP_CARD_W / 2;
    const HH = _MAP_CARD_H / 2;

    _mapCurrentEdges.forEach(({ fromId, toId }) => {
        const A = _mapCurrentNodePositions[fromId];
        const B = _mapCurrentNodePositions[toId];
        if (!A || !B) return;

        // Card centers in canvas space
        const Acx = A.x + _mapOffsetX + HW,  Acy = A.y + _mapOffsetY + HH;
        const Bcx = B.x + _mapOffsetX + HW,  Bcy = B.y + _mapOffsetY + HH;

        const dx   = Bcx - Acx, dy = Bcy - Acy;
        const dist = Math.sqrt(dx * dx + dy * dy) || 1;

        // Start at A's card edge toward B; end at B's card edge toward A
        const start = _mapCardEdge(Acx, Acy, Bcx, Bcy);
        const end   = _mapCardEdge(Bcx, Bcy, Acx, Acy);

        // Quadratic bezier (converted to cubic) — single control point at the
        // midpoint offset perpendicularly. This avoids S-curves that reverse
        // the arrowhead direction on near-horizontal connections.
        const bow = Math.min(dist * 0.12, 50);
        const qx  = (start.x + end.x) / 2 - dy * bow / dist;
        const qy  = (start.y + end.y) / 2 + dx * bow / dist;
        const cx1 = start.x + (qx - start.x) * 2 / 3;
        const cy1 = start.y + (qy - start.y) * 2 / 3;
        const cx2 = end.x   + (qx - end.x)   * 2 / 3;
        const cy2 = end.y   + (qy - end.y)   * 2 / 3;

        const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
        path.setAttribute('d', `M ${start.x} ${start.y} C ${cx1} ${cy1}, ${cx2} ${cy2}, ${end.x} ${end.y}`);
        path.setAttribute('stroke', _mapCurrentColor);
        path.setAttribute('stroke-width', '4.5');
        path.setAttribute('fill', 'none');
        path.setAttribute('opacity', '0.72');
        path.setAttribute('marker-end', 'url(#map-arrowhead)');
        svg.appendChild(path);
    });
}

function applyMapTransform() {
    const canvas = document.getElementById('column-map-canvas');
    if (!canvas) return;
    canvas.style.transform = `translate(${mapTransform.x}px, ${mapTransform.y}px) scale(${mapTransform.scale})`;
}

function setupMapInteraction() {
    const wrap = document.getElementById('column-map-scroll-wrap');
    if (!wrap) return;

    // Store handlers on the element so teardown can remove them
    wrap._mmdown = (e) => {
        if (e.button !== 0) return;
        if (e.target.closest('.map-node')) return;
        mapDragState.dragging = true;
        mapDragState.startX   = e.clientX;
        mapDragState.startY   = e.clientY;
        mapDragState.originX  = mapTransform.x;
        mapDragState.originY  = mapTransform.y;
        wrap.classList.add('dragging');
    };
    wrap._mmmove = (e) => {
        // Node drag takes priority over canvas pan
        if (_mapNodeDrag.active) {
            const mouse = _mapScreenToCanvas(e.clientX, e.clientY);
            const dx    = mouse.x - _mapNodeDrag.startMouseCanvas.x;
            const dy    = mouse.y - _mapNodeDrag.startMouseCanvas.y;

            // Move every node in the group by the same delta
            _mapNodeDrag.groupIds.forEach(id => {
                const start = _mapNodeDrag.groupStartLayout[id];
                if (!start) return;
                const lx = start.x + dx;
                const ly = start.y + dy;
                _mapCurrentNodePositions[id] = { x: lx, y: ly };
                const el = document.getElementById(`map-node-${id}`);
                if (el) { el.style.left = (lx + _mapOffsetX) + 'px'; el.style.top = (ly + _mapOffsetY) + 'px'; }
            });

            _mapRedrawArrows();
            return;
        }
        if (!mapDragState.dragging) return;
        mapTransform.x = mapDragState.originX + (e.clientX - mapDragState.startX);
        mapTransform.y = mapDragState.originY + (e.clientY - mapDragState.startY);
        applyMapTransform();
    };
    wrap._mmup = () => {
        if (_mapNodeDrag.active) {
            const { nodeId, groupIds } = _mapNodeDrag;
            _mapNodeDrag.active = false;
            _mapNodeDrag.nodeId = null;
            document.body.style.cursor = '';

            // Clear visual states from the whole group
            document.getElementById(`map-node-${nodeId}`)?.classList.remove('map-node-dragging');
            groupIds.slice(1).forEach(id =>
                document.getElementById(`map-node-${id}`)?.classList.remove('map-node-dragging-child')
            );

            // Persist every node that moved
            const toSave = groupIds
                .map(id => { const p = _mapCurrentNodePositions[id]; return p ? { id, map_x: p.x, map_y: p.y } : null; })
                .filter(Boolean);
            if (toSave.length) _mapSavePositions(toSave);
            return;
        }
        mapDragState.dragging = false;
        wrap.classList.remove('dragging');
    };
    wrap._mmwheel = (e) => {
        e.preventDefault();
        const factor   = e.deltaY > 0 ? 0.9 : 1.1;
        const newScale = Math.max(0.15, Math.min(4, mapTransform.scale * factor));
        // Zoom toward cursor
        const rect = wrap.getBoundingClientRect();
        const cx   = e.clientX - rect.left;
        const cy   = e.clientY - rect.top;
        mapTransform.x = cx - (cx - mapTransform.x) * (newScale / mapTransform.scale);
        mapTransform.y = cy - (cy - mapTransform.y) * (newScale / mapTransform.scale);
        mapTransform.scale = newScale;
        applyMapTransform();
    };

    wrap.addEventListener('mousedown', wrap._mmdown);
    document.addEventListener('mousemove', wrap._mmmove);
    document.addEventListener('mouseup',   wrap._mmup);
    wrap.addEventListener('wheel', wrap._mmwheel, { passive: false });
}

function teardownMapInteraction() {
    const wrap = document.getElementById('column-map-scroll-wrap');
    if (!wrap) return;
    if (wrap._mmdown)  { wrap.removeEventListener('mousedown', wrap._mmdown);  delete wrap._mmdown;  }
    if (wrap._mmmove)  { document.removeEventListener('mousemove', wrap._mmmove); delete wrap._mmmove;  }
    if (wrap._mmup)    { document.removeEventListener('mouseup', wrap._mmup);  delete wrap._mmup;    }
    if (wrap._mmwheel) { wrap.removeEventListener('wheel', wrap._mmwheel);      delete wrap._mmwheel; }
}


// ============================================
// Manual Session
// ============================================

let _manualSessionId = null;
let _manualSessionTools = [];  // Available tool schemas

function _msEscapeHtml(str) {
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
}

function _msToolCategory(name) {
    const map = {
        read_file: 'file', write_file: 'file', append_file: 'file',
        list_directory: 'file', count_lines: 'file', archive_file: 'file',
        web_search: 'search', web_fetch: 'search',
        git_status: 'git', git_diff: 'git', git_add: 'git', git_commit: 'git',
        git_checkout: 'git', git_log: 'git',
        run_shell: 'shell',
        get_task: 'task', list_tasks: 'task', update_task_status: 'task',
        create_mermaid_diagram: 'plan', write_interface_contract: 'plan',
    };
    return map[name] || 'other';
}

async function openManualSession(taskId) {
    try {
        const resp = await fetch(`${API_BASE}/manual-session/${taskId}/start`, { method: 'POST' });
        const data = await resp.json();
        if (!resp.ok) { showToast(data.detail || 'Failed to start session', 'error'); return; }
        _manualSessionId = data.session_id;
        _manualSessionTools = data.available_tools || [];
        document.getElementById('ms-title').textContent = `Manual Session — ${data.task_title}`;
        _msPopulateToolSelect();
        _msRenderMessages(data.messages);
        document.getElementById('manual-session-modal').classList.add('active');
    } catch (e) {
        showToast('Error starting manual session: ' + e.message, 'error');
    }
}

async function closeManualSessionModal() {
    if (_manualSessionId) {
        if (!await showConfirm('End Session', 'End this manual session and close?', 'End Session')) return;
        fetch(`${API_BASE}/manual-session/${_manualSessionId}/end`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ signal: 'MANUAL_END', summary: 'Closed by user' }),
        }).catch(() => {});
        _manualSessionId = null;
    }
    document.getElementById('manual-session-modal').classList.remove('active');
}

function _msPopulateToolSelect() {
    const sel = document.getElementById('ms-tool-select');
    sel.innerHTML = '<option value="">(select a tool)</option>';

    const groups = {};
    _manualSessionTools.forEach(schema => {
        const name = schema.function.name;
        const cat = _msToolCategory(name);
        if (!groups[cat]) groups[cat] = [];
        groups[cat].push(schema);
    });

    Object.entries(groups).sort(([a], [b]) => a.localeCompare(b)).forEach(([cat, tools]) => {
        const og = document.createElement('optgroup');
        og.label = cat.toUpperCase();
        tools.forEach(s => {
            const opt = document.createElement('option');
            opt.value = s.function.name;
            opt.textContent = s.function.name;
            og.appendChild(opt);
        });
        sel.appendChild(og);
    });
}

function onMsToolSelect() {
    const toolName = document.getElementById('ms-tool-select').value;
    const form = document.getElementById('ms-tool-form');
    form.innerHTML = '';
    if (!toolName) return;

    const schema = _manualSessionTools.find(s => s.function.name === toolName);
    if (!schema) return;

    const props = schema.function.parameters?.properties || {};
    const required = new Set(schema.function.parameters?.required || []);

    Object.entries(props).forEach(([key, spec]) => {
        const label = document.createElement('label');
        label.textContent = key + (required.has(key) ? ' *' : '');
        label.className = 'ms-arg-label';

        const isLong = spec.type === 'string' && (key === 'content' || key === 'path' || (spec.description || '').length > 40);
        const input = document.createElement('textarea');
        input.id = `ms-arg-${key}`;
        input.rows = isLong ? 3 : 1;
        input.placeholder = spec.description || '';
        input.className = 'ms-arg-input';

        form.appendChild(label);
        form.appendChild(input);
    });
}

async function msExecuteTool() {
    if (!_manualSessionId) return;
    const toolName = document.getElementById('ms-tool-select').value;
    if (!toolName) { showToast('Select a tool first.', 'warning'); return; }

    const schema = _manualSessionTools.find(s => s.function.name === toolName);
    if (!schema) return;

    const props = schema.function.parameters?.properties || {};
    const args = {};
    for (const key of Object.keys(props)) {
        const el = document.getElementById(`ms-arg-${key}`);
        const val = el ? el.value.trim() : '';
        if (val) {
            const t = (schema.function.parameters.properties[key] || {}).type;
            if (t === 'integer' || t === 'number') {
                const n = Number(val);
                args[key] = isNaN(n) ? val : n;
            } else if (t === 'boolean') {
                args[key] = val === 'true' || val === '1';
            } else if (t === 'object' || t === 'array') {
                try { args[key] = JSON.parse(val); } catch { args[key] = val; }
            } else {
                args[key] = val;
            }
        }
    }

    const execBtn = document.querySelector('.ms-exec-btn');
    execBtn.disabled = true;
    execBtn.textContent = 'Running…';

    try {
        const resp = await fetch(`${API_BASE}/manual-session/${_manualSessionId}/tool`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ tool_name: toolName, arguments: args }),
        });
        const data = await resp.json();
        if (!resp.ok) {
            if (resp.status === 404) {
                showToast('Session lost — server may have restarted. Start a new session.', 'error');
                _manualSessionId = null;
            } else {
                showToast(data.detail || 'Tool execution error', 'error');
            }
            return;
        }
        _msRenderMessages(data.messages);
    } catch (e) {
        showToast('Error: ' + e.message, 'error');
    } finally {
        execBtn.disabled = false;
        execBtn.textContent = 'Execute Tool';
    }
}

async function msAddMessage() {
    if (!_manualSessionId) return;
    const role = document.getElementById('ms-message-role').value;
    const content = document.getElementById('ms-message-content').value.trim();
    if (!content) return;

    try {
        const resp = await fetch(`${API_BASE}/manual-session/${_manualSessionId}/message`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ role, content }),
        });
        const data = await resp.json();
        if (resp.ok) {
            _msRenderMessages(data.messages);
            document.getElementById('ms-message-content').value = '';
        }
    } catch (e) {
        showToast('Error: ' + e.message, 'error');
    }
}

async function msEndSession() {
    if (!_manualSessionId) { closeManualSessionModal(); return; }
    const signal = document.getElementById('ms-signal-select').value;
    const summary = document.getElementById('ms-end-summary').value.trim();
    try {
        await fetch(`${API_BASE}/manual-session/${_manualSessionId}/end`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ signal, summary }),
        });
    } catch (_) {}
    _manualSessionId = null;
    document.getElementById('manual-session-modal').classList.remove('active');
}

function _msRenderMessages(messages) {
    const log = document.getElementById('ms-chat-log');
    if (!log) return;
    log.innerHTML = '';

    messages.forEach(msg => {
        const div = document.createElement('div');
        div.className = `ms-msg ms-msg-${msg.role}`;

        if (msg.role === 'tool_call') {
            const argsStr = msg.arguments ? JSON.stringify(msg.arguments, null, 2) : '';
            div.innerHTML = `<span class="ms-tool-name">⚙ ${_msEscapeHtml(msg.tool_name || '')}</span>`
                + (argsStr ? `<pre class="ms-tool-args">${_msEscapeHtml(argsStr)}</pre>` : '');
        } else if (msg.role === 'tool_result') {
            const result = msg.content || '';
            const copyBtn = document.createElement('button');
            copyBtn.className = 'ms-copy-btn';
            copyBtn.textContent = 'Copy';
            copyBtn.onclick = () => navigator.clipboard.writeText(result);
            const pre = document.createElement('pre');
            pre.className = 'ms-tool-result-pre';
            pre.textContent = result;
            div.appendChild(copyBtn);
            div.appendChild(pre);
        } else {
            div.innerHTML = `<span class="ms-role-badge">${_msEscapeHtml(msg.role)}</span>`
                + `<span class="ms-content">${_msEscapeHtml(msg.content || '')}</span>`;
        }

        log.appendChild(div);
    });

    log.scrollTop = log.scrollHeight;
}

// ============================================================
// Toolbar Quick-Actions
// ============================================================

async function toolbarStopAgent(taskId) {
    const resp = await fetch(`${API_BASE}/agent/stop/${taskId}`, { method: 'POST' });
    if (resp.ok) {
        showToast('Stop requested — loop will halt at its next opportunity.', 'info');
    } else {
        const d = await resp.json().catch(() => ({}));
        showToast(d.detail || 'No active loop for this task.', 'error');
    }
}

async function toolbarDemote(taskId) {
    const task = taskData[taskId];
    const label = task ? task.type : taskId;
    if (!await showConfirm('Demote Task', `Move "${label}" one stage backward in the pipeline?`, 'Demote')) return;
    const resp = await fetch(`${API_BASE}/tasks/${taskId}/demote`, { method: 'POST' });
    const d = await resp.json().catch(() => ({}));
    if (resp.ok) {
        showToast(`Demoted to "${d.type}".`, 'success');
        await loadTasksFromDatabase();
    } else {
        showToast(d.detail || 'Demote failed.', 'error');
    }
}

// Stage picker — small flyout positioned near the button
let _stagePickerTaskId = null;
function _removeStagePicker() {
    const el = document.getElementById('_stage-picker-flyout');
    if (el) el.remove();
    _stagePickerTaskId = null;
}

const _STAGE_LABELS = {
    architecture: 'Architecture', idea: 'Ideas', planning: 'Planning',
    indev: 'In Dev', conceptual_review: 'Review', optimization: 'Optimization',
    security: 'Security', full_review: 'Full Review', completed: 'Completed',
};

function toolbarStagePicker(taskId, btn) {
    // Toggle off if same task already open
    if (_stagePickerTaskId === taskId) { _removeStagePicker(); return; }
    _removeStagePicker();
    _stagePickerTaskId = taskId;

    const flyout = document.createElement('div');
    flyout.id = '_stage-picker-flyout';
    flyout.className = 'stage-picker-flyout';

    const pipeline = ['architecture','idea','planning','indev','conceptual_review','optimization','security','full_review','completed'];
    const current = taskData[taskId]?.type;
    pipeline.forEach(stage => {
        const item = document.createElement('button');
        item.className = 'stage-picker-item' + (stage === current ? ' current' : '');
        item.textContent = _STAGE_LABELS[stage] || stage;
        item.onclick = async () => {
            _removeStagePicker();
            const resp = await fetch(`${API_BASE}/tasks/${taskId}/set-stage`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ stage }),
            });
            const d = await resp.json().catch(() => ({}));
            if (resp.ok) {
                showToast(`Moved to "${_STAGE_LABELS[stage] || stage}".`, 'success');
                await loadTasksFromDatabase();
            } else {
                showToast(d.detail || 'Stage change failed.', 'error');
            }
        };
        flyout.appendChild(item);
    });

    document.body.appendChild(flyout);

    // Position below the button
    const rect = btn.getBoundingClientRect();
    flyout.style.left = rect.left + 'px';
    flyout.style.top  = (rect.bottom + 4) + 'px';

    // Close on outside click
    setTimeout(() => {
        document.addEventListener('click', function _closeStage(e) {
            if (!flyout.contains(e.target)) { _removeStagePicker(); document.removeEventListener('click', _closeStage); }
        });
    }, 0);
}

async function toolbarRunPipeline(taskId, pipeline) {
    const labels = { planning: 'Planning', review: 'Conceptual Review', optimization: 'Optimization', security: 'Security', 'full-review': 'Full Review' };
    const label = labels[pipeline] || pipeline;
    const resp = await fetch(`${API_BASE}/tasks/${taskId}/run-${pipeline}`, { method: 'POST' });
    const d = await resp.json().catch(() => ({}));
    if (resp.ok) {
        showToast(`${label} pipeline started.`, 'success');
    } else {
        showToast(d.detail || `${label} pipeline failed to start.`, 'error');
    }
}

async function toolbarClone(taskId) {
    const resp = await fetch(`${API_BASE}/tasks/${taskId}/clone`, { method: 'POST' });
    const d = await resp.json().catch(() => ({}));
    if (resp.ok) {
        showToast(`Cloned as new idea: "${d.title}".`, 'success');
        await loadTasksFromDatabase();
    } else {
        showToast(d.detail || 'Clone failed.', 'error');
    }
}

async function toolbarPin(taskId) {
    const resp = await fetch(`${API_BASE}/tasks/${taskId}/pin`, { method: 'POST' });
    const d = await resp.json().catch(() => ({}));
    if (resp.ok) {
        showToast('Pinned to top of column.', 'success');
        await loadTasksFromDatabase();
    } else {
        showToast(d.detail || 'Pin failed.', 'error');
    }
}

function toolbarOpenDiagnostics(taskId) {
    window.open(`/diagnostics?task=${encodeURIComponent(taskId)}`, '_blank');
}

function toolbarOpenMap(taskId) {
    const task = taskData[taskId];
    if (!task) return;
    openColumnMap(task.type, taskId);
}

