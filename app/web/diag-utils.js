/* ============================================================
   diag-utils.js — Shared state globals and pure utility helpers
   All other diag-*.js files depend on this; load it first.
   ============================================================ */

const API_BASE = '/api';

const TYPE_COLORS = {
    surveyor:     '#0d6efd',
    designer:     '#6f42c1',
    reviewer:     '#20c997',
    judge:        '#fd7e14',
    research:     '#ffc107',
    pitfall:      '#e83e8c',
    security:     '#dc3545',
    optimization: '#fd7e14',
    subdivision:  '#6610f2',
    maestro_loop: '#198754',
    file_summary: '#17a2b8',
    web_agent:    '#d946ef',
    unknown:      '#6c757d',
};

const TOOL_COLORS = {
    read:    '#3b82f6', // blue
    write:   '#8b5cf6', // purple
    list:    '#10b981', // green
    search:  '#f59e0b', // amber
    git:     '#ef4444', // red
    shell:   '#6366f1', // indigo
    task:    '#ec4899', // pink
    plan:    '#06b6d4', // cyan
    web:     '#d946ef', // fuchsia (was lime)
    other:   '#94a3b8', // slate
};

const TOOL_CATEGORY_MAP = {
    read_file:         'read',
    read_file_harder:  'read',
    read_file_lines:   'read',
    count_lines:       'read',
    write_file:        'write',
    append_file:       'write',
    archive_file:      'write',
    list_directory:    'list',
    find_files:        'list',
    list_tasks:        'list',
    search_files:      'list',
    web_search:        'web',
    web_fetch:         'web',
    git_status:        'git',
    git_diff:          'git',
    git_log:           'git',
    git_blame:         'git',
    git_show:          'git',
    git_create_branch: 'git',
    git_commit:        'git',
    git_checkout:      'git',
    run_shell:         'shell',
    run_shell_indev:   'shell',
    run_shell_review:  'shell',
    run_shell_security:'shell',
    get_task:          'task',
    update_task_status:'task',
    append_task_history:'task',
    record_benchmark:  'task',
    generate_architecture_doc:  'plan',
    generate_mermaid_diagram:   'plan',
    generate_interface_contract:'plan',
    spawn_research_agent:       'plan',
};

let selectedTaskId     = null;
let selectedEntryId    = null;
let allDiagTasks       = [];   // from GET /api/diagnostics/tasks
let allDiagLlms        = {};   // id → {name, max_context}, from GET /api/llms
let currentEntries     = [];   // lightweight entries for selected task (ascending order)
let currentSessions    = [];   // output of detectSessions()
let cachedSession      = null; // { groupKey, fullEntries, boundaries } — avoids re-fetching same session
let renderedSessionKey = null; // groupKey of session currently in the DOM

// ── Helpers ──────────────────────────────────────────────────

