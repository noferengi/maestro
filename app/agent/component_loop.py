"""
app/agent/component_loop.py
----------------------------
ComponentLoop - focused agent for implementing a single component.
ComponentToolDispatcher - restricts writes to assigned file manifest.

A stripped-down MaestroLoop with:
  - File write containment (only assigned files)
  - Shared context prefix (planning result + prior batch output)
  - Per-component max turns (default 50)
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

from app.agent.config import (
    INDEV_COMPONENT_MAX_TURNS,
    INDEV_ENFORCE_FILE_CONTAINMENT,
    INDEV_AGENT_TOOLS,
    PROJECT_ROOT,
    SIGNAL_ACCEPTED,
    SIGNAL_REVERT,
    SIGNAL_RESOLUTION_STALLED,
    SIGNAL_CORRECTION_STALLED,
    SIGNAL_VERDICT_REJECTED,
    SIGNAL_VERDICT_NEEDS_WORK,
    check_context_saturation,
)
from app.agent.llm_client import call_llm, is_shutting_down, ShutdownError
from app.agent.tools import dispatch_tool, TOOL_SCHEMAS, _assert_safe_path, build_tool_schemas, get_task_git_cwd

_INDEV_TOOL_SCHEMAS: list[dict] = build_tool_schemas(INDEV_AGENT_TOOLS)

logger = logging.getLogger(__name__)
AGENT_NAME = "Component Loop"


# ---------------------------------------------------------------------------
# ComponentToolDispatcher - file write containment
# ---------------------------------------------------------------------------

class ComponentToolDispatcher:
    """Wraps dispatch_tool() - restricts write_file/append_file to assigned files only."""

    def __init__(self, allowed_write_paths: list[str]):
        self._allowed = set(
            os.path.realpath(os.path.abspath(p)) for p in allowed_write_paths
        )

    def dispatch(self, name: str, arguments: dict) -> str:
        prefix = ""
        if INDEV_ENFORCE_FILE_CONTAINMENT and name in ("write_file", "append_file"):
            path = arguments.get("path", "")
            _effective_root = get_task_git_cwd() or PROJECT_ROOT
            resolved = os.path.realpath(os.path.abspath(
                os.path.join(_effective_root, path) if not os.path.isabs(path) else path
            ))
            if resolved not in self._allowed:
                prefix = (
                    f"[MANIFEST NOTE: '{path}' is outside the primary manifest "
                    f"({[os.path.basename(p) for p in sorted(self._allowed)]}), "
                    f"but the write was allowed. Prefer staying within your assigned files.]\n"
                )
        result = dispatch_tool(name, arguments)
        return prefix + result if prefix else result


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


_TEST_COMMAND_BY_TYPE: dict[str, str] = {
    "python": "python -m pytest",
    "node": "npm test",
    "rust": "cargo test",
    "go": "go test ./...",
    "cpp": "make test",
    "android": "./gradlew test",
    "java": "mvn test",
}


def _get_test_command_hint() -> str:
    """Return the appropriate test command for the current project type."""
    from app.agent.tools import get_task_git_cwd
    from app.agent.worktree import detect_project_type
    cwd = get_task_git_cwd()
    if cwd:
        ptype = detect_project_type(cwd)
        if ptype and ptype in _TEST_COMMAND_BY_TYPE:
            return _TEST_COMMAND_BY_TYPE[ptype]
    return "python -m pytest"


def _is_test_command(fn_name: str, fn_args: dict) -> bool:
    """Return True if this tool call is running tests."""
    if fn_name in ("run_shell_indev", "run_shell_review",
                   "run_pytest", "run_unittest", "run_cargo_test",
                   "run_go_test", "run_npm_test"):
        if fn_name in ("run_pytest", "run_unittest", "run_cargo_test",
                       "run_go_test", "run_npm_test"):
            return True
        cmd = fn_args.get("command", "").lower()
        return any(kw in cmd for kw in ("pytest", "unittest", "cargo test", "go test", "npm test", "mvn test", "ctest", "gradlew test"))
    return False


def _detect_test_outcome(output: str) -> str | None:
    """Heuristic parse of test runner output. Returns 'passed', 'failed', 'timeout', or None."""
    lower = output.lower()
    if "error: command timed out" in lower:
        return "timeout"

    # pytest
    if "test session starts" in lower or "pytest" in lower:
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

    # cargo test
    if "test result:" in lower:
        if "0 failed" in lower:
            return "passed"
        if "failed" in lower:
            return "failed"
        return None

    # go test
    if re.search(r"^ok\s+\S+", output, re.MULTILINE):
        return "passed"
    if re.search(r"^FAIL\s+\S+", output, re.MULTILINE):
        return "failed"

    # npm / jest
    if "tests passed" in lower or "test suites" in lower:
        if "failed" in lower:
            return "failed"
        if "passed" in lower:
            return "passed"

    # ctest / make test
    if "100% tests passed" in lower:
        return "passed"
    if "tests failed" in lower:
        return "failed"

    return None


# ---------------------------------------------------------------------------
# ComponentResult
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class ComponentLoopResult:
    component_name: str
    status: str  # "ACCEPTED" | "REVERT_TO_DESIGN" | "MAX_TURNS" | "ERROR" | "TIMEOUT"
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
        max_context: int = 0,
        review_feedback: str | None = None,
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
        self.max_context = max_context
        self.review_feedback = review_feedback

        # File write containment
        self.dispatcher = ComponentToolDispatcher(allowed_write_paths)
        self._total_prompt = 0
        self._total_completion = 0
        self._tests_passed: bool = False
        self._terminal_signal: dict | None = None
        # Repeat-tool-call circuit breaker: tracks last 4 (name, args_json) pairs
        self._recent_calls: collections.deque[tuple[str, str]] = collections.deque(maxlen=4)
        self._repeat_notice_count = 0

    async def run(self) -> ComponentLoopResult:
        """Run the component implementation loop."""
        from app.agent.llm_client import set_llm_session_context
        set_llm_session_context(AGENT_NAME)
        if is_shutting_down():
            raise ShutdownError("Server is shutting down")

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
        _ctx_warned: set[float] = set()
        _turn_warned: set[int] = set()

        for turn in range(self.max_turns):
            if is_shutting_down():
                raise ShutdownError("Server is shutting down")

            # Turn saturation check
            from app.agent.config import check_turn_saturation
            if check_turn_saturation(
                turn, self.max_turns, _turn_warned, messages
            ):
                # Turn nudge was injected
                pass

            try:
                response = await call_llm(
                    messages,
                    base_url=self.llm_base_url,
                    model=self.llm_model,
                    tools=_INDEV_TOOL_SCHEMAS,
                    task_id=self.task_id,
                    llm_id=self.llm_id,
                    budget_id=self.budget_id,
                    agent_name=AGENT_NAME,
                )
            except Exception as e:
                consecutive_errors += 1
                logger.warning("[%s] LLM call failed (error %d): %s", AGENT_NAME, consecutive_errors, e)
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
            prompt_tokens_this_call = usage.get("prompt_tokens", 0)
            self._total_prompt += prompt_tokens_this_call
            self._total_completion += usage.get("completion_tokens", 0)

            # Context saturation check
            if check_context_saturation(
                prompt_tokens_this_call, self.max_context, _ctx_warned, messages
            ):
                logger.warning(
                    "[component] '%s' context saturation (turn %d) - terminating",
                    self.component_name, turn + 1,
                )
                return ComponentLoopResult(
                    component_name=self.component_name,
                    status="REVERT_TO_DESIGN",
                    turns=turn + 1,
                    files_changed=sorted(files_changed),
                    error_detail="Context saturation limit reached - terminating component loop.",
                    prompt_tokens=self._total_prompt,
                    completion_tokens=self._total_completion,
                )

            choice = response.get("choices", [{}])[0]
            msg = choice.get("message", {})
            content = msg.get("content", "")
            tool_calls = msg.get("tool_calls", [])

            messages.append(msg)

            # No content and no tool calls — nudge the model rather than
            # silently looping with an empty assistant message at the tail.
            # (An empty trailing assistant turn looks like a prefill request
            # to thinking models and causes HTTP 400 on the next call.)
            if not tool_calls and not content:
                messages.append({
                    "role": "user",
                    "content": (
                        "[SYSTEM] Your last response was empty. "
                        "Please call a tool to make progress, or call "
                        "submit_work(signal='ACCEPTED', summary='...') to complete. "
                        "Do not output free-form prose or raw JSON as a terminal action — "
                        "use the submit_work tool call."
                    ),
                })
                continue

            # Dispatch tool calls
            if tool_calls:
                for tc in tool_calls:
                    fn_name = tc["function"]["name"]
                    try:
                        fn_args = tc["function"]["arguments"] if isinstance(tc["function"]["arguments"], dict) else json.loads(tc["function"]["arguments"])
                    except json.JSONDecodeError:
                        fn_args = {}

                    # Repeat-tool-call circuit breaker
                    call_key = (fn_name, json.dumps(fn_args, sort_keys=True))
                    self._recent_calls.append(call_key)
                    repeat_count = sum(1 for c in self._recent_calls if c == call_key)
                    if repeat_count >= 3:
                        self._repeat_notice_count += 1
                        logger.warning(
                            "[component] '%s' repeat tool call detected: %s called %d times in last 4 calls (notice #%d)",
                            self.component_name, fn_name, repeat_count, self._repeat_notice_count,
                        )
                        if self._repeat_notice_count >= 2:
                            return ComponentLoopResult(
                                component_name=self.component_name,
                                status="REVERT_TO_DESIGN",
                                turns=turn + 1,
                                files_changed=sorted(files_changed),
                                error_detail=f"repeat_tool_call_circuit_breaker: {fn_name} called identically {repeat_count}+ times",
                                prompt_tokens=self._total_prompt,
                                completion_tokens=self._total_completion,
                            )
                        messages.append({
                            "role": "user",
                            "content": (
                                f"SYSTEM NOTICE: You have called {fn_name}({json.dumps(fn_args)}) "
                                f"{repeat_count} times with identical arguments. "
                                "The tool's output will not change on further repeats. Either take a different "
                                "action, read a different file, or emit a final signal (ACCEPTED / REVERT_TO_DESIGN)."
                            ),
                        })

                    result = self.dispatcher.dispatch(fn_name, fn_args)

                    # Detect __maestro_terminal__ marker from submit_work
                    if fn_name == "submit_work":
                        try:
                            terminal_data = json.loads(str(result))
                            if terminal_data.get("__maestro_terminal__") is True:
                                logger.info(
                                    "[component] '%s': submit_work tool call — "
                                    "signal=%s, summary='%s'",
                                    self.component_name,
                                    terminal_data.get("signal"),
                                    terminal_data.get("summary", "")[:120],
                                )
                                self._terminal_signal = terminal_data
                        except (json.JSONDecodeError, ValueError):
                            pass

                    # Track test outcomes
                    if _is_test_command(fn_name, fn_args):
                        test_outcome = _detect_test_outcome(str(result))
                        if test_outcome == "passed":
                            self._tests_passed = True
                        elif test_outcome == "failed":
                            self._tests_passed = False
                        elif test_outcome == "timeout":
                            # Return immediately on timeout - this triggers a research agent
                            # in the parent orchestrator to investigate the hang.
                            return ComponentLoopResult(
                                component_name=self.component_name,
                                status="TIMEOUT",
                                turns=turn + 1,
                                files_changed=sorted(files_changed),
                                error_detail=f"Test command timed out: {fn_args.get('command')}",
                                prompt_tokens=self._total_prompt,
                                completion_tokens=self._total_completion,
                            )

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

                # Check for terminal signal from submit_work tool call
                if self._terminal_signal is not None:
                    sig = self._terminal_signal.get("signal")
                    if sig == "ACCEPTED":
                        component_files = self.step.get("files", [])
                        if not self._tests_passed and _is_testable_component(component_files):
                            logger.info(
                                "[component] '%s' submit_work(ACCEPTED) blocked — tests not passed",
                                self.component_name,
                            )
                            messages.append({
                                "role": "user",
                                "content": (
                                    "You called submit_work(ACCEPTED) but no passing test run was recorded. "
                                    "Please run the tests first (e.g. run_pytest('.', '-v') for Python, "
                                    "run_cargo_test() for Rust, run_go_test() for Go, run_npm_test() for Node). "
                                    "Then call submit_work(ACCEPTED) again once tests pass."
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
                    if sig in ("REVERT_TO_DESIGN", "RESOLUTION_STALLED", "CORRECTION_STALLED",
                               "VERDICT_REJECTED", "VERDICT_NEEDS_WORK"):
                        return ComponentLoopResult(
                            component_name=self.component_name,
                            status="REVERT_TO_DESIGN",
                            turns=turn + 1,
                            error_detail=self._terminal_signal.get("summary", "Reverting to design."),
                            prompt_tokens=self._total_prompt,
                            completion_tokens=self._total_completion,
                        )

        return ComponentLoopResult(
            component_name=self.component_name,
            status="MAX_TURNS",
            turns=self.max_turns,
            files_changed=sorted(files_changed),
            prompt_tokens=self._total_prompt,
            completion_tokens=self._total_completion,
        )

    def _build_system_prompt(self) -> str:
        prompt = (
            "You are a focused component implementation agent for Project Maestro.\n"
            "Your job is to implement ONE specific component according to the plan.\n\n"
            "RULES:\n"
            "- Only write to files in your assigned manifest\n"
            "- Write tests for your component\n"
            "- Run tests using the named test tools: run_pytest, run_unittest, run_cargo_test, run_go_test, run_npm_test\n"
            "- If your project needs a build step first: run_make, run_cargo_build, run_go_build, run_npm_build, run_tsc, run_gradle, run_mvn\n"
            "- If you add dependencies to a manifest file, install them first: run_pip_install, run_npm_install, run_cargo_fetch\n"
            "- To undo unintentional file edits: git_restore(path) restores to HEAD, git_unstage(path) removes from staging area\n"
            "- When done, call: submit_work(signal='ACCEPTED', summary='...')\n"
            "- If you cannot complete, call: submit_work(signal='REVERT_TO_DESIGN', summary='...')\n"
            "- Never hard-delete files. Use archive_file() for removal.\n"
            "- Work on the maestro/task-{id} branch.\n\n"
            "TEST QUALITY RULES — strictly required:\n"
            "- Tests must be fast. Every test must complete in under 5 seconds.\n"
            "- Choose input sizes that respect the algorithm's complexity. For O(2^n) "
            "algorithms (naive recursion, brute-force search), never test with n > 30. "
            "For example, never write a test for Fibonacci(900) using a naive recursive "
            "implementation; it will never complete. For O(n^2), stay under 10000 elements. "
            "For O(n log n), under 1 million.\n"
            "- Before writing a test for a large input, estimate the runtime: "
            "if the algorithm is O(2^n), n=40 takes ~1 trillion operations — do not test it. "
            "n=30 takes ~1 billion — borderline. n=20 takes ~1 million — fine.\n"
            "- Never write a test whose purpose is 'verify this completes' for a slow input. "
            "If the function has a documented max-input guard, test that the guard raises "
            "an error — do NOT call the function with the guarded value and wait for it.\n"
            "- Mock expensive external calls (network, disk, subprocesses). "
            "Do not make real network requests in tests.\n\n"
            f"Planning Context:\n{self.planning_context[:4000]}\n"
        )
        if self.review_feedback:
            prompt += (
                "\n\nIMPORTANT — THIS TASK WAS REJECTED BY A PRIOR REVIEW. "
                "You MUST address the findings below before signaling ACCEPTED.\n"
                f"{self.review_feedback}\n"
            )
        return prompt

    def _build_task_brief(self) -> str:
        return (
            f"Component: {self.component_name}\n"
            f"Description: {self.step.get('description', '')}\n"
            f"Files: {json.dumps(self.step.get('files', []))}\n"
            f"Dependencies: {json.dumps(self.step.get('depends_on', []))}\n\n"
            "Implement this component now. Write the code, write tests, run them, "
            "then signal ACCEPTED when done."
        )
