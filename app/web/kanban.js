// Kanban Board JavaScript - Extracted from kanban.html
// Includes drag-and-drop functionality for reordering within columns

// API Configuration
const API_BASE = '/api';

// WIP Limits configuration - maximum cards allowed per column
const WIP_LIMITS = {
    'architecture': 10,
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

// Global LLM and Budget caches
let allLlms = [];
let allBudgets = [];

// Transition status cache: taskId -> { status, data, rejectionCount }
let transitionCache = {};

// Active polling timers: taskId -> intervalId
let transitionPollers = {};

// Big Idea zoom state
let currentBigIdeaFilter = null;  // task ID or null for root view
let breadcrumbStack = [];         // array of {id, title} for nested zoom

// Descendant index: parentId -> [childId, ...]
let childIndex = {};
// Full descendant index: taskId -> [all descendant IDs recursively]
let descendantIndex = {};

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
    ].join('|');
}

// Load global LLMs and Budgets
async function loadLlmsAndBudgets() {
    try {
        const [llmRes, budgetRes] = await Promise.all([
            fetch(`${API_BASE}/llms`),
            fetch(`${API_BASE}/budgets`)
        ]);
        if (llmRes.ok) allLlms = await llmRes.json();
        if (budgetRes.ok) allBudgets = await budgetRes.json();
    } catch (e) {
        console.error('Failed to load LLMs/Budgets:', e);
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
    'planning': 'indev',
    'indev': 'conceptual_review',
    'conceptual_review': 'optimization',
    'optimization': 'security',
    'security': 'full_review',
    'full_review': 'completed'
};

function isValidDropTarget(sourceContainer, targetContainer) {
    const sourceCol = sourceContainer.id.replace('tasks-', '');
    const targetCol = targetContainer.id.replace('tasks-', '');
    // Always allow reorder within the same column
    if (sourceCol === targetCol) return true;
    // Allow moving to the next column only if the task can advance
    if (COLUMN_NEXT[sourceCol] === targetCol && canTaskAdvance(draggedTaskId)) return true;
    return false;
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
    const columns = ['architecture', 'idea', 'planning', 'indev', 'conceptual_review', 'optimization', 'security', 'full_review', 'completed'];

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
    const filteredTasks = Object.values(taskData).filter(t => {
        if (!t || !t.type) return false;
        if (t.type === 'cancelled') return false;
        if (currentBigIdeaFilter) {
            // Show the Big Idea itself + its descendants
            if (t.id === currentBigIdeaFilter) return true;
            const descendants = descendantIndex[currentBigIdeaFilter] || [];
            return descendants.includes(t.id);
        }
        return true;
    });

    const tasksByType = {};
    filteredTasks.forEach(task => {
        const renderCol = task.type === 'subdividing' ? 'idea' : task.type;
        if (!tasksByType[renderCol]) tasksByType[renderCol] = [];
        tasksByType[renderCol].push(task);
    });

    // Update breadcrumb bar
    updateBreadcrumbBar();

    columns.forEach(colType => {
        const tasks = (tasksByType[colType] || []).sort((a, b) => (a.position ?? 0) - (b.position ?? 0));
        tasks.forEach(task => {
            const container = document.getElementById(`tasks-${colType}`);
            if (container) {
                const card = createTaskCard(task.id, task.title, task.tags, task.owner, task.type);
                container.appendChild(card);
                cardCache[task.id] = card;
                fingerprintCache[task.id] = taskFingerprint(task);
            }
        });
    });

    console.log(`Rendered ${Object.values(taskData).filter(t => t && t.type).length} task cards from database`);

    // Update task counts
    updateTaskCounts();
}

function updateTaskCounts() {
    const columns = ['architecture', 'idea', 'planning', 'indev', 'conceptual_review', 'optimization', 'security', 'full_review', 'completed'];

    columns.forEach(columnType => {
        const container = document.getElementById(`tasks-${columnType}`);
        const countElement = document.getElementById(`count-${columnType}`);

        if (container && countElement) {
            const count = container.querySelectorAll('.task-card').length;
            countElement.textContent = count;
        }
    });
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

    // 2. Create new cards and rebuild changed ones
    for (const task of newTasks) {
        if (task.type === 'cancelled') continue;
        const renderCol = task.type === 'subdividing' ? 'idea' : task.type;
        const newFp = taskFingerprint(task);

        if (!cardCache[task.id]) {
            // New task — create and insert
            const card = createTaskCard(task.id, task.title, task.tags, task.owner, task.type);
            cardCache[task.id] = card;
            fingerprintCache[task.id] = newFp;
            columnsToSort.add(renderCol);
        } else if (fingerprintCache[task.id] !== newFp) {
            // Changed — rebuild the card element in-place
            const old = cardCache[task.id];
            const oldTask = taskData[task.id];
            if (oldTask) {
                columnsToSort.add(oldTask.type === 'subdividing' ? 'idea' : oldTask.type);
            }
            const newCard = createTaskCard(task.id, task.title, task.tags, task.owner, task.type);
            if (old.parentNode) old.parentNode.replaceChild(newCard, old);
            cardCache[task.id] = newCard;
            fingerprintCache[task.id] = newFp;
            columnsToSort.add(renderCol);
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

    updateBreadcrumbBar();
    updateTaskCounts();
}

// Rebuild a single card — used when only transition/processing state changes
// (those aren't in the fingerprint since they're client-side state).
function refreshCard(taskId) {
    const task = taskData[taskId];
    if (!task) return;
    const newCard = createTaskCard(task.id, task.title, task.tags, task.owner, task.type);
    const old = cardCache[taskId];
    if (old && old.parentNode) {
        old.parentNode.replaceChild(newCard, old);
    } else {
        const renderCol = task.type === 'subdividing' ? 'idea' : task.type;
        const container = document.getElementById(`tasks-${renderCol}`);
        if (container) container.appendChild(newCard);
    }
    cardCache[taskId] = newCard;
    fingerprintCache[taskId] = taskFingerprint(task);
}

// ============================================
// DOM Initialization
// ============================================

let autoRefreshInterval = null;

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
    }, 5000);
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

    await loadTasksFromDatabase();
    await loadTransitionStatuses();
    renderTasksFromDatabase();
}

