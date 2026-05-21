# Gap 12 — Lean4 proof depth (Mathlib context + proof state tool)

**Status:** Complete — all phases implemented, 38/38 tests pass, migration 0105 applied  
**Effort:** Small-Medium  
**Priority:** High — prerequisite for serious formal mathematics work; without it agents
write Lean4 blind and learn only from compilation errors, not from proof state

---

## Context — What We Are Trying to Do

The Mathematics / Proof Exploration pipeline (9 stages, migration 0088) is structurally
complete. `run_sympy`, `search_arxiv`, `search_oeis`, and the Lean4 verifier gate all
work. The pipeline can, in principle, pursue open problems like the twin prime conjecture.

In practice, two things make the PROOF_ATTEMPT stage much weaker than it could be:

1. **Agents don't know what Mathlib already has.** Lean4 Mathlib contains 80,000+
   theorems. An agent re-proving `Nat.Prime.eq_one_or_self_of_dvd` from scratch instead
   of calling it by name wastes turns and produces brittle proofs.

2. **Agents can't see proof state.** `run_sympy` returns stdout. When a Lean4 file
   fails, the agent gets a compilation error. What it cannot see is the *infoview output*
   — the proof goal, hypotheses in scope, and what the type checker is currently
   expecting at each `sorry` or cursor position. A human Lean4 developer reads this
   constantly. Without it the agent is writing in the dark.

This gap adds both capabilities. It also specifies the operational prerequisites and
the recommended first task sequence for pursuing the twin prime conjecture specifically.

---

## Design decisions

| Decision | Resolution |
|---|---|
| **Mathlib context** | A structured document in the project document store, written by the LITERATURE_SURVEY agent via `search_mathlib`. Agents read it via `get_document`. No new DB schema. |
| **`search_mathlib` tool** | Wraps `lake env lean --stdin` to query `#check` and `#search` against the project's Mathlib install, or falls back to a curated static index if Lean4 is unavailable. Returns name + type signature + module path. |
| **Proof state tool** | `get_lean4_proof_state(file, line)` — runs the Lean4 language server on a file, extracts the infoview at the given line, and returns the goal + context. Same Docker sandbox as `run_sympy`. Lean4 must be in the image (it already is). |
| **Sandbox change** | `sandbox.py` gains a new `lang="lean4_infoview"` mode that runs `lean --server` in line-query mode rather than compiling the whole file. |
| **Tool availability** | Both tools added to the math pipeline stage allowlists via a new migration. `search_mathlib` in LITERATURE_SURVEY, PROOF_STRATEGY, PROOF_ATTEMPT. `get_lean4_proof_state` in PROOF_ATTEMPT only. |

---

## Implementation plan

### Phase 1 — `search_mathlib` tool

**`app/agent/tools_math.py`** — add:

```python
def search_mathlib(query: str, max_results: int = 10) -> list[dict]:
    """
    Search Lean4 Mathlib for theorems, definitions, and lemmas matching query.
    Returns name, type signature, module path, and a one-line docstring if present.

    Primary path: runs `lake env lean --stdin` with a #search command in the
    project's Mathlib environment (requires lake + Mathlib in PATH).
    Fallback: searches a bundled static index (app/agent/mathlib_index.json)
    compiled from Mathlib4 docs. The static index covers the ~2000 most-used
    declarations across number theory, algebra, and analysis.
    """
```

**Static index** (`app/agent/mathlib_index.json`) — a pre-built JSON file:

```json
[
  {
    "name": "Nat.Prime",
    "type": "ℕ → Prop",
    "module": "Mathlib.Data.Nat.Prime.Basic",
    "doc": "p is prime if p ≥ 2 and has no divisors other than 1 and p"
  },
  {
    "name": "Nat.infinite_setOf_prime",
    "type": "Set.Infinite {p | Nat.Prime p}",
    "module": "Mathlib.Data.Nat.Prime.Infinite",
    "doc": "There are infinitely many primes"
  },
  ...
]
```

Build script: `scripts/build_mathlib_index.py` — uses `lake env lean` to query all
declarations in `Mathlib.NumberTheory.*`, `Mathlib.Data.Nat.*`, `Mathlib.Analysis.Prime.*`
and writes the JSON. Run once. Commit the result. Regenerate when Mathlib version changes.

Register in TOOL_REGISTRY and TOOL_SCHEMAS. Tool schema:

```python
"search_mathlib": {
    "name": "search_mathlib",
    "description": (
        "Search Lean4 Mathlib for existing theorems, lemmas, and definitions. "
        "Always call this before attempting to prove something — it may already exist. "
        "Returns name, type signature, and module path."
    ),
    "parameters": {
        "query":       {"type": "string",  "description": "Search terms (e.g. 'prime gap sieve', 'Nat.Prime dvd')"},
        "max_results": {"type": "integer", "description": "Max results (default 10, max 50)"}
    },
    "required": ["query"]
}
```

