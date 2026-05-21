# Gap 3 — Math / formal proof tooling

**Status:** Complete  
**Effort:** Medium-Large  
**Priority:** Medium — required for mathematics pipeline to be more than decorative

---

## Problem

The Mathematics / Proof Exploration pipeline template exists and the `python_sympy`
verifier works (subprocess, 30s timeout). Lean4 and Coq verifiers are stubs that
return `False` unconditionally. Agents have no tool for writing and executing symbolic
math code interactively — they can only have code verified at stage gates, not iterate
on it mid-session. There are no tools for searching mathematical literature.

---

## Design decisions (settled)

| Decision | Resolution |
|---|---|
| **SymPy execution** | Dedicated `run_sympy` tool — agent passes code string, receives stdout+stderr inline, no filesystem write. For committing a final result: `write_file` + `run_pytest` as usual. |
| **Sandbox** | Docker container: no network, memory-capped (512 MB default), complete host isolation. Agents cannot affect the host environment. Docker Desktop must be running on the host. |
| **Lean4 / Coq** | Same Docker image as SymPy — one `sympy-lean4-sandbox` image. Lean4 CLI included. Full error output (stderr) returned to agent on failure so it can read type-checker feedback and correct. |
| **Literature** | Dedicated `search_arxiv` and `search_oeis` tools returning clean structured records. No raw XML for agents to parse. |
| **Problem scope** | All four modes are valid simultaneous goals: (a) computational exploration to a bound, (b) formalization of known partial results, (c) novel hypothesis generation driven by autopilot (Gap 2), (d) calibration via known-answer conjectures. Any forward progress in any direction is counted. |

---

## Implementation plan

### Phase 1 — Docker sandbox image

**`docker/sympy-lean4-sandbox/Dockerfile`** — new file:

```dockerfile
FROM python:3.12-slim

# Python math libraries
RUN pip install --no-cache-dir sympy numpy scipy mpmath

# Lean4 via elan
RUN apt-get update && apt-get install -y curl git && \
    curl -sSf https://raw.githubusercontent.com/leanprover/elan/master/elan-init.sh \
    | sh -s -- -y --default-toolchain leanprover/lean4:stable && \
    rm -rf /var/lib/apt/lists/*
ENV PATH="/root/.elan/bin:$PATH"

# Coq (optional — degrade gracefully if absent)
RUN apt-get update && apt-get install -y coq && rm -rf /var/lib/apt/lists/* || true

WORKDIR /sandbox
USER nobody
```

**`app/agent/sandbox.py`** — new file, encapsulates all container execution:

```python
SANDBOX_IMAGE = "sympy-lean4-sandbox:latest"
DEFAULT_TIMEOUT = 120       # seconds; configurable per call
DEFAULT_MEMORY  = "512m"

def run_in_sandbox(code: str, lang: str = "python",
                   timeout: int = DEFAULT_TIMEOUT) -> dict:
    """
    Run code in the isolated Docker container.
    lang: "python" | "lean4" | "coq"
    Returns: {ok: bool, stdout: str, stderr: str, timed_out: bool}
    """
    ...
```

- Uses `docker run --rm --network none --memory {DEFAULT_MEMORY} --cpus 1`
- On Windows: code passed via a temp file mounted read-only (avoids shell quoting issues)
- Captures stdout + stderr separately
- On timeout: kills container, sets `timed_out: True` in result
- If Docker is not running: returns `{ok: False, error: "Docker is not available. Start Docker Desktop."}`

**Build script** (`scripts/build_sandbox.py` or `Makefile` target): `docker build -t sympy-lean4-sandbox:latest docker/sympy-lean4-sandbox/`

---

### Phase 2 — `run_sympy` agent tool

**`app/agent/tools.py`** — add to TOOL_REGISTRY:

```python
"run_sympy": {
    "fn": handle_run_sympy,
    "schema": {
        "name": "run_sympy",
        "description": "Execute Python/SymPy code for mathematical exploration. "
                       "Returns stdout and stderr. Use for scratch computation; "
                       "commit final results via write_file + run_pytest.",
        "parameters": {
            "code":    {"type": "string", "description": "Python source to execute"},
            "timeout": {"type": "integer", "description": "Max seconds (default 120, max 600)"}
        },
        "required": ["code"]
    }
}
```

