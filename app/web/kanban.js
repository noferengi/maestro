// Kanban Board JavaScript - Extracted from kanban.html
// Includes drag-and-drop functionality for reordering within columns

// API Configuration
const API_BASE = '/api';

// Track where each mousedown originated so that click-outside-to-close modals
// are not triggered by a drag that started inside the modal content and ended
// on the backdrop.  Only close when both mousedown AND click land on the backdrop.
let _modalMousedownTarget = null;
document.addEventListener('mousedown', function(e) { _modalMousedownTarget = e.target; });

// Default category colours — used as fallback before the API responds.
// After loadTasksFromDatabase(), archCategoryMap is populated from the API.
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

// Dynamic arch category map — populated from GET /api/projects/{name}/arch-categories.
// Each entry: { key, label, color, position, id? }
// Falls back to ARCH_CATEGORY_COLORS if the API is unreachable.
let archCategoryMap = {...ARCH_CATEGORY_COLORS};
let _archCategoryList = [];  // ordered list of {key, label, color, position, id}

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
    'final_review': 5,
    'human_review': 5,
    'completed': 15
};

// Task data storage with history tracking - loaded from database
let taskData = {};
let allTasks = [];

// Global LLM, Budget, and Compute Node caches
let allLlms = [];
let allProjects = [];  // [{name, path, description, llm_id, budget_id}] — kept in sync with loadProjects()
let allPipelineTemplates = [];  // [{id, name, description, is_builtin}] — populated once at init
let allBudgets = [];
let allComputeNodes = [];  // [{id, name, description, max_parallel_sessions, max_loaded_models}]

// Transition status cache: taskId -> { status, data, rejectionCount }
let transitionCache = {};

// Active polling timers: taskId -> intervalId
let transitionPollers = {};

// Inbox
let inboxMessages = [];
let _inboxUnreadCount = 0;
let _inboxHasNeedsHuman = false;
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
function _applyHighlightState(el, taskId) {
    const task = taskData[taskId] || (allTasks || []).find(t => t.id === taskId);
    const starred = task ? Boolean(task.is_starred) : false;
    el.classList.toggle('highlighted', starred);
    const btn = el.querySelector('.card-highlight-btn');
    if (btn) btn.textContent = starred ? '★' : '☆';
}

async function toggleHighlight(taskId) {
    const resp = await fetch(`${API_BASE}/tasks/${taskId}/star`, { method: 'POST' });
    if (resp.ok) {
        await loadTasksFromDatabase();
    } else {
        const d = await resp.json().catch(() => ({}));
        showToast(d.detail || 'Star failed.', 'error');
    }
}

// Grouped drag state
let isDraggingGroup = false;
let dragGroupDescendants = [];  // [{id, column, positionOffset}]
let dragGroupOldParentPos = 0;

// Card DOM cache: taskId -> element, built once and reused across renders
const cardCache = {};
// Render fingerprint cache: taskId -> string, detects which cards need updating
const fingerprintCache = {};

// Active pipeline template for the current project (null = use default column order)
let activePipelineTemplate = null;

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

        // Load the project's pipeline template and arch categories in parallel
        await Promise.all([
            _loadActivePipelineTemplate(),
            _loadArchCategories(),
        ]);

        // Build/update kanban columns from the active template
        buildKanbanColumns(activePipelineTemplate?.stages?.length ? activePipelineTemplate.stages : null);

        // Sync the pipeline dropdown to the newly loaded template
        populatePipelineDropdown();

        return true;
    } catch (error) {
        console.error('Error loading tasks from database:', error);
        return false;
    }
}

async function _loadActivePipelineTemplate() {
    activePipelineTemplate = null;
    const project = (allProjects || []).find(p => p.name === currentProject);
    const templateId = project?.pipeline_template_id;
    if (!templateId) return;
    try {
        activePipelineTemplate = await fetch(`${API_BASE}/pipelines/${templateId}`)
            .then(r => r.ok ? r.json() : null);
    } catch (_) {
        activePipelineTemplate = null;
    }
}

async function _loadArchCategories() {
    try {
        const r = await fetch(`${API_BASE}/projects/${encodeURIComponent(currentProject)}/arch-categories`);
        if (!r.ok) return;
        const cats = await r.json();
        if (!cats || !cats.length) return;
        _archCategoryList = cats;
        archCategoryMap = {};
        for (const c of cats) {
            // Resolve color: prefer DB value, then fall back to ARCH_CATEGORY_COLORS by
            // exact key match or title-cased key match (handles lowercase DB keys).
            const titleKey = c.key.charAt(0).toUpperCase() + c.key.slice(1);
            const fallback = ARCH_CATEGORY_COLORS[c.key] || ARCH_CATEGORY_COLORS[titleKey] || '#6c757d';
            const color = c.color || fallback;
            archCategoryMap[c.key] = color;
            // Also register the title-cased key so existing arch cards (stored with title
            // case, e.g. "Platform") still resolve a color.
            if (titleKey !== c.key) archCategoryMap[titleKey] = color;
        }
    } catch (_) {
        // Keep the defaults on network error
    }
}

// Apply column ordering and group bracket indicators from the active pipeline template.
// Runs after renderTasksFromDatabase() — mutates CSS only, never rebuilds DOM cards.
function applyPipelineTemplateLayout() {
    if (!activePipelineTemplate) {
        document.querySelectorAll('.column[data-pipeline-order]').forEach(col => {
            col.style.order = '';
            col.removeAttribute('data-pipeline-order');
        });
        return;
    }

    const stages = (activePipelineTemplate.stages || [])
        .sort((a, b) => (a.position ?? 0) - (b.position ?? 0));

    // Apply CSS order to columns; grouped stages share one column-group div.
    const renderedGroups = new Set();
    stages.forEach((stage, idx) => {
        let col;
        if (stage.group_id) {
            if (renderedGroups.has(stage.group_id)) return;
            renderedGroups.add(stage.group_id);
            col = document.getElementById(`column-group-${stage.group_id}`);
        } else {
            col = document.getElementById(`column-${stage.stage_key}`);
        }
        if (!col) return;
        col.style.order = idx;
        col.dataset.pipelineOrder = idx;
    });
}

// Build kanban columns entirely from the active pipeline template.
// All columns are created dynamically; there are no static column elements.
// Called after _loadActivePipelineTemplate() resolves.
function buildKanbanColumns(stages) {
    const board = document.querySelector('.kanban-board');
    if (!board) return;

    // Remove all columns from the previous template
    board.querySelectorAll('.column[data-board-col]').forEach(col => col.remove());

    if (!stages || !stages.length) return;

    // Architecture stages render in the arch bar only — never as kanban columns.
    const sortedStages = [...stages]
        .filter(s => s.agent_type !== 'arch_agent' && s.stage_key !== 'architecture')
        .sort((a, b) => (a.position ?? 0) - (b.position ?? 0));

    // Build group map: group_id → { group, stages: [] } ordered by position
    const groups = activePipelineTemplate?.groups || [];
    const groupMap = {};
    groups.forEach(g => { groupMap[g.id] = { group: g, stages: [] }; });
    sortedStages.forEach(s => {
        if (s.group_id && groupMap[s.group_id]) groupMap[s.group_id].stages.push(s);
    });

    const renderedGroupIds = new Set();

    sortedStages.forEach(stage => {
        if (stage.group_id && groupMap[stage.group_id]) {
            // Grouped stage — render the whole group column once on first encounter
            if (renderedGroupIds.has(stage.group_id)) return;
            renderedGroupIds.add(stage.group_id);

            const { group, stages: groupStages } = groupMap[stage.group_id];
            const groupName = (group.name || 'Group').toUpperCase();
            const firstKey = groupStages[0]?.stage_key || '';

            const col = document.createElement('div');
            col.className = 'column column-group';
            col.id = `column-group-${group.id}`;
            col.dataset.boardCol = '1';
            col.innerHTML =
                `<div class="column-header" onclick="openColumnMap('${firstKey}')" ` +
                `title="Click to open ${groupName} Map">` +
                `<span class="column-title">${groupName}</span>` +
                `<span class="task-count" id="count-group-${group.id}">0</span>` +
                `</div>` +
                groupStages.map(s =>
                    `<div class="tasks-container" id="tasks-${s.stage_key}" ` +
                    `onclick="handleTasksContainerClick(event,'${s.stage_key}')"></div>`
                ).join('');
            board.appendChild(col);
        } else {
            // Standalone stage column
            const label = (stage.label || stage.stage_key).toUpperCase();
            const isEntryStage = stage.agent_type === 'intake_agent';

            const col = document.createElement('div');
            col.className = 'column';
            col.id = `column-${stage.stage_key}`;
            col.dataset.boardCol = '1';
            col.innerHTML =
                `<div class="column-header" onclick="openColumnMap('${stage.stage_key}')" ` +
                `title="Click to open ${label} Map">` +
                `<span class="column-title">${label}</span>` +
                `<span class="task-count" id="count-${stage.stage_key}">0</span>` +
                `</div>` +
                (isEntryStage
                    ? `<button class="add-task-btn" onclick="openAddTaskModal('idea')">+ Add Idea</button>`
                    : '') +
                `<div class="tasks-container" id="tasks-${stage.stage_key}" ` +
                `onclick="handleTasksContainerClick(event,'${stage.stage_key}')"></div>`;
            board.appendChild(col);
        }
    });
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
        task.intake_exhausted ? '1' : '0',
        task.intake_rejection_count || 0,
        task.clarification_status || 'none',
        task.is_starred ? '1' : '0',
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
    'security': 'final_review',
    'final_review': 'human_review',
    'human_review': 'completed'
};

const COLUMN_DISPLAY = {
    'architecture': 'Architecture',
    'idea': 'Ideas',
    'subdividing': 'Ideas',
    'planning': 'Planning',
    'indev': 'In Development',
    'conceptual_review': 'AI Review',
    'optimization': 'AI Review',
    'security': 'AI Review',
    'final_review': 'AI Review',
    'human_review': 'Human Review',
    'completed': 'Completed',
};

// Returns the label for an advance button given a task's current type.
function _advanceBtnLabel(taskType, hasRejections) {
    if (hasRejections) return 'Retry Pipeline';
    if (taskType === 'human_review') return 'Approve & Merge';
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
    const _defaultColumns = ['idea', 'planning', 'indev', 'conceptual_review', 'optimization', 'security', 'final_review', 'human_review', 'completed'];
    const columns = activePipelineTemplate?.stages
        ? activePipelineTemplate.stages
              .sort((a, b) => (a.position ?? 0) - (b.position ?? 0))
              .map(s => s.stage_key)
        : _defaultColumns;

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
                const rawCard = createTaskCard(task);
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

    // Apply column ordering and group brackets from the active pipeline template
    applyPipelineTemplateLayout();

    // Self-modification banner (Gap 5)
    _renderSelfModBanner();
    _loadSelfModBadges();
}

function updateTaskCounts() {
    if (!activePipelineTemplate?.stages?.length) return;
    const stages = activePipelineTemplate.stages;
    const groups = activePipelineTemplate.groups || [];

    // Standalone stage columns have a count-{stage_key} badge in their header.
    stages.forEach(stage => {
        const container = document.getElementById(`tasks-${stage.stage_key}`);
        const countEl = document.getElementById(`count-${stage.stage_key}`);
        if (container && countEl) {
            countEl.textContent = container.querySelectorAll('.task-card').length;
        }
    });

    // Group column headers have a count-group-{groupId} badge; value = sum of member stages.
    groups.forEach(g => {
        const memberKeys = stages.filter(s => s.group_id === g.id).map(s => s.stage_key);
        const total = memberKeys.reduce((sum, key) => {
            const c = document.getElementById(`tasks-${key}`);
            return sum + (c ? c.querySelectorAll('.task-card').length : 0);
        }, 0);
        const countEl = document.getElementById(`count-group-${g.id}`);
        if (countEl) countEl.textContent = total;
    });
}

// ============================================
// DOCUMENT STORE modal
// ============================================

async function openDocumentStore() {
    if (!currentProject) { showToast('Select a project first.', 'warning'); return; }
    const res = await fetch(`${API_BASE}/projects/${encodeURIComponent(currentProject)}/documents`);
    const docs = res.ok ? await res.json() : [];
    const tbody = document.getElementById('doc-list');
    document.getElementById('doc-viewer').style.display = 'none';
    document.getElementById('doc-search').value = '';
    tbody.innerHTML = docs.length ? docs.map(d =>
        `<tr style="border-top:1px solid #2d3748;cursor:pointer" onclick="viewDoc('${d.key.replace(/'/g, "\\'")}')">
          <td style="padding:4px 8px;font-family:monospace;color:#60a5fa">${d.key}</td>
          <td style="padding:4px 8px">${(d.tags || []).join(', ') || '—'}</td>
          <td style="padding:4px 8px;color:#94a3b8">${d.written_by_task_id || '—'}</td>
          <td style="padding:4px 8px;color:#94a3b8">${d.updated_at ? new Date(d.updated_at).toLocaleString() : '—'}</td>
        </tr>`
    ).join('') : '<tr><td colspan="4" style="padding:12px 8px;color:#64748b;text-align:center">No documents yet.</td></tr>';
    document.getElementById('doc-store-modal').style.display = 'flex';
}

async function viewDoc(key) {
    const res = await fetch(`${API_BASE}/projects/${encodeURIComponent(currentProject)}/documents/${encodeURIComponent(key)}`);
    const doc = res.ok ? await res.json() : null;
    if (!doc) return;
    const viewer = document.getElementById('doc-viewer');
    viewer.textContent = doc.content || '';
    viewer.style.display = 'block';
}

function closeDocStore() { document.getElementById('doc-store-modal').style.display = 'none'; }

