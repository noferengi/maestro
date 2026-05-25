description = "SW Dev indev stage: switch to parallel_agents with dynamic_agents_from_key=implementation_steps"

import json as _json


def _get_template_id(conn, name):
    row = conn.execute(
        "SELECT id FROM pipeline_templates WHERE name = :n LIMIT 1", {"n": name}
    ).fetchone()
    return row["id"] if row else None


def _get_stage(conn, tid, stage_key):
    return conn.execute(
        "SELECT id, config, agent_type FROM pipeline_stages "
        "WHERE template_id = :tid AND stage_key = :key LIMIT 1",
        {"tid": tid, "key": stage_key},
    ).fetchone()


def _update_stage(conn, sid, agent_type, cfg):
    conn.execute(
        "UPDATE pipeline_stages SET agent_type = :at, config = CAST(:cfg AS jsonb) WHERE id = :sid",
        {"at": agent_type, "cfg": _json.dumps(cfg), "sid": sid},
    )


_COMPONENT_PROMPT_TPL = (
    "You are implementing component '{component}'.\n"
    "Your assigned files: {files}\n\n"
    "Planning context:\n{planning_context}\n\n"
    "Write or update only your assigned files. "
    "Call submit_work with signal=ACCEPTED when done, "
    "or signal=REVERT_TO_DESIGN if the design is fundamentally wrong."
)

_INDEV_AGENT_TOOLS = [
    "read_file", "read_file_metadata", "read_last_output",
    "write_file", "append_file", "patch_file", "move_file",
    "list_directory", "find_in_files", "find_files", "find_symbol",
    "find_callers", "find_imports_of", "write_archive",
    "read_git_status", "read_git_diff", "read_git_log", "read_git_blame",
    "read_git_show", "read_diff_stat",
    "write_git_branch", "write_git_commit", "write_git_checkout", "write_git_restore",
    "get_task", "list_tasks", "write_task_status", "write_task_history",
    "write_arch_doc", "write_mermaid", "write_interface_contract",
    "spawn_research_agent", "write_benchmark",
    "run_test_pytest", "run_check_mypy", "run_check_ruff", "run_check_black",
    "run_test_unittest", "run_test_npm", "run_test_cargo", "run_test_go",
    "read_test_summary",
    "run_build_make", "run_build_cargo", "run_build_go", "run_build_npm",
    "run_build_tsc", "run_build_gradle", "run_build_mvn",
    "run_deps_pip", "run_deps_npm", "run_deps_cargo",
    "consult_maestro", "report_tool_bug", "submit_work",
    "query_episodes", "ask_agent", "list_active_sessions",
]


def up(conn):
    tid = _get_template_id(conn, "Software Development")
    if not tid:
        print("[0119] WARNING: 'Software Development' template not found — skipping.")
        return

    row = _get_stage(conn, tid, "indev")
    if not row:
        print("[0119] WARNING: 'indev' stage not found — skipping.")
        return

    raw = row["config"]
    _old_cfg = raw if isinstance(raw, dict) else (_json.loads(raw) if raw else {})

    new_cfg = {
        "dynamic_agents_from_key": "implementation_steps",
        "subagent_type": "dangerous_edit",
        "max_turns": 200,
        "agent_tools": _INDEV_AGENT_TOOLS,
        "agent_system_prompt_template": _COMPONENT_PROMPT_TPL,
        "output_key": "component_outputs",
    }
    _update_stage(conn, row["id"], "parallel_agents", new_cfg)
    print("[0119] indev -> parallel_agents (dynamic_agents_from_key=implementation_steps).")


def down(conn):
    tid = _get_template_id(conn, "Software Development")
    if not tid:
        return

    row = _get_stage(conn, tid, "indev")
    if not row:
        return

    from app.agent.system_prompt import MAESTRO_SYSTEM_PROMPT
    revert_cfg = {
        "system_prompt": MAESTRO_SYSTEM_PROMPT,
        "agent_tools": _INDEV_AGENT_TOOLS,
        "max_turns": 200,
    }
    _update_stage(conn, row["id"], "dangerous_edit_llm_agent", revert_cfg)
    print("[0119] indev reverted -> dangerous_edit_llm_agent.")