`handle_run_sympy(code, timeout=120)`:
- Clamps timeout to `[10, 600]`
- Calls `run_in_sandbox(code, lang="python", timeout=timeout)`
- Returns formatted result: stdout, stderr (truncated to 8 KB each), `timed_out` flag
- Budget entry tagged `tool=run_sympy`

---

### Phase 3 — Lean4 verifier fix

**`app/agent/verifiers.py`** — replace the stub:

```python
def _run_lean4(lean_source: str) -> dict:
    result = run_in_sandbox(lean_source, lang="lean4", timeout=120)
    if result.get("error"):          # Docker unavailable
        return {"ok": False, "error": result["error"]}
    if result["timed_out"]:
        return {"ok": False, "error": "Lean4 verification timed out."}
    if result["ok"]:
        return {"ok": True, "stdout": result["stdout"]}
    return {"ok": False, "error": result["stderr"]}  # full error, not silent False
```

The Lean4 error output is structured (file:line:col message format) — agents can parse it in their reasoning to identify which tactic failed.

**Coq**: same pattern, degrades gracefully if Coq is absent from the image.

---

### Phase 4 — Literature tools

**`app/agent/tools_math.py`** — new file:

#### `search_arxiv`

Calls `https://export.arxiv.org/api/query` (Atom XML). Parses with `xml.etree.ElementTree` (stdlib, no extra deps). Returns:

```python
[{
    "id":       "2305.12345",
    "title":    "Bounded gaps between primes",
    "authors":  ["Yitang Zhang"],
    "year":     2013,
    "abstract": "...",          # first 500 chars
    "url":      "https://arxiv.org/abs/2305.12345",
    "pdf":      "https://arxiv.org/pdf/2305.12345"
}]
```

Parameters: `query: str`, `max_results: int = 5`, `category: str = ""` (e.g. `math.NT`).

#### `search_oeis`

Calls `https://oeis.org/search?q={query}&fmt=json`. Returns:

```python
[{
    "id":         "A001359",
    "name":       "Lesser of twin primes",
    "values":     [3, 5, 11, 17, 29, ...],   # first 20
    "offset":     "1",
    "references": ["...", "..."],
    "formula":    "...",
    "url":        "https://oeis.org/A001359"
}]
```

Parameters: `query: str`, `max_results: int = 5`.

Both tools added to TOOL_REGISTRY and available in math pipeline stages (see Phase 5).

---

### Phase 5 — Mathematics pipeline stage design

The existing template has 9 stages. Replace empty configs with real system prompts and tool allowlists:

| # | Stage key | Agent type | Allowed tools | Purpose |
|---|---|---|---|---|
| 1 | `LITERATURE_SURVEY` | `custom_llm` | `search_arxiv`, `search_oeis`, `web_fetch`, `write_document`, `read_document` | Survey existing work; record key references and known results in the document store |
| 2 | `PROBLEM_FORMALIZATION` | `custom_llm` | `read_document`, `write_file`, `read_file`, `run_sympy`, `consult_maestro` | Translate the informal problem into precise mathematical notation; define terms, identify unknowns |
| 3 | `CALIBRATION` | `custom_llm` | `run_sympy`, `search_arxiv`, `read_file`, `write_file` | Prove a weaker related result where the answer is known; establishes that the pipeline and agent work correctly before tackling the open problem |
| 4 | `COMPUTATIONAL_EXPLORATION` | `custom_llm` | `run_sympy`, `read_file`, `write_file`, `read_document`, `write_document` | Numerical and symbolic search up to stated bounds; record what is found and ruled out |
| 5 | `HYPOTHESIS_GENERATION` | `custom_llm` | `read_document`, `write_document`, `search_arxiv`, `consult_maestro` | Synthesize exploration results into candidate sub-conjectures or structural observations |
| 6 | `PROOF_STRATEGY` | `custom_llm` | `read_document`, `write_document`, `search_arxiv`, `consult_maestro`, `run_sympy` | Choose a proof approach; sketch the argument; identify the critical lemmas needed |
| 7 | `PROOF_ATTEMPT` | `custom_llm` | `run_sympy`, `write_file`, `read_file`, `read_document` | Write the formal proof (Lean4 or annotated mathematical prose); iterate using run_sympy for sub-computations |
| 8 | `FORMAL_VERIFICATION` | `verifier` | (gate only — no agent tools; runs Lean4 via sandbox) | Lean4 / SymPy gate: proof must pass before advancing |
| 9 | `WRITEUP` | `custom_llm` | `read_file`, `read_document`, `write_document`, `write_file` | Produce a clean mathematical exposition: motivation, approach, result, open questions |