function filterDocList(q) {
    document.querySelectorAll('#doc-list tr').forEach(row => {
        row.style.display = row.textContent.toLowerCase().includes(q.toLowerCase()) ? '' : 'none';
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

// ============================================
// Arch Category Management Modal
// ============================================

function openArchCategoryModal() {
    const modal = document.getElementById('arch-category-modal');
    if (!modal) return;
    const project = (allProjects || []).find(p => p.name === currentProject);
    const hasTemplate = !!project?.pipeline_template_id;
    const addBtn = document.getElementById('arch-cat-add-btn');
    if (addBtn) addBtn.style.display = hasTemplate ? '' : 'none';
    _renderArchCatList(hasTemplate);
    modal.classList.add('active');
}

function closeArchCategoryModal() {
    const modal = document.getElementById('arch-category-modal');
    if (modal) modal.classList.remove('active');
}

function _renderArchCatList(editable) {
    const list = document.getElementById('arch-cat-list');
    if (!list) return;
    list.innerHTML = '';
    if (!_archCategoryList.length) {
        list.innerHTML = '<p style="color:#6c757d;font-size:0.85rem">No categories loaded.</p>';
        return;
    }
    for (const cat of _archCategoryList) {
        const row = document.createElement('div');
        row.style.cssText = 'display:flex;align-items:center;gap:0.5rem;padding:0.4rem 0.5rem;background:#1e2635;border-radius:4px';
        row.dataset.catId = cat.id || '';
        row.dataset.catKey = cat.key;

        const swatch = document.createElement('span');
        swatch.style.cssText = `display:inline-block;width:20px;height:20px;border-radius:3px;background:${cat.color || '#6c757d'};flex-shrink:0`;

        const label = document.createElement('span');
        label.textContent = cat.label || cat.key;
        label.style.cssText = 'flex:1;font-size:0.9rem;color:#e2e8f0';

        const keyBadge = document.createElement('span');
        keyBadge.textContent = cat.key;
        keyBadge.style.cssText = 'font-size:0.75rem;color:#6c757d;font-family:monospace';

        row.appendChild(swatch);
        row.appendChild(label);
        row.appendChild(keyBadge);

        if (editable && cat.id) {
            const colorInput = document.createElement('input');
            colorInput.type = 'color';
            colorInput.value = cat.color || '#6c757d';
            colorInput.title = 'Change colour';
            colorInput.style.cssText = 'width:28px;height:24px;padding:0;border:none;cursor:pointer;background:none';
            colorInput.addEventListener('change', () => _archCatUpdateColor(cat.id, colorInput.value));

            const delBtn = document.createElement('button');
            delBtn.textContent = '\u2715';
            delBtn.title = 'Delete category';
            delBtn.style.cssText = 'background:none;border:none;color:#ef4444;cursor:pointer;font-size:0.85rem;padding:2px 4px';
            delBtn.addEventListener('click', () => _archCatDelete(cat.id, cat.key));

            row.appendChild(colorInput);
            row.appendChild(delBtn);
        }

        list.appendChild(row);
    }
}

async function _archCatUpdateColor(catId, newColor) {
    const project = (allProjects || []).find(p => p.name === currentProject);
    if (!project?.pipeline_template_id) return;
    try {
        await fetch(`${API_BASE}/pipelines/${project.pipeline_template_id}/arch-categories/${catId}`, {
            method: 'PUT',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({color: newColor}),
        });
        await _loadArchCategories();
        renderArchBar();
        const hasTemplate = true;
        _renderArchCatList(hasTemplate);
    } catch (err) {
        console.error('Failed to update arch category color:', err);
    }
}

async function _archCatDelete(catId, key) {
    const project = (allProjects || []).find(p => p.name === currentProject);
    if (!project?.pipeline_template_id) return;
    if (!confirm(`Delete category "${key}"? Arch cards with this category will keep their category tag but it won't be shown in the bar.`)) return;
    try {
        await fetch(`${API_BASE}/pipelines/${project.pipeline_template_id}/arch-categories/${catId}`, {method: 'DELETE'});
        await _loadArchCategories();
        renderArchBar();
        _renderArchCatList(true);
    } catch (err) {
        console.error('Failed to delete arch category:', err);
    }
}

async function archCatAddNew() {
    const project = (allProjects || []).find(p => p.name === currentProject);
    if (!project?.pipeline_template_id) return;
    const key = prompt('Category key (no spaces, e.g. "Infrastructure"):');
    if (!key || !key.trim()) return;
    const label = prompt('Display label:', key.trim()) || key.trim();
    const color = prompt('Hex color (e.g. #60a5fa):', '#60a5fa') || '#60a5fa';
    try {
        const nextPos = _archCategoryList.length;
        await fetch(`${API_BASE}/pipelines/${project.pipeline_template_id}/arch-categories`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({key: key.trim(), label: label.trim(), color, position: nextPos}),
        });
        await _loadArchCategories();
        renderArchBar();
        _renderArchCatList(true);
    } catch (err) {
        console.error('Failed to add arch category:', err);
    }
}

async function populateArchBar() {
    if (!currentProject) return;
    const btn = document.getElementById('arch-bar-populate');
    if (!btn || btn.disabled) return;

    try {
        const r = await fetch(`${API_BASE}/projects/${encodeURIComponent(currentProject)}/populate-arch/preview`);
        const data = await r.json();
        if (!r.ok) throw new Error(data.detail || 'Error');

        showArchPopulateModal(data);
    } catch (e) {
        console.error('populateArchBar preview:', e);
        showToast('Could not fetch preview: ' + e.message, 'error');
    }
}

function showArchPopulateModal(data) {
    const modal = document.getElementById('arch-populate-modal');
    const body = document.getElementById('arch-populate-modal-body');
    const confirmBtn = document.getElementById('arch-populate-confirm-btn');

    let html = '';
    if (!data.has_file_summaries || data.file_summary_count < 3) {
        html += `
            <div style="background:#fff3cd; color:#856404; padding:12px; border-radius:4px; margin-bottom:15px; border:1px solid #ffeeba; font-size: 0.85rem;">
                <strong>⚠ Limited Context:</strong> This project has only ${data.file_summary_count} file summaries. 
                Architecture generation works best with more project context. Consider adding source files or a 
                project description first. Proceed anyway?
            </div>`;
    }

    if (data.categories_to_generate.length === 0) {
        html += `<p style="font-size: 0.9rem;">All architecture categories have already been generated or are currently in progress.</p>`;
        confirmBtn.style.display = 'none';
    } else {
        html += `<p style="font-size: 0.9rem;">The following categories will be analyzed and generated:</p>`;
        html += `<ul style="margin-top:10px; padding-left:20px; font-size: 0.85rem; color: #495057;">`;
        data.categories_to_generate.forEach(cat => {
            html += `<li><strong>${escapeHtml(cat)}</strong></li>`;
        });
        html += `</ul>`;
        confirmBtn.style.display = 'inline-block';
        confirmBtn.onclick = () => executePopulateArch();
    }

    body.innerHTML = html;
    modal.classList.add('active');
}

async function executePopulateArch() {
    const modal = document.getElementById('arch-populate-modal');
    const confirmBtn = document.getElementById('arch-populate-confirm-btn');
    const body = document.getElementById('arch-populate-modal-body');

    confirmBtn.disabled = true;
    confirmBtn.textContent = 'Queueing...';

    try {
        const r = await fetch(`${API_BASE}/projects/${encodeURIComponent(currentProject)}/populate-arch`, { method: 'POST' });
        const data = await r.json();
        if (!r.ok) throw new Error(data.detail || 'Error');
        
        body.innerHTML = `<div style="text-align:center; padding:20px">
            <div style="font-size:2rem; margin-bottom:10px">✅</div>
            <p style="font-size: 1rem;">Queued <strong>${data.queued}</strong> generation jobs.</p>
        </div>`;
        confirmBtn.style.display = 'none';
        
        // Refresh jobs after a short delay
        setTimeout(() => {
            loadArchGenJobs().catch(() => {});
        }, 500);

    } catch (e) {
        showToast('Error: ' + e.message, 'error');
    } finally {
        confirmBtn.disabled = false;
        confirmBtn.textContent = 'Generate';
        // Auto close after 2 seconds on success
        if (!confirmBtn.style.display || confirmBtn.style.display === 'inline-block') {
            // Error case or nothing happened
        } else {
            setTimeout(() => modal.classList.remove('active'), 2000);
        }
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
        const color    = archCategoryMap[category] || archCategoryMap.General || '#6c757d';
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
        const color   = archCategoryMap[job.category] || archCategoryMap.General || '#6c757d';
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
    const btn = document.getElementById('arch-bar-toggle');
    
    // Auto-collapse if empty and no jobs pending, BUT do not persist to localStorage
    // Re-expands automatically if items are added because _archBarCollapsed remains at user's manual preference.
    const isAutoCollapse = (archTasks.length === 0 && _archGenJobs.length === 0);
    const displayCollapsed = isAutoCollapse || _archBarCollapsed;

    if (bar) bar.classList.toggle('collapsed', displayCollapsed);
    if (btn) {
        btn.style.display = isAutoCollapse ? 'none' : '';
        btn.textContent = displayCollapsed ? '\u25BC' : '\u25B2';
    }
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
            const rawCard = createTaskCard(task);
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
            const rawCard = createTaskCard(task);
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
        const stages = ['planning','indev','conceptual_review','optimization','security','human_review','completed'];
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
    } else if (stage === 'final_review' || stage === 'human_review') {
        const fr = s.final_review || {};
        if (fr.has_result) {
            const v = fr.worst_verdict || '';
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
window._sjDiffData = null;
let _sjDiffMode = 'unified'; // 'unified' | 'split'

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
    window._sjDiffData = null;
    _sjDiffMode = 'unified';
}

async function _buildStageJournal(taskId) {
    const task = taskData[taskId];
    const body = document.getElementById('sj-body');
    try {
        const [summaryResp, planResp, compResp, optResp, secResp, frResp, mrResp, diffResp, rjResp, txnResp, docsResp, mathResp] = await Promise.all([
            fetch(`/api/tasks/${taskId}/stage-summary`),
            fetch(`/api/tasks/${taskId}/planning-result`),
            fetch(`/api/tasks/${taskId}/component-status`),
            fetch(`/api/tasks/${taskId}/optimization-status`),
            fetch(`/api/tasks/${taskId}/security-status`),
            fetch(`/api/tasks/${taskId}/final-review-status`),
            fetch(`/api/tasks/${taskId}/merge-status`),
            fetch(`/api/tasks/${taskId}/diff`),
            fetch(`/api/tasks/${taskId}/research-jobs`),
            fetch(`/api/tasks/${taskId}/transition-status`),
            fetch(`/api/tasks/${taskId}/documents`),
            fetch(`/api/tasks/${taskId}/math-status`),
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
        const txn      = txnResp.ok      ? await txnResp.json()      : null;
        const docsList = docsResp.ok     ? await docsResp.json()     : [];
        const mathData = mathResp && mathResp.ok ? await mathResp.json() : null;

        let html = '';

        // ---- Transitions section ----
        const txnHistory = txn && Array.isArray(txn.history) && txn.history.length > 0 ? txn.history : null;
        if (txnHistory) {
            const VERDICT_ORDER = ['REJECTED','NOT_SUITABLE','NEEDS_RESEARCH','POSSIBLE','LIKELY'];
            const latestOutcome = (txnHistory[0].outcome || '').toUpperCase();
            const outcomeOk = latestOutcome === 'ACCEPTED';
            html += `<div class="sj-section">
                <div class="sj-section-title">&#128229; Intake Transitions <span class="sj-badge ${outcomeOk?'ok':'warn'}">${escHtml(latestOutcome)}</span></div>`;
            for (const run of txnHistory) {
                const runOutcome = (run.outcome || '').toUpperCase();
                const runOk = runOutcome === 'ACCEPTED';
                const runCls = runOutcome === 'ACCEPTED' ? 'accepted' : runOutcome === 'REJECTED' ? 'rejected' : 'passed';
                const ts = run.created_at ? new Date(run.created_at).toLocaleString() : '';
                const trigger = run.trigger ? ` · ${escHtml(run.trigger)}` : '';
                html += `<div class="sj-txn-run sj-txn-run--${runCls}">
                    <div class="sj-txn-run-header">
                        <span class="sj-badge ${runOk?'ok':runOutcome==='REJECTED'?'warn':'info'}">${escHtml(runOutcome)}</span>
                        <span class="sj-txn-meta">${escHtml(ts)}${trigger}</span>
                    </div>`;
                if (run.tally_narrative) {
                    html += `<div class="sj-txn-narrative">${escHtml(run.tally_narrative)}</div>`;
                } else if (runOutcome === 'REJECTED') {
                    html += `<div class="sj-txn-narrative" style="color:#6c757d;font-style:italic">No summary recorded — see votes below for individual reviewer reasoning.</div>`;
                }
                const votes = run.votes || [];
                if (votes.length > 0) {
                    for (const v of votes) {
                        const vKey = (v.verdict || '').toUpperCase();
                        const rawConf = v.confidence;
                        const confPct = rawConf != null ? (rawConf <= 1 ? Math.round(rawConf * 100) : Math.round(rawConf)) : null;
                        const confStr = confPct != null ? ` · ${confPct}%` : '';
                        html += `<div class="sj-vote-row">
                            <span class="sj-vote-badge vote-${vKey}">${escHtml(vKey)}</span>
                            <div><strong>${escHtml(v.stage||'')}</strong>${escHtml(confStr)}
                                ${v.justification ? `<div class="sj-check-detail">${escHtml((v.justification||'').slice(0,600))}</div>` : ''}
                            </div>
                        </div>`;
                    }
                }
                html += `</div>`;
            }
            html += '</div>';
        }

        // ---- Math Pipeline section ----
        if (mathData && mathData.is_math_pipeline && mathData.stage_history && mathData.stage_history.length > 0) {
            const MATH_LABELS = {
                LITERATURE_SURVEY: 'Literature Survey',
                PROBLEM_FORMALIZATION: 'Problem Formalization',
                CALIBRATION: 'Calibration',
                COMPUTATIONAL_EXPLORATION: 'Computational Exploration',
                HYPOTHESIS_GENERATION: 'Hypothesis Generation',
                PROOF_STRATEGY: 'Proof Strategy',
                PROOF_ATTEMPT: 'Proof Attempt',
                REFLECTION: 'Reflection',
                FORMAL_VERIFICATION: 'Formal Verification',
                WRITEUP: 'Writeup',
            };

            // Determine overall pipeline outcome from last stage
            const lastStage = mathData.stage_history[mathData.stage_history.length - 1];
            const allPassed = mathData.stage_history.every(s => s.last_exit_reason === 'pass');
            const overallBadge = allPassed
                ? '<span class="sj-badge ok">all stages passed</span>'
                : '<span class="sj-badge info">in progress</span>';

            html += `<div class="sj-section">
                <div class="sj-section-title">&#129518; Math Pipeline ${overallBadge}</div>
                <table class="sj-table sj-math-table">
                    <thead><tr><th>Stage</th><th>Result</th><th>Cycles</th><th>Started</th><th>Duration</th></tr></thead>
                    <tbody>`;

            for (const s of mathData.stage_history) {
                const label = MATH_LABELS[s.stage_key] || s.stage_key;
                const passed = s.last_exit_reason === 'pass';
                const running = !s.last_exit_reason || s.last_exit_reason === null;
                const resultCls = passed ? 'status-done' : running ? '' : 'status-failed';
                const resultTxt = s.last_exit_reason || '…';
                const retryNote = s.error_count > 0
                    ? `<span style="color:#6c757d;font-size:0.7rem;margin-left:0.4rem">(${s.error_count} error${s.error_count > 1 ? 's' : ''} before pass)</span>`
                    : '';
                const cyclesTxt = s.total_cycles === 1 ? '1' : `${s.total_cycles}`;

                let durTxt = '—';
                if (s.first_started_at && s.last_ended_at) {
                    const ms = new Date(s.last_ended_at) - new Date(s.first_started_at);
                    if (ms >= 3600000) durTxt = `${(ms/3600000).toFixed(1)}h`;
                    else if (ms >= 60000) durTxt = `${Math.round(ms/60000)}m`;
                    else durTxt = `${Math.round(ms/1000)}s`;
                }

                const startedTxt = s.first_started_at
                    ? new Date(s.first_started_at).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'})
                    : '—';

                html += `<tr>
                    <td><strong>${escHtml(label)}</strong></td>
                    <td class="${resultCls}">${escHtml(resultTxt)}${retryNote}</td>
                    <td>${escHtml(cyclesTxt)}</td>
                    <td style="color:#6c757d;font-size:0.78rem">${escHtml(startedTxt)}</td>
                    <td style="color:#6c757d;font-size:0.78rem">${escHtml(durTxt)}</td>
                </tr>`;
            }

            html += '</tbody></table>';

            // ---- Lean4 Source & Output (if stored from FORMAL_VERIFICATION) ----
            if (mathData.lean4_source || mathData.lean4_output) {
                const fvStage = mathData.stage_history.find(s => s.stage_key === 'FORMAL_VERIFICATION');
                const fvPassed = fvStage && fvStage.last_exit_reason === 'pass';
                const fvBadge = fvPassed
                    ? '<span class="sj-badge ok">&#10003; compiled</span>'
                    : '<span class="sj-badge warn">&#10007; failed</span>';

                html += `<div style="margin-top:0.8rem">
                    <div style="font-size:0.75rem;font-weight:700;color:#6c757d;margin-bottom:0.4rem">
                        &#9671; Lean4 Formal Verification ${fvBadge}
                    </div>`;

                if (mathData.lean4_source) {
                    html += `<details class="sj-doc-item" open>
                        <summary class="sj-doc-key">&#128196; Lean4 source (.lean)</summary>
                        <pre class="sj-doc-content sj-lean4-source">${escHtml(mathData.lean4_source)}</pre>
                    </details>`;
                }
                if (mathData.lean4_output) {
                    const hasError = mathData.lean4_output.toLowerCase().includes('error');
                    html += `<details class="sj-doc-item">
                        <summary class="sj-doc-key" style="${hasError?'color:#dc3545':''}">
                            &#128196; Compiler output${hasError ? ' — errors found' : ' — clean'}
                        </summary>
                        <pre class="sj-doc-content sj-lean4-output">${escHtml(mathData.lean4_output)}</pre>
                    </details>`;
                }
                html += '</div>';
            }

            html += '</div>';

            // ---- Reflection Report (extracted from doc store for prominence) ----
            const reflDoc = docsList.find(d => d.key && d.key.startsWith('reflection:'));
            if (reflDoc && reflDoc.content) {
                let reflData = null;
                try { reflData = JSON.parse(reflDoc.content); } catch(e) {}
                if (reflData) {
                    const conf = reflData.confidence != null ? Math.round(reflData.confidence * 100) : null;
                    const confStr = conf != null ? `${conf}%` : '';
                    const confCls = conf != null ? (conf >= 80 ? 'ok' : conf >= 50 ? 'info' : 'warn') : 'muted';
                    const issues = reflData.issues || [];
                    const blocking = issues.filter(i => i.severity === 'blocking');
                    const warnings = issues.filter(i => i.severity === 'warning');
                    const notes = issues.filter(i => i.severity === 'note');

                    html += `<div class="sj-section">
                        <div class="sj-section-title">&#129504; Reflection Report
                            ${confStr ? `<span class="sj-badge ${confCls}">confidence ${escHtml(confStr)}</span>` : ''}
                            ${blocking.length ? `<span class="sj-badge warn">${blocking.length} blocking</span>` : ''}
                        </div>`;

                    if (blocking.length || warnings.length || notes.length) {
                        for (const issue of issues) {
                            const sevCls = issue.severity === 'blocking' ? '#dc3545' : issue.severity === 'warning' ? '#fd7e14' : '#6c757d';
                            html += `<div class="sj-check-row">
                                <span class="sj-check-icon" style="color:${sevCls};font-size:0.75rem;min-width:5rem;font-weight:700;text-transform:uppercase">${escHtml(issue.severity)}</span>
                                <div class="sj-check-detail">${escHtml(issue.finding || '')}</div>
                            </div>`;
                        }
                    } else {
                        html += `<div style="color:#198754;font-size:0.85rem">No issues found.</div>`;
                    }

                    if (reflData.uncertain_about && reflData.uncertain_about.length) {
                        html += `<div style="font-size:0.75rem;color:#6c757d;margin-top:0.5rem"><em>Uncertain about:</em> ${escHtml(reflData.uncertain_about.join('; '))}</div>`;
                    }
                    html += '</div>';
                }
            }
        }

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

        // ---- Acceptance Criteria section ----
        if (task && task.acceptance_criteria && task.acceptance_criteria.length) {
            html += `<div class="sj-section">
                <div class="sj-section-title">&#10003; Acceptance Criteria</div>
                <ol style="margin:0.3rem 0 0 1.2rem;font-size:0.85rem;line-height:1.6">`;
            task.acceptance_criteria.forEach((c, i) => {
                html += `<li style="margin-bottom:0.25rem">${escHtml(typeof c === 'string' ? c : JSON.stringify(c))}</li>`;
            });
            html += '</ol></div>';
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
            window._sjDiffData = diffData;
            const branch = diffData.branch || '';
            const method = diffData.method;
            const hasDiff = diffData.diff && diffData.diff.trim().length > 0;
            const methodLabel = method === 'merge_commit'
                ? `<span class="sj-badge muted">merge commit ${escHtml((diffData.head_ref||'').slice(0,8))}</span>`
                : `<span class="sj-badge info">${escHtml(branch)}</span>`;
            html += `<div class="sj-section" id="sj-diff-section">
                <div class="sj-section-title">&#9998; Code Diff ${methodLabel}${hasDiff ? `<button class="sj-diff-mode-toggle" id="sj-split-toggle" onclick="_sjToggleDiffSplit()">&#9889; Split</button>` : ''}</div>`;
            if (hasDiff) {
                if (diffData.stat) {
                    // Parse "3 files changed, 142 insertions(+), 38 deletions(-)" into a styled banner
                    const _statRaw = diffData.stat.trim().split('\n').pop() || diffData.stat.trim();
                    const _mFiles = _statRaw.match(/(\d+) files? changed/);
                    const _mAdd   = _statRaw.match(/(\d+) insertion/);
                    const _mDel   = _statRaw.match(/(\d+) deletion/);
                    if (_mFiles || _mAdd || _mDel) {
                        html += `<div class="sj-diff-stat-banner">` +
                            (_mFiles ? `<span class="stat-files">${_mFiles[1]} file${+_mFiles[1]===1?'':'s'} changed</span>` : '') +
                            (_mAdd   ? `<span class="stat-add">+${_mAdd[1]}</span>` : '') +
                            (_mDel   ? `<span class="stat-del">&#x2212;${_mDel[1]}</span>` : '') +
                            `</div>`;
                    } else {
                        html += `<div class="diff-stat">${escHtml(_statRaw)}</div>`;
                    }
                }
                html += `<div id="sj-diff-content">${_renderDiff(diffData.diff)}</div>`;
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

        // ---- Stage Outputs section (task.content output_keys) ----
        // For math pipelines, lean4_source and lean4_output are shown in the Math
        // Pipeline section above — exclude them here to avoid duplication.
        const _mathShownKeys = (mathData && mathData.is_math_pipeline)
            ? new Set(['lean4_source', 'lean4_output']) : new Set();
        const content = task && task.content;
        if (content && typeof content === 'object' && !Array.isArray(content)) {
            const keys = Object.keys(content).filter(k => !k.startsWith('_') && !_mathShownKeys.has(k));
            if (keys.length > 0) {
                html += `<details class="sj-section sj-outputs-details">
                    <summary class="sj-section-title">&#128216; Stage Outputs (${keys.length})</summary>`;
                for (const k of keys) {
                    const val = content[k];
                    const valStr = typeof val === 'string' ? val : JSON.stringify(val, null, 2);
                    html += `<details class="sj-doc-item">
                        <summary class="sj-doc-key">${escHtml(k)}</summary>
                        <pre class="sj-doc-content">${escHtml(valStr)}</pre>
                    </details>`;
                }
                html += '</details>';
            }
        }

        // ---- Documents section (project doc store) ----
        // For math pipelines, reflection documents are shown in the Math Pipeline
        // section above — exclude them here to avoid duplication.
        const _mathShownDocs = (mathData && mathData.is_math_pipeline)
            ? new Set(docsList.filter(d => d.key && d.key.startsWith('reflection:')).map(d => d.key))
            : new Set();
        const _visibleDocs = docsList.filter(d => !_mathShownDocs.has(d.key));
        if (_visibleDocs.length > 0) {
            html += `<details class="sj-section sj-outputs-details">
                <summary class="sj-section-title">&#128196; Documents (${_visibleDocs.length})</summary>`;
            for (const doc of _visibleDocs) {
                const tags = (doc.tags || []).map(t =>
                    `<span class="sj-badge muted" style="margin-left:0.3rem;font-size:0.65rem">${escHtml(t)}</span>`
                ).join('');
                let display = doc.content || '';
                if (display.trimStart().startsWith('{') || display.trimStart().startsWith('[')) {
                    try { display = JSON.stringify(JSON.parse(display), null, 2); } catch(e) {}
                }
                html += `<details class="sj-doc-item">
                    <summary class="sj-doc-key">${escHtml(doc.key || '')}${tags}</summary>
                    <pre class="sj-doc-content">${escHtml(display)}</pre>
                </details>`;
            }
            html += '</details>';
        }

        if (!html) {
            html = '<div style="color:#6c757d;font-size:0.9rem;padding:1rem 0">No pipeline artifacts yet for this card.</div>';
        }

        body.innerHTML = html;
        _sjApplySyntaxHighlighting(body);
        _sjSetupHunkComments(taskId, body);

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

function _parseDiffFiles(diffText) {
    // Returns [{name, lines}] where each line is {type, content, oldNum, newNum}
    const rawLines = diffText.split('\n');
    const files = [];
    let current = null;
    let oldNum = 0, newNum = 0;

    for (const line of rawLines) {
        if (line.startsWith('diff --git')) {
            if (current) files.push(current);
            const fname = line.replace('diff --git ', '').split(' b/').pop() || line;
            current = { name: fname, lines: [] };
            oldNum = 0; newNum = 0;
            continue;
        }
        if (!current) continue;
        if (line.startsWith('index ') || line.startsWith('--- ') || line.startsWith('+++ ')) continue;
        if (line.startsWith('@@')) {
            const mOld = line.match(/@@ -(\d+)/);
            const mNew = line.match(/\+(\d+)/);
            oldNum = mOld ? parseInt(mOld[1], 10) - 1 : oldNum;
            newNum = mNew ? parseInt(mNew[1], 10) - 1 : newNum;
            current.lines.push({ type: 'hunk', content: line, oldNum: null, newNum: null });
        } else if (line.startsWith('+') && !line.startsWith('+++')) {
            newNum++;
            current.lines.push({ type: 'add', content: line.slice(1), oldNum: null, newNum });
        } else if (line.startsWith('-') && !line.startsWith('---')) {
            oldNum++;
            current.lines.push({ type: 'del', content: line.slice(1), oldNum, newNum: null });
        } else if (line.startsWith(' ') || line === '') {
            oldNum++; newNum++;
            current.lines.push({ type: 'ctx', content: line.slice(1), oldNum, newNum });
        }
    }
    if (current) files.push(current);
    return files;
}

function _renderUnifiedFile(file, fileIdx) {
    let html = '';
    let hunkIdx = -1;
    for (const line of file.lines) {
        if (line.type === 'hunk') {
            hunkIdx++;
            const hk = `${fileIdx}:${hunkIdx}`;
            html += `<div class="diff-hunk-header" data-hunk="${escHtml(hk)}">` +
                `<span class="diff-hunk-text">${escHtml(line.content)}</span>` +
                `<button class="diff-comment-btn" title="Add review note" onclick="_sjToggleHunkComment(this)">&#128172;</button>` +
                `</div>` +
                `<div class="diff-hunk-comment" data-hunk="${escHtml(hk)}" style="display:none">` +
                `<textarea class="diff-hunk-textarea" rows="2" placeholder="Review note for this hunk…"></textarea>` +
                `<div class="diff-hunk-comment-actions">` +
                `<button class="diff-comment-save" onclick="_sjSaveHunkComment(this)">Save</button>` +
                `<button class="diff-comment-cancel" onclick="_sjCancelHunkComment(this)">Cancel</button>` +
                `</div></div>`;
        } else if (line.type === 'add') {
            html += `<div class="diff-line diff-line-add">` +
                `<span class="diff-gutter">${line.newNum}</span>` +
                `<span class="diff-sigil">+</span>` +
                `<span class="diff-content">${escHtml(line.content)}</span>` +
                `</div>`;
        } else if (line.type === 'del') {
            html += `<div class="diff-line diff-line-del">` +
                `<span class="diff-gutter">${line.oldNum}</span>` +
                `<span class="diff-sigil">-</span>` +
                `<span class="diff-content">${escHtml(line.content)}</span>` +
                `</div>`;
        } else if (line.type === 'ctx') {
            html += `<div class="diff-line diff-line-ctx">` +
                `<span class="diff-gutter">${line.newNum}</span>` +
                `<span class="diff-sigil"> </span>` +
                `<span class="diff-content">${escHtml(line.content)}</span>` +
                `</div>`;
        }
    }
    return html;
}

function _renderSplitFile(file, fileIdx) {
    let html = '<table class="diff-split-table"><tbody>';
    let hunkIdx = -1;
    let i = 0;
    const lines = file.lines;

    while (i < lines.length) {
        const line = lines[i];
        if (line.type === 'hunk') {
            hunkIdx++;
            const hk = `${fileIdx}:${hunkIdx}`;
            html += `<tr class="diff-split-hunk-row">` +
                `<td colspan="2" class="diff-hunk-header" data-hunk="${escHtml(hk)}">` +
                `<span class="diff-hunk-text">${escHtml(line.content)}</span>` +
                `<button class="diff-comment-btn" title="Add review note" onclick="_sjToggleHunkComment(this)">&#128172;</button>` +
                `</td></tr>`;
            html += `<tr class="diff-hunk-comment-row" data-hunk="${escHtml(hk)}" style="display:none">` +
                `<td colspan="2" class="diff-hunk-comment">` +
                `<textarea class="diff-hunk-textarea" rows="2" placeholder="Review note for this hunk…"></textarea>` +
                `<div class="diff-hunk-comment-actions">` +
                `<button class="diff-comment-save" onclick="_sjSaveHunkComment(this)">Save</button>` +
                `<button class="diff-comment-cancel" onclick="_sjCancelHunkComment(this)">Cancel</button>` +
                `</div></td></tr>`;
            i++;
        } else if (line.type === 'ctx') {
            html += `<tr><td class="diff-split-cell diff-split-old diff-line-ctx">` +
                `<span class="diff-gutter">${line.oldNum}</span>` +
                `<span class="diff-content">${escHtml(line.content)}</span>` +
                `</td><td class="diff-split-cell diff-split-new diff-line-ctx">` +
                `<span class="diff-gutter">${line.newNum}</span>` +
                `<span class="diff-content">${escHtml(line.content)}</span>` +
                `</td></tr>`;
            i++;
        } else {
            // Collect consecutive del/add block and pair them side-by-side
            const dels = [], adds = [];
            while (i < lines.length && (lines[i].type === 'del' || lines[i].type === 'add')) {
                if (lines[i].type === 'del') dels.push(lines[i]);
                else adds.push(lines[i]);
                i++;
            }
            const count = Math.max(dels.length, adds.length);
            for (let p = 0; p < count; p++) {
                const d = dels[p], a = adds[p];
                html += `<tr>` +
                    `<td class="diff-split-cell diff-split-old${d ? ' diff-line-del' : ' diff-split-empty'}">` +
                    (d ? `<span class="diff-gutter">${d.oldNum}</span><span class="diff-content">${escHtml(d.content)}</span>` : '') +
                    `</td>` +
                    `<td class="diff-split-cell diff-split-new${a ? ' diff-line-add' : ' diff-split-empty'}">` +
                    (a ? `<span class="diff-gutter">${a.newNum}</span><span class="diff-content">${escHtml(a.content)}</span>` : '') +
                    `</td></tr>`;
            }
        }
    }
    html += '</tbody></table>';
    return html;
}

function _renderDiff(diffText) {
    const files = _parseDiffFiles(diffText);
    if (files.length === 0) return '';
    const renderFile = _sjDiffMode === 'split' ? _renderSplitFile : _renderUnifiedFile;

    if (files.length === 1) {
        const ext = files[0].name.split('.').pop().toLowerCase();
        return `<div class="diff-viewer" data-lang="${escHtml(ext)}">${renderFile(files[0], 0)}</div>`;
    }
    // Multiple files — render as tabs
    let tabsHtml = '<div class="sj-diff-tabs">';
    let panelsHtml = '<div class="sj-diff-panels">';
    files.forEach((f, i) => {
        const ext = f.name.split('.').pop().toLowerCase();
        const activeClass = i === 0 ? ' active' : '';
        tabsHtml += `<button class="sj-diff-tab${activeClass}" title="${escHtml(f.name)}" onclick="_sjSelectDiffTab(this,${i})">${escHtml(f.name.split('/').pop())}</button>`;
        panelsHtml += `<div class="sj-diff-panel${activeClass}">` +
            `<div class="diff-viewer" data-lang="${escHtml(ext)}">${renderFile(f, i)}</div></div>`;
    });
    tabsHtml += '</div>';
    panelsHtml += '</div>';
    return tabsHtml + panelsHtml;
}

function _sjSelectDiffTab(tabEl, idx) {
    const tabsEl = tabEl.closest('.sj-diff-tabs');
    const panelsEl = tabsEl.nextElementSibling;
    tabsEl.querySelectorAll('.sj-diff-tab').forEach((t, i) => t.classList.toggle('active', i === idx));
    panelsEl.querySelectorAll('.sj-diff-panel').forEach((p, i) => p.classList.toggle('active', i === idx));
}

function _sjToggleDiffSplit() {
    _sjDiffMode = _sjDiffMode === 'unified' ? 'split' : 'unified';
    const btn = document.getElementById('sj-split-toggle');
    if (btn) {
        btn.innerHTML = _sjDiffMode === 'split' ? '&#9889; Unified' : '&#9889; Split';
        btn.classList.toggle('active', _sjDiffMode === 'split');
    }
    const content = document.getElementById('sj-diff-content');
    if (!content || !window._sjDiffData) return;
    content.innerHTML = _renderDiff(window._sjDiffData.diff);
    _sjApplySyntaxHighlighting(content);
    _sjSetupHunkComments(window._sjTaskId, content);
}

function _sjApplySyntaxHighlighting(containerEl) {
    if (typeof Prism === 'undefined') return;
    const langMap = {
        py: 'python', js: 'javascript', ts: 'typescript', jsx: 'javascript', tsx: 'typescript',
        css: 'css', html: 'markup', xml: 'markup', json: 'json',
        sh: 'bash', bash: 'bash', zsh: 'bash',
        go: 'go', rs: 'rust', sql: 'sql', yaml: 'yaml', yml: 'yaml',
        md: 'markdown', java: 'java', cpp: 'cpp', c: 'c', h: 'c',
        rb: 'ruby', php: 'php', kt: 'kotlin', swift: 'swift',
    };
    containerEl.querySelectorAll('[data-lang]').forEach(el => {
        const ext = el.getAttribute('data-lang');
        const lang = langMap[ext];
        if (!lang || !Prism.languages[lang]) return;
        el.querySelectorAll('.diff-content').forEach(span => {
            try {
                span.innerHTML = Prism.highlight(span.textContent, Prism.languages[lang], lang);
            } catch {}
        });
    });
}

function _sjSetupHunkComments(taskId, containerEl) {
    const task = taskData[taskId];
    let comments = {};
    if (task && task.review_notes) {
        try {
            const p = JSON.parse(task.review_notes);
            if (p && p.v === 1) comments = p.hunks || {};
        } catch {}
    }
    Object.entries(comments).forEach(([hunkKey, text]) => {
        if (!text) return;
        const commentEl = [...containerEl.querySelectorAll('.diff-hunk-comment, .diff-hunk-comment-row')]
            .find(el => el.getAttribute('data-hunk') === hunkKey);
        if (commentEl) {
            const ta = commentEl.querySelector('.diff-hunk-textarea');
            if (ta) ta.value = text;
        }
        const hunkHeaderEl = [...containerEl.querySelectorAll('[data-hunk]')]
            .find(el => el.getAttribute('data-hunk') === hunkKey &&
                  !el.classList.contains('diff-hunk-comment') &&
                  !el.classList.contains('diff-hunk-comment-row'));
        if (hunkHeaderEl) {
            const commentBtn = hunkHeaderEl.querySelector('.diff-comment-btn');
            if (commentBtn) commentBtn.classList.add('diff-comment-has-note');
        }
    });
}

function _sjToggleHunkComment(btn) {
    const hunkEl = btn.closest('[data-hunk]');
    if (!hunkEl) return;
    const hunkKey = hunkEl.getAttribute('data-hunk');
    const commentEl = [...document.querySelectorAll('.diff-hunk-comment, .diff-hunk-comment-row')]
        .find(el => el.getAttribute('data-hunk') === hunkKey);
    if (!commentEl) return;
    const isHidden = commentEl.style.display === 'none';
    commentEl.style.display = isHidden ? '' : 'none';
    if (isHidden) commentEl.querySelector('.diff-hunk-textarea')?.focus();
}

function _sjCancelHunkComment(btn) {
    const commentEl = btn.closest('.diff-hunk-comment') || btn.closest('.diff-hunk-comment-row');
    if (commentEl) commentEl.style.display = 'none';
}

async function _sjSaveHunkComment(btn) {
    const taskId = window._sjTaskId;
    if (!taskId) return;
    const commentEl = btn.closest('.diff-hunk-comment') || btn.closest('.diff-hunk-comment-row');
    if (!commentEl) return;
    const hunkKey = commentEl.getAttribute('data-hunk');
    if (!hunkKey) return;
    const ta = commentEl.querySelector('.diff-hunk-textarea');
    const text = ta ? ta.value.trim() : '';

    const task = taskData[taskId];
    let notesData = { v: 1, hunks: {} };
    if (task && task.review_notes) {
        try {
            const p = JSON.parse(task.review_notes);
            if (p && p.v === 1) notesData = p;
        } catch {}
    }
    if (text) notesData.hunks[hunkKey] = text;
    else delete notesData.hunks[hunkKey];

    btn.disabled = true;
    try {
        const r = await fetch(`/api/tasks/${taskId}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ review_notes: JSON.stringify(notesData) }),
        });
        if (r.ok) {
            if (taskData[taskId]) taskData[taskId].review_notes = JSON.stringify(notesData);
            commentEl.style.display = 'none';
            const hunkHeaderEl = [...document.querySelectorAll('[data-hunk]')]
                .find(el => el.getAttribute('data-hunk') === hunkKey &&
                      !el.classList.contains('diff-hunk-comment') &&
                      !el.classList.contains('diff-hunk-comment-row'));
            const commentBtn = hunkHeaderEl?.querySelector('.diff-comment-btn');
            if (commentBtn) commentBtn.classList.toggle('diff-comment-has-note', !!text);
            _sjShowToast('Note saved.', 'success');
        } else {
            _sjShowToast('Failed to save note.', 'error');
        }
    } catch (e) {
        _sjShowToast('Error: ' + e.message, 'error');
    } finally {
        btn.disabled = false;
    }
}

function _sjToggleFullscreen() {
    const modal = document.getElementById('sj-modal-inner');
    const btn = document.getElementById('sj-expand-btn');
    const full = modal.classList.toggle('sj-fullscreen');
    btn.title = full ? 'Exit fullscreen' : 'Toggle fullscreen';
    btn.innerHTML = full ? '&#x2715;&#xFE0E;' : '&#x26F6;';

    let toolbar = document.getElementById('sj-fullscreen-toolbar');
    if (full) {
        if (!toolbar) {
            toolbar = document.createElement('div');
            toolbar.id = 'sj-fullscreen-toolbar';
            toolbar.className = 'sj-fullscreen-toolbar';
        }
        const taskId = window._sjTaskId;
        const task = taskId && taskData[taskId];
        const stage = task ? task.type : '';
        const isHumanReview = stage === 'human_review';
        toolbar.innerHTML = `
            <span class="sj-toolbar-title">${task ? escHtml(task.title) : ''}</span>
            <div class="sj-toolbar-actions">
                ${isHumanReview ? `
                <button class="sj-toolbar-btn sj-btn-approve" onclick="_sjApproveMerge('${escHtml(taskId)}')">
                    &#10003; Approve &amp; Merge
                </button>
                <button class="sj-toolbar-btn sj-btn-reject" onclick="_sjRequestChanges('${escHtml(taskId)}')">
                    &#10007; Request Changes
                </button>` : `<span class="sj-toolbar-stage-note">Stage: <b>${escHtml(stage)}</b> — approve/merge available in Human Review</span>`}
            </div>`;
        modal.insertBefore(toolbar, modal.firstChild);
    } else if (toolbar) {
        toolbar.remove();
    }
}

async function _sjApproveMerge(taskId) {
    if (!confirm('Approve and merge this task to main? This will run the final merge pipeline.')) return;
    const btn = document.querySelector('.sj-btn-approve');
    if (btn) { btn.disabled = true; btn.textContent = 'Merging…'; }
    try {
        const res = await fetch(`/api/tasks/${taskId}/merge`, {method: 'POST'});
        const data = await res.json();
        if (res.ok) {
            _sjShowToast('Merge started — task will move to Completed when done.', 'success');
        } else {
            _sjShowToast(`Merge failed: ${data.detail || JSON.stringify(data)}`, 'error');
        }
    } catch (e) {
        _sjShowToast(`Merge error: ${e.message}`, 'error');
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = '✓ Approve & Merge'; }
    }
}

async function _sjRequestChanges(taskId) {
    const reason = prompt('Reason for requesting changes (will be appended to task history):');
    if (!reason) return;
    try {
        await fetch(`/api/tasks/${taskId}/demote`, {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({target: 'indev', reason}),
        });
        _sjShowToast('Task demoted to INDEV with your feedback.', 'success');
        closeStageJournal();
        loadTasksFromDatabase();
    } catch (e) {
        _sjShowToast(`Error: ${e.message}`, 'error');
    }
}

function _sjShowToast(msg, type) {
    let toast = document.getElementById('sj-toast');
    if (!toast) {
        toast = document.createElement('div');
        toast.id = 'sj-toast';
        toast.className = 'sj-toast';
        document.body.appendChild(toast);
    }
    toast.textContent = msg;
    toast.className = `sj-toast sj-toast-${type} sj-toast-visible`;
    clearTimeout(toast._timer);
    toast._timer = setTimeout(() => toast.classList.remove('sj-toast-visible'), 4000);
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
    // Load projects and pipeline template list in parallel before tasks load
    await Promise.all([loadProjects(), loadPipelineTemplates()]);
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

    // Load inbox, start badge polling, and start escalation poll
    await loadInbox();
    _inboxPollInterval = setInterval(refreshInboxBadge, 60_000);
    _startEscalationPoll();
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
                    queueBtn.textContent = total > 0 ? `⚙ ${total}` : '⚙';
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
        stopped: schedulerData.stopped || [],
    };

    // Build fast lookup maps: taskId → entry
    const activeMap = {};
    (_schedulerState.active || []).forEach(item => { activeMap[item.id] = item; });
    const queuedMap = {};
    (_schedulerState.queued || []).forEach(item => { queuedMap[item.id] = item; });
    const stoppedMap = {};
    (_schedulerState.stopped || []).forEach(item => { stoppedMap[item.id] = item; });

    // Walk all cached cards and update their indicator element.
    Object.keys(cardCache).forEach(taskId => {
        const el = document.getElementById(`ji-${taskId}`);
        if (!el) return;

        let html = '';
        if (activeMap[taskId]) {
            const item = activeMap[taskId];
            const llm = item.llm_name || '';
            if (item.zombie) {
                const idle = item.idle_minutes > 60 
                    ? Math.round(item.idle_minutes/60) + 'h' 
                    : Math.round(item.idle_minutes) + 'm';
                html = `<span class="ji-dot"></span><span class="ji-label">Zombie \u00b7 ${idle} idle</span>`;
                el.className = 'card-job-indicator ji-zombie';
            } else {
                html = `<span class="ji-dot"></span><span class="ji-label">Running${llm ? ' \u00b7 ' + escapeHtml(llm) : ''}</span>`;
                el.className = 'card-job-indicator ji-running';
            }
        } else if (stoppedMap[taskId]) {
            const reason = stoppedMap[taskId].reason || 'planning failed';
            html = `<span class="ji-dot"></span><span class="ji-label">Stopped \u00b7 ${escapeHtml(reason)}</span>`;
            el.className = 'card-job-indicator ji-stopped';
        } else if (queuedMap[taskId]) {
            const reason = queuedMap[taskId].reason || 'pending';
            if (reason === 'awaiting_approval') {
                html = `<span class="ji-dot"></span><span class="ji-label">Awaiting approval</span>`;
                el.className = 'card-job-indicator ji-queued-human';
            } else {
                html = `<span class="ji-dot"></span><span class="ji-label">Queued \u00b7 ${escapeHtml(reason)}</span>`;
                el.className = 'card-job-indicator ji-queued';
            }
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

// ============================================================
// Pipeline template switcher
// ============================================================

async function loadPipelineTemplates() {
    try {
        const resp = await fetch(`${API_BASE}/pipelines`);
        allPipelineTemplates = resp.ok ? await resp.json() : [];
    } catch (_) {
        allPipelineTemplates = [];
    }
}

function populatePipelineDropdown() {
    const sel = document.getElementById('pipeline-select');
    if (!sel) return;
    const activeProject = allProjects.find(p => p.name === currentProject);
    const activeTid = activeProject?.pipeline_template_id ?? null;
    sel.innerHTML = allPipelineTemplates.map(t =>
        `<option value="${t.id}"${t.id === activeTid ? ' selected' : ''}>${t.name}</option>`
    ).join('');
    const editLink = document.getElementById('pipeline-edit-link');
    if (editLink) {
        const tid = activePipelineTemplate?.id || activeTid;
        editLink.href = tid ? `/pipelines/${tid}/edit` : '/pipelines';
    }
}

async function onPipelineSelectChange(templateIdStr) {
    const templateId = parseInt(templateIdStr, 10);
    if (!currentProject) return;
    const resp = await fetch(`${API_BASE}/projects/${encodeURIComponent(currentProject)}/pipeline`, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({template_id: templateId}),
    });
    if (!resp.ok) {
        console.error('Failed to switch pipeline:', await resp.text());
        return;
    }
    const proj = allProjects.find(p => p.name === currentProject);
    if (proj) proj.pipeline_template_id = templateId;
    await loadTasksFromDatabase();
    renderTasksFromDatabase();
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
            container.appendChild(_buildProjectTab(p.name, p.path, p.description, p.llm_id, p.budget_id, p.autopilot_budget_id, p.autopilot_max_in_flight, p.exclude_from_training, p.enabled !== false));
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

function _buildProjectTab(name, path, description, llmId, budgetId, autopilotBudgetId, autopilotMaxInFlight, excludeFromTraining, enabled = true) {
    const tab = document.createElement('div');
    tab.className = 'project-tab';
    if (!enabled) tab.classList.add('project-tab--disabled');
    tab.setAttribute('data-project', name);

    const label = document.createElement('span');
    label.className = 'project-tab-label';
    label.textContent = `📁 ${name}`;
    const titleParts = [path ? `Path: ${path}` : 'No path configured'];
    if (!enabled) titleParts.push('(disabled — scheduler paused)');
    label.title = titleParts.join(' · ');
    label.addEventListener('click', () => switchProject(name));

    const gear = document.createElement('button');
    gear.className = 'project-tab-gear';
    gear.textContent = '⚙';
    gear.title = 'Edit project settings';
    gear.addEventListener('click', (e) => {
        e.stopPropagation();
        openEditProjectModal(name, path || '', description || '', llmId || null, budgetId || null, autopilotBudgetId || null, autopilotMaxInFlight != null ? autopilotMaxInFlight : 10, excludeFromTraining || false, enabled !== false);
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

    document.getElementById('training-status-modal').addEventListener('click', function(e) {
        if (e.target === this && _modalMousedownTarget === this) closeTrainingStatusModal();
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
    // Clean up prerequisites selector listeners
    if (window._prereqCloseDropdown) {
        document.removeEventListener('click', window._prereqCloseDropdown);
        window._prereqCloseDropdown = null;
    }
    _prereqSelectedIds = [];
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
    document.getElementById('new-project-path-warn').style.display = 'none';
    document.getElementById('new-project-create-path').checked = false;
    populateProjectLlmSelect('new-project-llm-select', null);
    populateProjectBudgetSelect('new-project-budget-select', null);
    document.getElementById('new-project-modal').classList.add('active');
    document.getElementById('new-project-name').focus();
}

async function browseFolder(inputId) {
    try {
        const resp = await fetch(`${API_BASE}/system/browse-folder`);
        if (!resp.ok) return;
        const data = await resp.json();
        if (data.path) document.getElementById(inputId).value = data.path;
    } catch (_) { /* picker cancelled or unavailable */ }
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
    const create_if_missing = document.getElementById('new-project-create-path').checked;
    const errEl = document.getElementById('new-project-error');
    const warnEl = document.getElementById('new-project-path-warn');

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
            body: JSON.stringify({ name, path, description, llm_id, budget_id, create_if_missing }),
        });
        if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            if (resp.status === 422 && err.detail && err.detail.error === 'path_not_found') {
                warnEl.style.display = 'block';
                errEl.style.display = 'none';
                return;
            }
            errEl.textContent = (typeof err.detail === 'string' ? err.detail : null) || `Error ${resp.status}`;
            errEl.style.display = 'block';
            return;
        }
        warnEl.style.display = 'none';
        closeNewProjectModal();
        await loadProjects();
        switchProject(name);
    } catch (err) {
        errEl.textContent = `Network error: ${err.message}`;
        errEl.style.display = 'block';
    }
}

function openEditProjectModal(name, path, description, llmId, budgetId, autopilotBudgetId, autopilotMaxInFlight, excludeFromTraining, enabled = true) {
    document.getElementById('edit-project-original-name').value = name;
    document.getElementById('edit-project-modal-title').textContent = `Edit: ${name}`;
    document.getElementById('edit-project-name-input').value = name;
    document.getElementById('edit-project-path').value = path;
    document.getElementById('edit-project-description').value = description;
    document.getElementById('edit-project-error').style.display = 'none';
    document.getElementById('edit-project-path-warn').style.display = 'none';
    document.getElementById('edit-project-create-path').checked = false;
    populateProjectLlmSelect('edit-project-llm-select', llmId || null);
    populateProjectBudgetSelect('edit-project-budget-select', budgetId || null);
    populateProjectBudgetSelect('edit-project-autopilot-budget-select', autopilotBudgetId || null);
    document.getElementById('edit-project-max-in-flight').value = autopilotMaxInFlight != null ? autopilotMaxInFlight : 10;
    document.getElementById('edit-project-exclude-training').checked = excludeFromTraining === true;
    document.getElementById('edit-project-enabled').checked = enabled !== false;
    epCancelObjectiveForm();
    epLoadObjectives(name);
    loadProjectRouting(name);
    loadCostByModel(name);
    document.getElementById('edit-project-modal').classList.add('active');
    document.getElementById('edit-project-path').focus();
}

function closeEditProjectModal() {
    document.getElementById('edit-project-modal').classList.remove('active');
}

async function saveEditProject() {
    const originalName = document.getElementById('edit-project-original-name').value;
    const newName = document.getElementById('edit-project-name-input').value.trim();
    const path = document.getElementById('edit-project-path').value.trim();
    const description = document.getElementById('edit-project-description').value.trim();
    const llmVal = document.getElementById('edit-project-llm-select').value;
    const llm_id = llmVal ? parseInt(llmVal, 10) : null;
    const budgetVal = document.getElementById('edit-project-budget-select').value;
    const budget_id = budgetVal ? parseInt(budgetVal, 10) : null;
    const apBudgetVal = document.getElementById('edit-project-autopilot-budget-select').value;
    const autopilot_budget_id = apBudgetVal ? parseInt(apBudgetVal, 10) : null;
    const maxInFlightVal = document.getElementById('edit-project-max-in-flight').value;
    const autopilot_max_in_flight = maxInFlightVal ? parseInt(maxInFlightVal, 10) : 10;
    const exclude_from_training = document.getElementById('edit-project-exclude-training').checked;
    const enabled = document.getElementById('edit-project-enabled').checked;
    const create_if_missing = document.getElementById('edit-project-create-path').checked;
    const errEl = document.getElementById('edit-project-error');
    const warnEl = document.getElementById('edit-project-path-warn');

    if (!newName) {
        errEl.textContent = 'Project name is required.';
        errEl.style.display = 'block';
        return;
    }

    try {
        const resp = await fetch(`${API_BASE}/projects/${encodeURIComponent(originalName)}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: newName, path, description, llm_id, budget_id, autopilot_budget_id, autopilot_max_in_flight, exclude_from_training, enabled, create_if_missing }),
        });
        if (!resp.ok) {
            const err = await resp.json().catch(() => ({}));
            if (resp.status === 422 && err.detail && err.detail.error === 'path_not_found') {
                warnEl.style.display = 'block';
                errEl.style.display = 'none';
                return;
            }
            errEl.textContent = (typeof err.detail === 'string' ? err.detail : null) || `Error ${resp.status}`;
            errEl.style.display = 'block';
            return;
        }
        warnEl.style.display = 'none';
        closeEditProjectModal();
        const wasActive = currentProject === originalName;
        await loadProjects();
        if (wasActive && newName !== originalName) {
            switchProject(newName);
        }
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

// ──────────────────────────────────────────────────────────────────────────────
// Objectives panel (inside edit-project-modal)
// ──────────────────────────────────────────────────────────────────────────────

async function epLoadObjectives(projectName) {
    const container = document.getElementById('edit-project-objectives-list');
    if (!container) return;
    container.innerHTML = '<div style="color:#6c757d;font-size:0.85rem;padding:0.5rem 0">Loading…</div>';
    try {
        const resp = await fetch(`${API_BASE}/projects/${encodeURIComponent(projectName)}/objectives/tree`);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const tree = await resp.json();
        epRenderObjectivesTree(tree, projectName, container);
    } catch (e) {
        container.innerHTML = `<div style="color:#dc3545;font-size:0.85rem">Failed to load objectives: ${e.message}</div>`;
    }
}

function epRenderObjectivesTree(tree, projectName, container, depth) {
    depth = depth || 0;
    if (depth === 0) {
        if (!tree.length) {
            container.innerHTML = '<div style="color:#6c757d;font-size:0.85rem;padding:0.25rem 0">No objectives yet.</div>';
            return;
        }
        container.innerHTML = '';
    }
    tree.forEach(obj => {
        const item = _epBuildObjectiveItem(obj, projectName, depth);
        container.appendChild(item);
        if (obj.children && obj.children.length) {
            epRenderObjectivesTree(obj.children, projectName, container, depth + 1);
        }
    });
}

function _epBuildObjectiveItem(obj, projectName, depth) {
    depth = depth || 0;
    const item = document.createElement('div');
    item.className = 'objective-item' + (depth > 0 ? ' obj-child' : '');
    item.dataset.objId = obj.id;
    if (depth > 0) item.style.marginLeft = (depth * 1.2) + 'rem';

    const isStuck = obj.status === 'paused' && obj.last_assessment && obj.last_assessment.includes('spin');
    const isAppComplete = obj.appears_complete_since && obj.status === 'active';

    let statusBadge = '';
    if (obj.status === 'active') statusBadge = '<span class="obj-status-badge obj-status-active">active</span>';
    else if (obj.status === 'paused') statusBadge = '<span class="obj-status-badge obj-status-paused">paused</span>';
    else if (obj.status === 'complete') statusBadge = '<span class="obj-status-badge obj-status-complete">complete</span>';

    const maestroBadge = obj.created_by === 'maestro'
        ? '<span class="obj-maestro-badge">maestro</span>'
        : '';
    const stuckBadge = isStuck ? '<span class="obj-stuck-badge">&#128308; Stuck — review needed</span>' : '';

    const assessmentPreview = obj.last_assessment
        ? `<div class="obj-assessment-preview">${obj.last_assessment.slice(0, 120)}${obj.last_assessment.length > 120 ? '…' : ''}</div>`
        : '';

    const completeBanner = isAppComplete
        ? `<div class="obj-complete-banner">&#9888; Appears complete &mdash; <button class="btn-link" onclick="epConfirmComplete('${projectName}', ${obj.id})">Confirm &#10003;</button> &nbsp; <button class="btn-link obj-dismiss" onclick="epDismissComplete('${projectName}', ${obj.id})">Dismiss</button></div>`
        : '';

    const addSubBtn = obj.status !== 'complete' && depth === 0
        ? `<button class="obj-action-btn" title="Add sub-objective" onclick="epShowAddSubObjectiveForm(${obj.id}, ${JSON.stringify(obj.description)})">&#8627;</button>`
        : '';

    const actionBtns = obj.status !== 'complete'
        ? `<button class="obj-action-btn" title="Edit" onclick="epShowEditObjectiveForm(${obj.id}, ${JSON.stringify(obj.description)}, ${obj.priority}, ${obj.time_box_hours || ''})">✏</button>
           ${addSubBtn}
           ${obj.status === 'active'
               ? `<button class="obj-action-btn" title="Pause" onclick="epSetStatus('${projectName}', ${obj.id}, 'paused')">⏸</button>`
               : `<button class="obj-action-btn" title="Resume" onclick="epSetStatus('${projectName}', ${obj.id}, 'active')">▶</button>`}
           <button class="obj-action-btn obj-action-del" title="Delete" onclick="epDeleteObjective('${projectName}', ${obj.id})">&#10005;</button>`
        : `<button class="obj-action-btn obj-action-del" title="Delete" onclick="epDeleteObjective('${projectName}', ${obj.id})">&#10005;</button>`;

    const evidenceToggle = `<button class="obj-action-btn" title="Evidence log" onclick="epToggleEvidence('${projectName}', ${obj.id}, this)">&#128196;</button>`;

    item.innerHTML = `
        <div class="obj-header">
            <span class="obj-badge">&#9889;</span>
            <span class="obj-description">${obj.description}</span>${maestroBadge}
            <div class="obj-actions">${actionBtns}${evidenceToggle}</div>
        </div>
        <div class="obj-meta">${statusBadge} ${obj.priority !== 5 ? `<span class="obj-priority">priority ${obj.priority}</span>` : ''} ${obj.time_box_hours ? `<span class="obj-timebox">&#128339; ${obj.time_box_hours}h</span>` : ''} ${stuckBadge}</div>
        ${completeBanner}
        ${assessmentPreview}
        <div class="obj-evidence-panel" id="obj-evidence-${obj.id}" style="display:none"></div>
    `;
    return item;
}

function epShowAddObjectiveForm() {
    document.getElementById('ep-obj-edit-id').value = '';
    document.getElementById('ep-obj-parent-id').value = '';
    document.getElementById('ep-obj-description').value = '';
    document.getElementById('ep-obj-priority').value = 5;
    document.getElementById('ep-obj-timebox').value = '';
    const lbl = document.getElementById('ep-obj-parent-label');
    if (lbl) lbl.style.display = 'none';
    document.getElementById('edit-project-objective-form').style.display = 'block';
    document.getElementById('ep-add-obj-btn').style.display = 'none';
    document.getElementById('ep-obj-description').focus();
}

function epShowAddSubObjectiveForm(parentId, parentDesc) {
    document.getElementById('ep-obj-edit-id').value = '';
    document.getElementById('ep-obj-parent-id').value = parentId;
    document.getElementById('ep-obj-description').value = '';
    document.getElementById('ep-obj-priority').value = 5;
    document.getElementById('ep-obj-timebox').value = '';
    const lbl = document.getElementById('ep-obj-parent-label');
    if (lbl) {
        document.getElementById('ep-obj-parent-desc').textContent = parentDesc.slice(0, 60) + (parentDesc.length > 60 ? '…' : '');
        lbl.style.display = 'block';
    }
    document.getElementById('edit-project-objective-form').style.display = 'block';
    document.getElementById('ep-add-obj-btn').style.display = 'none';
    document.getElementById('ep-obj-description').focus();
}

function epShowEditObjectiveForm(objId, description, priority, timeboxHours) {
    document.getElementById('ep-obj-edit-id').value = objId;
    document.getElementById('ep-obj-description').value = description;
    document.getElementById('ep-obj-priority').value = priority;
    document.getElementById('ep-obj-timebox').value = timeboxHours || '';
    document.getElementById('edit-project-objective-form').style.display = 'block';
    document.getElementById('ep-add-obj-btn').style.display = 'none';
    document.getElementById('ep-obj-description').focus();
}

function epCancelObjectiveForm() {
    const form = document.getElementById('edit-project-objective-form');
    if (form) form.style.display = 'none';
    const btn = document.getElementById('ep-add-obj-btn');
    if (btn) btn.style.display = '';
    const lbl = document.getElementById('ep-obj-parent-label');
    if (lbl) lbl.style.display = 'none';
    const pid = document.getElementById('ep-obj-parent-id');
    if (pid) pid.value = '';
}

async function epSaveObjective() {
    const projectName = document.getElementById('edit-project-original-name').value;
    const editId = document.getElementById('ep-obj-edit-id').value;
    const description = document.getElementById('ep-obj-description').value.trim();
    const priority = parseInt(document.getElementById('ep-obj-priority').value, 10) || 5;
    const timeboxVal = document.getElementById('ep-obj-timebox').value;
    const time_box_hours = timeboxVal ? parseInt(timeboxVal, 10) : null;
    const parentIdVal = document.getElementById('ep-obj-parent-id').value;
    const parent_id = parentIdVal ? parseInt(parentIdVal, 10) : null;

    if (!description) {
        document.getElementById('ep-obj-description').focus();
        return;
    }

    const url = editId
        ? `${API_BASE}/projects/${encodeURIComponent(projectName)}/objectives/${editId}`
        : `${API_BASE}/projects/${encodeURIComponent(projectName)}/objectives`;
    const method = editId ? 'PUT' : 'POST';
    const body = { description, priority, time_box_hours };
    if (!editId && parent_id) body.parent_id = parent_id;

    try {
        const resp = await fetch(url, {
            method,
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        epCancelObjectiveForm();
        epLoadObjectives(projectName);
    } catch (e) {
        alert(`Failed to save objective: ${e.message}`);
    }
}

async function epToggleEvidence(projectName, objId, btn) {
    const panel = document.getElementById(`obj-evidence-${objId}`);
    if (!panel) return;
    if (panel.style.display !== 'none') {
        panel.style.display = 'none';
        return;
    }
    panel.textContent = 'Loading evidence…';
    panel.style.display = 'block';
    try {
        const resp = await fetch(`${API_BASE}/projects/${encodeURIComponent(projectName)}/objectives/${objId}/evidence`);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const text = await resp.text();
        panel.textContent = text || '(no evidence recorded yet)';
    } catch (e) {
        panel.textContent = `Error loading evidence: ${e.message}`;
    }
}

async function epDeleteObjective(projectName, objId) {
    if (!await showConfirm('Delete Objective', 'Delete this objective? Spawned cards will remain but lose the objective tag.', 'Delete')) return;
    try {
        const resp = await fetch(`${API_BASE}/projects/${encodeURIComponent(projectName)}/objectives/${objId}`, { method: 'DELETE' });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        epLoadObjectives(projectName);
    } catch (e) {
        alert(`Failed to delete objective: ${e.message}`);
    }
}

async function epSetStatus(projectName, objId, status) {
    try {
        const resp = await fetch(`${API_BASE}/projects/${encodeURIComponent(projectName)}/objectives/${objId}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ status }),
        });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        epLoadObjectives(projectName);
    } catch (e) {
        alert(`Failed to update objective: ${e.message}`);
    }
}

// ---------------------------------------------------------------------------
// GAP 10 — Model routing table + cost breakdown in project settings
// ---------------------------------------------------------------------------

async function loadProjectRouting(projectName) {
    const tbody = document.getElementById('project-routing-tbody');
    if (!tbody) return;
    tbody.innerHTML = '<tr><td colspan="3" style="color:#6c757d;font-size:0.82rem;padding:0.5rem">Loading…</td></tr>';

    try {
        // Load current routing table and pipeline stages in parallel
        const [routingResp, stages] = await Promise.all([
            fetch(`${API_BASE}/projects/${encodeURIComponent(projectName)}/routing`).then(r => r.ok ? r.json() : {}),
            (async () => {
                const proj = allProjects.find(p => p.name === projectName);
                if (!proj || !proj.pipeline_template_id) return [];
                const r = await fetch(`${API_BASE}/pipelines/${proj.pipeline_template_id}`);
                if (!r.ok) return [];
                const tmpl = await r.json();
                return (tmpl.stages || []).sort((a, b) => a.position - b.position);
            })(),
        ]);

        if (!stages.length) {
            tbody.innerHTML = '<tr><td colspan="3" style="color:#6c757d;font-size:0.82rem;padding:0.5rem">No pipeline stages found for this project.</td></tr>';
            return;
        }

        const nonModelTypes = new Set(['human_review', 'verifier', 'factory']);
        let html = '';
        stages.forEach(stage => {
            const isNoModel = nonModelTypes.has(stage.agent_type);
            const currentLlmId = routingResp[stage.stage_key] || '';
            if (isNoModel) {
                html += `<tr>
                    <td style="padding:0.3rem 0.5rem">${stage.label || stage.stage_key}</td>
                    <td style="padding:0.3rem 0.5rem;color:#adb5bd;font-style:italic">(no model needed)</td>
                    <td></td>
                </tr>`;
            } else {
                let optionsHtml = '<option value="">(project default)</option>';
                allLlms.forEach(l => {
                    const sel = l.id == currentLlmId ? ' selected' : '';
                    optionsHtml += `<option value="${l.id}"${sel}>${l.model} (id ${l.id})</option>`;
                });
                html += `<tr>
                    <td style="padding:0.3rem 0.5rem">${stage.label || stage.stage_key}</td>
                    <td style="padding:0.3rem 0.5rem">
                        <select onchange="setProjectRouting('${projectName}', '${stage.stage_key}', this.value)">${optionsHtml}</select>
                    </td>
                    <td style="padding:0.3rem 0.5rem">
                        ${currentLlmId ? `<button class="action-btn action-btn-danger" style="padding:2px 6px;font-size:0.75rem" onclick="clearProjectRouting('${projectName}', '${stage.stage_key}', this)">Clear</button>` : ''}
                    </td>
                </tr>`;
            }
        });
        tbody.innerHTML = html;
    } catch (e) {
        tbody.innerHTML = `<tr><td colspan="3" style="color:#dc3545;font-size:0.82rem;padding:0.5rem">Error: ${e.message}</td></tr>`;
    }
}

async function setProjectRouting(projectName, stageKey, llmIdStr) {
    if (!llmIdStr) {
        await clearProjectRouting(projectName, stageKey, null);
        return;
    }
    await fetch(`${API_BASE}/projects/${encodeURIComponent(projectName)}/routing/${encodeURIComponent(stageKey)}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ llm_id: parseInt(llmIdStr, 10) }),
    });
    loadProjectRouting(projectName);
}

async function clearProjectRouting(projectName, stageKey, btn) {
    await fetch(`${API_BASE}/projects/${encodeURIComponent(projectName)}/routing/${encodeURIComponent(stageKey)}`, {
        method: 'DELETE',
    });
    loadProjectRouting(projectName);
}

async function loadCostByModel(projectName) {
    const el = document.getElementById('project-cost-by-model');
    if (!el) return;
    try {
        const resp = await fetch(`${API_BASE}/projects/${encodeURIComponent(projectName)}/cost-by-model`);
        if (!resp.ok) { el.textContent = 'No cost data yet.'; return; }
        const data = await resp.json();
        if (!data.by_model || !data.by_model.length) { el.textContent = 'No LLM usage recorded for this project yet.'; return; }
        let html = '<table style="width:100%;border-collapse:collapse;font-size:0.82rem;margin-bottom:0.5rem"><thead><tr style="color:#6c757d"><th style="text-align:left;padding:0.2rem 0.4rem">Model</th><th style="text-align:right;padding:0.2rem 0.4rem">Tokens</th><th style="text-align:right;padding:0.2rem 0.4rem">Cost USD</th></tr></thead><tbody>';
        data.by_model.forEach(r => {
            html += `<tr><td style="padding:0.2rem 0.4rem">${r.model_name || `LLM ${r.llm_id}`}</td><td style="text-align:right;padding:0.2rem 0.4rem">${(r.total_tokens||0).toLocaleString()}</td><td style="text-align:right;padding:0.2rem 0.4rem">$${r.total_cost_usd.toFixed(4)}</td></tr>`;
        });
        html += '</tbody></table>';
        el.innerHTML = html;
    } catch (e) {
        el.textContent = 'Could not load cost data.';
    }
}

async function epConfirmComplete(projectName, objId) {
    await epSetStatus(projectName, objId, 'complete');
}

async function epDismissComplete(projectName, objId) {
    // Reset appears_complete_since by sending appears_complete=false via a synthetic assessment call.
    // The easiest API path is to PATCH the objective to clear the field — use the PUT endpoint
    // with a flag the backend can recognise, or simply PUT status=active which implicitly keeps
    // it active but we also need to clear appears_complete_since. We do that by sending a custom
    // field that the backend PUT handler already accepts via update_objective(**kwargs).
    try {
        const resp = await fetch(`${API_BASE}/projects/${encodeURIComponent(projectName)}/objectives/${objId}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ appears_complete_since: null }),
        });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        epLoadObjectives(projectName);
    } catch (e) {
        alert(`Failed to dismiss: ${e.message}`);
    }
}