---

### Phase 2 — `get_lean4_proof_state` tool

**`app/agent/sandbox.py`** — add `lang="lean4_infoview"` mode:

```python
def get_lean4_proof_state(lean_file: str, line: int, col: int = 0) -> dict:
    """
    Run the Lean4 language server on lean_file and return the proof state
    (infoview output) at the given line/col position.

    Returns:
    {
        "ok": bool,
        "goal": "⊢ ∀ n : ℕ, ∃ p, p > n ∧ Nat.Prime p",
        "hypotheses": ["h : Nat.Prime p", "hn : n < p"],
        "messages": ["...any warnings or errors at this position..."],
        "error": "..." or None
    }
    """
```

Implementation sketch:
- Write `lean_file` to a temp path inside the sandbox
- Run `lean --server` with a JSON RPC `{"method": "$/lean/plainGoal", "params": {"textDocument": ..., "position": ...}}`
- Parse the response and extract goal string + hypothesis list
- Kill the server process after one response (it doesn't need to stay alive)
- Timeout: 60 seconds (Lean server startup is slow)
- If Docker unavailable: return `{"ok": False, "error": "Docker unavailable"}`

**`app/agent/tools.py`** — register:

```python
"get_lean4_proof_state": {
    "name": "get_lean4_proof_state",
    "description": (
        "Get the Lean4 proof state (goal + hypotheses) at a specific line in a .lean file. "
        "Use after writing a proof attempt to understand what remains to be proved at each step. "
        "Place a `sorry` at the point you want to inspect — the infoview shows what sorry is standing in for."
    ),
    "parameters": {
        "lean_source": {"type": "string",  "description": "Full Lean4 source code of the file"},
        "line":        {"type": "integer", "description": "1-indexed line number to inspect (place `sorry` there)"},
        "col":         {"type": "integer", "description": "Column number (default 0)"}
    },
    "required": ["lean_source", "line"]
}
```

---

### Phase 3 — Migration: update math pipeline stage tool allowlists

**New migration** — add `search_mathlib` and `get_lean4_proof_state` to the relevant
math pipeline stages:

| Stage | Tool added |
|---|---|
| `LITERATURE_SURVEY` | `search_mathlib` |
| `PROBLEM_FORMALIZATION` | `search_mathlib` |
| `PROOF_STRATEGY` | `search_mathlib` |
| `PROOF_ATTEMPT` | `search_mathlib`, `get_lean4_proof_state` |

Migration updates the `tool_allowlist` JSON column on the relevant `pipeline_stages` rows
for the Mathematics / Proof Exploration built-in template.

---

### Phase 4 — Operational prerequisites (configuration, not code)

These are not code changes — they are setup steps required before the math pipeline
produces useful output on a problem like twin primes.

**4a — Orchestrator LLM**

Set `maestro_llm_id` to the strongest available reasoning model. Qwen 35B is sufficient
for mechanical tasks; for novel mathematics the orchestrator needs a frontier model.

```ini
[orchestration]
maestro_llm_id = 2    ; point at your strongest endpoint
```

**4b — Docker running and sandbox image built**

```bash
docker build -t sympy-lean4-sandbox:latest docker/sympy-lean4-sandbox/
```

Verify: `docker run --rm sympy-lean4-sandbox:latest python -c "import sympy; print(sympy.__version__)"` should print a version number.

**4c — Mathlib static index built**

```bash
venv/Scripts/python.exe scripts/build_mathlib_index.py
```

This takes 5–10 minutes on first run. The output is committed to the repo and only needs
regenerating when the Mathlib version changes.

---

### Phase 5 — Recommended first task sequence (Twin Primes)

Do not create an objective titled "Prove the twin prime conjecture." Create a sequence
of scoped objectives in ascending difficulty. Each must fully complete before the next
begins. This is the calibration-first principle from the CALIBRATION stage design.

**Objective 1 — Pipeline health check (1–2 hours)**

```
Title: Calibrate — Lean4 proof of infinitely many primes
Description:
  Prove Set.Infinite {p | Nat.Prime p} in Lean4.
  This result already exists in Mathlib as Nat.infinite_setOf_prime.
  The goal is to:
  1. Write a proof that uses the Mathlib declaration correctly (not a re-proof from scratch)
  2. Verify the proof compiles with zero sorries via the Lean4 gate
  3. Confirm the reflection agent produces confidence >= 0.8
  This is a diagnostic card. If it fails, fix the pipeline before continuing.
Priority: 10. Time-box: 4 hours.
```

**Objective 2 — Literature foundation (4–8 hours)**

```
Title: Twin primes — literature survey and Mathlib gap map
Description:
  1. Use search_arxiv to retrieve Zhang (2013), Maynard (2015), and the
     Polymath8b paper. Summarise each in the document store under keys
     tw/zhang, tw/maynard, tw/polymath8b.
  2. Use search_mathlib to enumerate all Mathlib declarations in
     Mathlib.NumberTheory.SieveMethods, Mathlib.NumberTheory.PrimeCounting,
     and related modules. Write a summary to tw/mathlib_inventory.
  3. Write a gap analysis to tw/gap_analysis: what does Mathlib have,
     what is absent, and what would need to be formalised as lemmas before
     a Zhang-style proof could be assembled.
Priority: 8. Time-box: 12 hours.
```

**Objective 3 — Bombieri-Vinogradov formalisation (days–weeks)**

```
Title: Formalise the Bombieri-Vinogradov theorem in Lean4
Description:
  Bombieri-Vinogradov is a key input to the GPY sieve (the foundation of
  Zhang's proof). No complete Lean4 formalisation exists in Mathlib.
  
  Approach:
  1. Read tw/mathlib_inventory to identify available building blocks
  2. Identify which sub-lemmas are already in Mathlib, which need new proofs
  3. Formalise sub-lemmas bottom-up, each as a separate card, each verified
     by the Lean4 gate before proceeding to the next
  4. Assemble the full theorem once all sub-lemmas are verified
  
  Success criterion: the Lean4 gate accepts the final statement with zero sorries.
  The result should be submittable as a Mathlib PR.
Priority: 8. Time-box: 168 hours (1 week).
```

**Objective 4 — Computational exploration (parallel with Objective 3)**

```
Title: Sieve computation — verify twin prime density to 10^13
Description:
  Use run_sympy (segmented Wheel sieve) to count and record all twin prime pairs
  up to 10^13. Verify against the Hardy-Littlewood conjecture's predicted count.
  Record deviations. Write results to tw/computational_survey.
  
  Separately: search for any prime gap patterns near known twin prime clusters
  that might suggest a local density argument.
Priority: 5. Time-box: 48 hours.
```

---

### Phase 6 — Tests

1. **Unit** — `search_mathlib`: static index path returns results when Lean4 unavailable; live path parses `#check` output correctly; max_results honoured.
2. **Unit** — `get_lean4_proof_state`: `sorry` at line N returns the goal at that position; Lean4 unavailable returns clear error; timeout fires correctly.
3. **Unit** — migration: all four stages have updated tool allowlists post-migration.
4. **Integration** — end-to-end: agent writes a file with a `sorry`, calls `get_lean4_proof_state`, reads the goal, fills in the tactic, calls `run_sympy` to verify a sub-computation, produces a complete proof.

---

## Files touched

| File | Change |
|---|---|
| `app/agent/tools_math.py` | Add `search_mathlib` (with static-index fallback) |
| `app/agent/sandbox.py` | Add `lang="lean4_infoview"` mode for proof state queries |
| `app/agent/tools.py` | Register `get_lean4_proof_state` |
| `app/agent/mathlib_index.json` | **New** — pre-built static Mathlib declaration index |
| `scripts/build_mathlib_index.py` | **New** — builds `mathlib_index.json` from a live Mathlib install |
| `app/migrations/versions/NNNN_math_tool_allowlists.py` | Add `search_mathlib` + `get_lean4_proof_state` to math stage allowlists |
| `app/tests/test_math_tools.py` | Extend with Phase 6 test cases |

---

## Acceptance criteria

- [ ] `search_mathlib("prime gap sieve")` returns at least 3 relevant Mathlib declarations including module paths, without requiring a live Lean4 install (static index fallback).
- [ ] `get_lean4_proof_state` with a file containing a `sorry` returns the proof goal at that line as a readable string (e.g. `"⊢ Nat.Prime p → p ≥ 2"`).
- [ ] `get_lean4_proof_state` with Docker unavailable returns `{"ok": False, "error": "..."}` — no crash.
- [ ] Math pipeline PROOF_ATTEMPT stage has both new tools in its allowlist post-migration.
- [ ] Calibration card (Objective 1 above) reaches ACCEPTED end-to-end with the Lean4 gate passing.
- [ ] All new code passes existing test suite with no regressions.

---

## On the twin prime conjecture itself

No version of this system — or any current AI system — will prove the twin prime conjecture. The conjecture has resisted the best human mathematicians for 170 years and requires techniques that do not yet exist. The value of this work is:

1. **Concrete mathematical contribution** — a Lean4 formalisation of Bombieri-Vinogradov would be accepted into Mathlib and has independent value regardless of whether it leads to a twin prime proof.
2. **Platform stress test** — the formal verification pipeline running on real open mathematics is the hardest possible test of the Maestro system's correctness, memory, and multi-session coherence.
3. **Training data** — every successful formal proof session generates high-quality failure-recovery training data (many of the most valuable sessions will be ones where the Lean4 gate rejected a proof and the agent corrected it).

The conjecture is the *north star*, not the deliverable. Forward progress is any verified lemma that didn't exist before.
