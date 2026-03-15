// Kanban Board JavaScript - Extracted from kanban.html
// Includes drag-and-drop functionality for reordering within columns

// API Configuration
const API_BASE = '/api';

// WIP Limits configuration - maximum cards allowed per column
const WIP_LIMITS = {
    'architecture': 10,
    'idea': 15,
    'planning': 10,
    'development': 5,
    'review': 5,
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
        return true;
    } catch (error) {
        console.error('Error loading tasks from database:', error);
        // Fallback to empty state
        return false;
    }
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
    'planning': 'development',
    'development': 'review',
    'review': 'completed'
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

    // Clear ALL existing task cards from ALL columns
    const columns = ['architecture', 'idea', 'planning', 'development', 'review', 'completed'];

    columns.forEach(columnType => {
        const container = document.getElementById(`tasks-${columnType}`);
        if (container) {
            // Remove ALL cards from this container
            while (container.firstChild) {
                container.removeChild(container.firstChild);
            }
            console.log(`Cleared ${container.id}: ${container.querySelectorAll('.task-card').length} cards removed`);
        }
    });

    // Create task cards from taskData, sorted by position within each column.
    // Group tasks by type first so the sort is per-column, not global (a global sort
    // would intermix position=0 cards from every column before any position=1 cards,
    // causing wrong render order when positions don't perfectly interleave).
    const tasksByType = {};
    Object.values(taskData).filter(t => t && t.type).forEach(task => {
        if (!tasksByType[task.type]) tasksByType[task.type] = [];
        tasksByType[task.type].push(task);
    });

    columns.forEach(colType => {
        const tasks = (tasksByType[colType] || []).sort((a, b) => (a.position ?? 0) - (b.position ?? 0));
        tasks.forEach(task => {
            const container = document.getElementById(`tasks-${task.type}`);
            if (container) {
                const card = createTaskCard(task.id, task.title, task.tags, task.owner, task.type);
                container.appendChild(card);
                console.log(`Created card for task ${task.id}: ${task.title}`);
            }
        });
    });

    console.log(`Rendered ${Object.values(taskData).filter(t => t && t.type).length} task cards from database`);

    // Re-attach drag listeners after re-render
    initializeDragAndDrop();

    // Update task counts
    updateTaskCounts();
}

