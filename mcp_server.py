"""
TheMaestro MCP Server

Exposes TheMaestro's internals as native MCP tools for Claude Code.
Registered in .claude/settings.local.json; runs as a stdio child process.

Tools:
  Diagnostic (read-only):
    maestro__diagnose_task          — complete snapshot for one task
    maestro__get_scheduler_state    — what's running / queued / stuck (DB)
    maestro__get_scheduler_api      — live scheduler state from running server
    maestro__get_budget_trace       — LLM call history with finish_reason extracted
    maestro__list_tasks             — list tasks with optional project/type filter
    maestro__get_gate_history       — planning gate failure history
    maestro__get_agent_sessions     — agent session history
    maestro__find_stuck_tasks       — tasks with open session + no recent LLM call
    maestro__get_planning_result    — full plan content (interface_contracts etc.)
    maestro__run_inspect_cards      — escape hatch: run inspect_cards.py sections
    maestro__get_capacity_status    — per-node/LLM slot utilisation (free/used/total)
    maestro__list_pending_merges    — completed tasks not yet merged to main
    maestro__get_project_health     — cold-start briefing: stages, sessions, spend, demotions

  Action (write):
    maestro__append_task_description  — add context to task description
    maestro__replace_task_description — full description replacement
    maestro__patch_planning_fields    — fix interface_contracts / file_manifest etc.
    maestro__set_task_type            — force pipeline stage change (no demotion record)
    maestro__append_task_history      — leave diagnostic breadcrumbs
    maestro__trigger_planning_run     — POST /api/tasks/{id}/run-planning
    maestro__demote_task              — move task backward with demotion record
    maestro__stop_agent               — graceful stop of a running MaestroLoop
    maestro__run_pipeline_stage       — trigger review / security / full_review
    maestro__get_budget_entry_full    — full prompt+response for one budget entry

  Monitor:
    maestro__monitor                  — block N seconds, return activity diff + pattern flags
"""

import sys
import os

# Put project root on sys.path before any local imports
_PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from mcp.server.fastmcp import FastMCP

from mcp_tools.diagnostics import (
    diagnose_task,
    get_scheduler_state,
    get_budget_trace,
    list_tasks,
    get_gate_history,
    get_agent_sessions,
    find_stuck_tasks,
    get_planning_result,
    run_inspect_cards,
    get_capacity_status,
    list_pending_merges,
    get_project_health,
)
from mcp_tools.actions import (
    append_task_description,
    replace_task_description,
    patch_planning_fields,
    set_task_type,
    append_task_history,
    trigger_planning_run,
    restart_server,
    get_scheduler_api_status,
    demote_task,
    stop_agent,
    run_pipeline_stage,
    get_budget_entry_full,
)
from mcp_tools.monitor import monitor

mcp = FastMCP("maestro")

# --- Diagnostic tools ---
mcp.tool()(diagnose_task)
mcp.tool()(get_scheduler_state)
mcp.tool()(get_budget_trace)
mcp.tool()(list_tasks)
mcp.tool()(get_gate_history)
mcp.tool()(get_agent_sessions)
mcp.tool()(find_stuck_tasks)
mcp.tool()(get_planning_result)
mcp.tool()(run_inspect_cards)
mcp.tool()(get_capacity_status)
mcp.tool()(list_pending_merges)
mcp.tool()(get_project_health)

# --- Live API tool ---
mcp.tool()(get_scheduler_api_status)

# --- Action tools ---
mcp.tool()(append_task_description)
mcp.tool()(replace_task_description)
mcp.tool()(patch_planning_fields)
mcp.tool()(set_task_type)
mcp.tool()(append_task_history)
mcp.tool()(trigger_planning_run)
mcp.tool()(restart_server)
mcp.tool()(demote_task)
mcp.tool()(stop_agent)
mcp.tool()(run_pipeline_stage)
mcp.tool()(get_budget_entry_full)

# --- Monitor tool ---
mcp.tool()(monitor)

if __name__ == "__main__":
    mcp.run()