// ──────────────────────────────────────────────────────────────────────────────

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
    let taskObj = {};
    if (typeof id === 'object' && id !== null) {
        taskObj = id;
        id = taskObj.id;
        title = taskObj.title;
        tags = taskObj.tags;
        owner = taskObj.owner;
        status = taskObj.type;
    } else {
        taskObj = (typeof taskData !== 'undefined' ? taskData[id] : null) || {};
        title = title || taskObj.title || '';
        tags = tags || taskObj.tags || [];
        owner = owner || taskObj.owner || '';
        status = status || taskObj.type || '';
    }

    // Ensure we have an array for tags
    const safeTags = Array.isArray(tags) ? tags : [];

    const card = document.createElement('div');
    card.className = `task-card ${status || 'idea'}`;
    card.setAttribute('data-id', id);
    card.setAttribute('data-status', status);
    card.setAttribute('draggable', 'true');

    // Check for rejection/processing state from transition cache
    const cached = (typeof transitionCache !== 'undefined' ? transitionCache[id] : null);
    const latestOutcome = cached && cached.history && cached.history.length > 0 ? cached.history[0].outcome : null;
    const rejectionCount = cached ? (cached.rejectionCount || 0) : 0;

    if (latestOutcome === 'rejected' || latestOutcome === 'failed') {
        card.classList.add('rejected');
    }
    // If we have an active poller, card is processing
    if (typeof transitionPollers !== 'undefined' && transitionPollers[id]) {
        card.classList.add('processing');
    }

    const tagsHtml = safeTags.map(tag => `<span class="tag">${tag}</span>`).join('') || '<span class="tag">general</span>';
    const ownerHtml = owner ? `<span>${owner}</span>` : '';

    const rejBadge = rejectionCount > 0 ? `<span class="rejection-badge" title="${rejectionCount} rejection(s)">${rejectionCount}x</span>` : '';
    const processingSpinner = transitionPollers[id] ? '<span class="processing-indicator">\u25E0</span>' : '';

    // PIP badge — small inline indicator; the full pip-card stack is built by wrapWithPipGroup()
    const pips = taskObj.pips || [];
    const pipBadge = pips.length > 0
        ? `<span class="pip-badge" title="${pips.length} Performance Improvement Plan(s)">PIP</span>`
        : '';
    const pipRequirementsHtml = '';  // requirements now live in .pip-card segments below the card

    // Clarification status badge
    const clarificationStatus = taskObj.clarification_status || 'none';
    let clarificationBadge = '';
    if (status === 'idea') {
        if (clarificationStatus === 'pending') {
            clarificationBadge = '<span class="intake-clarification-badge intake-clarification-badge--pending" title="Intake agent is researching this card…">Intake running…</span>';
            card.classList.add('intake-clarifying');
        } else if (clarificationStatus === 'awaiting_user') {
            clarificationBadge = '<span class="intake-clarification-badge intake-clarification-badge--ready" title="Intake agent has finished — click Review Intake to see results">Review required</span>';
        }
    }

    // Intake retry badge — shown on IDEA cards that have been rejected at least once
    let intakeRetryBadge = '';
    if (status === 'idea' || status === 'subdividing') {
        const rejCount = taskObj.intake_rejection_count || 0;
        const exhausted = taskObj.intake_exhausted || false;
        if (rejCount > 0) {
            const badgeTitle = exhausted
                ? `Intake exhausted after ${rejCount} rejection(s). Manual reset required.`
                : `Rejected ${rejCount} time(s) — scheduler will retry.`;
            const badgeText = exhausted ? `\u2716 ${rejCount}\xd7` : `\u21ba ${rejCount}\xd7`;
            intakeRetryBadge = `<span class="intake-retry-badge${exhausted ? ' intake-retry-badge--exhausted' : ''}" title="${badgeTitle}">${badgeText}</span>`;
        }
    }

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

    // Cached plan badge — shown on planning/idea cards with a valid cached result
    let cachedPlanBadge = '';
    if ((status === 'planning' || status === 'idea') && taskObj.has_cached_plan) {
        cachedPlanBadge = '<span class="cached-plan-badge" title="Cached plan available — will skip re-planning and advance to INDEV instantly">&#9889; Cached</span>';
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

    // Autopilot badge — shown on cards spawned by an autopilot objective
    const autopilotBadge = taskObj.autopilot_objective_id
        ? `<span class="autopilot-badge" title="Spawned by autopilot objective #${taskObj.autopilot_objective_id}">&#9889;</span>`
        : '';

    let parentLink = '';
    if (parentId && taskData[parentId]) {
        const parentTitle = taskData[parentId].title || parentId;
        parentLink = `<div class="parent-link" onclick="scrollToTask('${parentId}')" title="Parent: ${parentTitle}">&#8593; ${parentTitle}</div>`;
    }

    // Consultation interface
    let consultationHtml = '';
    if (taskObj.consultation_payload) {
        try {
            const cp = JSON.parse(taskObj.consultation_payload);
            if (cp.question && !cp.hint) {
                card.classList.add('consulting');
                consultationHtml = `
                    <div class="consultation-bubble" style="background:#fff3cd; border:1px solid #ffeeba; border-radius:6px; padding:0.75rem; margin-top:0.5rem; font-size:0.82rem; cursor:default" onclick="event.stopPropagation()">
                        <div style="font-weight:bold; color:#856404; margin-bottom:0.4rem">\ud83d\udcac Maestro Consultation</div>
                        <div style="font-style:italic; margin-bottom:0.75rem; color:#533f03">${cp.question}</div>
                        <textarea class="consult-hint-input" style="width:100%; border:1px solid #ced4da; border-radius:4px; padding:0.4rem; font-size:0.8rem; margin-bottom:0.5rem; color:black" placeholder="Provide steering hint..." rows="2"></textarea>
                        <div style="display:flex; justify-content:flex-end">
                            <button class="btn btn-primary btn-sm" onclick="event.stopPropagation();resumeFromConsultation('${id}', this)" style="padding:0.2rem 0.6rem; font-size:0.75rem; height:auto; line-height:1">Resume Agent</button>
                        </div>
                    </div>
                `;
            }
        } catch(e) {}
    }

    // Prerequisite labels for zoom view
    let prereqHtml = '';
    if (currentBigIdeaFilter) {
        prereqHtml = buildPrereqLabels(id);
    }

    const showFooter = (status !== 'idea' && status !== 'architecture' && status !== 'subdividing' && status !== 'cancelled');
    const footerHtml = showFooter
        ? `<div class="card-stage-footer csf-loading" id="csf-${id}" onclick="event.stopPropagation();openStageJournal('${id}')">\u2026</div>`
        : '';

    const _isStarred = Boolean((taskData[id] || {}).is_starred);

    // Initiative 4 — Card Visual Differentiation stripes
    let stripeHtml = '';
    if (status === 'human_review') {
        stripeHtml = '<div class="card-stripe card-stripe-review">⚠ Review Required</div>';
    } else if (status === 'completed') {
        stripeHtml = '<div class="card-stripe card-stripe-completed">✓ Completed</div>';
    }

    card.innerHTML = `
        ${stripeHtml}
        <button class="card-highlight-btn" title="${_isStarred ? 'Unstar (remove priority boost)' : 'Star (boost scheduler priority)'}" onclick="event.stopPropagation();toggleHighlight('${id}')">${_isStarred ? '\u2605' : '\u2606'}</button>
        ${parentLink}
        <div class="task-title"${isBigIdea ? ` onclick="zoomIntoBigIdea('${id}')" style="cursor:pointer"` : ''}>${title}${rejBadge}${processingSpinner}${subdivBadge}${bigIdeaBadge}${contractIndicator}${pipBadge}${autopilotBadge}</div>
        <div class="task-meta">
            ${tagsHtml}
            ${ownerHtml}
            ${clarificationBadge}
            ${intakeRetryBadge}
            ${cachedPlanBadge}
        </div>
        ${pipRequirementsHtml}
        ${prereqHtml}
        ${consultationHtml}
        ${footerHtml}
        <div class="card-job-indicator" id="ji-${id}"></div>
        <div class="card-toolbar">
            <span class="toolbar-sep"></span>
            ${status === 'idea' ? `<button class="toolbar-btn" title="Re-run clarification agent — reset and rewrite this card's spec" onclick="event.stopPropagation();retriggerIntakeClarification('${id}')">✎</button>` : ''}
            <button class="toolbar-btn" title="Research — run a research agent on this card" onclick="event.stopPropagation();openResearchDialog('${id}')">🔍</button>
            <button class="toolbar-btn" title="Subdivide — run subdivision agent on this card" onclick="event.stopPropagation();toolbarSubdivide('${id}')">✂</button>
            <button class="toolbar-btn" title="Run Planning pipeline (uses cache if available)" onclick="event.stopPropagation();toolbarRunPipeline('${id}','planning')">📋</button>
            <button class="toolbar-btn" title="Force Recompute Plan — bypass cache, recompute with prior failure context" onclick="event.stopPropagation();toolbarForceRecompute('${id}')">&#x1F504;</button>
            <button class="toolbar-btn" title="Run Conceptual Review pipeline" onclick="event.stopPropagation();toolbarRunPipeline('${id}','review')">👁</button>
            <button class="toolbar-btn" title="Run Optimization pipeline" onclick="event.stopPropagation();toolbarRunPipeline('${id}','optimization')">⚡</button>
            <button class="toolbar-btn" title="Run Security pipeline" onclick="event.stopPropagation();toolbarRunPipeline('${id}','security')">🔒</button>
            <button class="toolbar-btn" title="Manual Session — drive tool calls yourself" onclick="event.stopPropagation();openManualSession('${id}')">⌨</button>
            <span class="toolbar-sep"></span>
            <button class="toolbar-btn" title="Run Agent — start MaestroLoop" onclick="event.stopPropagation();runAgentFromToolbar('${id}')">▶</button>
            <button class="toolbar-btn" title="Stop Agent — request graceful halt" onclick="event.stopPropagation();toolbarStopAgent('${id}')">⏹</button>
            <button class="toolbar-btn toolbar-btn-peek" title="Peek — watch live LLM output" onclick="event.stopPropagation();openLivePeek('${id}')">&#128065;</button>
            <!-- Stage management demoted to overflow menu -->
            <span class="toolbar-sep"></span>
            <button class="toolbar-btn" title="Open in Diagnostics" onclick="event.stopPropagation();toolbarOpenDiagnostics('${id}')">📊</button>
            <button class="toolbar-btn" title="Card Story — agent session timeline" onclick="event.stopPropagation();toolbarOpenStory('${id}')">📜</button>
            <button class="toolbar-btn" title="Stage Journal — artifacts, gate checks, code diff" onclick="event.stopPropagation();openStageJournal('${id}')">📋</button>
            <button class="toolbar-btn" title="Research Jobs — view research agents run for this card" onclick="event.stopPropagation();viewResearchJobs('${id}')">🗂</button>
            ${['indev','conceptual_review','optimization','security','human_review','completed'].includes(status) ? `<button class="toolbar-btn" title="View code diff" onclick="event.stopPropagation();openStageDiff('${id}')">&#9998;</button>` : ''}
            <button class="toolbar-btn" title="Clone as new Idea" onclick="event.stopPropagation();toolbarClone('${id}')">⧉</button>
            <button class="toolbar-btn" title="Pin to top of column" onclick="event.stopPropagation();toolbarPin('${id}')">📌</button>
            <button class="toolbar-btn" title="Open in Column Map (DAG view)" onclick="event.stopPropagation();toolbarOpenMap('${id}')">🔗</button>
        </div>
        <div class="task-actions">
            <button class="action-btn" onclick="editTask('${id}')">Edit</button>
            <button class="action-btn action-btn-danger" onclick="deleteTask('${id}')">Delete</button>
        </div>
    `;

    // Initiative 4 — Card overflow button
    const overflowBtn = document.createElement('button');
    overflowBtn.className = 'card-overflow-btn';
    overflowBtn.innerHTML = '&#8942;'; // vertical ellipsis
    overflowBtn.title = 'Advanced actions';
    overflowBtn.onclick = (e) => {
        e.stopPropagation();
        _showCardOverflowMenu(id, overflowBtn);
    };
    card.appendChild(overflowBtn);

    _applyHighlightState(card, id);

    // Make rejected/failed cards clickable to open transition detail
    if (rejectionCount > 0) {
        card.style.cursor = 'pointer';
        card.addEventListener('click', (e) => {
            // Don't open overlay if a button was clicked
            if (e.target.closest('.action-btn, .card-overflow-btn, .toolbar-btn')) return;
            openTransitionModal(id);
        });
    }

    const ready = canTaskAdvance(id);

    if (status === 'subdividing') {
        // Subdividing — always show View + Edit + View Children; Advance if ready
        const actionsDiv = card.querySelector('.task-actions');
        actionsDiv.innerHTML = `
            <button class="action-btn" onclick="viewTask('${id}')">View</button>
            <button class="action-btn" onclick="editTask('${id}')">Edit</button>
            <button class="action-btn" onclick="viewChildren('${id}')">View Children</button>
        `;
        // Delete demoted to overflow implicitly via Advanced... (or for all if we want)
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
        const cs = clarificationStatus;
        const needsReview = cs === 'pending' || cs === 'awaiting_user';

        if (needsReview) {
            // Hard gate: show "Review Intake →" instead of pipeline button
            const reviewBtn = document.createElement('button');
            reviewBtn.className = 'action-btn action-btn-advance';
            if (cs === 'pending') {
                reviewBtn.textContent = 'Intake running…';
                reviewBtn.disabled = true;
            } else {
                reviewBtn.textContent = 'Review Intake →';
                reviewBtn.onclick = (e) => { e.stopPropagation(); openIntakeModal(id); };
            }
            actionsDiv.appendChild(reviewBtn);
        } else {
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
        }

        // Show View Children if this card is a Big Idea or has non-cancelled children
        const hasChildren = (childIndex[id] || []).some(cid => taskData[cid] && taskData[cid].type !== 'cancelled');
        if (hasChildren || isBigIdea) {
            const childBtn = document.createElement('button');
            childBtn.className = 'action-btn';
            childBtn.textContent = 'View Children';
            childBtn.onclick = (e) => { e.stopPropagation(); viewChildren(id); };
            actionsDiv.appendChild(childBtn);
        }
    } else if (status === 'human_review') {
        const actionsDiv = card.querySelector('.task-actions');
        // Replace actions with full-width Accept & Merge
        actionsDiv.innerHTML = '';
        const mergeBtn = document.createElement('button');
        mergeBtn.className = 'action-btn action-btn-advance full-width';
        mergeBtn.textContent = 'Accept & Merge';
        mergeBtn.onclick = async (e) => {
            e.stopPropagation();
            if (!await showConfirm('Accept & Merge',
                `Run the git merge pipeline for "${taskObj.title}" and mark it COMPLETED?`,
                'Accept & Merge')) return;
            const r = await fetch(`${API_BASE}/tasks/${id}/merge`, { method: 'POST' });
            const d = await r.json().catch(() => ({}));
            if (r.ok) {
                showToast('Merge pipeline started.', 'success');
                await loadTasksFromDatabase();
            } else {
                showToast(d.detail || 'Merge failed.', 'error');
            }
        };
        actionsDiv.appendChild(mergeBtn);
    } else if (status === 'planning' || status === 'indev' || status === 'conceptual_review' || status === 'optimization' || status === 'security') {
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
        const actionsDiv = card.querySelector('.task-actions');
        actionsDiv.innerHTML = `<button class="action-btn action-btn-primary full-width" onclick="viewTaskHistory('${id}')">View Proof</button>`;
        // Unmerge and Delete demoted to overflow
    } else if (status === 'architecture') {
        const actionsDiv = card.querySelector('.task-actions');
        if (actionsDiv) {
            actionsDiv.innerHTML = `<button class="action-btn" onclick="editArchitectureTask('${id}')">Edit</button>
                                 <button class="action-btn action-btn-danger" onclick="deleteTask('${id}')">Delete</button>`;
        }
    }

    // Initiative 4 — Research/Benchmarks/Reports are hidden from card body for COMPLETED tasks
    if (status !== 'completed') {
        // Research Jobs button — available on any status that can have research (not idea/subdividing/architecture)
        if (status !== 'idea' && status !== 'subdividing' && status !== 'architecture') {
            const researchBtn = document.createElement('button');
            researchBtn.className = 'action-btn';
            researchBtn.textContent = 'Research Jobs';
            researchBtn.onclick = (e) => { e.stopPropagation(); viewResearchJobs(id); };
            card.querySelector('.task-actions').appendChild(researchBtn);
        }

        // Benchmarks button — visible once optimization stage has run
        if (status === 'optimization' || status === 'security' || status === 'human_review') {
            const benchBtn = document.createElement('button');
            benchBtn.className = 'action-btn';
            benchBtn.textContent = 'Benchmarks';
            benchBtn.onclick = (e) => { e.stopPropagation(); viewBenchmarks(id); };
            card.querySelector('.task-actions').appendChild(benchBtn);
        }

        // Reports button — always present on pipeline cards; opens Stage Journal
        if (status !== 'architecture') {
            const reportsBtn = document.createElement('button');
            reportsBtn.className = 'action-btn action-btn-reports';
            reportsBtn.textContent = 'Reports';
            reportsBtn.onclick = (e) => { e.stopPropagation(); openStageJournal(id); };
            card.querySelector('.task-actions').appendChild(reportsBtn);
        }
    }

    card.addEventListener('dragstart', handleDragStart);
    card.addEventListener('dragend', handleDragEnd);

    return card;
}

/**
 * Show a flyout menu with advanced actions for a card.
 */
function _showCardOverflowMenu(taskId, anchorEl) {
    const task = taskData[taskId];
    if (!task) return;
    const status = (task.type || '').toLowerCase();

    // Remove any existing flyout
    const existing = document.getElementById('_card-overflow-flyout');
    if (existing) existing.remove();

    const flyout = document.createElement('div');
    flyout.id = '_card-overflow-flyout';
    flyout.className = 'stage-picker-flyout'; // Reuse existing styles
    flyout.style.minWidth = '160px';

    const items = [];

    // Common actions for all cards
    items.push({ label: 'Edit Card', icon: '✎', action: () => editTask(taskId) });
    
    if (status === 'completed') {
        items.push({ label: 'Unmerge Task', icon: '↩', action: () => unmergeTask(taskId), danger: true });
    }

    if (status !== 'architecture') {
        items.push({ label: 'Clone Idea', icon: '⧉', action: () => toolbarClone(taskId) });
    }

    items.push({ label: 'Delete Card', icon: '×', action: () => deleteTask(taskId), danger: true });

    items.push({ separator: true });

    // Reports/Jobs (always available in overflow)
    if (status !== 'architecture') {
        items.push({ label: 'Stage Journal', icon: '📋', action: () => openStageJournal(taskId) });
        items.push({ label: 'Research Jobs', icon: '🗂', action: () => viewResearchJobs(taskId) });
        if (['optimization', 'security', 'human_review', 'completed'].includes(status)) {
            items.push({ label: 'Benchmarks', icon: '📊', action: () => viewBenchmarks(taskId) });
        }
    }

    items.push({ separator: true });

    // Advanced Stage Controls (Initiative 4 follow-up)
    items.push({ 
        label: 'Advanced...', 
        icon: '⚙', 
        action: (e) => {
            e.stopPropagation();
            _removeCardOverflow();
            toolbarStagePicker(taskId, anchorEl); // Re-open stage picker at the same spot
        } 
    });

    items.forEach(item => {
        if (item.separator) {
            const hr = document.createElement('div');
            hr.style.height = '1px';
            hr.style.background = '#eee';
            hr.style.margin = '0.2rem 0';
            flyout.appendChild(hr);
            return;
        }
        const btn = document.createElement('button');
        btn.className = 'stage-picker-item';
        if (item.danger) btn.style.color = '#dc3545';
        btn.innerHTML = `<span style="display:inline-block;width:20px">${item.icon}</span> ${item.label}`;
        btn.onclick = (e) => {
            e.stopPropagation();
            _removeCardOverflow();
            item.action(e);
        };
        flyout.appendChild(btn);
    });

    document.body.appendChild(flyout);

    const rect = anchorEl.getBoundingClientRect();
    flyout.style.left = Math.min(window.innerWidth - 170, rect.left) + 'px';
    flyout.style.top  = (rect.bottom + 4) + 'px';

    // Close on outside click
    setTimeout(() => {
        document.addEventListener('click', function _closeOverflow(e) {
            if (!flyout.contains(e.target)) {
                _removeCardOverflow();
                document.removeEventListener('click', _closeOverflow);
            }
        });
    }, 0);
}

function _removeCardOverflow() {
    const el = document.getElementById('_card-overflow-flyout');
    if (el) el.remove();
}

// ============================================
// Advance Task (Initiate Pipeline stages)
// ============================================

async function advanceTask(taskId) {
    const task = taskData[taskId];
    if (!task) return;

    // Clarification gate: redirect IDEA cards that haven't been reviewed yet
    if (task.type === 'idea') {
        const cs = task.clarification_status || 'none';
        if (cs === 'pending' || cs === 'awaiting_user') {
            openIntakeModal(taskId);
            return;
        }
    }

    const advanceStartedAt = new Date().toISOString();
    
    // Map status to specific API endpoints
    const endpointMap = {
        'idea': 'advance',
        'subdividing': 'advance',
        'planning': 'run-planning',
        'indev': 'run-review', // after indev we want conceptual review
        'conceptual_review': 'run-review',
        'optimization': 'run-security',
        'security': 'run-final-review',
        'human_review': 'merge'
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

// ============================================
// Intake Clarification Modal
// ============================================

let _intakeCurrentTaskId = null;
let _intakePendingPoller = null;

function _intakeModalBgClick(event) {
    if (event.target === document.getElementById('intake-modal')) closeIntakeModal();
}

async function openIntakeModal(taskId) {
    _intakeCurrentTaskId = taskId;
    const task = taskData[taskId];
    const cs = (task && task.clarification_status) || 'none';
    document.getElementById('intake-modal-title').textContent = 'Intake Review: ' + (task ? task.title : taskId);
    document.getElementById('intake-modal').classList.add('active');

    if (cs === 'pending') {
        // Show loading state; auto-transition when reconcile updates taskData
        _setIntakeModalLoading(true);
        _intakePendingPoller = setInterval(() => {
            const t = taskData[_intakeCurrentTaskId];
            if (!t || t.clarification_status !== 'pending') {
                clearInterval(_intakePendingPoller);
                _intakePendingPoller = null;
                if (t && t.clarification_status === 'awaiting_user') {
                    fetch(`${API_BASE}/tasks/${_intakeCurrentTaskId}/clarification`)
                        .then(r => r.ok ? r.json() : null)
                        .then(data => {
                            if (data) {
                                _setIntakeModalLoading(false);
                                _renderIntakeModal(data, taskData[_intakeCurrentTaskId]);
                            }
                        });
                }
            }
        }, 2000);
        return;
    }

    _setIntakeModalLoading(false);
    try {
        const r = await fetch(`${API_BASE}/tasks/${taskId}/clarification`);
        if (!r.ok) {
            const err = await r.json().catch(() => ({}));
            showToast('Failed to load intake draft: ' + (err.detail || 'Not found'), 'error');
            return;
        }
        const data = await r.json();
        _renderIntakeModal(data, task);
    } catch (err) {
        showToast('Error loading intake draft: ' + err.message, 'error');
    }
}

function _setIntakeModalLoading(loading) {
    const bodyContent = document.getElementById('intake-modal-body-content');
    const approveBtn = document.getElementById('intake-approve-btn');
    const footer = document.querySelector('#intake-modal .modal-footer');
    if (loading) {
        bodyContent.innerHTML =
            `<div style="text-align:center;padding:3rem 1rem;color:#6c757d">
                <div class="processing-indicator" style="font-size:1.5rem">◷</div>
                <div style="margin-top:0.75rem;font-size:0.95rem">Thinking about your idea…</div>
                <div style="margin-top:0.4rem;font-size:0.82rem">The intake agent is analysing this card.<br>This usually takes 1–3 minutes.</div>
            </div>`;
        if (approveBtn) approveBtn.disabled = true;
        if (footer) footer.querySelectorAll('button').forEach(b => { b.disabled = true; });
    } else {
        // Restore the original body structure if it was replaced by the loading state
        const hasOriginalContent = bodyContent.querySelector('#intake-original-desc');
        if (!hasOriginalContent) {
            bodyContent.innerHTML = `
                <div style="display:grid;grid-template-columns:1fr 1fr;gap:1rem;margin-bottom:1rem">
                    <div>
                        <div style="font-size:0.8rem;font-weight:600;color:#6c757d;text-transform:uppercase;margin-bottom:0.4rem">Original Description</div>
                        <div id="intake-original-desc" style="background:#f8f9fa;border:1px solid #dee2e6;border-radius:4px;padding:0.75rem;font-size:0.85rem;white-space:pre-wrap;max-height:260px;overflow-y:auto;color:#495057"></div>
                    </div>
                    <div>
                        <div style="font-size:0.8rem;font-weight:600;color:#0d6efd;text-transform:uppercase;margin-bottom:0.4rem">Suggested Rewrite <span style="font-weight:400;color:#6c757d;text-transform:none">(editable)</span></div>
                        <textarea id="intake-rewrite-desc" style="width:100%;height:260px;border:1px solid #0d6efd;border-radius:4px;padding:0.75rem;font-size:0.85rem;resize:vertical;font-family:inherit;box-sizing:border-box"></textarea>
                    </div>
                </div>
                <div id="intake-rationale-row" style="margin-bottom:1rem;display:none">
                    <div style="font-size:0.8rem;font-weight:600;color:#6c757d;text-transform:uppercase;margin-bottom:0.25rem">Why the agent rewrote it</div>
                    <div id="intake-rationale" style="font-size:0.82rem;color:#495057;background:#fffdf0;border-left:3px solid #ffc107;padding:0.5rem 0.75rem;border-radius:0 4px 4px 0"></div>
                </div>
                <div id="intake-prereqs-section" style="margin-bottom:1rem;display:none">
                    <div style="font-size:0.8rem;font-weight:600;color:#6c757d;text-transform:uppercase;margin-bottom:0.5rem">Suggested Prerequisites</div>
                    <div id="intake-prereqs-list"></div>
                </div>
                <div id="intake-subtasks-section" style="margin-bottom:1rem;display:none">
                    <div style="font-size:0.8rem;font-weight:600;color:#6c757d;text-transform:uppercase;margin-bottom:0.5rem">Suggested Subtasks</div>
                    <div id="intake-subtasks-list"></div>
                </div>
                <div id="intake-questions-section" style="margin-bottom:1rem;display:none">
                    <div style="background:#fff3cd;border:1px solid #ffc107;border-radius:4px;padding:0.75rem">
                        <div style="font-size:0.8rem;font-weight:700;color:#856404;margin-bottom:0.4rem">Open Questions — resolve before planning</div>
                        <ul id="intake-questions-list" style="margin:0;padding-left:1.25rem;font-size:0.85rem;color:#664d03"></ul>
                    </div>
                </div>
                <div style="border-top:1px solid #dee2e6;padding-top:1rem">
                    <div style="font-size:0.8rem;font-weight:600;color:#6c757d;text-transform:uppercase;margin-bottom:0.5rem">Ask the agent to refine</div>
                    <div id="intake-chat-history" style="max-height:180px;overflow-y:auto;margin-bottom:0.5rem;display:flex;flex-direction:column;gap:0.4rem"></div>
                    <div style="display:flex;gap:0.5rem">
                        <input type="text" id="intake-chat-input" class="form-control" placeholder="e.g. Add error handling requirements, or make acceptance criteria more specific…" style="flex:1" onkeydown="if(event.key==='Enter'&&!event.shiftKey){sendIntakeMessage();event.preventDefault();}">
                        <button class="btn btn-secondary" onclick="sendIntakeMessage()" id="intake-chat-send">Send</button>
                    </div>
                </div>`;
        }
        if (approveBtn) approveBtn.disabled = false;
        if (footer) footer.querySelectorAll('button').forEach(b => { b.disabled = false; });
    }
}

function _renderIntakeModal(data, task) {
    const draft = data.draft || {};
    const original = data.description_original || (task && task.description) || '';

    document.getElementById('intake-original-desc').textContent = original;
    document.getElementById('intake-rewrite-desc').value = draft.rewritten_description || original;

    if (draft.design_rationale) {
        document.getElementById('intake-rationale').textContent = draft.design_rationale;
        document.getElementById('intake-rationale-row').style.display = '';
    }

    // Prerequisites
    const prereqs = draft.suggested_prerequisites || [];
    if (prereqs.length) {
        const list = document.getElementById('intake-prereqs-list');
        list.innerHTML = prereqs.map((p, i) =>
            `<label style="display:flex;align-items:flex-start;gap:0.5rem;margin-bottom:0.4rem;font-size:0.85rem">
                <input type="checkbox" data-prereq-idx="${i}" checked style="margin-top:2px;flex-shrink:0">
                <span><strong>${escapeHtml(p.title || p.task_id)}</strong> <span style="color:#6c757d">[${p.task_id}]</span>${p.reason ? ' — ' + escapeHtml(p.reason) : ''}</span>
            </label>`
        ).join('');
        document.getElementById('intake-prereqs-section').style.display = '';
    }

    // Subtasks
    const subtasks = draft.suggested_subtasks || [];
    if (subtasks.length) {
        const list = document.getElementById('intake-subtasks-list');
        list.innerHTML = subtasks.map((s, i) =>
            `<label style="display:flex;align-items:flex-start;gap:0.5rem;margin-bottom:0.4rem;font-size:0.85rem">
                <input type="checkbox" data-subtask-idx="${i}" checked style="margin-top:2px;flex-shrink:0">
                <span><strong>${escapeHtml(s.title)}</strong>${s.description ? ': ' + escapeHtml(s.description.slice(0, 120)) + (s.description.length > 120 ? '…' : '') : ''}</span>
            </label>`
        ).join('');
        document.getElementById('intake-subtasks-section').style.display = '';
    }

    // Open questions
    const questions = draft.open_questions || [];
    if (questions.length) {
        document.getElementById('intake-questions-list').innerHTML =
            questions.map(q => `<li>${escapeHtml(q)}</li>`).join('');
        document.getElementById('intake-questions-section').style.display = '';
    }

    // Conversation history
    const history = draft.conversation_history || [];
    const chatDiv = document.getElementById('intake-chat-history');
    chatDiv.innerHTML = history.map(msg => _renderIntakeChatBubble(msg.role, msg.content)).join('');
    chatDiv.scrollTop = chatDiv.scrollHeight;
}

let _intakeInvestigationOpen = false;

function toggleIntakeInvestigation() {
    const body = document.getElementById('intake-investigation-body');
    const toggle = document.getElementById('intake-investigation-toggle');
    _intakeInvestigationOpen = !_intakeInvestigationOpen;
    body.style.display = _intakeInvestigationOpen ? '' : 'none';
    const span = toggle.querySelector('span');
    if (span) span.textContent = (_intakeInvestigationOpen ? '▼' : '▶') + span.textContent.slice(1);
    if (_intakeInvestigationOpen) _loadIntakeInvestigation(_intakeCurrentTaskId);
}

async function _loadIntakeInvestigation(taskId) {
    if (!taskId) return;
    const content = document.getElementById('intake-investigation-content');
    content.innerHTML = '<div style="color:#6c757d;font-style:italic">Loading…</div>';
    try {
        const r = await fetch(`${API_BASE}/tasks/${taskId}/clarification/trace`);
        if (!r.ok) { content.innerHTML = '<div style="color:#dc3545">Could not load trace.</div>'; return; }
        const data = await r.json();
        const count = document.getElementById('intake-investigation-count');
        if (count) count.textContent = `(${data.total} step${data.total !== 1 ? 's' : ''})`;
        if (!data.steps || !data.steps.length) {
            content.innerHTML = '<div style="color:#6c757d;font-style:italic">No investigation trace recorded.</div>';
            return;
        }
        content.innerHTML = data.steps.map(step => _renderInvestigationStep(step)).join('');
    } catch (err) {
        content.innerHTML = `<div style="color:#dc3545">Error: ${escapeHtml(err.message)}</div>`;
    }
}

function _renderInvestigationStep(step) {
    const isFinal = step.is_final;
    const borderColor = isFinal ? '#198754' : '#dee2e6';
    const bgColor = isFinal ? '#f0fff4' : '#fafafa';
    const toolsHtml = (step.tools_used || []).map(t =>
        `<span style="display:inline-flex;align-items:center;gap:0.25rem;background:#e9ecef;border-radius:3px;padding:0.1rem 0.4rem;font-size:0.75rem;color:#495057;margin:0.15rem 0.15rem 0 0">
            <span style="color:#6c757d;font-family:monospace">${escapeHtml(t.name)}</span>
            ${t.arg ? `<span style="color:#6c757d">→</span><span style="max-width:140px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escapeHtml(t.arg)}">${escapeHtml(t.arg)}</span>` : ''}
        </span>`
    ).join('');
    const reasoningHtml = step.reasoning_preview
        ? `<div style="margin-top:0.35rem;color:#495057;font-style:italic;line-height:1.4">${escapeHtml(step.reasoning_preview)}</div>`
        : '';
    const finalBadge = isFinal
        ? `<span style="margin-left:0.5rem;background:#198754;color:#fff;font-size:0.7rem;padding:0.1rem 0.4rem;border-radius:3px">Draft produced</span>`
        : '';
    const ts = step.created_at ? new Date(step.created_at).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'}) : '';
    return `<div style="border-left:3px solid ${borderColor};background:${bgColor};padding:0.5rem 0.65rem;margin-bottom:0.5rem;border-radius:0 4px 4px 0">
        <div style="display:flex;align-items:center;gap:0.4rem;margin-bottom:0.25rem">
            <span style="font-weight:600;color:#495057">Step ${step.step}</span>
            ${finalBadge}
            <span style="color:#adb5bd;font-size:0.75rem;margin-left:auto">${ts}</span>
        </div>
        ${toolsHtml ? `<div style="margin-bottom:0.2rem">${toolsHtml}</div>` : ''}
        ${reasoningHtml}
    </div>`;
}

function _renderIntakeChatBubble(role, content) {
    const isUser = role === 'user';
    return `<div style="display:flex;justify-content:${isUser ? 'flex-end' : 'flex-start'}">
        <div style="max-width:80%;background:${isUser ? '#0d6efd' : '#f0f0f0'};color:${isUser ? '#fff' : '#212529'};border-radius:10px;padding:0.4rem 0.7rem;font-size:0.82rem;white-space:pre-wrap">${escapeHtml(content)}</div>
    </div>`;
}

function closeIntakeModal() {
    if (_intakePendingPoller) { clearInterval(_intakePendingPoller); _intakePendingPoller = null; }
    document.getElementById('intake-modal').classList.remove('active');
    _intakeCurrentTaskId = null;
    // Collapse investigation panel for next open
    _intakeInvestigationOpen = false;
    const invBody = document.getElementById('intake-investigation-body');
    if (invBody) invBody.style.display = 'none';
    const invToggle = document.getElementById('intake-investigation-toggle');
    if (invToggle) { const s = invToggle.querySelector('span'); if (s) s.textContent = '▶' + s.textContent.slice(1); }
}

async function sendIntakeMessage() {
    const taskId = _intakeCurrentTaskId;
    if (!taskId) return;
    const input = document.getElementById('intake-chat-input');
    const message = input.value.trim();
    if (!message) return;

    input.value = '';
    const sendBtn = document.getElementById('intake-chat-send');
    sendBtn.disabled = true;
    sendBtn.textContent = '…';

    const chatDiv = document.getElementById('intake-chat-history');
    chatDiv.insertAdjacentHTML('beforeend', _renderIntakeChatBubble('user', message));
    chatDiv.scrollTop = chatDiv.scrollHeight;

    try {
        const r = await fetch(`${API_BASE}/tasks/${taskId}/clarification/message`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ message }),
        });
        const data = await r.json().catch(() => ({}));
        if (!r.ok) {
            showToast('Chat failed: ' + (data.detail || 'Unknown error'), 'error');
        } else {
            if (data.response) {
                chatDiv.insertAdjacentHTML('beforeend', _renderIntakeChatBubble('assistant', data.response));
                chatDiv.scrollTop = chatDiv.scrollHeight;
            }
            if (data.updated_draft && data.updated_draft.rewritten_description) {
                document.getElementById('intake-rewrite-desc').value = data.updated_draft.rewritten_description;
            }
        }
    } catch (err) {
        showToast('Chat error: ' + err.message, 'error');
    } finally {
        sendBtn.disabled = false;
        sendBtn.textContent = 'Send';
    }
}

async function approveIntakeClarification() {
    const taskId = _intakeCurrentTaskId;
    if (!taskId) return;

    const btn = document.getElementById('intake-approve-btn');
    btn.disabled = true;
    btn.textContent = 'Approving…';

    // Collect the (possibly edited) rewritten description
    const rewrittenDescription = document.getElementById('intake-rewrite-desc').value.trim();

    // Collect checked prerequisites
    const prereqCheckboxes = document.querySelectorAll('#intake-prereqs-list input[type=checkbox]:checked');
    const draft = await fetch(`${API_BASE}/tasks/${taskId}/clarification`).then(r => r.json()).catch(() => ({}));
    const allPrereqs = (draft.draft && draft.draft.suggested_prerequisites) || [];
    const applyPrerequisites = Array.from(prereqCheckboxes)
        .map(cb => {
            const idx = parseInt(cb.dataset.prereqIdx);
            return allPrereqs[idx] ? allPrereqs[idx].task_id : null;
        })
        .filter(Boolean);

    // Collect checked subtasks
    const subtaskCheckboxes = document.querySelectorAll('#intake-subtasks-list input[type=checkbox]:checked');
    const allSubtasks = (draft.draft && draft.draft.suggested_subtasks) || [];
    const applySubtasks = Array.from(subtaskCheckboxes)
        .map(cb => {
            const idx = parseInt(cb.dataset.subtaskIdx);
            return allSubtasks[idx] || null;
        })
        .filter(Boolean);

    try {
        const r = await fetch(`${API_BASE}/tasks/${taskId}/clarification/approve`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
                rewritten_description: rewrittenDescription,
                apply_prerequisites: applyPrerequisites,
                apply_subtasks: applySubtasks,
            }),
        });
        const data = await r.json().catch(() => ({}));
        if (!r.ok) {
            showToast('Approval failed: ' + (data.detail || 'Unknown error'), 'error');
            btn.disabled = false;
            btn.textContent = 'Approve & Run Pipeline';
            return;
        }
        closeIntakeModal();
        await loadTasksFromDatabase();
        if (data.created_subtasks && data.created_subtasks.length) {
            showToast(`Created ${data.created_subtasks.length} subtask(s).`, 'success');
        }
        // Now advance the task
        await advanceTask(taskId);
    } catch (err) {
        showToast('Approval error: ' + err.message, 'error');
        btn.disabled = false;
        btn.textContent = 'Approve & Run Pipeline';
    }
}

async function skipIntakeClarification() {
    const taskId = _intakeCurrentTaskId;
    if (!taskId) return;
    try {
        const r = await fetch(`${API_BASE}/tasks/${taskId}/clarification/skip`, { method: 'POST' });
        if (!r.ok) { showToast('Skip failed', 'error'); return; }
        closeIntakeModal();
        await loadTasksFromDatabase();
    } catch (err) {
        showToast('Skip error: ' + err.message, 'error');
    }
}

async function retriggerIntakeClarification(taskId) {
    const task = taskData[taskId];
    if (!task) return;
    if (!confirm(`Re-run the clarification agent for "${task.title}"?\n\nThis will reset the current draft.`)) return;
    try {
        const r = await fetch(`${API_BASE}/tasks/${taskId}/clarification/retrigger`, { method: 'POST' });
        if (!r.ok) {
            const e = await r.json().catch(() => ({}));
            showToast('Re-clarify failed: ' + (e.detail || 'Unknown error'), 'error');
            return;
        }
        showToast('Clarification agent started — card will update automatically.', 'success');
        await loadTasksFromDatabase();
    } catch (err) {
        showToast('Re-clarify error: ' + err.message, 'error');
    }
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
        queueBtn.textContent = total > 0 ? `⚙ ${total}` : '⚙';
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
            human_review:'#dc3545'
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
        const resp = await fetch(`${API_BASE}/inbox?project=${encodeURIComponent(currentProject)}`);
        if (!resp.ok) return;
        inboxMessages = await resp.json();
        _inboxUnreadCount = inboxMessages.filter(m => !m.read).length;
        // Check if any unread has source_type="needs_human"
        _inboxHasNeedsHuman = inboxMessages.some(m => !m.read && m.source_type === 'needs_human');
        _updateInboxBadge();
    } catch (e) {
        console.error('[inbox] load failed:', e);
    }
}

async function refreshInboxBadge() {
    try {
        const resp = await fetch(`${API_BASE}/inbox/unread-count?project=${encodeURIComponent(currentProject)}`);
        if (!resp.ok) return;
        const data = await resp.json();
        _inboxUnreadCount = data.count;
        _inboxHasNeedsHuman = data.has_needs_human;
        _updateInboxBadge();
    } catch (e) { /* silent */ }
}

function _updateInboxBadge() {
    const badge = document.getElementById('inbox-badge');
    const btn = document.getElementById('inbox-btn');
    if (!badge) return;

    if (_inboxUnreadCount > 0) {
        badge.textContent = _inboxUnreadCount > 99 ? '99+' : String(_inboxUnreadCount);
        badge.style.display = 'flex';
    } else {
        badge.style.display = 'none';
    }

    if (btn) {
        if (_inboxUnreadCount > 0 && _inboxHasNeedsHuman) {
            btn.classList.add('needs-human-pending');
        } else {
            btn.classList.remove('needs-human-pending');
        }
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
                project_id: currentProject,
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
// Escalation Dialog (NEEDS_HUMAN)
// ============================================

let _escalationCurrentMsg = null;
let _escalationSeenIds = new Set();
let _escalationPollTimer = null;

async function _escalationPoll() {
    try {
        const resp = await fetch(`${API_BASE}/inbox/escalations`);
        if (!resp.ok) return;
        const msgs = await resp.json();
        const unseen = msgs.filter(m => !_escalationSeenIds.has(m.id));
        if (unseen.length > 0) {
            _showEscalationDialog(unseen[0]);
        }
    } catch (e) { /* silent */ }
}

function _showEscalationDialog(msg) {
    _escalationCurrentMsg = msg;
    _escalationSeenIds.add(msg.id);

    const titleEl = document.getElementById('escalation-task-title');
    const bodyEl = document.getElementById('escalation-body');
    if (titleEl) titleEl.textContent = msg.task_title || msg.task_id || '';

    let summary = msg.subject || '';
    try {
        if (msg.data_json) {
            const d = JSON.parse(msg.data_json);
            if (d.summary) summary = d.summary;
        }
    } catch (e) { /* use subject */ }
    if (bodyEl) bodyEl.textContent = summary;

    const overlay = document.getElementById('escalation-overlay');
    if (overlay) overlay.style.display = 'flex';
}

function closeEscalationDialog() {
    const overlay = document.getElementById('escalation-overlay');
    if (overlay) overlay.style.display = 'none';
    _escalationCurrentMsg = null;
}

async function escalationAcknowledge() {
    if (_escalationCurrentMsg) {
        try {
            await fetch(`${API_BASE}/inbox/${_escalationCurrentMsg.id}`, {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ read: true }),
            });
            _inboxUnreadCount = Math.max(0, _inboxUnreadCount - 1);
            _updateInboxBadge();
        } catch (e) { /* silent */ }
    }
    closeEscalationDialog();
}

async function escalationGoToCard() {
    const msg = _escalationCurrentMsg;
    await escalationAcknowledge();
    if (!msg || !msg.task_id) return;

    // Find the task and switch to its project if needed
    let task = allTasks.find(t => t.id === msg.task_id);
    if (!task && msg.task_id) {
        // Task may be in a different project — look it up
        try {
            const resp = await fetch(`${API_BASE}/tasks/${msg.task_id}`);
            if (resp.ok) task = await resp.json();
        } catch (e) { /* ignore */ }
    }
    if (task && task.project && task.project !== currentProject) {
        await switchProject(task.project);
    }
    // Scroll card into view
    setTimeout(() => {
        const card = document.querySelector(`[data-task-id="${msg.task_id}"]`);
        if (card) {
            card.scrollIntoView({ behavior: 'smooth', block: 'center' });
            card.classList.add('highlight-flash');
            setTimeout(() => card.classList.remove('highlight-flash'), 1500);
        }
    }, 400);
}

function _startEscalationPoll() {
    if (_escalationPollTimer) return;
    _escalationPoll();  // immediate check on startup
    _escalationPollTimer = setInterval(_escalationPoll, 15_000);
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

    }

    await loadTasksFromDatabase();
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
    _initPrereqsSelector(task);

    document.getElementById('task-modal').classList.add('active');
}

// ============================================
// Prerequisites Selector (searchable multi-select + mini DAG)
// ============================================

let _prereqSelectedIds = [];   // currently selected prerequisite task IDs
let _prereqAllTasks = [];      // all tasks in current project (cache)
let _prereqSearchTimer = null;

function _initPrereqsSelector(task) {
    const group = document.getElementById('task-prereqs-group');
    if (!group) return;
    group.style.display = '';

    _prereqSelectedIds = [...(task.prerequisites || [])];
    _prereqSelectedIds = _prereqSelectedIds.filter(id => id !== task.id); // exclude self

    // Load all tasks in the current project (excluding self and architecture tasks)
    _prereqAllTasks = Object.values(taskData)
        .filter(t => t.id !== task.id && t.is_active && t.type !== 'architecture')
        .sort((a, b) => (a.title || '').localeCompare(b.title || ''));

    // Clear inputs
    document.getElementById('task-prereqs-input').value = '';
    document.getElementById('prereqs-dropdown').style.display = 'none';
    _renderPrereqSelected();
    document.getElementById('prereqs-dag-container').style.display = 'none';

    // Wire up search input
    const input = document.getElementById('task-prereqs-input');
    input.oninput = () => {
        clearTimeout(_prereqSearchTimer);
        _prereqSearchTimer = setTimeout(() => _renderPrereqDropdown(input.value.trim()), 120);
    };
    input.onfocus = () => {
        if (!input.value.trim() && _prereqAllTasks.length) _renderPrereqDropdown('');
    };

    // Close dropdown on outside click
    const closeDropdown = (e) => {
        if (!document.getElementById('prereqs-selector').contains(e.target)) {
            document.getElementById('prereqs-dropdown').style.display = 'none';
        }
    };
    setTimeout(() => document.addEventListener('click', closeDropdown), 0);
    window._prereqCloseDropdown = closeDropdown;
}

function _renderPrereqDropdown(query) {
    const dropdown = document.getElementById('prereqs-dropdown');
    const q = (query || '').toLowerCase();
    const filtered = _prereqAllTasks.filter(t => {
        const inSelected = _prereqSelectedIds.includes(t.id);
        if (inSelected) return false;
        if (!q) return true;
        return (t.title || '').toLowerCase().includes(q) || (t.id || '').toLowerCase().includes(q);
    });

    if (!filtered.length) {
        dropdown.innerHTML = `<div style="padding:0.6rem 0.8rem;color:#6c757d;font-size:0.85rem">${q ? 'No matching tasks.' : 'No other tasks in this project.'}</div>`;
    } else {
        dropdown.innerHTML = filtered.slice(0, 30).map(t => {
            const stageColors = {
                'idea': '#6c757d', 'planning': '#6f42c1', 'indev': '#0d6efd',
                'conceptual_review': '#fd7e14', 'optimization': '#20c997',
                'security': '#dc3545', 'final_review': '#e83e8c',
                'human_review': '#ffc107', 'completed': '#198754'
            };
            const color = stageColors[t.type] || '#6c757d';
            return `<div class="prereq-option" data-task-id="${t.id}" style="padding:0.45rem 0.8rem;cursor:pointer;display:flex;align-items:center;gap:0.5rem;transition:background 0.1s"
                onmouseover="this.style.background='#f0f4ff'" onmouseout="this.style.background=''"
                onclick="event.stopPropagation();_addPrerequisite('${t.id}');document.getElementById('task-prereqs-input').value='';document.getElementById('prereqs-dropdown').style.display='none';document.getElementById('task-prereqs-input').focus()">
                <span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${color};flex-shrink:0"></span>
                <span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:0.88rem">${escapeHtml(t.title)}</span>
                <span style="font-size:0.72rem;color:#adb5bd;flex-shrink:0">${t.type.replace('_', ' ')}</span>
            </div>`;
        }).join('');
        if (filtered.length > 30) {
            dropdown.innerHTML += `<div style="padding:0.4rem 0.8rem;color:#6c757d;font-size:0.78rem;border-top:1px solid #e9ecef">…and ${filtered.length - 30} more. Refine your search.</div>`;
        }
    }
    dropdown.style.display = '';
}

function _addPrerequisite(taskId) {
    if (_prereqSelectedIds.includes(taskId)) return;
    _prereqSelectedIds.push(taskId);
    _renderPrereqSelected();
}

function _removePrerequisite(taskId) {
    _prereqSelectedIds = _prereqSelectedIds.filter(id => id !== taskId);
    _renderPrereqSelected();
}

function _renderPrereqSelected() {
    const container = document.getElementById('preregs-selected');
    if (!_prereqSelectedIds.length) {
        container.innerHTML = '<span style="color:#adb5bd;font-size:0.82rem;font-style:italic">No prerequisites selected</span>';
        document.getElementById('prereqs-dag-container').style.display = 'none';
        return;
    }

    container.innerHTML = _prereqSelectedIds.map(id => {
        const t = taskData[id];
        if (!t) return '';
        const stageColors = {
            'idea': '#6c757d', 'planning': '#6f42c1', 'indev': '#0d6efd',
            'conceptual_review': '#fd7e14', 'optimization': '#20c997',
            'security': '#dc3545', 'final_review': '#e83e8c',
            'human_review': '#ffc107', 'completed': '#198754'
        };
        const color = stageColors[t.type] || '#6c757d';
        return `<span style="display:inline-flex;align-items:center;gap:0.3rem;padding:0.2rem 0.5rem;background:#e7f1ff;border:1px solid #b8daff;border-radius:4px;font-size:0.82rem">
            <span style="display:inline-block;width:6px;height:6px;border-radius:50%;background:${color}"></span>
            <span>${escapeHtml(t.title)}</span>
            <button type="button" onclick="event.stopPropagation();_removePrerequisite('${id}')" style="background:none;border:none;color:#0d6efd;cursor:pointer;font-size:1rem;line-height:1;padding:0 0.1rem" title="Remove">&times;</button>
        </span>`;
    }).join('');

    // Render mini DAG
    _renderPrereqDag();
}

function _renderPrereqDag() {
    const container = document.getElementById('prereqs-dag-container');
    const svg = document.getElementById('prereqs-dag-svg');
    if (!_prereqSelectedIds.length) {
        container.style.display = 'none';
        return;
    }

    container.style.display = '';
    const allPrereqTasks = _prereqSelectedIds.map(id => taskData[id]).filter(Boolean);
    if (!allPrereqTasks.length) return;

    // Build a flat list: current task → prerequisite tasks
    // If prerequisites have their own prerequisites, show the chain
    const visited = new Set();
    const nodes = [];
    const edges = [];

    function walk(taskId, depth) {
        if (visited.has(taskId)) return;
        visited.add(taskId);
        const t = taskData[taskId];
        if (!t) return;
        nodes.push({ id: taskId, title: (t.title || '').slice(0, 30), depth });
        (t.prerequisites || []).forEach(pid => {
            if (!visited.has(pid)) {
                edges.push({ from: taskId, to: pid });
                walk(pid, depth + 1);
            }
        });
    }

    // Start from current task (we use currentTaskId)
    const currentTask = taskData[currentTaskId];
    if (currentTask) {
        nodes.push({ id: currentTaskId, title: (currentTask.title || '').slice(0, 25) + '…', depth: 0, isCurrent: true });
        _prereqSelectedIds.forEach(id => walk(id, 1));
    }

    const width = Math.max(400, container.clientWidth || 400);
    const nodeW = 130, nodeH = 28;
    const colGap = 50, rowGap = 36;
    const maxCols = Math.max(1, Math.floor((width - nodeW) / (nodeW + colGap)));

    // Layout: BFS by depth, left-to-right within each depth level
    const byDepth = {};
    nodes.forEach(n => { (byDepth[n.depth] = byDepth[n.depth] || []).push(n); });
    const depths = Object.keys(byDepth).map(Number).sort((a, b) => a - b);

    const layout = {};
    depths.forEach(d => {
        const cols = byDepth[d];
        cols.forEach((n, i) => {
            layout[n.id] = {
                x: (i % maxCols) * (nodeW + colGap) + 10,
                y: Math.floor(i / maxCols) * (nodeH + rowGap) + 10,
            };
        });
    });

    const maxX = Math.max(...Object.values(layout).map(l => l.x), 0) + nodeW + 10;
    const maxY = Math.max(...Object.values(layout).map(l => l.y), 0) + nodeH + 10;
    svg.setAttribute('viewBox', `0 0 ${maxX + 10} ${maxY + 10}`);

    const stageColors = {
        'idea': '#6c757d', 'planning': '#6f42c1', 'indev': '#0d6efd',
        'conceptual_review': '#fd7e14', 'optimization': '#20c997',
        'security': '#dc3545', 'final_review': '#e83e8c',
        'human_review': '#ffc107', 'completed': '#198754'
    };

    let svgContent = '';

    // Edges
    edges.forEach(e => {
        const from = layout[e.from];
        const to = layout[e.to];
        if (!from || !to) return;
        const x1 = from.x + nodeW, y1 = from.y + nodeH / 2;
        const x2 = to.x, y2 = to.y + nodeH / 2;
        const mx = (x1 + x2) / 2;
        svgContent += `<path d="M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}" fill="none" stroke="#adb5bd" stroke-width="1.5" marker-end="url(#arrowhead)"/>`;
    });

    // Nodes
    nodes.forEach(n => {
        const pos = layout[n.id];
        if (!pos) return;
        const t = taskData[n.id];
        const color = n.isCurrent ? '#0d6efd' : (stageColors[t?.type] || '#6c757d');
        const fill = n.isCurrent ? '#e7f1ff' : '#fff';
        const stroke = n.isCurrent ? '#0d6efd' : '#dee2e6';
        const sw = n.isCurrent ? 2 : 1;
        const title = (n.title || '').replace(/</g, '&lt;').replace(/>/g, '&gt;');
        svgContent += `<rect x="${pos.x}" y="${pos.y}" width="${nodeW}" height="${nodeH}" rx="4" fill="${fill}" stroke="${stroke}" stroke-width="${sw}"/>`;
        svgContent += `<text x="${pos.x + 6}" y="${pos.y + nodeH / 2 + 4}" font-size="10" fill="${color}" font-family="inherit" font-weight="${n.isCurrent ? '600' : '400'}">${title}</text>`;
    });

    // Arrowhead marker
    svgContent = `<defs><marker id="arrowhead" markerWidth="8" markerHeight="6" refX="8" refY="3" orient="auto"><polygon points="0 0, 8 3, 0 6" fill="#adb5bd"/></marker></defs>` + svgContent;

    svg.innerHTML = svgContent;
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
        prerequisites: [..._prereqSelectedIds],
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
// Global Config: Infrastructure Management
// ============================================

function initializeGlobalConfigButtons() {
    // Only one button now
    const infraBtn = document.getElementById('manage-infra-btn');
    if (infraBtn) infraBtn.addEventListener('click', () => openInfraModal('llm'));

    document.getElementById('infra-modal').addEventListener('click', function(e) {
        if (e.target === this && _modalMousedownTarget === this) closeInfraModal();
    });
}

async function openInfraModal(tab = 'llm') {
    document.getElementById('infra-modal').classList.add('active');
    switchInfraTab(tab);
}

function closeInfraModal() {
    document.getElementById('infra-modal').classList.remove('active');
    _llmEditingId = null;
    _budgetEditingId = null;
    _cnEditingId = null;
}

async function switchInfraTab(tab) {
    // Toggle tab buttons
    document.querySelectorAll('.infra-tab').forEach(btn => {
        btn.classList.toggle('active', btn.id === `infra-tab-${tab}`);
    });
    // Toggle panes
    document.querySelectorAll('.infra-pane').forEach(pane => {
        pane.classList.toggle('active', pane.id === `infra-pane-${tab}`);
    });

    // Load data for the active tab
    if (tab === 'llm') {
        await loadLlmsAndBudgets();
        renderLlmList();
        populateComputeNodeSelect('llm-compute-node', null);
        populateComputeNodeSelect('llm-edit-compute-node', null);
        switchLlmTab('add');
    } else if (tab === 'budget') {
        await loadLlmsAndBudgets();
        renderBudgetList();
        switchBudgetTab('add');
    } else if (tab === 'compute') {
        await loadLlmsAndBudgets();
        renderComputeNodeList();
        switchComputeNodeTab('add');
    } else if (tab === 'tools') {
        await openToolsModal();
    }
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

// --- LLM Modal (Tab) ---

let _llmEditingId = null;  // Currently editing LLM id (null = add mode)

async function openLlmModal() {
    openInfraModal('llm');
}

function closeLlmModal() {
    closeInfraModal();
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
    // Populate capability checkboxes
    const caps = llm.capabilities || [];
    document.querySelectorAll('#llm-edit-capabilities-checkboxes input[type=checkbox]').forEach(cb => {
        cb.checked = caps.includes(cb.value);
    });
    const toolsCb = document.getElementById('llm-edit-supports-tools');
    const visionCb = document.getElementById('llm-edit-supports-vision');
    if (toolsCb) toolsCb.checked = llm.supports_tools !== false;
    if (visionCb) visionCb.checked = !!llm.supports_vision;
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
        const caps = (l.capabilities || []).map(c => `<span class="llm-cap-badge">${c}</span>`).join('');
        html += `<tr style="border-bottom:1px solid #f0f0f0">
            <td style="padding:0.4rem">${l.id}</td>
            <td style="padding:0.4rem">${l.address}:${l.port}</td>
            <td style="padding:0.4rem"><a href="#" onclick="editLlmEntry(${l.id}); return false;" style="color:#0d6efd;text-decoration:none;cursor:pointer">${l.model}</a>${caps}</td>
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
    // Capability tags
    const capCheckboxesId = prefix === 'llm' ? 'llm-capabilities-checkboxes' : 'llm-edit-capabilities-checkboxes';
    const capabilities = Array.from(
        document.querySelectorAll(`#${capCheckboxesId} input[type=checkbox]:checked`)
    ).map(cb => cb.value);
    const supToolsId = prefix === 'llm' ? 'llm-supports-tools' : 'llm-edit-supports-tools';
    const supVisionId = prefix === 'llm' ? 'llm-supports-vision' : 'llm-edit-supports-vision';
    const supports_tools = document.getElementById(supToolsId)?.checked !== false;
    const supports_vision = !!document.getElementById(supVisionId)?.checked;
    return { address, port, model, parallel_sessions: parallelRaw, max_context: contextRaw, notes,
             cost_per_million_prompt_tokens: costPrompt,
             cost_per_million_completion_tokens: costCompletion,
             compute_node_id, capabilities, supports_tools, supports_vision };
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

// --- Budget Modal (Tab) ---

let _budgetEditingId = null;  // Currently editing budget id (null = add mode)

async function openBudgetModal() {
    openInfraModal('budget');
}

function closeBudgetModal() {
    closeInfraModal();
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

// --- Compute Node Modal (Tab) ---

let _cnEditingId = null;  // Currently editing compute node id (null = add mode)

async function openComputeNodeModal() {
    openInfraModal('compute');
}

function closeComputeNodeModal() {
    closeInfraModal();
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
    closeInfraModal();
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
    human_review:       '#fd7e14',
    completed:         '#198754',
    subdividing:       '#6f42c1',
};

const MAP_COLUMN_LABELS = {
    architecture:      'ARCHITECTURE MAP',
    idea:              'IDEAS MAP',
    planning:          'PLANNING MAP',
    indev:             'IN DEVELOPMENT MAP',
    conceptual_review: 'AI REVIEW MAP — CONCEPT',
    optimization:      'AI REVIEW MAP — OPTIMIZATION',
    security:          'AI REVIEW MAP — SECURITY',
    human_review:       'HUMAN REVIEW MAP',
    completed:         'COMPLETED MAP',
};

let mapLinkMode = { active: false, sourceId: null };

function toggleMapLinkMode() {
    mapLinkMode.active = !mapLinkMode.active;
    mapLinkMode.sourceId = null;

    const btn = document.getElementById('column-map-link-btn');
    if (!btn) return;

    if (mapLinkMode.active) {
        btn.textContent = 'Link Mode: ON (Select Prereq)';
        btn.classList.add('active');
        document.getElementById('column-map-scroll-wrap').style.cursor = 'crosshair';
        showToast('Link Mode: Select the prerequisite task first.', 'info');
    } else {
        btn.textContent = 'Link Mode: OFF';
        btn.classList.remove('active');
        document.getElementById('column-map-scroll-wrap').style.cursor = '';
    }

    // Clear any existing highlights
    document.querySelectorAll('.map-node').forEach(n => {
        n.style.outline = '';
        n.style.outlineOffset = '';
    });
}

function _handleMapNodeClick(e, id) {
    if (!mapLinkMode.active) return;
    
    // Stop propagation so we don't trigger background clicks
    e.stopPropagation();

    if (!mapLinkMode.sourceId) {
        // First click: the prerequisite
        mapLinkMode.sourceId = id;
        const el = document.getElementById(`map-node-${id}`);
        if (el) {
            el.style.outline = '4px solid #0d6efd';
            el.style.outlineOffset = '2px';
        }
        const btn = document.getElementById('column-map-link-btn');
        if (btn) btn.textContent = 'Link Mode: ON (Select Dependent)';
        showToast('Selected prerequisite. Now click the dependent task.', 'info');
    } else {
        // Second click: the dependent
        const prereqId = mapLinkMode.sourceId;
        const dependentId = id;

        if (prereqId === dependentId) {
            showToast('Cannot link a task to itself', 'warning');
            return;
        }

        // Association: dependentId depends on prereqId (toggle)
        _toggleMapPrerequisite(dependentId, prereqId);
        
        // Turn off link mode
        toggleMapLinkMode();
    }
}

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
    // Always use 'project' view to span all columns as requested, 
    // but we can keep the title informative.
    columnMapType = 'project'; 
    mapTransform = { x: 0, y: 0, scale: 1 };

    document.querySelector('.kanban-board').style.display = 'none';
    const container = document.getElementById('column-map-container');
    container.style.display = 'flex';

    // Global title for the 2D view
    const label = 'PROJECT MAP (ALL COLUMNS)';
    document.getElementById('column-map-title').textContent = label;

    renderColumnMap(columnMapType);
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
        if (colType === 'project' || colType === 'all') return true;
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

    tasks.forEach(t => {
        // 1. Parent-child relationships (IDEAS tree)
        if (t.parent_task_id && taskMap[t.parent_task_id]) {
            edges.push({ fromId: t.parent_task_id, toId: t.id });
            (childrenOf[t.parent_task_id] = childrenOf[t.parent_task_id] || []).push(t.id);
        }
        // 2. Prerequisite relationships (PLANNING/INDEV/etc. dependencies)
        (t.prerequisites || []).forEach(prereqId => {
            if (taskMap[prereqId]) {
                // Avoid redundant edges if it's already a parent-child edge
                if (t.parent_task_id !== prereqId) {
                    edges.push({ fromId: prereqId, toId: t.id });
                    (childrenOf[prereqId] = childrenOf[prereqId] || []).push(t.id);
                }
            }
        });
    });

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
    const isGlobal = (colType === 'project' || colType === 'all');
    _mapCurrentColor = isGlobal ? '#adb5bd' : (MAP_COLORS[colType] || '#6c757d');
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
        const nodeColor = MAP_COLORS[task.type] || '#6c757d';
        node.className = `map-node ${task.type || ''}`;
        node.style.left           = (x + OX) + 'px';
        node.style.top            = (y + OY) + 'px';
        node.style.borderLeftColor = nodeColor;

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
            <button class="card-highlight-btn" title="${task.is_starred ? 'Unstar (remove priority boost)' : 'Star (boost scheduler priority)'}" onclick="event.stopPropagation();toggleHighlight('${id}')">${task.is_starred ? '★' : '☆'}</button>
            <div class="map-node-title" onclick="editTask('${id}')">${task.title || '(untitled)'}${badges ? ' ' + badges : ''}</div>
            <div class="map-node-meta">${tagHtml}${ownerHtml}</div>
            <div class="map-node-prereq-handle" data-handle-for="${id}" title="Drag to create prerequisite" style="position:absolute;top:4px;right:4px;width:16px;height:16px;border-radius:50%;background:#0d6efd;color:#fff;font-size:10px;display:flex;align-items:center;justify-content:center;cursor:crosshair;z-index:10;opacity:0;transition:opacity 0.15s" onmousedown="event.stopPropagation();_mapStartPrereqDrag(event,'${id}')">+</div>
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
                <button class="toolbar-btn" title="Card Story — agent session timeline" onclick="event.stopPropagation();toolbarOpenStory('${id}')">📜</button>
                <button class="toolbar-btn" title="Clone as new Idea" onclick="event.stopPropagation();toolbarClone('${id}')">⧉</button>
                <button class="toolbar-btn" title="Pin to top of column" onclick="event.stopPropagation();toolbarPin('${id}')">📌</button>
            </div>
            <div class="map-node-actions">${actionHtml}</div>`;

        _applyHighlightState(node, id);

        // Click-to-link handler
        node.addEventListener('click', (e) => _handleMapNodeClick(e, id));

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

// ============================================
// Column Map: Drag-to-Connect Prerequisites
// ============================================

let _mapPrereqDrag = { active: false, sourceId: null, line: null };

function _mapStartPrereqDrag(event, sourceId) {
    event.preventDefault();
    event.stopPropagation();
    _mapPrereqDrag = { active: true, sourceId, line: null };

    // Get source node position
    const sourcePos = _mapCurrentNodePositions[sourceId];
    if (!sourcePos) return;

    const svg = document.getElementById('column-map-svg');
    const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
    line.setAttribute('stroke', '#0d6efd');
    line.setAttribute('stroke-width', '2');
    line.setAttribute('stroke-dasharray', '6,3');
    line.setAttribute('x1', sourcePos.x + _mapOffsetX + 130);
    line.setAttribute('y1', sourcePos.y + _mapOffsetY + 20);
    line.setAttribute('x2', sourcePos.x + _mapOffsetX + 130);
    line.setAttribute('y2', sourcePos.y + _mapOffsetY + 20);
    svg.appendChild(line);
    _mapPrereqDrag.line = line;

    // Highlight all nodes as drop targets
    document.querySelectorAll('.map-node').forEach(n => {
        n.style.outline = '2px dashed #0d6efd44';
        n.style.outlineOffset = '2px';
    });

    document.addEventListener('mousemove', _mapPrereqDragMove);
    document.addEventListener('mouseup', _mapPrereqDragEnd);
}

function _mapPrereqDragMove(event) {
    if (!_mapPrereqDrag.active) return;
    const line = _mapPrereqDrag.line;
    if (!line) return;

    const svg = document.getElementById('column-map-svg');
    const pt = svg.createSVGPoint();
    pt.x = event.clientX;
    pt.y = event.clientY;
    const svgP = pt.matrixTransform(svg.getScreenCTM().inverse());

    line.setAttribute('x2', svgP.x);
    line.setAttribute('y2', svgP.y);
}

function _mapPrereqDragEnd(event) {
    if (!_mapPrereqDrag.active) return;
    _mapPrereqDrag.active = false;

    document.removeEventListener('mousemove', _mapPrereqDragMove);
    document.removeEventListener('mouseup', _mapPrereqDragEnd);

    // Remove the temp line
    if (_mapPrereqDrag.line) {
        _mapPrereqDrag.line.remove();
        _mapPrereqDrag.line = null;
    }

    // Remove highlight from all nodes
    document.querySelectorAll('.map-node').forEach(n => {
        n.style.outline = '';
        n.style.outlineOffset = '';
    });

    // Check if we dropped on another node
    const target = event.target.closest('.map-node');
    if (!target) { _mapPrereqDrag.sourceId = null; return; }

    const targetId = target.id.replace('map-node-', '');
    const sourceId = _mapPrereqDrag.sourceId;
    if (!sourceId || sourceId === targetId) { _mapPrereqDrag.sourceId = null; return; }

    // Toggle target as prerequisite of source (drag-to-toggle)
    _toggleMapPrerequisite(sourceId, targetId);
    _mapPrereqDrag.sourceId = null;
}

async function _toggleMapPrerequisite(taskId, prereqId) {
    const task = taskData[taskId];
    if (!task) return;

    const existing = task.prerequisites || [];
    const isRemoving = existing.includes(prereqId);
    
    let updatedPrereqs;
    if (isRemoving) {
        updatedPrereqs = existing.filter(id => id !== prereqId);
    } else {
        updatedPrereqs = [...existing, prereqId];
    }

    try {
        const resp = await fetch(`${API_BASE}/tasks/${taskId}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ prerequisites: updatedPrereqs })
        });
        if (!resp.ok) {
            showToast(`Failed to ${isRemoving ? 'remove' : 'add'} prerequisite`, 'error');
            return;
        }
        const result = await resp.json();
        taskData[taskId] = result;
        // Update allTasks cache
        const idx = allTasks.findIndex(t => t.id === taskId);
        if (idx !== -1) allTasks[idx] = result;

        // Immediately update visual arrows in the map view
        if (isRemoving) {
            _mapCurrentEdges = _mapCurrentEdges.filter(e => !(e.fromId === prereqId && e.toId === taskId));
        } else {
            _mapCurrentEdges.push({ fromId: prereqId, toId: taskId });
        }
        _mapRedrawArrows();

        showToast(`${isRemoving ? 'Removed' : 'Added'} prerequisite: ${taskData[prereqId]?.title?.slice(0, 30) || prereqId}`, 'success');
    } catch (err) {
        showToast(`Failed to ${isRemoving ? 'toggle' : 'add'} prerequisite: ` + err.message, 'error');
    }
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

