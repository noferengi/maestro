# Test Conventions — app/tests/

This file explains how tests in this directory are isolated, what to mock and what not to,
and why each pattern exists. Read this before adding a new test file.

---

## Database Isolation

### The default: conftest.py handles everything

`conftest.py` sets `MAESTRO_TEST_DB` at **module level** (during pytest collection, before any
test module is imported). This redirects every `import app.database` to use `data/test.db`
instead of the production `data/kanban.db`. You do not need to do anything special — just
import and call database functions normally.

The session-scoped autouse fixture `_test_schema()` then:
1. Applies all pending migrations via the real migration runner (same path as production)
2. Truncates every data table (preserving `schema_migrations`) so the session starts clean

`test.db` is left on disk after the run for failure inspection. It is gitignored.

The function-scoped autouse fixture `_db_rollback()` wraps **every individual test** in a
transaction that is rolled back on teardown. Each test sees a clean database and leaves no
state behind. Tests do not share state — you do not need to clean up rows or use unique
IDs to avoid collisions.

### When to use a per-test isolated database instead

Use `tmp_path` + `monkeypatch.setenv` + `importlib.reload` when:
- You are testing **database module reload behavior** (e.g., testing that the engine
  correctly re-points after an env var change)
- Your test needs a completely separate schema from `test.db` for structural reasons

Pattern (from `test_research_jobs.py`):
```python
def test_something(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("MAESTRO_TEST_DB", db_path)

    import importlib
    import app.database as db_mod
    importlib.reload(db_mod)
    from migrations.runner import migrate as run_migrate, ConnectionWrapper
    with db_mod.engine.begin() as _conn:
        run_migrate(ConnectionWrapper(_conn, is_postgres=False))

    # Now use db_mod functions directly — fully isolated SQLite
    job = db_mod.create_research_job(...)
```

**Why migration runner, not `Base.metadata.create_all`:** `create_all` builds the schema
from current ORM model definitions only — it bypasses the 65-migration history. A migration
that back-fills data or has a specific column default would not be reflected. Always use
the migration runner for schema setup so the test database matches production exactly.

**Why `db_mod.engine` not `get_connection()`:** `runner.get_connection()` reads
`ADMIN_DATABASE_URL` from config, which may resolve to the production Postgres URL if
`use_postgres = true`. `db_mod.engine` is the engine created by the reloaded `session.py`
from `MAESTRO_TEST_DB`, so it always points at the isolated `tmp_path` file.

**Important:** `importlib.reload` is necessary here because `app.database` builds its
`engine` and `SessionLocal` at import time from `MAESTRO_TEST_DB`. Without the reload, the
module object still holds the engine pointing at the old path. After `monkeypatch.setenv`,
the env var is set but the already-imported module doesn't re-read it.

**Also important:** When using this pattern, pass `llm_id=None, budget_id=None` to any CRUD
function that has FK references to the `llms` or `budgets` tables — those rows don't exist
in the freshly-created schema. Passing real IDs causes `IntegrityError: FOREIGN KEY constraint
failed`.

---

## Git Isolation

**The TheMaestro repository must never have test git commands run against it.**

The agent's git tools (`git_checkout`, `git_commit`, `git_diff`, etc.) operate inside a
project path, not the TheMaestro repo. But test code runs inside the TheMaestro repo. If
a test accidentally calls `git_commit()` without redirecting the working directory, it will
commit to TheMaestro's own git history.

### How to isolate git in tests

**Option 1 — `tmp_path` with real git init** (for integration tests that need real git):
```python
def test_checkpoint(tmp_path):
    repo = tmp_path / "project"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)])
    # ... work in repo, not in TheMaestro
```
See `test_integration.py` for the full pattern.

**Option 2 — `_task_git_cwd` ContextVar** (for testing path safety logic):
```python
from app.agent.tools import _task_git_cwd

token = _task_git_cwd.set(str(tmp_path))
try:
    result = git_checkout("main")
    # git tools now consider tmp_path as the project root
finally:
    _task_git_cwd.reset(token)
```
See `test_tools_safety.py`. This is for testing **whether** the allowlist/blocklist fires
correctly — not for running real git commands.

**Option 3 — Don't call git tools at all** (for agent logic tests):
Most agent unit tests (research, intake, planning) never call git tools. Patch `call_llm`
to control LLM responses. Git operations are an orthogonal concern tested elsewhere.

### What NOT to do
- Do not call `tools.git_commit()`, `tools.git_checkout()`, etc. in a test without first
  setting `_task_git_cwd` to a `tmp_path` directory that was `git init`'d.
- Do not `os.chdir()` to a temp directory hoping git commands will stay contained — the
  tools use their own path resolution, not the process CWD.

---

## LLM / HTTP Mocking

