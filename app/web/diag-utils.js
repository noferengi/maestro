/* ============================================================
   diag-utils.js — Shared state globals and pure utility helpers
   All other diag-*.js files depend on this; load it first.
   ============================================================ */

const API_BASE = '/api';

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

/** Classify entry type from first system message content */
function labelEntry(systemContent) {
    const lc = (systemContent || '').toLowerCase();
    if (lc.includes('codebase surveyor'))                              return 'surveyor';
    if (lc.includes('software architect'))                             return 'designer';
    if (lc.includes('design evaluator') || lc.includes('design judge')) return 'judge';
    if (lc.includes('design reviewer'))                                return 'reviewer';
    if (lc.includes('research agent'))                                 return 'research';
    if (lc.includes('software quality analyst'))                       return 'pitfall';
    if (lc.length > 400)                                               return 'maestro_loop';
    return 'unknown';
}