async function unmergeTask(taskId) {
    const task = taskData[taskId];
    const label = task ? task.title : taskId;
    if (!await showConfirm(
        'Unmerge Task',
        `This will run "git revert" on the merge commit and move "${label}" back to Human Review. Continue?`,
        'Unmerge'
    )) return;
    const resp = await fetch(`${API_BASE}/tasks/${taskId}/unmerge`, { method: 'POST' });
    const d = await resp.json().catch(() => ({}));
    if (resp.ok) {
        showToast(`Unmerged — moved to Human Review. ${d.git || ''}`, 'success');
        await loadTasksFromDatabase();
    } else {
        showToast(d.detail || 'Unmerge failed.', 'error');
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
    security: 'Security', human_review: 'Full Review', completed: 'Completed',
};

function toolbarStagePicker(taskId, btn) {
    // Toggle off if same task already open
    if (_stagePickerTaskId === taskId) { _removeStagePicker(); return; }
    _removeStagePicker();
    _stagePickerTaskId = taskId;

    const flyout = document.createElement('div');
    flyout.id = '_stage-picker-flyout';
    flyout.className = 'stage-picker-flyout';

    const pipeline = ['architecture','idea','planning','indev','conceptual_review','optimization','security','human_review','completed'];
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
    const labels = { planning: 'Planning', review: 'Conceptual Review', optimization: 'Optimization', security: 'Security', 'final-review': 'Final Review' };
    const label = labels[pipeline] || pipeline;
    const resp = await fetch(`${API_BASE}/tasks/${taskId}/run-${pipeline}`, { method: 'POST' });
    const d = await resp.json().catch(() => ({}));
    if (resp.ok) {
        showToast(`${label} pipeline started.`, 'success');
    } else {
        showToast(d.detail || `${label} pipeline failed to start.`, 'error');
    }
}

async function toolbarForceRecompute(taskId) {
    // Set cache_mode to force_with_context, then trigger the planning pipeline.
    // The pipeline will recompute from scratch while injecting context from prior failures.
    try {
        const modeResp = await fetch(`${API_BASE}/tasks/${taskId}/cache-mode`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ mode: 'force_with_context' }),
        });
        if (!modeResp.ok) {
            const d = await modeResp.json().catch(() => ({}));
            showToast(d.detail || 'Failed to set recompute mode.', 'error');
            return;
        }
        const planResp = await fetch(`${API_BASE}/tasks/${taskId}/run-planning`, { method: 'POST' });
        const d = await planResp.json().catch(() => ({}));
        if (planResp.ok) {
            showToast('Recomputing plan (with prior failure context)...', 'success');
            setCardProcessing(taskId, true);
            startTransitionPolling(taskId, new Date().toISOString());
        } else {
            showToast(d.detail || 'Failed to start planning pipeline.', 'error');
        }
    } catch (err) {
        showToast('Force recompute failed: ' + err.message, 'error');
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

function toolbarOpenStory(taskId) {
    window.open(`/story?task=${encodeURIComponent(taskId)}`, '_blank');
}

function toolbarOpenMap(taskId) {
    const task = taskData[taskId];
    if (!task) return;
    openColumnMap(task.type, taskId);
}


// ============================================
// Maestro Flight Control
// ============================================

async function openMaestroConfigModal() {
    document.getElementById('maestro-config-modal').classList.add('active');
    
    // Populate dropdowns
    const enabledCheck = document.getElementById('mcfg-enabled');
    const llmSelect = document.getElementById('mcfg-llm-id');
    const budgetSelect = document.getElementById('mcfg-budget-id');
    const autoSteer = document.getElementById('mcfg-auto-steer');
    const autoJanitor = document.getElementById('mcfg-auto-janitor');
    const autoMerge = document.getElementById('mcfg-auto-merge');
    
    llmSelect.innerHTML = '<option value="">Loading...</option>';
    budgetSelect.innerHTML = '<option value="">Loading...</option>';
    
    try {
        const [llms, budgets, config] = await Promise.all([
            fetch(`${API_BASE}/llms`).then(r => r.json()),
            fetch(`${API_BASE}/budgets`).then(r => r.json()),
            fetch(`${API_BASE}/maestro/config`).then(r => r.json())
        ]);
        
        llmSelect.innerHTML = '<option value="">(select an LLM)</option>' + 
            llms.map(l => `<option value="${l.id}">${l.model} (${l.address}:${l.port})</option>`).join('');
            
        budgetSelect.innerHTML = '<option value="">(select a budget)</option>' + 
            budgets.map(b => `<option value="${b.id}">${b.name}</option>`).join('');
            
        enabledCheck.checked = !!config.enabled;
        if (config.llm_id) llmSelect.value = config.llm_id;
        if (config.budget_id) budgetSelect.value = config.budget_id;
        
        autoSteer.checked = !!config.auto_steer;
        autoJanitor.checked = !!config.auto_janitor;
        autoMerge.checked = !!config.auto_merge;
        
    } catch (e) {
        showToast('Error loading Maestro configuration.', 'error');
        llmSelect.innerHTML = '<option value="">Error loading</option>';
        budgetSelect.innerHTML = '<option value="">Error loading</option>';
    }
}

function closeMaestroConfigModal() {
    document.getElementById('maestro-config-modal').classList.remove('active');
}

async function saveMaestroConfig() {
    const enabled = document.getElementById('mcfg-enabled').checked;
    const llmId = document.getElementById('mcfg-llm-id').value;
    const budgetId = document.getElementById('mcfg-budget-id').value;
    const autoSteer = document.getElementById('mcfg-auto-steer').checked;
    const autoJanitor = document.getElementById('mcfg-auto-janitor').checked;
    const autoMerge = document.getElementById('mcfg-auto-merge').checked;
    
    const data = {
        enabled: enabled,
        auto_steer: autoSteer,
        auto_janitor: autoJanitor,
        auto_merge: autoMerge
    };
    if (llmId) data.llm_id = parseInt(llmId);
    if (budgetId) data.budget_id = parseInt(budgetId);
    
    try {
        const resp = await fetch(`${API_BASE}/maestro/config`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(data)
        });
        
        if (resp.ok) {
            showToast('Maestro global configuration saved.', 'success');
            closeMaestroConfigModal();
        } else {
            showToast('Failed to save Maestro configuration.', 'error');
        }
    } catch (e) {
        showToast('Error: ' + e.message, 'error');
    }
}

