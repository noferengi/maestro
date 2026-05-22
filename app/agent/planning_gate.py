"""
app/agent/planning_gate.py
--------------------------
Due diligence gate for PLANNING -> IN DEV transition.

All checks are deterministic except the LLM feasibility re-check, which also
handles spec-compliance detection (whether the design implements a forbidden
approach). Spec compliance is intentionally LLM-evaluated — keyword matching
is too brittle to distinguish "removes memoization" from "uses memoization".
Results stored in transition_results with transition="planning_to_indev".
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from app.agent.config import (
    PLANNING_GATE_FEASIBILITY_RECHECK,
    PLANNING_GATE_CONTEXT_SAFETY_MARGIN,
    PIPELINE_DONE_STATUSES,
    PROJECT_ROOT,
)
from app.agent.llm_client import call_llm, is_shutting_down, ShutdownError

logger = logging.getLogger(__name__)
AGENT_NAME = "Planning Gate"

_PYTHON_BUILTINS: frozenset[str] = frozenset({
    "int", "str", "float", "bool", "bytes", "list", "dict", "set", "tuple",
    "none", "nonetype", "exception", "object", "type", "any", "callable",
})

_STDLIB_MODULES: frozenset[str] = frozenset({
    "datetime", "typing", "dataclasses", "json", "os", "sys", "asyncio",
    "collections", "pathlib", "re", "enum", "abc", "functools", "itertools",
    "uuid", "time", "logging", "io", "pickle", "threading", "hashlib",
})

_KOTLIN_BUILTINS: frozenset[str] = frozenset({
    "long", "string", "boolean", "int", "float", "double", "char", "short", "byte",
    "bytearray", "unit", "any", "nothing", "pair", "triple", "array",
    "list", "map", "set", "sequence", "mutablelist", "mutablemap", "mutableset",
    "flow", "stateflow", "sharedflow", "deferred", "job", "channel",
})

_ANDROID_FRAMEWORK: frozenset[str] = frozenset({
    "context", "intent", "bundle", "activity", "fragment", "viewmodel",
    "coroutinescope", "dispatcher", "lifecycleowner", "lifecyclescope",
    "application", "service", "broadcastreceiver",
})


def _is_primitive_or_stdlib(item: str, project_path: str | None = None) -> bool:
    """Return True if a consumes entry looks like a stdlib/primitive false-positive.

    Also returns True if the item corresponds to an existing file in the project,
    meaning it's already satisfied on disk.
    """
    import os
    low = item.lower().strip()
    
    # Noise words that suggest it's a reference to an existing entity
    if low.endswith(" type") or low.endswith(" module") or low.endswith(" class") or low.endswith(" dataclass") or low.endswith(" enum") or low.endswith(" exception"):
        # Strip the suffix and check if the base exists or is a primitive
        base = low.rsplit(" ", 1)[0].strip()
        if base in _PYTHON_BUILTINS or base in _KOTLIN_BUILTINS:
            return True
        # Continue to file check with the base
        low = base

    if " for " in low:
        first = low.split(" for ")[0].strip()
        if first in _PYTHON_BUILTINS or first in _KOTLIN_BUILTINS:
            return True
    
    # Check if it's an existing file in the project
    if project_path and os.path.isdir(project_path):
        # item might be a path (src/models.py) or a dotted module (src.models)
        # Try as direct path first
        candidate_path = os.path.join(project_path, item)
        if os.path.isfile(candidate_path):
            return True
        
        # Try base (without "dataclass" etc)
        candidate_base = os.path.join(project_path, low)
        if os.path.isfile(candidate_base):
            return True

        # Try as dotted module (src.models -> src/models.py)
        if "." in item and not item.endswith(".py"):
            dotted_path = item.replace(".", "/") + ".py"
            candidate_dotted = os.path.join(project_path, dotted_path)
            if os.path.isfile(candidate_dotted):
                return True
        
        # Heuristic: if it's a single word and we see it in the project (case-insensitive search)
        # this is expensive, so we only do it for small items
        if len(low) > 3 and " " not in low and "." not in low:
            # We don't want to walk the whole tree here, but maybe check common dirs
            for sub in ["src", "app", "lib"]:
                search_dir = os.path.join(project_path, sub)
                if os.path.isdir(search_dir):
                    # Quick check for filename match
                    for root, dirs, files in os.walk(search_dir):
                        # Prune hidden dirs
                        dirs[:] = [d for d in dirs if not d.startswith(".")]
                        for f in files:
                            if f.lower().startswith(low):
                                return True
                        if len(files) > 100: # safety break
                            break

    normalized = (
        low.removeprefix("python ")
           .removeprefix("kotlin ")
           .removeprefix("android ")
    )
    # Split by space and take first word (e.g. "CareLevel enum" -> "CareLevel")
    base = normalized.split(" ")[0].split(".")[0]
    return (
        base in _PYTHON_BUILTINS
        or base in _STDLIB_MODULES
        or base in _KOTLIN_BUILTINS
        or base in _ANDROID_FRAMEWORK
    )


@dataclass(slots=True)
class GateCheck:
    name: str
    passed: bool
    hard_fail: bool  # True = blocks advancement; False = warning only
    detail: str = ""


@dataclass(slots=True)
class GateResult:
    task_id: str
    passed: bool
    checks: list[GateCheck] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    llm_check_unavailable: bool = False


class PlanningGate:
    """7-check due diligence for PLANNING -> IN DEV transition."""

    def __init__(
        self,
        task_id: str,
        planning_result: dict,
        all_tasks: list[dict],
        *,
        max_context: int | None = None,
        llm_base_url: str | None = None,
        llm_model: str | None = None,
        llm_id: int | None = None,
        budget_id: int | None = None,
        project_path: str | None = None,
        task_description: str = "",
        domain: str = "software",
    ):
        self.task_id = task_id
        self.plan = planning_result
        self.all_tasks = all_tasks
        self.max_context = max_context or 100000
        self.llm_base_url = llm_base_url
        self.llm_model = llm_model
        self.llm_id = llm_id
        self.budget_id = budget_id
        self.project_path = project_path
        self.task_description = task_description
        self.domain = domain

    async def run(self) -> GateResult:
        """Execute all 7 checks and return the gate result."""
        from app.agent.llm_client import set_llm_session_context
        set_llm_session_context(AGENT_NAME)
        checks: list[GateCheck] = []

        # Check 0a: Python namespace conflicts (module vs package collision)
        # Only meaningful for software/code domains; skip for proof, writing, etc.
        if self.domain != "software":
            checks.append(GateCheck(
                name="namespace_conflicts",
                passed=True,
                hard_fail=True,
                detail=f"Skipped (domain={self.domain!r} — Python namespace check not applicable).",
            ))
        else:
            checks.append(self._check_namespace_conflicts())

        # Check 0b: CREATE targets that already exist on disk
        checks.append(self._check_proposed_creates_exist())

        # Check 1: Interface completeness
        checks.append(self._check_interface_completeness())

        # Check 1b: Implementation steps present
        checks.append(self._check_implementation_steps_present())

        # Check 2: Circular dependency detection
        checks.append(self._check_circular_dependencies())

        # Check 3: Test strategy completeness
        checks.append(self._check_test_strategy())

        # Check 4: Prerequisites resolved
        checks.append(self._check_prerequisites())

        # Check 5: File manifest safety
        checks.append(self._check_file_safety())

        # Check 6: Implementation feasibility re-check (LLM)
        prompt_tokens = 0
        completion_tokens = 0
        llm_check_unavailable = False
        if PLANNING_GATE_FEASIBILITY_RECHECK:
            check6, pt, ct, llm_check_unavailable = await self._check_feasibility()
            checks.append(check6)
            prompt_tokens += pt
            completion_tokens += ct
        else:
            checks.append(GateCheck(
                name="feasibility_recheck",
                passed=True,
                hard_fail=False,
                detail="Skipped (disabled in config)",
            ))

        # Check 7: Context budget
        checks.append(self._check_context_budget())

        # Overall pass: no hard failures
        hard_failures = [c for c in checks if not c.passed and c.hard_fail]
        passed = len(hard_failures) == 0

        logger.info(
            "[planning_gate] Task '%s': %s (%d/%d checks passed, %d hard failures)",
            self.task_id,
            "PASSED" if passed else "FAILED",
            sum(1 for c in checks if c.passed),
            len(checks),
            len(hard_failures),
        )

        return GateResult(
            task_id=self.task_id,
            passed=passed,
            checks=checks,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            llm_check_unavailable=llm_check_unavailable,
        )

    # ------------------------------------------------------------------
    # Check 0a: Python namespace conflicts
    # ------------------------------------------------------------------

    def _check_namespace_conflicts(self) -> GateCheck:
        """Detect Python module/package namespace collisions in the file manifest.

        Having both foo.py and foo/__init__.py in the same directory is a hard
        Python conflict — the interpreter can only treat foo as one of them.
        """
        import os
        from pathlib import Path

        manifest = self.plan.get("file_manifest", [])
        proposed_paths = {e.get("path", "") for e in manifest if e.get("path")}
        conflicts: list[str] = []

        project_root = Path(self.project_path) if self.project_path else None

        for path in proposed_paths:
            if not path.endswith(".py"):
                continue
            if path.endswith("/__init__.py"):
                # If there's also a sibling module file: foo/__init__.py + foo.py
                stem = path[: -len("/__init__.py")]
                sibling_module = f"{stem}.py"
                if sibling_module in proposed_paths:
                    pass  # caught by the module-side loop below
                elif project_root and (project_root / sibling_module).exists():
                    conflicts.append(
                        f"'{path}' conflicts with existing module '{sibling_module}' on disk"
                    )
            else:
                # foo.py — check if foo/__init__.py is also proposed or on disk
                stem = path[:-3]
                pkg_init = f"{stem}/__init__.py"
                if pkg_init in proposed_paths:
                    conflicts.append(
                        f"'{path}' and '{pkg_init}' cannot coexist (module vs package)"
                    )
                elif project_root and (project_root / stem).is_dir():
                    conflicts.append(
                        f"'{path}' conflicts with existing package directory '{stem}/' on disk"
                    )

        if conflicts:
            return GateCheck(
                name="namespace_conflicts",
                passed=False,
                hard_fail=True,
                detail=(
                    f"Python namespace collision(s) detected: {'; '.join(conflicts)}. "
                    "A .py module and a same-named package directory cannot coexist."
                ),
            )
        return GateCheck(
            name="namespace_conflicts",
            passed=True,
            hard_fail=True,
            detail="No Python namespace collisions.",
        )

    # ------------------------------------------------------------------
    # Check 0b: CREATE targets that already exist on disk
    # ------------------------------------------------------------------

    def _check_proposed_creates_exist(self) -> GateCheck:
        """Hard-fail when the plan proposes CREATE on files that already exist.

        A design that proposes to create a file that is already on disk will
        either overwrite existing work or produce a conflict.  The correct
        action in that case is 'update' or 'verify', not 'create'.
        """
        from pathlib import Path

        if not self.project_path:
            return GateCheck(
                name="create_target_exists",
                passed=True,
                hard_fail=True,
                detail="No project path available; skipping disk check.",
            )

        manifest = self.plan.get("file_manifest", [])
        collisions: list[str] = []

        for entry in manifest:
            if entry.get("action", "").lower() != "create":
                continue
            path = entry.get("path", "")
            if not path:
                continue
            full = Path(self.project_path) / path
            if full.exists():
                collisions.append(path)

        if collisions:
            return GateCheck(
                name="create_target_exists",
                passed=False,
                hard_fail=True,
                detail=(
                    f"Plan proposes CREATE on {len(collisions)} file(s) that already exist: "
                    f"{', '.join(collisions[:5])}. "
                    "Change action to 'update' or 'verify', or the Component Loop will conflict with existing work."
                ),
            )
        return GateCheck(
            name="create_target_exists",
            passed=True,
            hard_fail=True,
            detail="No CREATE/exist collisions.",
        )

    # ------------------------------------------------------------------
    # Check 1: Interface completeness
    # ------------------------------------------------------------------

    def _check_interface_completeness(self) -> GateCheck:
        """Every consumes resolves to a provides. Empty contracts are fine (simple task)."""
        contracts = self.plan.get("interface_contracts", [])
        if not contracts:
            # Many simple tasks have no cross-component interfaces — not a hard failure.
            return GateCheck(
                name="interface_completeness",
                passed=True,
                hard_fail=False,
                detail="No interface contracts defined (simple task — skipping).",
            )

        all_provides: set[str] = set()
        all_consumes: set[str] = set()

        for contract in contracts:
            provides = contract.get("provides", [])
            consumes = contract.get("consumes", [])
            all_provides.update(provides)
            all_consumes.update(consumes)

        # First pass: exact matches
        unresolved = all_consumes - all_provides
        if not unresolved:
            return GateCheck(
                name="interface_completeness",
                passed=True,
                hard_fail=True,
                detail=f"{len(contracts)} contracts validated, all consumes resolved exactly.",
            )

        # Second pass: stdlib/primitive filtering
        still_unresolved = set()
        filtered_stdlib = set()
        for u in unresolved:
            if _is_primitive_or_stdlib(u, self.project_path):
                filtered_stdlib.add(u)
            else:
                still_unresolved.add(u)
        
        if not still_unresolved:
            return GateCheck(
                name="interface_completeness",
                passed=True,
                hard_fail=False,
                detail=f"Filtered {len(filtered_stdlib)} stdlib/primitive consumes.",
            )

        # Third pass: Fuzzy/Substring matching
        # If a consumed item is a logical subset of a provided item, it's resolved.
        # e.g. "Plant dataclass" matches "Plant dataclass with __post_init__ validation"
        really_unresolved = set()
        fuzzy_resolved = set()
        
        def normalize(s: str) -> str:
            # Lowercase, remove non-alphanumeric, strip noise
            import re
            s = s.lower().strip()
            # Remove " (v1.0)" or similar at the end
            s = re.sub(r"\s*\(.*?\)$", "", s)
            # Remove " with ...", " from ...", " for ..."
            s = s.split(" with ")[0].split(" from ")[0].split(" for ")[0].strip()
            # Remove trailing "s" (crude plural handling)
            if s.endswith("s") and len(s) > 4:
                s = s[:-1]
            return s

        norm_provides = {normalize(p): p for p in all_provides}
        
        for u in still_unresolved:
            nu = normalize(u)
            found = False
            # Try normalized exact match
            if nu in norm_provides:
                found = True
            else:
                # Try substring match: is nu a substring of any normalized provide?
                for np in norm_provides:
                    if nu == np or (len(nu) > 3 and nu in np) or (len(np) > 3 and np in nu):
                        found = True
                        break
            
            if found:
                fuzzy_resolved.add(u)
            else:
                really_unresolved.add(u)

        if really_unresolved:
            detail = f"Unresolved consumes (advisory): {', '.join(sorted(really_unresolved))}"
            if filtered_stdlib or fuzzy_resolved:
                detail += " (some entries were auto-filtered or fuzzy-matched)"
            # Advisory only — the planning model can't reliably distinguish cross-module
            # interfaces from function parameters. INDEV tests are the real arbiter.
            return GateCheck(name="interface_completeness", passed=False, hard_fail=False, detail=detail)

        return GateCheck(
            name="interface_completeness",
            passed=True,
            hard_fail=True,
            detail=f"{len(contracts)} contracts validated, all consumes resolved (exact or fuzzy).",
        )

    # ------------------------------------------------------------------
    # Check 1b: Implementation steps present
    # ------------------------------------------------------------------

    def _check_implementation_steps_present(self) -> GateCheck:
        """Ensure the plan has at least one implementation step, unless work is already done."""
        steps = self.plan.get("implementation_steps", [])
        rationale = self.plan.get("design_rationale", "").lower()
        
        # Heuristics for "already complete"
        already_done_keywords = ["already exist", "already complete", "already implemented", "no new files needed", "no implementation changes"]
        is_already_done = any(k in rationale for k in already_done_keywords)

        if not steps:
            if is_already_done:
                return GateCheck(
                    name="implementation_steps_present",
                    passed=True,
                    hard_fail=False,
                    detail="No implementation steps defined, but rationale suggests work is already complete.",
                )
            return GateCheck(
                name="implementation_steps_present",
                passed=False,
                hard_fail=True,
                detail="Planning result has no implementation steps — plan is incomplete.",
            )
        return GateCheck(
            name="implementation_steps_present",
            passed=True,
            hard_fail=True,
            detail=f"{len(steps)} implementation step(s) defined.",
        )

    # ------------------------------------------------------------------
    # Check 2: Circular dependency detection
    # ------------------------------------------------------------------

    def _check_circular_dependencies(self) -> GateCheck:
        """Detect cycles in the proposed dependency graph."""
        dep_graph = self.plan.get("dependency_graph", {})
        if not dep_graph:
            return GateCheck(
                name="circular_dependency",
                passed=True,
                hard_fail=True,
                detail="No dependency graph to check.",
            )

        from app.agent.static_analysis import _detect_cycles
        cycles = _detect_cycles(dep_graph)
        if cycles:
            cycle_strs = [" -> ".join(c) for c in cycles[:3]]
            return GateCheck(
                name="circular_dependency",
                passed=False,
                hard_fail=True,
                detail=f"{len(cycles)} cycle(s): {'; '.join(cycle_strs)}",
            )

        return GateCheck(
            name="circular_dependency",
            passed=True,
            hard_fail=True,
            detail=f"No cycles in {len(dep_graph)} nodes.",
        )

    # ------------------------------------------------------------------
    # Check 3: Test strategy completeness
    # ------------------------------------------------------------------

    def _check_test_strategy(self) -> GateCheck:
        """A test strategy must exist. Coverage matching is advisory only.

        The LLM names test subjects by component name (e.g. "UserService"), not
        by filename (e.g. "app/services/user.py"), so strict filename-to-component
        matching produces false failures. We hard-fail only on a completely absent
        test_strategy; unmatched files are soft-reported for visibility.
        """
        test_strategy = self.plan.get("test_strategy", [])
        if not test_strategy:
            return GateCheck(
                name="test_strategy",
                passed=False,
                hard_fail=True,
                detail="No test strategy defined at all.",
            )

        # Advisory: report files that appear completely unmentioned (soft only)
        file_manifest = self.plan.get("file_manifest", [])
        components_needing_tests = set()
        for entry in file_manifest:
            action = entry.get("action", "")
            path = entry.get("path", "")
            if action in ("create", "modify") and path.endswith(".py") and "test" not in path:
                components_needing_tests.add(path)

        tested_components = set()
        for ts in test_strategy:
            component = ts.get("component", "")
            if component:
                tested_components.add(component)
            for f in ts.get("test_cases", []):
                tested_components.add(f)

        untested = components_needing_tests - tested_components
        if untested:
            # Soft advisory — doesn't block the gate
            logger.debug(
                "[planning_gate] Test strategy advisory: %d file(s) not explicitly named: %s",
                len(untested), ", ".join(sorted(untested)[:5]),
            )

        return GateCheck(
            name="test_strategy",
            passed=True,
            hard_fail=True,
            detail=f"{len(test_strategy)} test strategies defined.",
        )

    # ------------------------------------------------------------------
    # Check 4: Prerequisites resolved
    # ------------------------------------------------------------------

    def _check_prerequisites(self) -> GateCheck:
        """All task prerequisites are in COMPLETED/ACCEPTED state."""
        task_dict = None
        for t in self.all_tasks:
            if t.get("id") == self.task_id:
                task_dict = t
                break

        if not task_dict:
            return GateCheck(
                name="prerequisites_resolved",
                passed=True,
                hard_fail=True,
                detail="Task not found in task list (assuming no prereqs).",
            )

        prereqs = task_dict.get("prerequisites", [])
        if not prereqs:
            return GateCheck(
                name="prerequisites_resolved",
                passed=True,
                hard_fail=True,
                detail="No prerequisites.",
            )

        task_by_id = {t["id"]: t for t in self.all_tasks}
        unfinished = []
        for pid in prereqs:
            ptask = task_by_id.get(pid)
            if not ptask or ptask.get("type", "").lower() not in PIPELINE_DONE_STATUSES:
                unfinished.append(pid)

        if unfinished:
            return GateCheck(
                name="prerequisites_resolved",
                passed=False,
                hard_fail=True,
                detail=f"Unfinished prerequisites: {', '.join(unfinished)}",
            )

        return GateCheck(
            name="prerequisites_resolved",
            passed=True,
            hard_fail=True,
            detail=f"All {len(prereqs)} prerequisites completed.",
        )

    # ------------------------------------------------------------------
    # Check 5: File manifest safety
    # ------------------------------------------------------------------

    def _check_file_safety(self) -> GateCheck:
        """All paths pass _assert_safe_path(), no blocked commands.

        _assert_safe_path() uses the effective_root set by set_task_git_cwd()
        (called by run_planning_gate() before this method runs), which means
        paths are validated against the task's own project root — not
        TheMaestro's source tree.
        """
        import os
        from app.agent.tools import _assert_safe_path

        file_manifest = self.plan.get("file_manifest", [])
        issues = []

        for entry in file_manifest:
            path = entry.get("path", "")
            if path:
                try:
                    # Pass path directly — _assert_safe_path resolves relative
                    # paths against _task_git_cwd (the task's project root).
                    _assert_safe_path(path)
                except ValueError as e:
                    issues.append(str(e))

        if issues:
            return GateCheck(
                name="file_safety",
                passed=False,
                hard_fail=True,
                detail=f"{len(issues)} safety violation(s): {issues[0]}",
            )

        return GateCheck(
            name="file_safety",
            passed=True,
            hard_fail=True,
            detail=f"All {len(file_manifest)} paths validated.",
        )

    # ------------------------------------------------------------------
    # Check 6: Implementation feasibility re-check (LLM)
    # ------------------------------------------------------------------

    async def _check_feasibility(self) -> tuple[GateCheck, int, int, bool]:
        """LLM confirms plan is feasible and respects explicit task constraints.

        Combines general feasibility with spec-compliance detection — the LLM is better
        placed than keyword matching to judge whether a plan *implements* a forbidden
        approach vs. merely *mentions* it while describing what it replaces.

        A confirmed spec violation is returned as hard_fail=True.
        General feasibility concerns are soft-fail only.

        Returns (GateCheck, prompt_tokens, completion_tokens, llm_check_unavailable).
        llm_check_unavailable=True means all retries failed and the check was skipped.
        """
        from app.agent.llm_client import call_llm, extract_text_response

        task_desc_block = (
            f"TASK DESCRIPTION:\n{self.task_description[:1500]}\n\n"
            if self.task_description else ""
        )
        # Acceptance criteria (from clarification draft)
        acceptance_criteria = getattr(self, "acceptance_criteria", None)
        ac_block = ""
        if acceptance_criteria:
            if isinstance(acceptance_criteria, list):
                ac_block = f"ACCEPTANCE CRITERIA (must all be satisfied):\n{json.dumps(acceptance_criteria, indent=1)}\n\n"
            elif isinstance(acceptance_criteria, str):
                try:
                    ac_list = json.loads(acceptance_criteria)
                    if isinstance(ac_list, list):
                        ac_block = f"ACCEPTANCE CRITERIA (must all be satisfied):\n{json.dumps(ac_list, indent=1)}\n\n"
                except (json.JSONDecodeError, ValueError):
                    ac_block = f"ACCEPTANCE CRITERIA:\n{acceptance_criteria}\n\n"

        # Strip pitfalls/advisory fields — they describe risks NOT being implemented and
        # confuse the spec checker into treating "we won't use X" as "we implement X".
        spec_plan = {
            "design_rationale": self.plan.get("design_rationale", ""),
            "file_manifest": [
                {"path": f.get("path"), "action": f.get("action"), "purpose": f.get("purpose")}
                for f in self.plan.get("file_manifest", [])
            ],
            "implementation_steps": [
                {"component": s.get("component"), "description": s.get("description")}
                for s in self.plan.get("implementation_steps", [])
            ],
        }
        domain = getattr(self, "domain", "software")

        if domain == "proof":
            review_body = (
                "Review the plan above on four dimensions:\n\n"
                "1. MATHEMATICAL SOUNDNESS — is the proof strategy logically valid? "
                "Does the plan use a correct proof approach (induction, contradiction, etc.)?\n\n"
                "2. MATHLIB LEMMA VERIFICATION — are cited Mathlib lemmas real and correctly typed? "
                "Flag any lemma names that look invented or incorrectly stated.\n\n"
                "3. ZERO-SORRY COMMITMENT — does the plan explicitly commit to discharging every "
                "proof obligation? Flag any 'sorry' placeholders left intentionally.\n\n"
                "4. BUILD VERIFICATION — does the plan specify `lake build` or an equivalent "
                "step to confirm the Lean4 file compiles without errors?\n"
            )
            critical_rules = ""
        elif domain == "writing":
            review_body = (
                "Review the plan above on three dimensions:\n\n"
                "1. NARRATIVE COHERENCE — does the outline/structure match the task spec?\n\n"
                "2. SCOPE COMPLIANCE — do word count, genre, POV, and other constraints from "
                "the task description match what the plan commits to delivering?\n\n"
                "3. DELIVERABLE COVERAGE — are all chapters, scenes, or sections specified "
                "in the task description addressed in the plan?\n"
            )
            critical_rules = ""
        elif domain == "research":
            review_body = (
                "Review the plan above on three dimensions:\n\n"
                "1. METHODOLOGY SOUNDNESS — is the research approach valid for the stated goal?\n\n"
                "2. CITATION SCOPE — does the plan cover the required topics, sources, or "
                "literature areas specified in the task?\n\n"
                "3. DELIVERABLE COVERAGE — are all sections from the task spec addressed?\n"
            )
            critical_rules = ""
        elif domain == "data_analysis":
            review_body = (
                "Review the plan above on three dimensions:\n\n"
                "1. STATISTICAL VALIDITY — are the proposed analyses appropriate for the data "
                "and research question?\n\n"
                "2. PIPELINE CORRECTNESS — does the plan cover data loading, transformation, "
                "and output in the correct format?\n\n"
                "3. SPEC COMPLIANCE — are all required metrics, visualizations, and output "
                "formats from the task description addressed?\n"
            )
            critical_rules = ""
        else:
            # software / bug_triage / default
            review_body = (
                "Review the plan above on three dimensions:\n\n"
                "1. FEASIBILITY — is the plan technically sound and achievable?\n\n"
                "2. SPEC COMPLIANCE — does the plan's actual implementation use an approach "
                "the task explicitly forbids?\n\n"
                "3. ACCEPTANCE CRITERIA COVERAGE — does the plan's implementation_steps "
                "address every item in the ACCEPTANCE CRITERIA section above?\n"
                "   List any criteria that are NOT addressed by the plan.\n"
            )
            critical_rules = (
                "\n   CRITICAL RULES:\n"
                "   - Read ONLY the file_manifest actions and implementation_steps descriptions "
                "to judge what the code will DO. Ignore any mentions of approaches being "
                "avoided, replaced, warned about, or listed as risks — those are advisory notes.\n"
                "   - 'action: verify' means checking existing code — it is NOT implementing anything.\n"
                "   - A plan that says 'replace X with Y' implements Y, not X. Only flag if Y "
                "is the forbidden approach.\n"
                "   - If the implementation_steps say 'naive recursion' or 'fib(n-1) + fib(n-2)', "
                "that IS compliant with a naive-recursion requirement — do NOT flag it.\n"
            )

        prompt = (
            f"{task_desc_block}{ac_block}"
            f"PLAN (implementation fields only):\n{json.dumps(spec_plan, indent=1)[:3000]}\n\n"
            f"{review_body}"
            f"{critical_rules}\n"
            "To complete your review, call the submit_work tool with:\n"
            "{\n"
            '  "signal": "ACCEPTED",\n'
            '  "summary": "Your justification",\n'
            '  "payload": {\n'
            '    "feasible": true/false,\n'
            '    "spec_violation": true/false,\n'
            '    "spec_violation_detail": "which constraint is violated and how" or "",\n'
            '    "acceptance_criteria_coverage": "which criteria are met, which are missing" or "",\n'
            '    "concerns": ["concern 1", ...]\n'
            '  }\n'
            "}"
        )
        messages = [
            {"role": "system", "content": "You are a feasibility and spec-compliance reviewer. Use submit_work to output your result when ready."},
            {"role": "user", "content": prompt},
        ]

        max_attempts = 3
        last_error: Exception | None = None

        from app.agent.tools import build_tool_schemas, dispatch_tool
        gate_tools = build_tool_schemas(["submit_work"])

        for attempt in range(1, max_attempts + 1):
            if is_shutting_down():
                raise ShutdownError("Server is shutting down")

            try:
                response = await call_llm(
                    messages,
                    base_url=self.llm_base_url,
                    model=self.llm_model,
                    tools=gate_tools,
                    tool_choice="auto",
                    task_id=self.task_id,
                    llm_id=self.llm_id,
                    budget_id=self.budget_id,
                    agent_name=AGENT_NAME,
                )
                usage = response.get("usage", {})
                pt = usage.get("prompt_tokens", 0)
                ct = usage.get("completion_tokens", 0)

                assistant_msg = response.get("choices", [{}])[0].get("message", {})
                tool_calls = assistant_msg.get("tool_calls") or []

                if not tool_calls:
                    messages.append(assistant_msg)
                    messages.append({"role": "user", "content": "You must call submit_work to output your result."})
                    continue

                for tc in tool_calls:
                    tc_result = dispatch_tool(
                        tc["function"]["name"],
                        json.loads(tc["function"]["arguments"]) if isinstance(tc["function"]["arguments"], str) else tc["function"]["arguments"],
                    )

                    if isinstance(tc_result, str) and "__maestro_terminal__" in tc_result:
                        data = json.loads(tc_result)
                        payload = data.get("payload", {})
                        feasible = payload.get("feasible", True)
                        spec_violation = payload.get("spec_violation", False)
                        spec_detail = payload.get("spec_violation_detail", "")
                        concerns = payload.get("concerns", [])

                        if spec_violation:
                            detail = f"Spec violation: {spec_detail}"
                            if concerns:
                                detail += f"; {'; '.join(concerns)}"
                            return GateCheck(
                                name="feasibility_recheck",
                                passed=False,
                                hard_fail=True,
                                detail=detail,
                            ), pt, ct, False

                        return GateCheck(
                            name="feasibility_recheck",
                            passed=feasible,
                            hard_fail=False,
                            detail=f"{'Feasible' if feasible else 'Concerns'}: {'; '.join(concerns) if concerns else 'none'}",
                        ), pt, ct, False

            except Exception as e:
                last_error = e
                logger.warning(
                    "[planning_gate] Gate check 6: LLM call failed (attempt %d/%d): %s",
                    attempt, max_attempts, e,
                )
                if attempt < max_attempts:
                    await asyncio.sleep(2 ** attempt)  # 2s, 4s

        logger.warning(
            "[planning_gate] Task '%s': feasibility_recheck unavailable after %d attempts - "
            "proceeding with warning. Last error: %s",
            self.task_id, max_attempts, last_error,
        )
        return GateCheck(
            name="feasibility_recheck",
            passed=True,
            hard_fail=False,
            detail=f"LLM feasibility recheck unavailable after {max_attempts} attempts",
        ), 0, 0, True

    # ------------------------------------------------------------------
    # Check 7: Context budget
    # ------------------------------------------------------------------

    def _check_context_budget(self) -> GateCheck:
        """Each implementation step fits within max_context * (1 - safety_margin)."""
        budget = int(self.max_context * (1 - PLANNING_GATE_CONTEXT_SAFETY_MARGIN))
        steps = self.plan.get("implementation_steps", [])
        oversized = []

        for step in steps:
            est = step.get("estimated_context_tokens", 0)
            if est > budget:
                component = step.get("component", "unknown")
                oversized.append(f"{component} ({est} > {budget})")

        if oversized:
            return GateCheck(
                name="context_budget",
                passed=False,
                hard_fail=True,
                detail=f"Oversized steps: {', '.join(oversized[:3])}",
            )

        return GateCheck(
            name="context_budget",
            passed=True,
            hard_fail=True,
            detail=f"All {len(steps)} steps within budget ({budget} tokens).",
        )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def run_planning_gate(
    task_id: str,
    planning_result: dict,
    all_tasks: list[dict],
    *,
    max_context: int | None = None,
    llm_base_url: str | None = None,
    llm_model: str | None = None,
    llm_id: int | None = None,
    budget_id: int | None = None,
    project_path: str | None = None,
    task_description: str = "",
    domain: str = "software",
) -> dict:
    """Run the planning gate and return a result dict."""
    if project_path is not None:
        from app.agent.tools import set_task_git_cwd
        set_task_git_cwd(project_path)
    gate = PlanningGate(
        task_id=task_id,
        planning_result=planning_result,
        all_tasks=all_tasks,
        max_context=max_context,
        llm_base_url=llm_base_url,
        llm_model=llm_model,
        llm_id=llm_id,
        budget_id=budget_id,
        project_path=project_path,
        task_description=task_description,
        domain=domain,
    )
    result = await gate.run()
    return {
        "task_id": result.task_id,
        "passed": result.passed,
        "checks": [
            {"name": c.name, "passed": c.passed, "hard_fail": c.hard_fail, "detail": c.detail}
            for c in result.checks
        ],
        "prompt_tokens": result.prompt_tokens,
        "completion_tokens": result.completion_tokens,
        "llm_check_unavailable": result.llm_check_unavailable,
    }