LLM calls are always mocked in tests. There is no test mode that hits a real LLM endpoint.
Choose the right level based on what you are testing.

### Level 1 — Patch `call_llm` directly (most tests)

Use this when you are testing **agent logic** — verdict extraction, life management, vote
tallying, pipeline stage transitions. You don't care about the HTTP layer; you just need
the LLM to return a specific string.

**Research agent** — use `_sequential_llm()`:
```python
from unittest.mock import patch

def _sequential_llm(*responses):
    """Returns an async callable that yields responses in order (last repeats)."""
    calls = list(responses)
    async def _call(*a, **kw):
        return calls.pop(0) if len(calls) > 1 else calls[0]
    return _call

def _llm_resp(content, finish_reason="stop"):
    return {"choices": [{"message": {"content": content, "tool_calls": None},
                         "finish_reason": finish_reason}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

# In a test:
with patch("app.agent.research.call_llm", _sequential_llm(
    _llm_resp("I'll investigate..."),     # turn 1
    _llm_resp('{"verdict": "POSSIBLE"}'), # epilogue
)):
    result = asyncio.run(agent.run())
```

**Intake pipeline** — use the `_SequentialCallLLM` class pattern (see
`test_intake_pipeline.py`). Intake calls `call_llm` multiple times across stages; the class
keeps a call counter.

**Patch target:** Always patch on the **module where `call_llm` is called**, not where it is
defined.
- `"app.agent.research.call_llm"` — for research agent tests
- `"app.agent.intake.call_llm"` — for intake pipeline tests
- `"app.agent.file_summary_agent.execute_file_summary"` (or `app.agent.llm_client.call_llm`)
  — for file summary tests

Patching `"app.agent.llm_client.call_llm"` directly works only if the module under test
imported `call_llm` via a lazy `from app.agent.llm_client import call_llm` inside a function
body (which resolves the attribute at call time). If it imported at module level, the local
name is already bound and the patch won't intercept it.

### Level 2 — Patch `httpx.AsyncClient` (llm_client tests only)

Use this when you are testing **`llm_client.py` itself** — payload construction, budget
entry creation, error handling. You need to intercept at the HTTP level to inspect the
request body.

```python
def _make_mock_client(post_return):
    mock_response = MagicMock()
    mock_response.json.return_value = post_return
    mock_response.raise_for_status = MagicMock()
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    mock_client.post = AsyncMock(return_value=mock_response)
    mock_cls = MagicMock(return_value=mock_client)
    return mock_cls

with patch("httpx.AsyncClient", _make_mock_client(response_body)):
    result = asyncio.run(call_llm(messages, ...))
```

Do not use this level for agent tests — it's too low and tests the wrong thing.

### When you do NOT need to mock the LLM

- Pure unit tests: verdict tallying (`test_verdicts.py`), DAG resolution (`test_dag_resolver.py`),
  JSON extraction (`test_json_utils.py`), static analysis (`test_static_analysis.py`).
  These functions do not call `call_llm`. No mock needed.
- Database CRUD tests: `create_task`, `get_task`, `update_research_job`, etc. are tested
  against the real test DB. No LLM involved.
- Path safety and tool tests (`test_tools_safety.py`): tests the blocklist and allowlist
  logic, not LLM calls.

---

## File I/O and PROJECT_ROOT

Agent tools enforce a path-containment check: all file operations must be inside
`PROJECT_ROOT`. Tests that call `read_file()`, `write_file()`, `list_files()`, etc. must
redirect this root to `tmp_path`, or the test file paths will fail the containment check.

```python
@pytest.fixture(autouse=True)
def patch_project_root(tmp_path, monkeypatch):
    monkeypatch.setattr("app.agent.tools.PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr("app.agent.tools.ARCHIVE_DIR", str(tmp_path / ".archive"))
    return tmp_path
```

This is already done as `autouse=True` in `test_read_file_redesign.py`. Add a similar
fixture to any new test file that exercises file tools.

---

## ContextVar Isolation

Two `ContextVar`s in the agent system need explicit reset between tests:

### `_prepped_files` (app.agent.tools)

Tracks which files the agent has "prepped" (called `read_file()` on) in the current task
context. Must be reset between tests — otherwise another `read_file()` call in test B will
see files prepped by test A.

```python
@pytest.fixture(autouse=True)
def reset_prepped_files():
    from app.agent.tools import _prepped_files
    _prepped_files.set(None)
    yield
    _prepped_files.set(None)
```

### `_task_git_cwd` (app.agent.tools)

Overrides the working directory for git tool calls. Set it to a `tmp_path` git repo when
testing git operations; reset it in `finally`.

### `_file_summary_cache` (app.agent.project_snapshot)

Not a ContextVar — it's a plain module-level dict shared across the whole process. Clear it
explicitly in tests that call `async_build_file_summary()`.

