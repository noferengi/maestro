# Tool Philosophy — `app/agent/tools.py`

This document is the canonical reference for how Maestro agent tools are designed,
what safety guarantees they provide, and what every new tool must implement.

---

## Core principles

### No shell access

Every subprocess invocation uses `shell=False` (immutable — never change this).
Arguments are passed as a fixed `list[str]`, never as a shell command string.
This means:

- Shell metacharacters (`|`, `&`, `;`, `>`, `$(...)`, backticks) are inert.
- Chaining is impossible at the OS level — the LLM cannot chain commands by
  smuggling them into an argument.
- Injection attacks have no attack surface.

If a tool needs to run an external program, it calls `_run_tool_subprocess(args, cwd, ...)`.
The `args` list is always constructed from validated, individually-validated tokens,
never from a raw user-supplied string.

### No command chaining

Each tool does exactly one operation.  Tools are intentionally narrow:
`run_test_pytest` runs pytest, `run_check_mypy` runs mypy — they are separate tools,
not flags on a generic `run_shell` tool.  The LLM orchestrates sequences of
single-operation calls; the tool layer does not orchestrate on its behalf.

The old `run_shell_indev` / `run_shell_build` / `run_shell_security` groupings are
gone.  Per-stage access is controlled by `build_tool_schemas(allowed_names)` in
`config.py` — only the explicitly listed tools appear in the LLM's schema.

### Pipelines via composition

Complex workflows (lint → fix → test → commit) are handled by the LLM sequencing
individual tool calls.  The tools themselves do not have `then:` or `pipeline:`
arguments.  This keeps individual tools auditable, testable, and safe.

---

## Universal output filters

Every tool that produces multi-line output **must** support these keyword arguments:

| Param | Type | Meaning |
|---|---|---|
| `head` | `int \| None` | Keep only the first N lines |
| `tail` | `int \| None` | Keep only the last N lines |
| `grep` | `str \| None` | Keep only lines matching this regex (case-insensitive) |

These are applied via `_slice_output(raw, head=head, tail=tail, grep=grep)`.

The order is: grep first, then head or tail.  `head` and `tail` are mutually
exclusive; if both are supplied, `head` wins.

`read_last_output` additionally supports `offset` (skip first N lines) and
`limit` (keep at most N lines after offset), applied before grep.

### Guarantee

Any tool that might produce output exceeding ~50 lines **must** accept and pass
through `head`, `tail`, and `grep`.  This is a hard requirement for all new tools
and a debt item for existing tools that are missing it.  See the compliance table
below.

### Corresponding schema entries

Every `head`/`tail`/`grep` parameter must be exposed in the tool's `TOOL_SCHEMAS`
entry.  The standard property block is:

```python
"head":  {"type": "integer", "description": "Return only the first N output lines."},
"tail":  {"type": "integer", "description": "Return only the last N output lines."},
"grep":  {"type": "string",  "description": "Filter output lines matching this regex/substring."},
```

The tool description string must include the phrase `head/tail/grep filter output`
so the LLM knows the parameters exist before reading the schema detail.

---

## Output cap

All tool results pass through `_cap_tool_result(name, result)` in the dispatcher,
which hard-truncates at 200 KiB with an appended notice.  This is a backstop, not
a substitute for head/tail/grep on verbose tools.

---

## Safety layers

| Layer | Where | What it enforces |
|---|---|---|
| Read path guard | `_assert_safe_path(path)` | No `.git` internals, no `.archive` contents |
| Write path guard | `_assert_safe_write_path(path)` | Must be inside `effective_root`; blocks venv, __pycache__, node_modules etc. |
| Flag allowlist | `_validate_flags(flags, tool, allowed, value_flags)` | Only whitelisted flags pass to subprocesses; shell metacharacters blocked |
| Shell metachar regex | `_SHELL_METACHAR_RE` | Rejects `|`, `&`, `;`, `>`, `<`, `$`, backticks, `(`, `)` |
| Git branch guard | `write_git_checkout`, `write_git_branch` | Only `maestro/task-*` branches permitted |
| Git repo guard | `_is_inside_maestro_repo(cwd)` | Agents cannot git-operate on Maestro's own repo |
| Self-protection | `_assert_safe_write_path` | Writes to Maestro's own source tree are blocked |

---

## `_slice_output` — canonical filter function

```python
def _slice_output(
    text: str,
    *,
    head: int | None = None,
    tail: int | None = None,
    grep: str | None = None,
    offset: int | None = None,
    limit: int | None = None,
) -> str:
    """Apply head/tail/grep/offset/limit filters to a multi-line string."""
```

Located at `tools.py` line ~1332.  All output-producing tools call this.  Never
implement custom slicing inline — always delegate to `_slice_output`.

---

## TOOL_REGISTRY vs TOOL_SCHEMAS

`TOOL_REGISTRY: dict[str, Callable]` maps tool name → Python function for dispatch.
`TOOL_SCHEMAS: list[dict]` is the JSON schema list handed to the LLM.

**Both must be updated together.** A tool in `TOOL_SCHEMAS` but not `TOOL_REGISTRY`
will silently appear in the LLM's schema but return `ERROR: Unknown tool` at runtime.
A tool in `TOOL_REGISTRY` but not `TOOL_SCHEMAS` is invisible to the LLM.

`build_tool_schemas(allowed_names)` filters `TOOL_SCHEMAS` by name — it silently
drops unknown names.  If you rename a tool, update every `allowed_tools` list that
references the old name (including seeded `custom_agent_definitions` rows in
migrations).