async function openMaestroDecisionsModal() {
    document.getElementById('maestro-decisions-modal').classList.add('active');
    await refreshDecisionsList();
}

function closeMaestroDecisionsModal() {
    document.getElementById('maestro-decisions-modal').classList.remove('active');
}

async function refreshDecisionsList() {
    const list = document.getElementById('decisions-list');
    list.innerHTML = '<div style="padding:1rem;text-align:center">Loading decisions...</div>';
    
    try {
        const resp = await fetch(`${API_BASE}/maestro/${encodeURIComponent(currentProject)}/decisions`);
        const decisions = await resp.json();
        
        if (decisions.length === 0) {
            list.innerHTML = '<div style="padding:2rem;text-align:center;color:#6c757d">No binding decisions recorded for this project.</div>';
            return;
        }
        
        list.innerHTML = decisions.map(d => `
            <div style="padding:1rem;border-bottom:1px solid #dee2e6;display:flex">
                <div style="flex:1">
                    <div style="font-weight:bold;margin-bottom:0.25rem">${d.topic}</div>
                    <div style="font-size:0.85rem">${d.decision}</div>
                    ${d.rationale ? `<div style="font-size:0.75rem;color:#6c757d;margin-top:0.3rem">Rationale: ${d.rationale}</div>` : ''}
                </div>
                <button class="btn btn-sm btn-outline-danger" style="align-self:center" onclick="deleteMaestroDecision(${d.id})">Delete</button>
            </div>
        `).join('');
    } catch (e) {
        list.innerHTML = '<div style="padding:1rem;text-align:center;color:red">Error loading decisions.</div>';
    }
}