function updateTaskCounts() {
    const columns = ['architecture', 'idea', 'planning', 'development', 'review', 'completed'];

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
// DOM Initialization
// ============================================

let autoRefreshInterval = null;

document.addEventListener('DOMContentLoaded', async function() {
    await Promise.all([loadTasksFromDatabase(), loadLlmsAndBudgets()]);

    // Fetch transition statuses for idea tasks before first render
    await loadTransitionStatuses();

    initializeProjectTabs();
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
    console.log('Starting auto-refresh (5 second interval)...');
    autoRefreshInterval = setInterval(async () => {
        console.log('Auto-refresh: Checking for database updates...');
        try {
            const response = await fetch(`${API_BASE}/projects/${encodeURIComponent(currentProject)}/tasks`);
            if (response.ok) {
                const newTasks = await response.json();

                // Compare task counts
                if (newTasks.length !== allTasks.length) {
                    console.log(`Database changed: ${allTasks.length} -> ${newTasks.length} tasks`);
                    allTasks = newTasks;
                    allTasks.forEach(task => {
                        taskData[task.id] = task;
                    });
                    renderTasksFromDatabase();
                } else {
                    // Check if any task data changed
                    let dataChanged = false;
                    for (const task of newTasks) {
                        if (taskData[task.id] && JSON.stringify(taskData[task.id]) !== JSON.stringify(task)) {
                            dataChanged = true;
                            break;
                        }
                    }
                    if (dataChanged) {
                        console.log('Data changed, refreshing UI...');
                        allTasks = newTasks;
                        allTasks.forEach(task => {
                            taskData[task.id] = task;
                        });
                        renderTasksFromDatabase();
                    }
                }
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

// Initialize project tab selection
function initializeProjectTabs() {
    document.querySelectorAll('.project-tab').forEach(tab => {
        tab.addEventListener('click', function() {
            const projectName = this.getAttribute('data-project');
            switchProject(projectName);
        });
    });

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
    document.getElementById('new-project-modal').classList.add('active');
    document.getElementById('new-project-name').focus();
}

function closeNewProjectModal() {
    document.getElementById('new-project-modal').classList.remove('active');
}

function saveNewProject() {
    const newProjectName = document.getElementById('new-project-name').value.trim();
    if (!newProjectName) {
        document.getElementById('new-project-name').focus();
        return;
    }
    closeNewProjectModal();

    const tabsContainer = document.querySelector('.project-tabs');
    const addBtn = document.getElementById('add-project');
    const newTab = document.createElement('div');
    newTab.className = 'project-tab active';
    newTab.id = `project-${newProjectName.toLowerCase().replace(/\s+/g, '-')}`;
    newTab.setAttribute('data-project', newProjectName);
    newTab.textContent = `📁 ${newProjectName}`;

    tabsContainer.insertBefore(newTab, addBtn);

    console.log(`New project created: ${newProjectName}`);
    // Wire up click handler for the newly created tab
    newTab.addEventListener('click', function() {
        const projectName = this.getAttribute('data-project');
        switchProject(projectName);
    });

    // Switch to the new (empty) project
    switchProject(newProjectName);
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

    card.innerHTML = `
        <div class="task-title">${title}${rejBadge}${processingSpinner}</div>
        <div class="task-meta">
            ${tagsHtml}
            ${ownerHtml}
        </div>
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

    if (status === 'idea') {
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
    } else if (status === 'planning') {
        if (ready) {
            const moveBtn = card.querySelector('.task-actions');
            moveBtn.innerHTML += `<button class="action-btn" onclick="moveTask('${id}', 'development')">Move to IN PROGRESS</button>`;
        }
    } else if (status === 'development') {
        if (ready) {
            const moveBtn = card.querySelector('.task-actions');
            moveBtn.innerHTML += `<button class="action-btn" onclick="moveTask('${id}', 'review')">Move to IN REVIEW</button>`;
        }
    } else if (status === 'review') {
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
    const card = document.querySelector(`.task-card[data-id="${taskId}"]`);
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
                // Reload — card will appear in PLANNING column
                await loadTasksFromDatabase();
                // Re-fetch transition data for all idea tasks
                await loadTransitionStatuses();
                renderTasksFromDatabase();
            } else {
                // rejected or failed
                setCardProcessing(taskId, false);
                cacheTransitionData(taskId, data);

                // Mark card as rejected and re-render
                const card = document.querySelector(`.task-card[data-id="${taskId}"]`);
                if (card) {
                    card.classList.add('rejected');
                }

                // Show the failure overlay
                openTransitionModal(taskId);

                // Re-render to show rejection badge
                await loadTasksFromDatabase();
                await loadTransitionStatuses();
                renderTasksFromDatabase();
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
                + (ready ? `<button class="action-btn" onclick="moveTask('${taskId}', 'development')">Move to IN PROGRESS</button>` : '');
        } else if (newStatus === 'development') {
            actions.innerHTML = `<button class="action-btn" onclick="editTask('${taskId}')">Edit</button>
                                 <button class="action-btn action-btn-danger" onclick="deleteTask('${taskId}')">Delete</button>`
                + (ready ? `<button class="action-btn" onclick="moveTask('${taskId}', 'review')">Move to IN REVIEW</button>` : '');
        } else if (newStatus === 'review') {
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

    // Auto-move from development to review after 15 seconds
    if (newStatus === 'development') {
        setTimeout(async () => {
            if (taskData[taskId]) {
                await moveTask(taskId, 'review');
                console.log(`Auto-move: Task ${taskId} moved to review after 15 seconds`);
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
        if (draggedElement) _draggedEl.classList.add('dragging');
    }, 0);

    // Create a single shared ghost placeholder (not appended yet — inserted into
    // the container DOM during dragover so surrounding cards are pushed apart by
    // normal block layout).
    insertIndicator = document.createElement('div');
    insertIndicator.className = 'drop-ghost';
    insertIndicator.setAttribute('aria-hidden', 'true');

    console.log(`Drag Start: card=${draggedTaskId}`);
}

function handleDragEnd(e) {
    this.classList.remove('dragging');

    if (insertIndicator && insertIndicator.parentNode) {
        insertIndicator.parentNode.removeChild(insertIndicator);
    }
    insertIndicator = null;

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
            // Re-fetch this column's tasks from the server to get authoritative positions
            const freshResponse = await fetch(`/api/projects/${encodeURIComponent(currentProject)}/tasks`);
            if (freshResponse.ok) {
                const freshTasks = await freshResponse.json();
                // Replace taskData entries with fresh server data
                freshTasks.forEach(task => { taskData[task.id] = task; });
                allTasks = freshTasks;
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

    document.getElementById('llm-modal').addEventListener('click', function(e) {
        if (e.target === this) closeLlmModal();
    });
    document.getElementById('budget-modal').addEventListener('click', function(e) {
        if (e.target === this) closeBudgetModal();
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

async function openLlmModal() {
    await loadLlmsAndBudgets();
    renderLlmList();
    document.getElementById('llm-modal').classList.add('active');
}

function closeLlmModal() {
    document.getElementById('llm-modal').classList.remove('active');
}

function renderLlmList() {
    const container = document.getElementById('llm-list');
    if (allLlms.length === 0) {
        container.innerHTML = '<p style="color:#6c757d;font-size:0.85rem">No LLM endpoints configured.</p>';
        return;
    }
    let html = '<table style="width:100%;font-size:0.85rem;border-collapse:collapse">';
    html += '<tr style="border-bottom:1px solid #dee2e6"><th style="text-align:left;padding:0.4rem">ID</th><th style="text-align:left;padding:0.4rem">Endpoint</th><th style="text-align:left;padding:0.4rem">Model</th><th></th></tr>';
    allLlms.forEach(l => {
        html += `<tr style="border-bottom:1px solid #f0f0f0">
            <td style="padding:0.4rem">${l.id}</td>
            <td style="padding:0.4rem">${l.address}:${l.port}</td>
            <td style="padding:0.4rem">${l.model}</td>
            <td style="padding:0.4rem"><button class="action-btn action-btn-danger" onclick="deleteLlmEntry(${l.id})">Delete</button></td>
        </tr>`;
    });
    html += '</table>';
    container.innerHTML = html;
}

async function addLlm() {
    const address = document.getElementById('llm-address').value.trim();
    const port = parseInt(document.getElementById('llm-port').value) || 8008;
    const model = document.getElementById('llm-model').value.trim();
    if (!address || !model) { showInlineError('llm-error', 'Address and model are required.'); return; }

    const res = await fetch(`${API_BASE}/llms`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ address, port, model })
    });
    if (!res.ok) {
        const err = await res.json();
        showInlineError('llm-error', err.detail || 'Failed to create LLM.');
        return;
    }
    document.getElementById('llm-model').value = '';
    await loadLlmsAndBudgets();
    renderLlmList();
}

async function deleteLlmEntry(id) {
    if (!confirm('Delete this LLM endpoint?')) return;
    await fetch(`${API_BASE}/llms/${id}`, { method: 'DELETE' });
    await loadLlmsAndBudgets();
    renderLlmList();
}

// --- Budget Modal ---

async function openBudgetModal() {
    await loadLlmsAndBudgets();
    renderBudgetList();
    document.getElementById('budget-modal').classList.add('active');
}

function closeBudgetModal() {
    document.getElementById('budget-modal').classList.remove('active');
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
            <td style="padding:0.4rem">${b.name}</td>
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

async function deleteBudgetEntry(id) {
    if (!confirm('Delete this budget?')) return;
    await fetch(`${API_BASE}/budgets/${id}`, { method: 'DELETE' });
    await loadLlmsAndBudgets();
    renderBudgetList();
}