`TOOL_CATEGORIES: dict[str, str]` is a documentation-only grouping used by the
pipeline editor UI.  It must also be kept in sync with name changes.

---

## Compliance table

Tools marked ✗ are missing head/tail/grep and are tech debt.

### Read / search tools

| Tool | head/tail/grep | Notes |
|---|---|---|
| `read_file` | n/a | windowed 250-line reader, own pagination |
| `read_file_metadata` | n/a | single-record output |
| `read_last_output` | ✓ + offset/limit | re-slices previous tool output |
| `find_files` | ✗ | up to 200 path lines |
| `find_in_files` | ✓ | |
| `list_directory` | ✗ | can be large for deep dirs |

### Git read tools

| Tool | head/tail/grep | Notes |
|---|---|---|
| `read_git_status` | ✗ | usually short, but can grow |
| `read_git_diff` | ✓ (fn) / ✗ (schema) | schema doesn't expose params yet |
| `read_git_log` | ✓ (fn) / ✗ (schema) | schema doesn't expose params yet |
| `read_git_blame` | ✗ | often very long |
| `read_git_show` | ✗ | can be very long |
| `read_diff_stat` | ✗ | usually short |

### Testing tools

| Tool | head/tail/grep |
|---|---|
| `run_test_pytest` | ✓ |
| `run_test_unittest` | ✗ |
| `run_test_npm` | ✗ |
| `run_test_cargo` | ✗ |
| `run_test_go` | ✗ |
| `read_test_summary` | n/a (structured JSON) |

### Code quality tools

| Tool | head/tail/grep |
|---|---|
| `run_check_mypy` | ✗ |
| `run_check_ruff` | ✗ |
| `run_check_black` | ✗ |

### Build tools

| Tool | head/tail/grep |
|---|---|
| `run_build_make` | ✗ |
| `run_build_cargo` | ✗ |
| `run_build_go` | ✗ |
| `run_build_npm` | ✗ |
| `run_build_tsc` | ✗ |
| `run_build_gradle` | ✗ |
| `run_build_mvn` | ✗ |

### Dependency tools

| Tool | head/tail/grep |
|---|---|
| `run_deps_pip` | ✗ |
| `run_deps_npm` | ✗ |
| `run_deps_cargo` | ✗ |

### Security audit tools

| Tool | head/tail/grep |
|---|---|
| `run_audit_bandit` | ✗ |
| `run_audit_pip` | ✗ |
| `run_audit_semgrep` | ✗ |
| `run_audit_npm` | ✗ |

### Infrastructure / diagnostic tools

| Tool | head/tail/grep | Notes |
|---|---|---|
| `get_system_health` | ✗ | |
| `consult_maestro` | n/a (short structured output) | |
| `read_log_window` | ✓ | reads from `logs/maestro.log`; anomaly counts in header |
| `get_budget_history` | ✓ (detail table) | summary header always included |

### Pipeline management tools

| Tool | head/tail/grep | Notes |
|---|---|---|
| `list_pipelines` | n/a | compact JSON listing |
| `get_pipeline` | n/a | full JSON for one template |
| `clone_pipeline` | n/a | write |
| `update_pipeline` | n/a | write; merges metadata, bumps version |
| `update_pipeline_stage` | n/a | write; merges config keys — never overwrites full config |
| `assign_project_pipeline` | n/a | write; auto-migrates cards with invalid stage_key |
| `transfer_pipeline_cards` | n/a | write; explicit stage_key map |

---

## Known discrepancies (tech debt)

1. **Migration 0079 stale tool names** — `allowed_tools` in the seeded
   `custom_agent_definitions` rows use pre-rename names that no longer exist:

   | Stale name | Current name |
   |---|---|
   | `search_files` | `find_in_files` or `find_files` |
   | `run_pytest` | `run_test_pytest` |
   | `run_mypy` | `run_check_mypy` |
   | `run_ruff` | `run_check_ruff` |
   | `run_black_check` | `run_check_black` |
   | `run_bandit` | `run_audit_bandit` |
   | `run_pip_audit` | `run_audit_pip` |
   | `git_restore` | `write_git_restore` |

   `build_tool_schemas` silently drops unknowns — affected agents receive empty
   tool sets for these capabilities without any error at startup.

2. **`consult_maestro` missing from TOOL_REGISTRY** — function defined, schema
   exists, listed in TOOL_CATEGORIES, but absent from TOOL_REGISTRY.  A call to
   it returns `ERROR: Unknown tool`.

3. **`git_add` / `git_unstage`** — referenced in migration 0079 but do not exist
   under any name.  `write_git_commit` does an implicit `git add -A` internally;
   explicit staged-commit workflow is not available.

---

## Adding a new tool — checklist

- [ ] Function in `tools.py` using `_assert_safe_path` / `_assert_safe_write_path`
- [ ] If it runs a subprocess: use `_run_tool_subprocess(args, cwd, ...)` with `shell=False`
- [ ] If output can exceed ~50 lines: add `head`/`tail`/`grep` params, call `_slice_output`
- [ ] Entry in `TOOL_REGISTRY` (name → function)
- [ ] Entry in `TOOL_SCHEMAS` with all params documented, including head/tail/grep if applicable
- [ ] Entry in `TOOL_CATEGORIES` (for the pipeline editor UI)
- [ ] If the tool should be available to custom agents: add its name to the relevant
     `allowed_tools` list in `custom_agent_definitions` or the pipeline editor UI