async function addMaestroDecision() {
    const topic = document.getElementById('dec-topic').value.trim();
    const decision = document.getElementById('dec-content').value.trim();
    const rationale = document.getElementById('dec-rationale').value.trim();
    
    if (!topic || !decision) {
        showToast('Topic and Decision are required.', 'warning');
        return;
    }
    
    try {
        const resp = await fetch(`${API_BASE}/maestro/${encodeURIComponent(currentProject)}/decisions`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ topic, decision, rationale, is_binding: true })
        });
        
        if (resp.ok) {
            showToast('Decision added and is now BINDING.', 'success');
            document.getElementById('dec-topic').value = '';
            document.getElementById('dec-content').value = '';
            document.getElementById('dec-rationale').value = '';
            await refreshDecisionsList();
        } else {
            showToast('Failed to add decision.', 'error');
        }
    } catch (e) {
        showToast('Error: ' + e.message, 'error');
    }
}

async function deleteMaestroDecision(id) {
    if (!confirm('Are you sure you want to delete this architectural decision?')) return;
    
    try {
        const resp = await fetch(`${API_BASE}/maestro/decisions/${id}`, { method: 'DELETE' });
        if (resp.ok) {
            showToast('Decision deleted.', 'success');
            await refreshDecisionsList();
        }
    } catch (e) {
        showToast('Error: ' + e.message, 'error');
    }
}