```python
@pytest.fixture(autouse=False)   # opt-in, not autouse
def clean_session_cache():
    from app.agent import project_snapshot
    project_snapshot._file_summary_cache.clear()
    yield
    project_snapshot._file_summary_cache.clear()
```

**Why opt-in?** Most tests don't call `async_build_file_summary()`. Clearing the cache in
every test would be silent overhead. Only include `clean_session_cache` as a parameter in
tests that explicitly exercise this path.

**Key detail:** `async_build_file_summary()` caches combined (LLM-enhanced) results under
`("llm", abs_path, mtime, size)` — with the `"llm"` prefix. `build_file_summary()` (sync)
caches structural-only results under `(abs_path, mtime, size)` — no prefix. These are
intentionally different keys. When pre-populating the cache in a test, use the right key for
what you are simulating.

---

## Monkeypatching: Rules of Thumb

### Patch on the module that *calls* the function, not where it's *defined*

```python
# WRONG — patches the definition site; local binding in research.py is unaffected
monkeypatch.setattr("app.agent.llm_client.call_llm", fake)

# RIGHT — patches where research.py will import it from
monkeypatch.setattr("app.agent.research.call_llm", fake)
```

This applies to any `from X import Y` at module level in the production code. If the
production code uses a lazy import *inside a function body* (`from X import Y` inside `def`
or `async def`), then patching the attribute on module `X` works because the import resolves
at call time:

```python
# Production code does lazy import inside function body:
async def async_build_file_summary(...):
    from app.agent.file_summary_agent import enqueue_file_summary  # resolves NOW
    ...

# Test can patch the attribute on the source module:
monkeypatch.setattr("app.agent.file_summary_agent.enqueue_file_summary", fake)
# OR equivalently:
import app.agent.file_summary_agent as fsa
monkeypatch.setattr(fsa, "enqueue_file_summary", fake)
```

### Prefer patching the module object over string paths when the target doesn't exist

`monkeypatch.setattr("a.b.c", value, raising=True)` (the default) will raise if `c` doesn't
exist on `a.b`. Avoid `raising=False` — it silently creates an attribute that may confuse
future readers and can introduce subtle bugs (see history: the `project_snapshot.get_file_summary`
incident where `raising=False` broke a test in ways that were hard to diagnose).

### Don't patch things you don't need to patch

If the real implementation is harmless in tests, use it. Examples:
- `wait_for_completion()` from the scheduler: its real implementation returns `True`
  immediately when the key isn't in the registry. If your test's fake `enqueue_file_summary`
  never registers an event, the real `wait_for_completion` already does the right thing —
  no patch needed.
- Database CRUD: use the real functions against `test.db`. Only mock DB functions when you
  are testing something that calls them as a side effect and you want to isolate from the DB
  entirely (e.g., testing the scheduler's `_run_file_summary_job` without a running DB).

---

## What Never Needs Mocking

| What | Why |
|------|-----|
| `tally_votes()`, `Vote`, `TallyResult` | Pure functions, no I/O |
| `DAGResolver` | Pure algorithm, no I/O |
| `extract_json_block()`, `parse_json_block()` | Pure string functions |
| `analyze_file()` (tree-sitter) | Reads a real file; use a real `tmp_path` file |
| `build_file_summary()` (sync structural summary) | Reads a real file; use `tmp_path` |
| SQLAlchemy CRUD (`create_task`, etc.) | Use `test.db` via conftest |
| `get_or_create_completion_event()`, `signal_completion()`, `wait_for_completion()` | In-memory threading, safe and fast |

---

## Summary Decision Tree

```
Writing a new test — what do I need to isolate?

1. Does it call call_llm / hit an LLM?
   YES → patch at the module-call level with _sequential_llm() or similar
   NO  → no LLM mock needed

2. Does it read/write files?
   YES → use tmp_path + patch PROJECT_ROOT + patch ARCHIVE_DIR
   NO  → no file mock needed

3. Does it run git commands?
   YES, real git needed → use tmp_path, run `git init` there, set _task_git_cwd
   YES, testing allowlist logic only → set _task_git_cwd to tmp_path (no init needed)
   NO  → no git isolation needed

4. Does it read/write the database?
   YES, standard CRUD → conftest.py handles it; just use app.database normally
   YES, testing DB init/reload behavior → tmp_path + monkeypatch.setenv + importlib.reload
   NO  → no DB isolation needed

5. Does it call async_build_file_summary()?
   YES → add clean_session_cache fixture; use ("llm", abs_path, mtime, size) cache keys
   NO  → no session cache isolation needed

6. Does it use _prepped_files or _task_git_cwd ContextVars?
   YES → reset in autouse fixture (see test_read_file_redesign.py for the pattern)
   NO  → no ContextVar reset needed
```