// Load projects from the API and render the sidebar tabs
async function loadProjects() {
    try {
        const resp = await fetch(`${API_BASE}/projects`);
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const projects = await resp.json();

        const container = document.getElementById('project-tabs-container');
        container.innerHTML = '';

        projects.forEach(p => {
            container.appendChild(_buildProjectTab(p.name, p.path, p.description));
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

function _buildProjectTab(name, path, description) {
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
        openEditProjectModal(name, path || '', description || '');
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
        if (e.target === this) closeModal();
    });

    document.getElementById('history-modal').addEventListener('click', function(e) {
        if (e.target === this) closeHistoryModal();
    });

    document.getElementById('new-project-modal').addEventListener('click', function(e) {
        if (e.target === this) closeNewProjectModal();
    });

    document.getElementById('new-project-name').addEventListener('keydown', function(e) {
        if (e.key === 'Enter') saveNewProject();
        if (e.key === 'Escape') closeNewProjectModal();
    });

    document.getElementById('edit-project-modal').addEventListener('click', function(e) {
        if (e.target === this) closeEditProjectModal();
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

    // Default LLM/Budget selection (first option = default)
    const defaultLlmId = allLlms.length > 0 ? allLlms[0].id : null;
    const defaultBudgetId = allBudgets.length > 0 ? allBudgets[0].id : null;
    populateLlmSelect(defaultLlmId);
    populateBudgetSelect(defaultBudgetId);

    document.getElementById('task-modal').classList.add('active');
}

function showArchContentFields(targetStatus) {
    const contentFields = document.getElementById('modal-content-fields');
    const isArchitecture = targetStatus === 'architecture';

    if (isArchitecture) {
        contentFields.style.display = 'block';
        if (currentProject === 'TheMaestro') {
            document.getElementById('architecture-content-dags').style.display = 'block';
            document.getElementById('architecture-content-config').style.display = 'block';
            document.getElementById('architecture-content-repl').style.display = 'block';
            document.getElementById('architecture-content-tests').style.display = 'block';
            document.getElementById('architecture-content-frontend').style.display = 'none';
            document.getElementById('architecture-content-backend').style.display = 'none';
            document.getElementById('architecture-content-database').style.display = 'none';
            document.getElementById('architecture-content-style').style.display = 'none';
        } else {
            document.getElementById('architecture-content-frontend').style.display = 'block';
            document.getElementById('architecture-content-backend').style.display = 'block';
            document.getElementById('architecture-content-database').style.display = 'block';
            document.getElementById('architecture-content-style').style.display = 'block';
            document.getElementById('architecture-content-dags').style.display = 'none';
            document.getElementById('architecture-content-config').style.display = 'none';
            document.getElementById('architecture-content-repl').style.display = 'none';
            document.getElementById('architecture-content-tests').style.display = 'none';
        }
    } else {
        contentFields.style.display = 'none';
    }
}

function closeModal() {
    document.getElementById('task-modal').classList.remove('active');
    currentTaskId = null;
    currentTargetStatus = null;
}

function openNewProjectModal() {
    document.getElementById('new-project-name').value = '';
    document.getElementById('new-project-path').value = '';
    document.getElementById('new-project-description').value = '';
    document.getElementById('new-project-error').style.display = 'none';
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
            body: JSON.stringify({ name, path, description }),
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

function openEditProjectModal(name, path, description) {
    document.getElementById('edit-project-original-name').value = name;
    document.getElementById('edit-project-modal-title').textContent = `Edit: ${name}`;
    document.getElementById('edit-project-name-display').textContent = name;
    document.getElementById('edit-project-path').value = path;
    document.getElementById('edit-project-description').value = description;
    document.getElementById('edit-project-error').style.display = 'none';
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
    const errEl = document.getElementById('edit-project-error');

    try {
        const resp = await fetch(`${API_BASE}/projects/${encodeURIComponent(name)}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path, description }),
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
    if (!confirm(`Delete project "${name}"? This does not delete its tasks.`)) return;

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
        alert('Task title is required!');
        return;
    }

    const tags = tagsInput.split(',').map(t => t.trim()).filter(t => t);

    // Build content object for architecture tasks
    const content = currentTargetStatus === 'architecture' ? {
        frontend: document.getElementById('arch-content-frontend').value,
        backend: document.getElementById('arch-content-backend').value,
        database: document.getElementById('arch-content-database').value,
        style: document.getElementById('arch-content-style').value,
        dags: document.getElementById('arch-content-dags').value,
        config: document.getElementById('arch-content-config').value,
        repl: document.getElementById('arch-content-repl').value,
        tests: document.getElementById('arch-content-tests').value
    } : null;

    const llmVal = document.getElementById('task-llm-select').value;
    const budgetVal = document.getElementById('task-budget-select').value;
    const llm_id = llmVal ? parseInt(llmVal) : null;
    const budget_id = budgetVal ? parseInt(budgetVal) : null;

    if (currentTaskId) {
        // Update existing task via PUT request
        const taskDataPayload = {
            title,
            description,
            owner,
            tags,
            llm_id,
            budget_id,
            ...(content && { content })
        };

        const response = await fetch(`${API_BASE}/tasks/${currentTaskId}`, {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(taskDataPayload)
        });

        if (!response.ok) {
            alert('Failed to update task');
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
            owner,
            tags,
            llm_id,
            budget_id,
            project: currentProject,
            ...(content && { content })
        };

        const response = await fetch(`${API_BASE}/tasks`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(newTaskData)
        });

        if (!response.ok) {
            alert('Failed to create task');
            return;
        }

        const newTask = await response.json();
        taskData[newTask.id] = newTask;
        allTasks.push(newTask);
        console.log(`New task created: ${newTask.id}`);
    }

    closeModal();
    renderTasksFromDatabase();
}

function canAddTaskToColumn(status) {
    if (!status || status === 'architecture') return true;
    const check = checkWipLimit(status);
    if (!check.allowed) {
        alert(`WIP Limit Reached! Column '${status.toUpperCase()}' has ${check.current} tasks (limit: ${check.limit}).`);
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

    // Push current state onto breadcrumb stack
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

function updateBreadcrumbBar() {
    const bar = document.getElementById('breadcrumb-bar');
    const trail = document.getElementById('breadcrumb-trail');
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
        const resp = await fetch(`${API_BASE}/tasks/${taskId}/children`);
        if (!resp.ok) return;
        const children = await resp.json();

        const task = taskData[taskId] || {};
        const title = task.title || taskId;

        let html = `<h3 style="margin-bottom:1rem">Children of: ${title}</h3>`;
        if (children.length === 0) {
            html += '<p style="color:#6c757d">No children found.</p>';
        } else {
            children.forEach(c => {
                const statusColor = c.type === 'cancelled' ? '#dc3545' :
                                    c.type === 'completed' ? '#198754' :
                                    c.type === 'planning' ? '#ffc107' : '#0d6efd';
                html += `
                    <div style="border:1px solid #dee2e6;border-radius:6px;padding:0.75rem;margin-bottom:0.5rem;border-left:4px solid ${statusColor}">
                        <strong>${c.title}</strong>
                        <span style="float:right;font-size:0.75rem;text-transform:uppercase;color:${statusColor};font-weight:600">${c.type}</span>
                        <div style="font-size:0.85rem;color:#6c757d;margin-top:0.25rem">${c.description || ''}</div>
                        ${c.subdivision_generation > 0 ? `<span class="subdivision-badge gen" style="margin-top:0.35rem;display:inline-block">Gen ${c.subdivision_generation}</span>` : ''}
                        ${c.is_big_idea ? '<span class="big-idea-badge" style="margin-left:0.35rem">Big Idea</span>' : ''}
                        ${c.interface_contracts ? buildContractPills(c.interface_contracts) : ''}
                    </div>
                `;
            });
        }

        // Also show subdivision records
        const recResp = await fetch(`${API_BASE}/tasks/${taskId}/subdivision-records`);
        if (recResp.ok) {
            const records = await recResp.json();
            if (records.length > 0) {
                html += '<h4 style="margin-top:1rem;margin-bottom:0.5rem">Subdivision History</h4>';
                records.forEach(r => {
                    const statusBg = r.status === 'active' ? '#d1e7dd' :
                                     r.status === 'superseded' ? '#fff3cd' : '#f8d7da';
                    html += `
                        <div style="background:${statusBg};border-radius:4px;padding:0.5rem;margin-bottom:0.35rem;font-size:0.85rem">
                            Attempt #${r.attempt_number} (gen ${r.generation}) — <strong>${r.status}</strong>
                            — ${(r.child_task_ids || []).length} children
                            — ${r.prompt_tokens || 0} prompt / ${r.completion_tokens || 0} completion tokens
                        </div>
                    `;
                });
            }
        }

        document.getElementById('transition-modal-title').textContent = 'Subdivision Details';
        document.getElementById('transition-modal-body').innerHTML = html;
        document.getElementById('transition-modal').classList.add('active');
    } catch (err) {
        console.error('Error viewing children:', err);
    }
}

async function viewResearchJobs(taskId) {
    try {
        const resp = await fetch(`${API_BASE}/tasks/${taskId}/research-jobs`);
        if (!resp.ok) return;
        const jobs = await resp.json();

        const task = taskData[taskId] || {};
        const title = task.title || taskId;

        let html = `<h3 style="margin-bottom:1rem">Research Jobs: ${title}</h3>`;
        if (jobs.length === 0) {
            html += '<p style="color:#6c757d">No research jobs for this task.</p>';
        } else {
            jobs.forEach(j => {
                const statusColor = j.status === 'completed' ? '#198754' :
                                    j.status === 'failed'    ? '#dc3545' :
                                    j.status === 'cancelled' ? '#fd7e14' : '#6c757d';
                const findings = j.findings
                    ? (j.findings.length > 300 ? j.findings.slice(0, 300) + '…' : j.findings)
                    : '<em style="color:#6c757d">No findings yet.</em>';
                html += `
                    <div style="border:1px solid #dee2e6;border-radius:6px;padding:0.75rem;margin-bottom:0.5rem;border-left:4px solid ${statusColor}">
                        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:0.35rem">
                            <span style="font-size:0.75rem;text-transform:uppercase;color:${statusColor};font-weight:600">${j.status}</span>
                            <span class="transition-timestamp">${j.created_at || ''}</span>
                        </div>
                        <div style="font-weight:600;margin-bottom:0.25rem">${j.question || ''}</div>
                        <div style="font-size:0.85rem;color:#495057;margin-bottom:0.35rem">${findings}</div>
                        <div style="font-size:0.75rem;color:#6c757d">
                            Lives used: ${j.lives_used ?? '—'} &nbsp;|&nbsp;
                            Tokens: ${j.prompt_tokens ?? 0} prompt / ${j.completion_tokens ?? 0} completion
                            ${j.completed_at ? `&nbsp;|&nbsp; Completed: ${j.completed_at}` : ''}
                        </div>
                    </div>
                `;
            });
        }

        document.getElementById('transition-modal-title').textContent = 'Research Jobs';
        document.getElementById('transition-modal-body').innerHTML = html;
        document.getElementById('transition-modal').classList.add('active');
    } catch (err) {
        console.error('Error viewing research jobs:', err);
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
    const rejBadge = rejectionCount > 0 ? `<span class="rejection-badge" title="${rejectionCount} rejection(s)">${rejectionCount}x</span>` : '';
    const processingSpinner = transitionPollers[id] ? '<span class="processing-indicator">\u25E0</span>' : '';

    // Subdivision badges
    const taskObj = taskData[id] || {};
    const parentId = taskObj.parent_task_id;
    const generation = taskObj.subdivision_generation || 0;
    const isSubdividing = status === 'subdividing';

    let subdivBadge = '';
    if (isSubdividing) {
        subdivBadge = '<span class="subdivision-badge subdividing" title="Subdividing...">Subdividing</span>';
        card.classList.add('subdividing');
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

    card.innerHTML = `
        ${parentLink}
        <div class="task-title"${isBigIdea ? ` onclick="zoomIntoBigIdea('${id}')" style="cursor:pointer"` : ''}>${title}${rejBadge}${processingSpinner}${subdivBadge}${bigIdeaBadge}${contractIndicator}</div>
        <div class="task-meta">
            ${tagsHtml}
            ${ownerHtml}
        </div>
        ${prereqHtml}
        <div class="task-actions">
            <button class="action-btn" onclick="editTask('${id}')">Edit</button>
            <button class="action-btn action-btn-danger" onclick="deleteTask('${id}')">Delete</button>
        </div>
    `;

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
        // Subdividing — show children button instead of advance
        const actionsDiv = card.querySelector('.task-actions');
        actionsDiv.innerHTML = `
            <button class="action-btn" onclick="viewChildren('${id}')">View Children</button>
            <button class="action-btn action-btn-danger" onclick="deleteTask('${id}')">Delete</button>
        `;
    } else if (status === 'idea') {
        const actionsDiv = card.querySelector('.task-actions');
        const advanceBtn = document.createElement('button');
        advanceBtn.className = 'action-btn action-btn-advance';
        if (transitionPollers[id]) {
            advanceBtn.textContent = 'Processing...';
            advanceBtn.disabled = true;
        } else {
            advanceBtn.textContent = rejectionCount > 0 ? 'Retry Advance' : 'Advance to Planning';
        }
        advanceBtn.onclick = (e) => {
            e.stopPropagation();
            advanceTask(id);
        };
        actionsDiv.appendChild(advanceBtn);

        // If this task has children (was previously subdivided and reverted), show children button
        if (taskObj._hasChildren) {
            const childBtn = document.createElement('button');
            childBtn.className = 'action-btn';
            childBtn.textContent = 'View Children';
            childBtn.onclick = (e) => { e.stopPropagation(); viewChildren(id); };
            actionsDiv.appendChild(childBtn);
        }
    } else if (status === 'planning') {
        if (ready) {
            const moveBtn = card.querySelector('.task-actions');
            moveBtn.innerHTML += `<button class="action-btn" onclick="moveTask('${id}', 'indev')">Move to IN DEVELOPMENT</button>`;
        }
    } else if (status === 'indev') {
        if (ready) {
            const moveBtn = card.querySelector('.task-actions');
            moveBtn.innerHTML += `<button class="action-btn" onclick="moveTask('${id}', 'conceptual_review')">Move to CONCEPTUAL REVIEW</button>`;
        }
    } else if (status === 'conceptual_review') {
        if (ready) {
            const moveBtn = card.querySelector('.task-actions');
            moveBtn.innerHTML += `<button class="action-btn" onclick="moveTask('${id}', 'optimization')">Move to OPTIMIZATION</button>`;
        }
    } else if (status === 'optimization') {
        if (ready) {
            const moveBtn = card.querySelector('.task-actions');
            moveBtn.innerHTML += `<button class="action-btn" onclick="moveTask('${id}', 'security')">Move to SECURITY</button>`;
        }
    } else if (status === 'security') {
        if (ready) {
            const moveBtn = card.querySelector('.task-actions');
            moveBtn.innerHTML += `<button class="action-btn" onclick="moveTask('${id}', 'full_review')">Move to FINAL REVIEW</button>`;
        }
    } else if (status === 'full_review') {
        if (ready) {
            const moveBtn = card.querySelector('.task-actions');
            moveBtn.innerHTML += `<button class="action-btn" onclick="moveTask('${id}', 'completed')">Move to COMPLETED</button>`;
        }
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

    card.addEventListener('dragstart', handleDragStart);
    card.addEventListener('dragend', handleDragEnd);

    return card;
}

// ============================================
// Advance Task (Idea -> Planning pipeline)
// ============================================

async function advanceTask(taskId) {
    try {
        const response = await fetch(`${API_BASE}/tasks/${taskId}/advance`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' }
        });
        if (!response.ok) {
            const err = await response.json();
            alert(`Advance failed: ${err.detail || 'Unknown error'}`);
            return;
        }
        const result = await response.json();
        console.log('Advance initiated:', result);

        // Mark card as processing immediately
        setCardProcessing(taskId, true);

        // Start polling for transition status
        startTransitionPolling(taskId);
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

function startTransitionPolling(taskId) {
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

            // Pipeline completed — stop polling
            clearInterval(transitionPollers[taskId]);
            delete transitionPollers[taskId];

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
        alert('No transition data available for this task.');
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
}

// ============================================
// Task Deletion
// ============================================

async function deleteTask(taskId) {
    if (!confirm('Delete this task? This cannot be undone.')) return;

    const response = await fetch(`${API_BASE}/tasks/${taskId}`, { method: 'DELETE' });
    if (!response.ok) {
        alert('Failed to delete task');
        return;
    }

    delete taskData[taskId];
    allTasks = allTasks.filter(t => t.id !== taskId);
    const card = document.querySelector(`.task-card[data-id="${taskId}"]`);
    if (card) {
        const container = card.closest('.tasks-container');
        card.remove();
        if (container) updateTaskCount(container.id.replace('tasks-', ''));
    }
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
        alert('Task not found');
        return;
    }

    if (task.immutable) {
        console.log('Cannot move immutable architecture task');
        return;
    }

    if (!canTaskAdvance(taskId)) {
        alert('Task cannot advance: it needs a description, LLM, and budget assigned.');
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
        alert('Failed to move task: ' + errorText);
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
        alert('Task title is required!');
        return;
    }

    const tags = tagsInput.split(',').map(t => t.trim()).filter(t => t);

    // Build content object for architecture tasks
    const content = currentTargetStatus === 'architecture' ? {
        frontend: document.getElementById('arch-content-frontend').value,
        backend: document.getElementById('arch-content-backend').value,
        database: document.getElementById('arch-content-database').value,
        style: document.getElementById('arch-content-style').value,
        dags: document.getElementById('arch-content-dags').value,
        config: document.getElementById('arch-content-config').value,
        repl: document.getElementById('arch-content-repl').value,
        tests: document.getElementById('arch-content-tests').value
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
        alert('Failed to update task');
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
    currentTargetStatus = task.type;

    document.getElementById('modal-title').textContent = `Edit Architecture: ${task.title}`;
    document.getElementById('task-title').value = task.title;

    if (task.content) {
        const content = task.content;
        if (content.frontend) document.getElementById('arch-content-frontend').value = content.frontend;
        if (content.backend) document.getElementById('arch-content-backend').value = content.backend;
        if (content.database) document.getElementById('arch-content-database').value = content.database;
        if (content.style) document.getElementById('arch-content-style').value = content.style;
        if (content.dags) document.getElementById('arch-content-dags').value = content.dags;
        if (content.config) document.getElementById('arch-content-config').value = content.config;
        if (content.repl) document.getElementById('arch-content-repl').value = content.repl;
        if (content.tests) document.getElementById('arch-content-tests').value = content.tests;
    }
    showArchContentFields('architecture');
    document.getElementById('task-description').value = '';
    document.getElementById('task-tags').value = '';
    document.getElementById('task-owner').value = '';

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

function handleDragStart(e) {
    draggedElement = this;
    draggedTaskId = this.getAttribute('data-id');
    dragSourceContainer = this.closest('.tasks-container');

    // Capture original index among non-ghost siblings
    const siblings = [...dragSourceContainer.querySelectorAll('.task-card:not(.drop-ghost)')];
    draggedOriginalIndex = siblings.indexOf(draggedElement);

    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', draggedTaskId);

    // Defer adding .dragging by one tick so the browser captures the drag image
    // BEFORE opacity/pointer-events take effect.  Applying it synchronously inside
    // dragstart causes some browsers to treat the element as gone and cancel the
    // drag session immediately (symptom: dragend fires right after dragstart with
    // no dragover/drop events in between).
    const _draggedEl = this;
    setTimeout(() => {
        if (draggedElement) {
            _draggedEl.classList.add('dragging');
            // Collapse the card from layout so it doesn't skew sibling
            // midpoint calculations during dragover.  Done in JS too so
            // it works even if the CSS is cached.
            _draggedEl.style.height = '1px';
            _draggedEl.style.minHeight = '0';
            _draggedEl.style.padding = '0';
            _draggedEl.style.margin = '0';
            _draggedEl.style.border = 'none';
            _draggedEl.style.overflow = 'hidden';
            _draggedEl.style.opacity = '0';
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
    this.classList.remove('dragging');
    // Clear inline styles set during dragstart collapse
    this.style.height = '';
    this.style.minHeight = '';
    this.style.padding = '';
    this.style.margin = '';
    this.style.border = '';
    this.style.overflow = '';
    this.style.opacity = '';

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
    // Exclude both the dragging card and the ghost itself from midpoint geometry
    const cards = [...container.querySelectorAll('.task-card:not(.dragging):not(.drop-ghost)')];

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
    document.querySelectorAll('.task-card').forEach(card => {
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

    document.getElementById('llm-modal').addEventListener('click', function(e) {
        if (e.target === this) closeLlmModal();
    });
    document.getElementById('budget-modal').addEventListener('click', function(e) {
        if (e.target === this) closeBudgetModal();
    });
    document.getElementById('tools-modal').addEventListener('click', function(e) {
        if (e.target === this) closeToolsModal();
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
    return { address, port, model, parallel_sessions: parallelRaw, max_context: contextRaw, notes };
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
    if (!confirm('Delete this LLM endpoint?')) return;
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
    document.getElementById('budget-edit-placeholder').style.display = 'none';
    document.getElementById('budget-edit-form').style.display = 'block';
    document.getElementById('budget-edit-error').style.display = 'none';
    switchBudgetTab('edit');
    // Fetch usage summary
    loadBudgetSummary(id);
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

function renderBudgetList() {
    const container = document.getElementById('budget-list');
    if (allBudgets.length === 0) {
        container.innerHTML = '<p style="color:#6c757d;font-size:0.85rem">No budgets configured.</p>';
        return;
    }
    let html = '<table style="width:100%;font-size:0.85rem;border-collapse:collapse">';
    html += '<tr style="border-bottom:1px solid #dee2e6"><th style="text-align:left;padding:0.4rem">ID</th><th style="text-align:left;padding:0.4rem">Name</th><th></th></tr>';
    allBudgets.forEach(b => {
        html += `<tr style="border-bottom:1px solid #f0f0f0">
            <td style="padding:0.4rem">${b.id}</td>
            <td style="padding:0.4rem"><a href="#" onclick="editBudgetEntry(${b.id}); return false;" style="color:#0d6efd;text-decoration:none;cursor:pointer">${b.name}</a></td>
            <td style="padding:0.4rem"><button class="action-btn action-btn-danger" onclick="deleteBudgetEntry(${b.id})">Delete</button></td>
        </tr>`;
    });
    html += '</table>';
    container.innerHTML = html;
}

async function addBudget() {
    const name = document.getElementById('budget-name').value.trim();
    if (!name) { showInlineError('budget-error', 'Budget name is required.'); return; }

    const res = await fetch(`${API_BASE}/budgets`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name })
    });
    if (!res.ok) {
        const err = await res.json();
        showInlineError('budget-error', err.detail || 'Failed to create budget.');
        return;
    }
    document.getElementById('budget-name').value = '';
    await loadLlmsAndBudgets();
    renderBudgetList();
}

async function saveBudgetEdit() {
    if (!_budgetEditingId) return;
    const name = document.getElementById('budget-edit-name').value.trim();
    if (!name) { showInlineError('budget-edit-error', 'Budget name is required.'); return; }

    const res = await fetch(`${API_BASE}/budgets/${_budgetEditingId}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name })
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
    if (!confirm('Delete this budget?')) return;
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