async function resumeFromConsultation(taskId, btn) {
    const card = btn.closest('.task-card');
    const hintInput = card.querySelector('.consult-hint-input');
    const hint = hintInput.value.trim();
    
    if (!hint) {
        showToast('Please provide a hint to resume the agent.', 'warning');
        return;
    }
    
    btn.disabled = true;
    btn.textContent = 'Resuming...';
    
    try {
        const resp = await fetch(`${API_BASE}/tasks/${taskId}/resume`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ hint })
        });
        
        if (resp.ok) {
            showToast('Hint sent. Agent is resuming...', 'success');
            // The card will be refreshed by the next background poll
            await refreshTasks();
        } else {
            const d = await resp.json();
            showToast(d.detail || 'Failed to resume.', 'error');
            btn.disabled = false;
            btn.textContent = 'Send Hint & Resume';
        }
    } catch (e) {
        showToast('Error: ' + e.message, 'error');
        btn.disabled = false;
        btn.textContent = 'Send Hint & Resume';
    }
}

// =============================================================================
// Autopilot / Mission Dialog  (Phase 7)
// =============================================================================

let _autopilotOn = false;

async function _refreshAutopilotState() {
    try {
        const r = await fetch('/api/settings/autopilot');
        if (!r.ok) return;
        const d = await r.json();
        _autopilotOn = d.autopilot === 'on';
        _applyAutopilotBtn();
        // Sync hour fields if the dialog is open
        const sh = document.getElementById('mc-start-hour');
        const eh = document.getElementById('mc-stop-hour');
        if (sh) sh.value = d.start_hour;
        if (eh) eh.value = d.stop_hour;
    } catch (_) {}
}

function _applyAutopilotBtn() {
    const btn = document.getElementById('autopilot-btn');
    if (!btn) return;
    if (_autopilotOn) {
        btn.textContent = '⏸ Human in the Loop';
        btn.style.background = '#d97706';
        btn.style.borderColor = '#b45309';
        btn.style.color = '#fff';
    } else {
        btn.textContent = '⚡ Leave it to the Maestro';
        btn.style.background = '';
        btn.style.borderColor = '';
        btn.style.color = '';
    }
}

function handleAutopilotClick() {
    if (_autopilotOn) {
        // Pause immediately — no dialog
        _disengageAutopilot();
    } else {
        openMissionModal();
    }
}

async function _disengageAutopilot() {
    showToast('Pausing Maestro…', 'info');
    try {
        const r = await fetch('/api/settings/autopilot', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ autopilot: 'off' }),
        });
        if (r.ok) {
            _autopilotOn = false;
            _applyAutopilotBtn();
            showToast('Maestro paused — Human in the Loop.', 'success');
        } else {
            showToast('Failed to pause Maestro.', 'error');
        }
    } catch (e) {
        showToast('Error: ' + e.message, 'error');
    }
}

async function openMissionModal() {
    // Load persistent defaults from localStorage
    const raw = localStorage.getItem('maestro_mission_defaults');
    const defaults = raw ? JSON.parse(raw) : {};

    document.getElementById('mc-time-enabled').checked  = defaults.time_enabled  ?? true;
    document.getElementById('mc-time-hours').value       = defaults.time_hours    ?? 8;
    document.getElementById('mc-tokens-enabled').checked = defaults.tokens_enabled ?? true;
    document.getElementById('mc-tokens-k').value         = defaults.tokens_k      ?? 500;
    document.getElementById('mc-cards-enabled').checked  = defaults.cards_enabled  ?? false;
    document.getElementById('mc-cards-n').value           = defaults.cards_n       ?? 5;
    document.getElementById('mc-goal-enabled').checked   = defaults.goal_enabled   ?? false;
    document.getElementById('mc-save-schedule').checked  = defaults.save_schedule  ?? false;

    // Populate goal-card select from current project tasks
    const sel = document.getElementById('mc-goal-card');
    sel.innerHTML = '<option value="">Select card…</option>';
    for (const t of allTasks) {
        if (t.type !== 'architecture' && t.type !== 'completed') {
            const opt = document.createElement('option');
            opt.value = t.id;
            opt.textContent = t.title || t.id;
            if (defaults.goal_card_id === t.id) opt.selected = true;
            sel.appendChild(opt);
        }
    }

    // Prefill hours from API
    const r = await fetch('/api/settings/autopilot').catch(() => null);
    if (r && r.ok) {
        const d = await r.json();
        document.getElementById('mc-start-hour').value = d.start_hour;
        document.getElementById('mc-stop-hour').value  = d.stop_hour;
    }

    document.getElementById('mission-modal').style.display = 'flex';
}

function closeMissionModal() {
    document.getElementById('mission-modal').style.display = 'none';
}

