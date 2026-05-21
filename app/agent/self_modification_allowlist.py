"""
Paths the _maestro_self project is allowed to write to.

THIS FILE ITSELF IS ON THE LIST. Edits to this file are significant: they change
what Maestro can write to. Every such edit will appear prominently in git diffs
and human review. Edit with care.

HARD_BLOCKED paths are permanently off-limits regardless of ALLOWED_PATHS.
The HARD_BLOCKED check fires at write time even with all capability toggles enabled.
"""

from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent  # repo root


def _p(*parts: str) -> str:
    return str((_ROOT / Path(*parts)).resolve())


ALLOWED_PATHS: frozenset[str] = frozenset({
    # This file — explicitly self-referential.
    _p("app/agent/self_modification_allowlist.py"),

    # Agent system
    _p("app/agent/loop.py"),
    _p("app/agent/config.py"),
    _p("app/agent/verifiers.py"),
    _p("app/agent/pipeline_router.py"),
    _p("app/agent/consult_agent.py"),
    _p("app/agent/tools_math.py"),
    _p("app/agent/sandbox.py"),

    # Database layer
    _p("app/database/crud_autopilot.py"),
    _p("app/database/crud_tasks.py"),
    _p("app/database/crud_pipeline.py"),
    _p("app/database/crud_malleable.py"),
    _p("app/database/crud_documents.py"),

    # Frontend
    _p("app/web/kanban.js"),
    _p("app/web/index.html"),
    _p("app/web/style.css"),
    _p("app/web/pipeline_editor.js"),
    _p("app/web/pipeline_editor.html"),
    _p("app/web/pipeline_editor.css"),

    # Tests (agents may add tests for their own changes)
    _p("app/tests/test_self_modification.py"),
    _p("app/tests/test_consult_maestro.py"),
    _p("app/tests/test_autopilot_objectives.py"),
    _p("app/tests/test_math_tools.py"),
    _p("app/tests/test_objective_hierarchy.py"),
})

# Permanently off-limits regardless of the allowlist above.
# These paths cannot be written to even with all capability toggles enabled.
HARD_BLOCKED: frozenset[str] = frozenset({
    _p("app/agent/tools.py"),      # contains _assert_safe_write_path itself
    _p("app/agent/workspace.py"),  # deletion audit module
    _p("app/migrations"),          # never auto-generate migrations
    _p(".env"),
    _p("maestro.ini"),             # config must remain human-controlled
    _p("app/tests/conftest.py"),
})
