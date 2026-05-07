import sys
sys.path.insert(0, r'D:\workspace\TheMaestro')

from mcp.server.fastmcp import FastMCP
from mcp_tools.diagnostics import (
    diagnose_task, get_scheduler_state, get_budget_trace, list_tasks,
    get_gate_history, get_agent_sessions, find_stuck_tasks,
    get_planning_result, run_inspect_cards, get_capacity_status,
    list_pending_merges, get_project_health, preview_dispatch,
)
from mcp_tools.actions import (
    append_task_description, replace_task_description, patch_planning_fields,
    set_task_type, append_task_history, trigger_planning_run, restart_server,
    demote_task, stop_agent, run_pipeline_stage, get_budget_entry_full,
    get_scheduler_api_status,
)
from mcp_tools.monitor import monitor

mcp = FastMCP("test")

all_funcs = [
    # Diagnostics
    diagnose_task, get_scheduler_state, get_budget_trace, list_tasks,
    get_gate_history, get_agent_sessions, find_stuck_tasks,
    get_planning_result, run_inspect_cards, get_capacity_status,
    list_pending_merges, get_project_health, preview_dispatch,
    # Actions
    append_task_description, replace_task_description, patch_planning_fields,
    set_task_type, append_task_history, trigger_planning_run, restart_server,
    demote_task, stop_agent, run_pipeline_stage, get_budget_entry_full,
    get_scheduler_api_status,
    # Monitor
    monitor,
]

for func in all_funcs:
    mcp.tool()(func)
    print(f"  Registered: {func.__name__}")

print(f"\nTotal registered: {len(all_funcs)}")

# Try to inspect the tool manager
print(f"\nMCP attrs: {[a for a in dir(mcp) if not a.startswith('__')]}")

# Check the internal tool registry
if hasattr(mcp, 'tool'):
    print(f"mcp.tool type: {type(mcp.tool)}")