function escapeHtml(str) {
    return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;')
        .replace(/'/g, '&#39;');
}

/** 1024-based token formatting. e.g. 350100 → "341.9K" */
function fmtTokens(n) {
    if (n >= 1048576) return `${(n / 1048576).toFixed(1)}M`;
    if (n >= 1024)    return `${(n / 1024).toFixed(1)}K`;
    return String(n);
}

function formatTimestamp(isoStr) {
    if (!isoStr) return '';
    const d = new Date(isoStr);
    if (isNaN(d)) return isoStr;
    return d.toLocaleString('en-US', {
        month: 'short', day: 'numeric',
        hour: '2-digit', minute: '2-digit', hour12: false,
    });
}

/**
 * Prepare an array of conceptual turn objects for a session group.
 * Maestro Loop: SYSTEM Prompt -> USER Prompt -> Turn 1 (tool) -> Turn 2 (tool)...
 */
function getConceptualTurns(group) {
    const turns = [];
    if (group.length === 0) return turns;

    // 1. SYSTEM and USER (Turn 0 Setup)
    const first = group[0];
    turns.push({
        label: 'SYSTEM Prompt',
        entryId: first.id,
        type: 'system',
        msgIdx: 0,
        entry: first
    });

    // Determine the user message index.  Usually it's 1.
    // If we have multiple entries, Entry 1 is likely the first USER turn.
    // If we have only 1 entry, Msg 1 is likely the USER turn.
    const userEntry = group.length > 1 ? group[1] : first;
    const userMsgIdx = group.length > 1 ? -1 : 1; // -1 means "full entry", 1 means "msg index 1"

    turns.push({
        label: 'USER Prompt',
        entryId: userEntry.id,
        type: 'user',
        msgIdx: userMsgIdx,
        entry: userEntry
    });

    // 2. Loop Phase: Turn 1 ... N
    let turnCount = 0;
    for (let i = 0; i < group.length; i++) {
        const entry = group[i];
        
        // Skip entry 0 (setup) for Turn labels unless it's a 1-entry session with tools
        if (i === 0 && group.length > 1) continue;
        
        // If entry i-1 called a tool, entry i response starts with that tool result.
        // BUT the label for Turn N should describe what the assistant DID in Entry N.
        
        let toolName = entry.first_tool || '';
        let argStr = '';
        if (toolName) {
            try {
                const argsRaw = entry.first_tool_args;
                const args = (typeof argsRaw === 'string' ? JSON.parse(argsRaw) : argsRaw) || {};
                const val = args.path || args.glob_pattern || args.pattern || args.command || args.task_id || Object.values(args)[0];
                if (val !== undefined) {
                    let v = String(val);
                    if (v.length > 40) v = v.slice(0, 37) + '...';
                    argStr = `('${v}')`;
                }
            } catch (_) {}
            
            if (entry.tool_calls > 1) {
                toolName = 'Mx. ' + toolName;
            }
        }

        turnCount++;
        const isLast = (i === group.length - 1);
        // isAbruptEnd: last entry in session still had pending tool calls → no tool result was ever received
        const isAbruptEnd = isLast && !!toolName;
        const toolPart = toolName ? `${toolName}${argStr}` : (isLast ? 'VERDICT' : `Call ${i + 1}`);
        const label = `Turn ${turnCount} - ${toolPart}`;

        turns.push({
            label: label,
            entryId: entry.id,
            type: 'turn',
            turnNum: turnCount,
            toolName: toolName,
            isAbruptEnd: isAbruptEnd,
            entry: entry,
            msgIdx: null  // boundary msgIdx is only known after full fetch; null lets selectEntry/jumpToEntry match by entryId alone
        });
    }

    return turns;
}

/** Classify entry type from first system message content */
function labelEntry(systemContent) {
    const lc = (systemContent || '').toLowerCase();
    if (lc.includes('codebase surveyor'))                              return 'surveyor';
    if (lc.includes('software architect'))                             return 'designer';
    if (lc.includes('design evaluator') || lc.includes('design judge')) return 'judge';
    if (lc.includes('design reviewer'))                                return 'reviewer';
    if (lc.includes('research agent'))                                 return 'research';
    if (lc.includes('software quality analyst'))                       return 'pitfall';
    if (lc.includes('security expert'))                                return 'security';
    if (lc.includes('performance profiler') ||
        lc.includes('optimization expert') ||
        lc.includes('optimization judge'))                             return 'optimization';
    if (lc.includes('feasibility reviewer'))                          return 'surveyor';
    if (lc.includes('code reviewer'))                                  return 'reviewer';
    if (lc.includes('subdivision agent'))                              return 'subdivision';
    if (lc.includes('web search synthesis agent'))                    return 'web_agent';
    if (lc.includes('elite agentic software engineer') ||
        lc.includes('maestro orchestrator'))                           return 'maestro_loop';

    if (lc.length > 400)                                               return 'maestro_loop';
    return 'unknown';
}

/** Classify entry tool category from tool name */
function labelTool(toolName) {
    return TOOL_CATEGORY_MAP[toolName] || 'other';
}

/** Classify entry type from the first user message content (for system-less calls like file summaries) */
function labelEntryFromUser(userContent) {
    const lc = (userContent || '').toLowerCase();
    if (lc.includes('summarize the following source file') ||
        lc.includes('summarize lines ') ||
        lc.includes('source file') && lc.includes('summarize'))       return 'file_summary';
    return 'unknown';
}
