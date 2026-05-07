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
    diagnose_task, get_scheduler_state, get_budget_trace, list_tasks,
    get_gate_history, get_agent_sessions, find_stuck_tasks,
    get_planning_result, run_inspect_cards, get_capacity_status,
    list_pending_merges, get_project_health, preview_dispatch,
    append_task_description, replace_task_description, patch_planning_fields,
    set_task_type, append_task_history, trigger_planning_run, restart_server,
    demote_task, stop_agent, run_pipeline_stage, get_budget_entry_full,
    get_scheduler_api_status,
    monitor,
]

for func in all_funcs:
    mcp.tool()(func)

# List tools via the public API (async)
import asyncio

async def list_all():
    tools = await mcp.list_tools()
    print(f"Total tools from list_tools(): {len(tools)}")
    for t in tools:
        print(f"  {t.name}: {t.description[:80]}...")

asyncio.run(list_all())
