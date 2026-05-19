# Gap 3 — Math / formal proof tooling

**Status:** Planning  
**Effort:** Medium-Large  
**Priority:** Medium — required for mathematics pipeline to be more than decorative

## Problem

The Mathematics / Proof Exploration pipeline template exists and the `python_sympy`
verifier works (subprocess, 30s timeout). Lean4 and Coq verifiers are stubs that
return `False` unconditionally. Agents have no tool for writing and executing symbolic
math code interactively — they can only have code verified at stage gates, not iterate
on it mid-session. There are no tools for searching mathematical literature.

## Rough phases

1. `run_sympy` agent tool — write code, execute, return stdout+stderr inline
2. Lean4 CLI integration in `verifiers.py`
3. Mathematics pipeline stage configs — prompts, tool allowlists, intent descriptions
4. Literature access — arXiv and OEIS
5. Conjecture exploration loop design

## Open questions

### SymPy execution model
- Does the agent write a `.py` file into the project directory and call `run_test_pytest`
  on it (reuses existing infrastructure, files are tracked in git), or is there a
  dedicated `run_sympy` tool that takes a code string argument and returns output inline
  without touching the filesystem?
- The existing verifier runs `python -c code` with a 30s ceiling. For computational
  searches (primality up to 10^12, sieve computations) the agent may need minutes. What
  is the acceptable timeout ceiling and should it be configurable per task or per stage?
- Should the tool capture and return both stdout and stderr, or only stdout on success
  and stderr on failure?

### Lean4 integration
- Is Lean4 already installed on this machine? If not, does that need to happen before
  this plan proceeds, or should the verifier degrade gracefully with a clear error
  rather than returning silent `False`?
- What does the agent receive when Lean4 verification fails? Currently `_run_lean4`
  returns `False` with no feedback. Lean4 error messages are structured — should the
  full error output be passed back into the agent's context so it can read why the proof
  failed and attempt a correction?
- Does the agent write `.lean` files to the project directory, or does the verifier
  create a temp file from task content and clean it up?

### Pipeline stage design
- The Mathematics template has 9 stages. What agent type is each stage using today?
  Do any of them have real system prompts or are they all empty config?
- What should the stage sequence look like for a proof exploration task? One option:
  Literature Survey → Problem Formalization → Computational Search → Proof Strategy →
  Proof Attempt → Formal Verification → Write-up. Is that the right decomposition?
- Which tools should each stage be allowed to use? Formalization needs read/write file
  access. Computational search needs `run_sympy`. Formal verification needs the Lean4
  verifier gate. Write-up needs document store.

### Literature access
- arXiv has a structured API. OEIS has a search endpoint. Should these be dedicated
  agent tools (`search_arxiv`, `search_oeis`) that parse responses into clean records,
  or is `web_fetch` on the raw API URLs sufficient and the agent handles parsing?
- Are there other sources (MathSciNet, zbMATH, ProofWiki) worth supporting?

### Problem scope
- "Twin prime conjecture" is unsolved by all of mathematics. What is the realistic
  target? Options: (a) computational exploration up to a stated bound, (b) formalization
  of known partial results (Zhang's bounded gaps, Maynard's work), (c) proof of related
  weaker conjectures where the answer is known. Which of these is the intended use case,
  and does the pipeline need to handle all three or just one?