**System prompt themes per stage** (abbreviated — full prompts in the DB agent definition):
- `LITERATURE_SURVEY`: *"Your goal is to understand what is already known. Record every relevant theorem, partial result, and known technique in the document store. Dead ends in the literature are as valuable as successes."*
- `COMPUTATIONAL_EXPLORATION`: *"Run computations, record results—including null results. State the bound you searched to. The autopilot system uses your documented findings to decide next steps."*
- `PROOF_ATTEMPT`: *"If Lean4 verification fails, read the error output carefully. The type-checker is telling you exactly what is wrong. Treat each error as a unit test for your proof."*

---

### Phase 6 — Update existing `python_sympy` verifier

The existing verifier uses `subprocess` with a 30s hard timeout. Replace with `run_in_sandbox`:

```python
def _run_sympy_verify(code: str) -> dict:
    return run_in_sandbox(code, lang="python", timeout=30)
```

This brings stage-gate verification and mid-session `run_sympy` calls to the same isolated environment. No more host-process execution anywhere in the math path.

---

### Phase 7 — Tests

1. **Unit** — `run_in_sandbox`: Docker unavailable returns clear error; timeout fires correctly; stdout/stderr captured separately.
2. **Unit** — `handle_run_sympy`: timeout clamped to [10, 600]; output truncated at 8 KB.
3. **Unit** — `_run_lean4`: full stderr returned on failure; not silent `False`.
4. **Unit** — `search_arxiv`: parses Atom XML correctly; max_results honoured.
5. **Unit** — `search_oeis`: parses JSON correctly; graceful on missing fields.
6. **Integration** — full `run_sympy` round-trip: agent calls tool, Docker executes, result in budget trace.
7. **Integration** — Lean4 verifier: known valid proof returns `ok: True`; known invalid proof returns `ok: False` with non-empty error string.

---

## Operational note (Windows)

Docker Desktop must be running before the Maestro server starts for any math tooling to work.
Add a startup check in `app/main.py` `lifespan` that calls `docker info` and logs a warning
(not an error — the rest of Maestro works fine without it) if Docker is unavailable.

---

## Files touched (expected)

| File | Change |
|---|---|
| `docker/sympy-lean4-sandbox/Dockerfile` | New sandbox image |
| `app/agent/sandbox.py` | New — Docker execution wrapper |
| `app/agent/tools.py` | Register `run_sympy` |
| `app/agent/tools_math.py` | New — `search_arxiv`, `search_oeis` |
| `app/agent/verifiers.py` | Replace Lean4/Coq stubs; route `python_sympy` through sandbox |
| `app/main.py` | Docker availability check on startup |
| `maestro.ini` | `[math]` section: `sandbox_memory_mb`, `sandbox_timeout_default` |
| Math pipeline template (DB) | Stage system prompts + tool allowlists (migration or seed script) |
| `app/tests/` | Unit + integration tests |

---

## Acceptance criteria

- [x] `run_sympy` tool executes agent-supplied code in Docker with no access to host filesystem or network.
- [x] Docker unavailability produces a clear human-readable error, not a crash or silent failure.
- [x] Lean4 verification failure returns the full compiler error to the agent (not `False`).
- [x] `search_arxiv` and `search_oeis` return structured records parseable without regex.
- [x] Existing `python_sympy` stage-gate verifier routes through the sandbox (no more host-process execution).
- [x] All 9 math pipeline stages have non-empty system prompts and explicit tool allowlists (migration 0088).
- [x] All new code passes existing test suite with no regressions (877 passed).
