"""
app/agent/maestro_orchestrator.py
----------------------------------
GlobalMaestroAgent — system-level orchestrator that runs above any pipeline.

Invoked by goal iteration loops and "Leave it to the Maestro" triggers.
Does NOT run within a card's pipeline stage.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from app.database import (
    get_goal,
    goal_to_dict,
    append_goal_evidence,
    get_goals_for_project,
    get_active_goals_for_project,
    get_all_projects,
)
from app.database import create_task, get_tasks_by_project
from app.agent.system_prompt import build_orchestrator_system_prompt
from app.agent.llm_client import call_llm
from app.database import get_llm

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tools available to the orchestrator
# ---------------------------------------------------------------------------

_TOOL_SCHEMAS = [
    {
        "name": "create_task",
        "description": "Create a new task card in a pipeline.",
        "parameters": {
            "type": "object",
            "properties": {
                "title":               {"type": "string"},
                "description":         {"type": "string"},
                "project":             {"type": "string"},
                "pipeline_template_id": {"type": "integer", "description": "ID of the pipeline template"},
                "prerequisites":       {"type": "array", "items": {"type": "string"}, "default": []},
                "goal_id":             {"type": "integer"},
            },
            "required": ["title", "description", "project"],
        },
    },
    {
        "name": "list_tasks",
        "description": "List tasks for a project, optionally filtered by status.",
        "parameters": {
            "type": "object",
            "properties": {
                "project":       {"type": "string"},
                "status_filter": {"type": "string", "description": "e.g. 'idea', 'planning', 'completed'"},
            },
            "required": ["project"],
        },
    },
    {
        "name": "get_project_health",
        "description": "Get a health summary for a project: stage counts, active sessions, recent spend.",
        "parameters": {
            "type": "object",
            "properties": {
                "project": {"type": "string"},
            },
            "required": ["project"],
        },
    },
    {
        "name": "append_goal_evidence",
        "description": "Append a note to the goal's evidence log.",
        "parameters": {
            "type": "object",
            "properties": {
                "goal_id": {"type": "integer"},
                "text":    {"type": "string"},
            },
            "required": ["goal_id", "text"],
        },
    },
    {
        "name": "submit_orchestration_result",
        "description": "Terminal action: report what was done and stop.",
        "parameters": {
            "type": "object",
            "properties": {
                "signal":  {"type": "string", "enum": ["DONE", "NEEDS_HUMAN", "BLOCKED"]},
                "summary": {"type": "string"},
            },
            "required": ["signal", "summary"],
        },
    },
]


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class GlobalMaestroAgent:
    MAX_TURNS = 20

    def __init__(
        self,
        project_name: str,
        llm_id: int,
        budget_id: int,
        goal_id: int | None = None,
    ):
        self.project_name = project_name
        self.llm_id = llm_id
        self.budget_id = budget_id
        self.goal_id = goal_id
        llm = get_llm(llm_id)
        self._base_url = f"http://{llm.address}:{llm.port}/v1" if llm else None

    def _build_messages(self, directive: str) -> list[dict]:
        system = build_orchestrator_system_prompt()
        health = self._all_project_health_summary()
        goal_ctx = self._goal_context() if self.goal_id else "(no specific goal)"

        user_content = (
            f"## Current System Health\n{health}\n\n"
            f"## Active Goal\n{goal_ctx}\n\n"
            f"## Your Task\n{directive}"
        )
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ]

    def _all_project_health_summary(self) -> str:
        try:
            projects = get_all_projects()
            lines = []
            for p in projects:
                tasks = get_tasks_by_project(p.name)
                by_stage: dict[str, int] = {}
                for t in tasks:
                    stage = t.stage_key or t.type or "unknown"
                    by_stage[stage] = by_stage.get(stage, 0) + 1
                summary = ", ".join(f"{k}: {v}" for k, v in sorted(by_stage.items()))
                lines.append(f"- **{p.name}**: {summary or 'no active tasks'}")
            return "\n".join(lines) or "(no projects)"
        except Exception as exc:
            logger.warning("Health summary failed: %s", exc)
            return "(health summary unavailable)"

    def _goal_context(self) -> str:
        try:
            goal = get_goal(self.goal_id)
            if not goal:
                return f"(goal {self.goal_id} not found)"
            d = goal_to_dict(goal)
            criteria_text = "\n".join(
                f"  - {c}" if isinstance(c, str) else f"  - {c.get('text', c)}"
                for c in (d.get("criteria") or [])
            )
            return (
                f"**{d['title']}** (id={d['id']}, status={d['status']}, "
                f"iteration={d.get('iteration_count', 0)})\n"
                f"Statement: {d['statement']}\n"
                f"Criteria:\n{criteria_text or '  (none specified)'}\n"
                f"Evidence tail:\n{(d.get('evidence') or '')[-800:]}"
            )
        except Exception as exc:
            logger.warning("Goal context failed: %s", exc)
            return "(goal context unavailable)"

    # ------------------------------------------------------------------
    # Tool dispatch
    # ------------------------------------------------------------------

    def _dispatch_tool(self, name: str, args: dict) -> Any:
        if name == "create_task":
            return self._tool_create_task(args)
        if name == "list_tasks":
            return self._tool_list_tasks(args)
        if name == "get_project_health":
            return self._tool_get_project_health(args)
        if name == "append_goal_evidence":
            return self._tool_append_goal_evidence(args)
        if name == "submit_orchestration_result":
            return {"__terminal__": True, "signal": args.get("signal"), "summary": args.get("summary")}
        return {"error": f"unknown tool: {name}"}

    def _tool_create_task(self, args: dict) -> dict:
        task = create_task(
            title=args["title"],
            task_type="idea",
            description=args.get("description", ""),
            project=args["project"],
            pipeline_template_id=args.get("pipeline_template_id"),
            prerequisites=args.get("prerequisites") or [],
        )
        if not task:
            return {"error": "Failed to create task"}
        # Link to goal if specified
        goal_id = args.get("goal_id")
        if goal_id and task:
            from app.database import update_task
            update_task(task.id, goal_id=goal_id)
        return {"task_id": task.id, "title": task.title, "stage": task.stage_key or task.type}

    def _tool_list_tasks(self, args: dict) -> dict:
        tasks = get_tasks_by_project(args["project"])
        status_filter = args.get("status_filter")
        if status_filter:
            tasks = [t for t in tasks if (t.stage_key or t.type) == status_filter]
        return {
            "tasks": [
                {"id": t.id, "title": t.title, "stage": t.stage_key or t.type}
                for t in tasks[:50]
            ]
        }

    def _tool_get_project_health(self, args: dict) -> dict:
        project_name = args["project"]
        tasks = get_tasks_by_project(project_name)
        by_stage: dict[str, int] = {}
        for t in tasks:
            stage = t.stage_key or t.type or "unknown"
            by_stage[stage] = by_stage.get(stage, 0) + 1
        goals = get_active_goals_for_project(project_name)
        return {
            "project": project_name,
            "task_counts_by_stage": by_stage,
            "total_tasks": len(tasks),
            "active_goals": len(goals),
        }

    def _tool_append_goal_evidence(self, args: dict) -> dict:
        append_goal_evidence(args["goal_id"], args["text"])
        return {"status": "evidence appended"}

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self, directive: str) -> dict:
        messages = self._build_messages(directive)
        turn = 0

        while turn < self.MAX_TURNS:
            turn += 1
            try:
                response = await call_llm(
                    messages=messages,
                    tools=_TOOL_SCHEMAS,
                    tool_choice="auto",
                    llm_id=self.llm_id,
                    budget_id=self.budget_id,
                    base_url=self._base_url,
                    agent_name="GlobalMaestroAgent",
                )
            except Exception as exc:
                logger.error("Orchestrator LLM call failed (turn %d): %s", turn, exc)
                return {"signal": "ERROR", "summary": str(exc)}

            choice = response.get("choices", [{}])[0]
            msg = choice.get("message", {})
            messages.append(msg)

            tool_calls = msg.get("tool_calls") or []
            if not tool_calls:
                # No tool call — treat final text as done
                return {
                    "signal": "DONE",
                    "summary": msg.get("content", ""),
                }

            for tc in tool_calls:
                fn = tc.get("function", {})
                tool_name = fn.get("name", "")
                try:
                    tool_args = json.loads(fn.get("arguments", "{}"))
                except json.JSONDecodeError:
                    tool_args = {}

                result = self._dispatch_tool(tool_name, tool_args)

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "content": json.dumps(result),
                })

                if isinstance(result, dict) and result.get("__terminal__"):
                    return {"signal": result["signal"], "summary": result["summary"]}

        return {"signal": "EXHAUSTED", "summary": f"Reached {self.MAX_TURNS} turns without completion"}