async function startMission() {
    const timeEnabled   = document.getElementById('mc-time-enabled').checked;
    const timeHours     = parseFloat(document.getElementById('mc-time-hours').value) || 0;
    const tokensEnabled = document.getElementById('mc-tokens-enabled').checked;
    const tokensK       = parseFloat(document.getElementById('mc-tokens-k').value) || 0;
    const cardsEnabled  = document.getElementById('mc-cards-enabled').checked;
    const cardsN        = parseInt(document.getElementById('mc-cards-n').value) || 0;
    const goalEnabled   = document.getElementById('mc-goal-enabled').checked;
    const goalCardId    = document.getElementById('mc-goal-card').value || null;
    const saveSchedule  = document.getElementById('mc-save-schedule').checked;
    const startHour     = parseInt(document.getElementById('mc-start-hour').value) || 0;
    const stopHour      = parseInt(document.getElementById('mc-stop-hour').value) || 24;

    // Save to localStorage
    localStorage.setItem('maestro_mission_defaults', JSON.stringify({
        time_enabled: timeEnabled, time_hours: timeHours,
        tokens_enabled: tokensEnabled, tokens_k: tokensK,
        cards_enabled: cardsEnabled, cards_n: cardsN,
        goal_enabled: goalEnabled, goal_card_id: goalCardId,
        save_schedule: saveSchedule,
    }));

    const mission = {
        time_limit_seconds: timeEnabled && timeHours > 0 ? Math.round(timeHours * 3600) : null,
        token_budget:       tokensEnabled && tokensK > 0 ? Math.round(tokensK * 1024) : null,
        card_count_target:  cardsEnabled && cardsN > 0   ? cardsN : null,
        goal_card_id:       goalEnabled ? goalCardId : null,
    };

    const body = {
        autopilot: 'on',
        start_hour: startHour,
        stop_hour:  stopHour,
        save_schedule: saveSchedule,
        mission,
    };

    try {
        const r = await fetch('/api/settings/autopilot', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        if (r.ok) {
            _autopilotOn = true;
            _applyAutopilotBtn();
            closeMissionModal();
            showToast('Maestro engaged — Leave it to the Maestro!', 'success');
        } else {
            const d = await r.json().catch(() => ({}));
            showToast(d.detail || 'Failed to engage Maestro.', 'error');
        }
    } catch (e) {
        showToast('Error: ' + e.message, 'error');
    }
}

// Poll autopilot state on startup and every 30 s (mission may terminate server-side)
_refreshAutopilotState();
setInterval(_refreshAutopilotState, 30000);

// ---------------------------------------------------------------------------
// Document Store modal
// ---------------------------------------------------------------------------

let _docStoreAllDocs = [];   // full list from last fetch
let _docStoreSelectedKey = null;

async function openDocStoreModal() {
    if (!currentProject) {
        showToast('Select a project first.', 'error');
        return;
    }
    document.getElementById('doc-store-modal-title').textContent =
        `Document Store — ${currentProject}`;
    document.getElementById('doc-store-modal').style.display = 'flex';
    document.getElementById('doc-store-search').value = '';
    _docStoreSelectedKey = null;
    document.getElementById('doc-store-detail').innerHTML =
        '<p style="color:#6c757d;font-style:italic;text-align:center;margin-top:3rem">Select a document to view its content.</p>';
    await docStoreRefresh();
}

function closeDocStoreModal() {
    document.getElementById('doc-store-modal').style.display = 'none';
}

async function docStoreRefresh() {
    if (!currentProject) return;
    try {
        const r = await fetch(`/api/projects/${encodeURIComponent(currentProject)}/documents`);
        if (!r.ok) throw new Error(await r.text());
        _docStoreAllDocs = await r.json();
        _renderDocStoreList(_docStoreAllDocs);
    } catch (e) {
        document.getElementById('doc-store-list').innerHTML =
            `<p style="color:#dc3545;padding:1rem">Error: ${escapeHtml(e.message)}</p>`;
    }
}

function docStoreFilter() {
    const q = document.getElementById('doc-store-search').value.toLowerCase();
    const filtered = q
        ? _docStoreAllDocs.filter(d => d.key.toLowerCase().includes(q))
        : _docStoreAllDocs;
    _renderDocStoreList(filtered);
}

function _renderDocStoreList(docs) {
    const el = document.getElementById('doc-store-list');
    if (!docs.length) {
        el.innerHTML = '<p style="color:#6c757d;padding:1rem;font-style:italic">No documents.</p>';
        return;
    }
    el.innerHTML = docs.map(d => {
        const active = d.key === _docStoreSelectedKey;
        const tagsHtml = (d.tags || []).map(t =>
            `<span style="background:#e9ecef;border-radius:3px;padding:1px 5px;font-size:0.75rem;margin-right:3px">${escapeHtml(t)}</span>`
        ).join('');
        const size = d.content_size_bytes != null
            ? ` <span style="color:#adb5bd">${_fmtBytes(d.content_size_bytes)}</span>` : '';
        return `<div class="doc-store-item${active ? ' doc-store-item--active' : ''}"
                     style="padding:0.5rem 0.9rem;cursor:pointer;border-bottom:1px solid #f0f0f0;
                            ${active ? 'background:#e8f0fe;' : ''}"
                     onclick="docStoreSelect(${JSON.stringify(d.key)})">
            <div style="font-weight:500;font-size:0.88rem;word-break:break-all">${escapeHtml(d.key)}</div>
            <div style="margin-top:2px">${tagsHtml}${size}</div>
            <div style="color:#adb5bd;font-size:0.75rem">${_relTime(d.updated_at)}</div>
        </div>`;
    }).join('');
}

async function docStoreSelect(key) {
    _docStoreSelectedKey = key;
    // Re-render list to show active highlight
    const q = document.getElementById('doc-store-search').value.toLowerCase();
    _renderDocStoreList(q ? _docStoreAllDocs.filter(d => d.key.toLowerCase().includes(q)) : _docStoreAllDocs);

    const detail = document.getElementById('doc-store-detail');
    detail.innerHTML = '<p style="color:#6c757d;padding:1rem">Loading…</p>';
    try {
        const r = await fetch(
            `/api/projects/${encodeURIComponent(currentProject)}/documents/${encodeURIComponent(key)}`
        );
        if (!r.ok) throw new Error(await r.text());
        const doc = await r.json();
        const tagsHtml = (doc.tags || []).map(t =>
            `<span style="background:#e9ecef;border-radius:3px;padding:2px 6px;font-size:0.8rem">${escapeHtml(t)}</span>`
        ).join(' ');
        detail.innerHTML = `
            <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:0.75rem">
                <div>
                    <div style="font-weight:600;font-size:1rem;word-break:break-all">${escapeHtml(doc.key)}</div>
                    <div style="color:#6c757d;font-size:0.8rem;margin-top:2px">
                        Written by: ${escapeHtml(doc.written_by_task_id || 'human')} &nbsp;·&nbsp;
                        Updated: ${_relTime(doc.updated_at)}
                    </div>
                    <div style="margin-top:4px">${tagsHtml}</div>
                </div>
                <button class="btn btn-sm" onclick="docStoreOpenEdit(${JSON.stringify(doc.key)})"
                    style="flex-shrink:0;margin-left:1rem">Edit</button>
            </div>
            <pre style="background:#f8f9fa;border:1px solid #dee2e6;border-radius:4px;
                        padding:0.75rem;font-size:0.83rem;overflow-x:auto;white-space:pre-wrap;
                        word-break:break-word;max-height:60vh;overflow-y:auto">${escapeHtml(doc.content)}</pre>`;
    } catch (e) {
        detail.innerHTML = `<p style="color:#dc3545;padding:1rem">Error: ${escapeHtml(e.message)}</p>`;
    }
}

function docStoreOpenNew() {
    document.getElementById('doc-edit-modal-title').textContent = 'New Document';
    document.getElementById('doc-edit-key').value = '';
    document.getElementById('doc-edit-key').disabled = false;
    document.getElementById('doc-edit-tags').value = '';
    document.getElementById('doc-edit-content').value = '';
    document.getElementById('doc-edit-delete-btn').style.display = 'none';
    document.getElementById('doc-store-edit-modal').style.display = 'flex';
}

async function docStoreOpenEdit(key) {
    try {
        const r = await fetch(
            `/api/projects/${encodeURIComponent(currentProject)}/documents/${encodeURIComponent(key)}`
        );
        if (!r.ok) throw new Error(await r.text());
        const doc = await r.json();
        document.getElementById('doc-edit-modal-title').textContent = 'Edit Document';
        document.getElementById('doc-edit-key').value = doc.key;
        document.getElementById('doc-edit-key').disabled = true;
        document.getElementById('doc-edit-tags').value = (doc.tags || []).join(', ');
        document.getElementById('doc-edit-content').value = doc.content || '';
        document.getElementById('doc-edit-delete-btn').style.display = 'inline-block';
        document.getElementById('doc-store-edit-modal').style.display = 'flex';
    } catch (e) {
        showToast('Error loading document: ' + e.message, 'error');
    }
}

async function docStoreSave() {
    const key = document.getElementById('doc-edit-key').value.trim();
    const content = document.getElementById('doc-edit-content').value;
    const rawTags = document.getElementById('doc-edit-tags').value.trim();
    const tags = rawTags ? rawTags.split(',').map(t => t.trim()).filter(Boolean) : null;
    if (!key) { showToast('Key is required.', 'error'); return; }
    if (!currentProject) { showToast('No project selected.', 'error'); return; }
    try {
        const r = await fetch(
            `/api/projects/${encodeURIComponent(currentProject)}/documents/${encodeURIComponent(key)}`,
            {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ content, tags }),
            }
        );
        if (!r.ok) throw new Error(await r.text());
        document.getElementById('doc-store-edit-modal').style.display = 'none';
        showToast('Document saved.', 'success');
        _docStoreSelectedKey = key;
        await docStoreRefresh();
        await docStoreSelect(key);
    } catch (e) {
        showToast('Save failed: ' + e.message, 'error');
    }
}

async function docStoreDelete() {
    const key = document.getElementById('doc-edit-key').value.trim();
    if (!key || !currentProject) return;
    if (!confirm(`Delete document "${key}"?`)) return;
    try {
        const r = await fetch(
            `/api/projects/${encodeURIComponent(currentProject)}/documents/${encodeURIComponent(key)}`,
            { method: 'DELETE' }
        );
        if (!r.ok) throw new Error(await r.text());
        document.getElementById('doc-store-edit-modal').style.display = 'none';
        showToast('Document deleted.', 'success');
        _docStoreSelectedKey = null;
        document.getElementById('doc-store-detail').innerHTML =
            '<p style="color:#6c757d;font-style:italic;text-align:center;margin-top:3rem">Select a document to view its content.</p>';
        await docStoreRefresh();
    } catch (e) {
        showToast('Delete failed: ' + e.message, 'error');
    }
}

function _fmtBytes(n) {
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KiB`;
    return `${(n / (1024 * 1024)).toFixed(1)} MiB`;
}

function _relTime(iso) {
    if (!iso) return '';
    const diff = Date.now() - new Date(iso).getTime();
    const m = Math.floor(diff / 60000);
    if (m < 1) return 'just now';
    if (m < 60) return `${m}m ago`;
    const h = Math.floor(m / 60);
    if (h < 24) return `${h}h ago`;
    return `${Math.floor(h / 24)}d ago`;
}

// ---------------------------------------------------------------------------
// Gap 5 — Self-modification UI helpers
// ---------------------------------------------------------------------------

function _removeSelfModBanner() {
    const existing = document.getElementById('self-mod-banner');
    if (existing) existing.remove();
}

async function _renderSelfModBanner() {
    _removeSelfModBanner();
    if (currentProject !== '_maestro_self') return;

    const banner = document.createElement('div');
    banner.id = 'self-mod-banner';
    banner.className = 'self-mod-banner';
    banner.textContent = '⚠ Self-Modification Mode — writes target Maestro source tree';

    // Fetch integration branch status asynchronously and append to banner
    try {
        const resp = await fetch(`${API_BASE}/projects/_maestro_self/integration-branch-status`);
        if (resp.ok) {
            const data = await resp.json();
            const info = document.createElement('span');
            info.className = 'self-mod-branch-info';
            info.textContent = ` | Branch: ${data.branch} @ ${(data.head_sha || '').slice(0, 8)} (+${data.commits_ahead_of_main} ahead of main)`;
            banner.appendChild(info);
        }
    } catch (_) {}

    // Insert before the kanban board
    const board = document.querySelector('.kanban-board') || document.body;
    board.parentNode.insertBefore(banner, board);
}

async function _loadSelfModBadges() {
    if (currentProject !== '_maestro_self') return;
    try {
        const resp = await fetch(`${API_BASE}/tasks/self-mod-merge/revert-votes`);
        if (!resp.ok) return;
        const votes = await resp.json();
        if (!votes || votes.length === 0) return;

        // Attach badges to any card currently in the DOM
        document.querySelectorAll('.task-card').forEach(card => {
            if (!card.querySelector('.revert-vote-badge')) {
                const badge = document.createElement('span');
                badge.className = 'revert-vote-badge';
                badge.textContent = `⚠ ${votes.length} revert vote${votes.length !== 1 ? 's' : ''}`;
                badge.title = votes.map(v => `${v.task_id}: ${v.reason}`).join('\n');
                card.appendChild(badge);
            }
        });
    } catch (_) {}
}

// ---------------------------------------------------------------------------
// Training Status Modal
// ---------------------------------------------------------------------------

async function openTrainingStatusModal() {
    document.getElementById('training-status-modal').classList.add('active');
    await loadTrainingStatus();
}

function closeTrainingStatusModal() {
    document.getElementById('training-status-modal').classList.remove('active');
}

async function loadTrainingStatus() {
    const el = document.getElementById('training-status-content');
    el.innerHTML = '<div style="color:#6c757d;font-size:0.85rem">Loading...</div>';
    try {
        const resp = await fetch(`${API_BASE}/training/status`);
        if (!resp.ok) throw new Error(resp.statusText);
        const s = await resp.json();
        const pct = s.threshold > 0 ? Math.min(100, Math.round(s.qualified_unexported / s.threshold * 100)) : 0;
        const lastExport = s.last_export_at
            ? new Date(s.last_export_at).toLocaleString()
            : 'Never';
        const filesHtml = (s.exports || []).map(f =>
            `<tr>
              <td style="font-family:monospace;font-size:0.75rem;word-break:break-all">${f.path}</td>
              <td style="text-align:right;white-space:nowrap">${f.count.toLocaleString()}</td>
              <td style="text-align:right;white-space:nowrap">${f.size_mb.toFixed(1)} MB</td>
            </tr>`
        ).join('');
        el.innerHTML = `
            <div style="margin-bottom:1rem">
                <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:4px">
                    <span style="font-weight:600">Qualified sessions ready to export</span>
                    <span style="font-size:0.85rem;color:#6c757d">${s.qualified_unexported.toLocaleString()} / ${s.threshold.toLocaleString()} threshold</span>
                </div>
                <div style="background:#e9ecef;border-radius:4px;height:10px;overflow:hidden">
                    <div style="background:#0d6efd;height:100%;width:${pct}%;transition:width 0.3s"></div>
                </div>
            </div>
            <div style="font-size:0.85rem;color:#495057;margin-bottom:1rem">
                <strong>Last export:</strong> ${lastExport}
                ${s.last_export_count ? `&nbsp;&mdash;&nbsp;${s.last_export_count.toLocaleString()} sessions` : ''}
            </div>
            ${filesHtml ? `
            <table style="width:100%;border-collapse:collapse;font-size:0.8rem">
                <thead>
                    <tr style="border-bottom:1px solid #dee2e6;color:#6c757d">
                        <th style="text-align:left;padding:4px 6px">File</th>
                        <th style="text-align:right;padding:4px 6px">Sessions</th>
                        <th style="text-align:right;padding:4px 6px">Size</th>
                    </tr>
                </thead>
                <tbody>${filesHtml}</tbody>
            </table>` : '<div style="color:#6c757d;font-size:0.85rem">No export files yet.</div>'}
        `;
    } catch (e) {
        el.innerHTML = `<div style="color:#dc3545">Error loading training status: ${e.message}</div>`;
    }
}

async function triggerTrainingExport() {
    const btn = document.getElementById('training-export-btn');
    const msg = document.getElementById('training-export-msg');
    btn.disabled = true;
    msg.textContent = 'Exporting...';
    try {
        const resp = await fetch(`${API_BASE}/training/export`, { method: 'POST' });
        const data = await resp.json();
        if (data.count === 0) {
            msg.textContent = 'No qualifying sessions to export.';
        } else {
            msg.textContent = `Exported ${data.count} sessions.`;
        }
        await loadTrainingStatus();
    } catch (e) {
        msg.textContent = `Export failed: ${e.message}`;
    } finally {
        btn.disabled = false;
    }
}

// ---------------------------------------------------------------------------
// Live Stream Peek Drawer
// ---------------------------------------------------------------------------
// A slide-in panel that subscribes to /api/tasks/{id}/live and renders tokens
// as they arrive from the LLM.  Only one drawer is open at a time.

let _peekTaskId = null;       // task id currently peeked
let _peekSource = null;       // active EventSource
let _peekSeq = 0;             // last seq received (for reconnect)
let _peekAgentName = '';
let _peekAutoScroll = true;
let _peekLastToolSep = null;  // lp-tool-sep waiting for tool_result events
let _peekToolResultIdx = 0;   // which item in _peekLastToolSep gets the next result

function openLivePeek(taskId) {
    const task = taskData[taskId];
    const title = task ? escapeHtml(task.title || taskId) : escapeHtml(taskId);

    // Create drawer if it doesn't exist
    let drawer = document.getElementById('live-peek-drawer');
    if (!drawer) {
        drawer = document.createElement('div');
        drawer.id = 'live-peek-drawer';
        drawer.className = 'live-peek-drawer';
        drawer.innerHTML = `
            <div class="lp-header">
                <span class="lp-pulse" id="lp-pulse"></span>
                <span class="lp-title" id="lp-title">Live Stream</span>
                <span class="lp-agent" id="lp-agent"></span>
                <div class="lp-header-btns">
                    <button class="lp-btn" title="Clear output" onclick="_peekClear()">&#10005; Clear</button>
                    <button class="lp-btn" title="Close" onclick="closeLivePeek()">&#10005;</button>
                </div>
            </div>
            <div class="lp-output" id="lp-output">
                <div class="lp-waiting" id="lp-waiting">Connecting to live stream…</div>
            </div>
            <div class="lp-footer">
                <label class="lp-autoscroll-label">
                    <input type="checkbox" id="lp-autoscroll" checked onchange="_peekAutoScrollToggle(this)">
                    Auto-scroll
                </label>
                <span class="lp-seq" id="lp-seq"></span>
            </div>
        `;
        document.body.appendChild(drawer);

        // Pause auto-scroll on manual scroll up
        document.getElementById('lp-output').addEventListener('scroll', _peekCheckScroll);
    }

    // Reset tool result tracking
    _peekLastToolSep = null;
    _peekToolResultIdx = 0;

    // Switch task if already open
    if (_peekTaskId !== taskId) {
        _peekCloseSse();
        _peekTaskId = taskId;
        _peekSeq = 0;
        _peekAutoScroll = true;
        document.getElementById('lp-autoscroll').checked = true;
        document.getElementById('lp-output').innerHTML =
            `<div class="lp-waiting" id="lp-waiting">Connecting to live stream…</div>`;
    }

    document.getElementById('lp-title').textContent = title;
    drawer.classList.add('open');
    _peekConnect();
}

function closeLivePeek() {
    _peekCloseSse();
    const drawer = document.getElementById('live-peek-drawer');
    if (drawer) drawer.classList.remove('open');
    _peekTaskId = null;
}

function _peekClear() {
    const out = document.getElementById('lp-output');
    if (out) out.innerHTML = `<div class="lp-waiting">Stream cleared.</div>`;
    _peekLastToolSep = null;
    _peekToolResultIdx = 0;
}

function _peekAutoScrollToggle(cb) {
    _peekAutoScroll = cb.checked;
}

function _peekCheckScroll() {
    const out = document.getElementById('lp-output');
    if (!out) return;
    const atBottom = out.scrollHeight - out.scrollTop - out.clientHeight < 40;
    _peekAutoScroll = atBottom;
    const cb = document.getElementById('lp-autoscroll');
    if (cb) cb.checked = atBottom;
}

function _peekCloseSse() {
    if (_peekSource) {
        _peekSource.close();
        _peekSource = null;
    }
}

function _peekConnect() {
    if (!_peekTaskId) return;
    _peekCloseSse();

    const url = `${API_BASE}/tasks/${_peekTaskId}/live?since=${_peekSeq}`;
    const es = new EventSource(url);
    _peekSource = es;

    const pulse = document.getElementById('lp-pulse');

    es.addEventListener('token', e => {
        const data = JSON.parse(e.data);
        _peekSeq = data.seq;

        // Update agent label if changed
        if (data.agent_name && data.agent_name !== _peekAgentName) {
            _peekAgentName = data.agent_name;
            const agentEl = document.getElementById('lp-agent');
            if (agentEl) agentEl.textContent = data.agent_name;
        }
        if (pulse) pulse.classList.add('active');

        _peekAppendToken(data);
        _peekScrollDown();
        _peekUpdateSeq();
    });

    es.addEventListener('status', e => {
        let data;
        try { data = JSON.parse(e.data); } catch { return; }
        if (pulse) pulse.classList.toggle('active', data.active);
        if (!data.active) {
            const agentEl = document.getElementById('lp-agent');
            if (agentEl && _peekAgentName) agentEl.textContent = _peekAgentName + ' (idle)';
        }
    });

    es.addEventListener('done', () => {
        if (pulse) pulse.classList.remove('active');
        _peekAppendSeparator('Stream complete');
        _peekScrollDown();
        _peekCloseSse();
    });

    es.onerror = () => {
        // Browser will auto-reconnect on EventSource error; just update indicator
        if (pulse) pulse.classList.remove('active');
    };
}

function _peekAppendToken(data) {
    const out = document.getElementById('lp-output');
    if (!out) return;

    // Remove placeholder
    const waiting = document.getElementById('lp-waiting');
    if (waiting) waiting.remove();

    if (data.turn_type === 'tool_invoked') {
        let tools;
        try { tools = JSON.parse(data.text); } catch { tools = [{name: String(data.text), args: ''}]; }
        // Normalise old format (array of strings) to new format (array of {name, args})
        if (tools.length > 0 && typeof tools[0] === 'string') {
            tools = tools.map(n => ({name: n, args: ''}));
        }
        const sep = document.createElement('div');
        sep.className = 'lp-tool-sep';
        tools.forEach(tool => {
            const item = document.createElement('div');
            item.className = 'lp-tool-item';
            let argsObj = null;
            try { if (tool.args) argsObj = JSON.parse(tool.args); } catch {}
            const hasArgs = argsObj && Object.keys(argsObj).length > 0;
            // One-line preview: first 2 key=val pairs
            let previewStr = '';
            if (hasArgs) {
                const entries = Object.entries(argsObj);
                const parts = entries.slice(0, 2).map(([k, v]) => {
                    const vs = typeof v === 'string'
                        ? '"' + (v.length > 42 ? v.slice(0, 42) + '…' : v) + '"'
                        : String(v).slice(0, 42);
                    return k + '=' + vs;
                });
                previewStr = '(' + parts.join(', ') + (entries.length > 2 ? ', …' : '') + ')';
            }
            const toggleEl = document.createElement('span');
            toggleEl.className = 'lp-tool-toggle';
            toggleEl.textContent = '▶';
            toggleEl.onclick = () => _lpToggleTool(toggleEl);
            const nameEl = document.createElement('span');
            nameEl.textContent = '➤ ' + tool.name;
            const previewEl = document.createElement('span');
            previewEl.className = 'lp-tool-preview';
            previewEl.textContent = previewStr;
            item.appendChild(toggleEl);
            item.appendChild(nameEl);
            item.appendChild(previewEl);
            if (hasArgs) {
                const argsEl = document.createElement('div');
                argsEl.className = 'lp-tool-args';
                const pre = document.createElement('pre');
                pre.textContent = JSON.stringify(argsObj, null, 2);
                argsEl.appendChild(pre);
                item.appendChild(argsEl);
            }
            sep.appendChild(item);
        });
        out.appendChild(sep);
        _peekLastToolSep = sep;
        _peekToolResultIdx = 0;
        return;
    }

    if (data.turn_type === 'tool_result') {
        let parsed;
        try { parsed = JSON.parse(data.text); } catch { return; }
        const sep = _peekLastToolSep;
        if (!sep) return;
        const items = sep.querySelectorAll('.lp-tool-item');
        const item = items[_peekToolResultIdx];
        _peekToolResultIdx++;
        if (!item) return;
        const resultEl = document.createElement('div');
        resultEl.className = 'lp-tool-result';
        const label = document.createElement('div');
        label.className = 'lp-tool-result-label';
        label.textContent = '↳ result';
        const pre = document.createElement('pre');
        pre.textContent = parsed.result || '';
        resultEl.appendChild(label);
        resultEl.appendChild(pre);
        item.appendChild(resultEl);
        // If item is already expanded, show result immediately
        const argsEl = item.querySelector('.lp-tool-args');
        if (argsEl && argsEl.classList.contains('open')) {
            resultEl.classList.add('open');
        }
        _peekScrollDown();
        return;
    }

    if (data.turn_type === 'turn_end') {
        const sep = document.createElement('div');
        sep.className = 'lp-turn-sep';
        out.appendChild(sep);
        return;
    }

    // content token — append to last text node or create new
    let last = out.lastElementChild;
    if (!last || !last.classList.contains('lp-text-block')) {
        last = document.createElement('div');
        last.className = 'lp-text-block';
        out.appendChild(last);
    }
    // Preserve newlines; escape HTML in the raw token
    const escaped = data.text
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;');
    last.innerHTML += escaped.replace(/\n/g, '<br>');
}

function _lpToggleTool(toggleEl) {
    const item = toggleEl.closest('.lp-tool-item');
    const argsEl = item && item.querySelector('.lp-tool-args');
    const resultEl = item && item.querySelector('.lp-tool-result');
    if (!argsEl && !resultEl) return;
    const wasOpen = (argsEl && argsEl.classList.contains('open'))
                 || (resultEl && resultEl.classList.contains('open'));
    const nowOpen = !wasOpen;
    if (argsEl) argsEl.classList.toggle('open', nowOpen);
    if (resultEl) resultEl.classList.toggle('open', nowOpen);
    toggleEl.textContent = nowOpen ? '▼' : '▶';
}

function _peekAppendSeparator(label) {
    const out = document.getElementById('lp-output');
    if (!out) return;
    const sep = document.createElement('div');
    sep.className = 'lp-done-sep';
    sep.textContent = label;
    out.appendChild(sep);
}

function _peekScrollDown() {
    if (!_peekAutoScroll) return;
    const out = document.getElementById('lp-output');
    if (out) out.scrollTop = out.scrollHeight;
}

function _peekUpdateSeq() {
    const el = document.getElementById('lp-seq');
    if (el) el.textContent = `seq ${_peekSeq}`;
}

