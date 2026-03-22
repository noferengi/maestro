"""
app/agent/component_loop.py
----------------------------
ComponentLoop — focused agent for implementing a single component.
ComponentToolDispatcher — restricts writes to assigned file manifest.

A stripped-down MaestroLoop with:
  - File write containment (only assigned files)
  - Shared context prefix (planning result + prior batch output)
  - Per-component max turns (default 50)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

from app.agent.config import (
    INDEV_COMPONENT_MAX_TURNS,
    INDEV_LLM_TEMPERATURE,
    INDEV_ENFORCE_FILE_CONTAINMENT,
    INDEV_AGENT_TOOLS,
    PROJECT_ROOT,
)
from app.agent.llm_client import call_llm
from app.agent.tools import dispatch_tool, TOOL_SCHEMAS, _assert_safe_path, build_tool_schemas

_INDEV_TOOL_SCHEMAS: list[dict] = build_tool_schemas(INDEV_AGENT_TOOLS)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ComponentToolDispatcher — file write containment
# ---------------------------------------------------------------------------

class ComponentToolDispatcher:
    """Wraps dispatch_tool() — restricts write_file/append_file to assigned files only."""

    def __init__(self, allowed_write_paths: list[str]):
        self._allowed = set(
            os.path.realpath(os.path.abspath(p)) for p in allowed_write_paths
        )

    def dispatch(self, name: str, arguments: dict) -> str:
        if INDEV_ENFORCE_FILE_CONTAINMENT and name in ("write_file", "append_file"):
            path = arguments.get("path", "")
            resolved = os.path.realpath(os.path.abspath(
                os.path.join(PROJECT_ROOT, path) if not os.path.isabs(path) else path
            ))
            if resolved not in self._allowed:
                return (
                    f"ERROR: Write denied. File '{path}' is not in this component's "
                    f"assigned manifest. Allowed: {[os.path.basename(p) for p in sorted(self._allowed)]}"
                )
        return dispatch_tool(name, arguments)


# ---------------------------------------------------------------------------
# ComponentResult
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Test outcome detection helpers
# ---------------------------------------------------------------------------

_NON_TESTABLE_EXTENSIONS = {
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
    ".md", ".txt", ".rst", ".css", ".html", ".js", ".ts",
    ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".sql",
}


def _is_testable_component(file_list: list[str]) -> bool:
    """Return True if any file in the manifest looks like testable source code."""
    for path in file_list:
        ext = os.path.splitext(path)[1].lower()
        if ext not in _NON_TESTABLE_EXTENSIONS:
            return True
    return False


def _is_test_command(fn_name: str, fn_args: dict) -> bool:
    """Return True if this tool call is running tests."""
    if fn_name in ("run_shell_indev", "run_shell_review"):
        cmd = fn_args.get("command", "").lower()
        return "pytest" in cmd or "unittest" in cmd
    return False


def _detect_test_outcome(output: str) -> str | None:
    """Heuristic parse of pytest output. Returns 'passed', 'failed', or None."""
    lower = output.lower()
    has_pytest = (
        "passed" in lower or "failed" in lower or "error" in lower
        or "pytest" in lower or "test session starts" in lower
    )
    if not has_pytest:
        return None
    has_failures = (
        " failed" in lower or "failures" in lower
        or " error" in lower or "errors" in lower
        or "FAILED" in output
    )
    if has_failures:
        return "failed"
    if "passed" in lower:
        return "passed"
    return None


# ---------------------------------------------------------------------------
# ComponentResult
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class ComponentLoopResult:
    component_name: str
    status: str  # "ACCEPTED" | "REVERT_TO_DESIGN" | "MAX_TURNS" | "ERROR"
    turns: int = 0
    files_changed: list[str] = field(default_factory=list)
    tests_passed: bool = False
    error_detail: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0


# ---------------------------------------------------------------------------
# ComponentLoop
# ---------------------------------------------------------------------------

class ComponentLoop:
    """Focused agent loop for implementing a single component."""

    def __init__(
        self,
        task_id: str,
        component_name: str,
        implementation_step: dict,
        planning_context: str,
        allowed_write_paths: list[str],
        *,
        max_turns: int = INDEV_COMPONENT_MAX_TURNS,
        llm_base_url: str | None = None,
        llm_model: str | None = None,
        llm_id: int | None = None,
        budget_id: int | None = None,
    ):
        self.task_id = task_id
        self.component_name = component_name
        self.step = implementation_step
        self.planning_context = planning_context
        self.max_turns = max_turns
        self.llm_base_url = llm_base_url
        self.llm_model = llm_model
        self.llm_id = llm_id
        self.budget_id = budget_id

        # File write containment
        self.dispatcher = ComponentToolDispatcher(allowed_write_paths)
        self._total_prompt = 0
        self._total_completion = 0
        self._tests_passed: bool = False

    async def run(self) -> ComponentLoopResult:
        """Run the component implementation loop."""
        logger.info(
            "[component] Starting '%s' for task '%s' (max %d turns)",
            self.component_name, self.task_id, self.max_turns,
        )

        system_prompt = self._build_system_prompt()
        messages: list[dict] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": self._build_task_brief()},
        ]

        consecutive_errors = 0
        files_changed: set[str] = set()

        for turn in range(self.max_turns):
            try:
                response = await call_llm(
                    messages,
                    base_url=self.llm_base_url,
                    model=self.llm_model,
                    temperature=INDEV_LLM_TEMPERATURE,
                    tools=_INDEV_TOOL_SCHEMAS,
                    task_id=self.task_id,
                    llm_id=self.llm_id,
                    budget_id=self.budget_id,
                )
            except Exception as e:
                consecutive_errors += 1
                logger.warning("[component] LLM call failed (error %d): %s", consecutive_errors, e)
                if consecutive_errors >= 3:
                    return ComponentLoopResult(
                        component_name=self.component_name,
                        status="REVERT_TO_DESIGN",
                        turns=turn + 1,
                        error_detail=f"3 consecutive LLM failures: {e}",
                        prompt_tokens=self._total_prompt,
                        completion_tokens=self._total_completion,
                    )
                continue

            consecutive_errors = 0
            usage = response.get("usage", {})
            self._total_prompt += usage.get("prompt_tokens", 0)
            self._total_completion += usage.get("completion_tokens", 0)

            choice = response.get("choices", [{}])[0]
            msg = choice.get("message", {})
            content = msg.get("content", "")
            tool_calls = msg.get("tool_calls", [])

            messages.append(msg)

            # Check for terminal signal
            if content:
                if '"signal": "ACCEPTED"' in content or '"signal":"ACCEPTED"' in content:
                    component_files = self.step.get("files", [])
                    if not self._tests_passed and _is_testable_component(component_files):
                        logger.info(
                            "[component] '%s' signaled ACCEPTED without passing tests — requesting test run",
                            self.component_name,
                        )
                        messages.append({
                            "role": "user",
                            "content": (
                                "You signaled ACCEPTED but no passing test run was recorded. "
                                "Please run the tests first:\n\n"
                                "  run_shell_indev('python -m pytest <relevant test paths> -v')\n\n"
                                "Then signal ACCEPTED once tests pass."
                            ),
                        })
                        continue
                    return ComponentLoopResult(
                        component_name=self.component_name,
                        status="ACCEPTED",
                        turns=turn + 1,
                        files_changed=sorted(files_changed),
                        tests_passed=self._tests_passed,
                        prompt_tokens=self._total_prompt,
                        completion_tokens=self._total_completion,
                    )
                if '"signal": "REVERT_TO_DESIGN"' in content or '"signal":"REVERT_TO_DESIGN"' in content:
                    return ComponentLoopResult(
                        component_name=self.component_name,
                        status="REVERT_TO_DESIGN",
                        turns=turn + 1,
                        error_detail=content[:500],
                        prompt_tokens=self._total_prompt,
                        completion_tokens=self._total_completion,
                    )

            # Dispatch tool calls
            if tool_calls:
                for tc in tool_calls:
                    fn_name = tc["function"]["name"]
                    try:
                        fn_args = json.loads(tc["function"]["arguments"])
                    except json.JSONDecodeError:
                        fn_args = {}

                    result = self.dispatcher.dispatch(fn_name, fn_args)

                    # Track test outcomes
                    if _is_test_command(fn_name, fn_args):
                        test_outcome = _detect_test_outcome(str(result))
                        if test_outcome == "passed":
                            self._tests_passed = True
                        elif test_outcome == "failed":
                            self._tests_passed = False

                    # Track file changes
                    if fn_name in ("write_file", "append_file") and not result.startswith("ERROR"):
                        path = fn_args.get("path", "")
                        if path:
                            files_changed.add(path)

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": str(result)[:4000],
                    })

        return ComponentLoopResult(
            component_name=self.component_name,
            status="MAX_TURNS",
            turns=self.max_turns,
            files_changed=sorted(files_changed),
            prompt_tokens=self._total_prompt,
            completion_tokens=self._total_completion,
        )

    def _build_system_prompt(self) -> str:
        return (
            "You are a focused component implementation agent for Project Maestro.\n"
            "Your job is to implement ONE specific component according to the plan.\n\n"
            "RULES:\n"
            "- Only write to files in your assigned manifest\n"
            "- Write tests for your component\n"
            "- Run tests to verify your implementation using run_shell_indev('python -m pytest ...')\n"
            "- When done, output: {\"signal\": \"ACCEPTED\"}\n"
            "- If you cannot complete, output: {\"signal\": \"REVERT_TO_DESIGN\"}\n"
            "- Never hard-delete files. Use archive_file() for removal.\n"
            "- Work on the maestro/task-{id} branch.\n\n"
            f"Planning Context:\n{self.planning_context[:4000]}\n"
        )

    def _build_task_brief(self) -> str:
        return (
            f"Component: {self.component_name}\n"
            f"Description: {self.step.get('description', '')}\n"
            f"Files: {json.dumps(self.step.get('files', []))}\n"
            f"Dependencies: {json.dumps(self.step.get('depends_on', []))}\n\n"
            "Implement this component now. Write the code, write tests, run them, "
            "then signal ACCEPTED when done."
        )
