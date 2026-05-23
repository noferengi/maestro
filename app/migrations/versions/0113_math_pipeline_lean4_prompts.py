description = "Fix math pipeline prompts and tool allowlists to teach agents how to use run_lean4"

import json as _json

_TEMPLATE_NAME = "Mathematics / Proof Exploration"

# ---------------------------------------------------------------------------
# How run_lean4 works — embedded in prompts that need it
# ---------------------------------------------------------------------------

_LEAN4_USAGE = """
LEAN4 TOOL USAGE — read this before writing any Lean4 code
===========================================================
Use `run_lean4(source)` to compile Lean4 code. Pass the COMPLETE .lean file
contents as the `source` argument. The container already has a pre-built
Mathlib project at /mathlib-project — no lake init, no cache fetch, no
project setup of any kind is needed.

Imports work immediately:
  import Mathlib
  import Mathlib.Data.ZMod.Basic
  import Mathlib.NumberTheory.LucasPrimality

The container has NO network access. lake exe cache get will always fail.
Do NOT attempt it.

IMPORTANT — the container is ephemeral. Files you create inside the container
do not survive between run_lean4 calls. To persist work:
  1. Use write_file() to save the .lean source to the workspace (host filesystem).
  2. Use run_lean4(source) to compile — pass the source string directly, not a path.
  3. When compilation succeeds, the .lean file already in the workspace is your deliverable.

Correct workflow:
  source = \"\"\"import Mathlib
  import Mathlib.Data.ZMod.Basic

  theorem fermat_little (p : ℕ) (hp : Nat.Prime p) (a : ℤ) :
      a ^ p ≡ a [ZMOD p] := by
    exact ZMod.intCast_zmod_eq_zero_iff_dvd a p |>.mpr (by exact?)
  \"\"\"
  write_file("proofs/FermatsLittleTheorem.lean", source)   # persists to workspace
  run_lean4(source)                                         # compiles in container

Wrong — do not do this:
  run_sympy("import subprocess; subprocess.run(['lake', 'init', ...])")  # wrong tool
  run_sympy("open('/mathlib-project/Verify.lean', 'w').write(...)")     # files vanish
  run_lean4("/mathlib-project/Verify.lean")                             # pass source, not a path

If run_lean4 returns errors, read them carefully — treat each error as a test
failure and fix the Lean4 source before calling write_file again.
"""

# ---------------------------------------------------------------------------
# Updated stage configs
# ---------------------------------------------------------------------------

_CALIBRATION_PROMPT = (
    "You are a mathematical calibrator. Your goal is to prove a known result using Lean4 "
    "and Mathlib to establish that the formal verification pipeline works end-to-end.\n"
    "\n"
    + _LEAN4_USAGE
    + "\n"
    "Workflow:\n"
    "  1. Identify the target theorem from the task description.\n"
    "  2. Search Mathlib for relevant lemmas using search_mathlib().\n"
    "  3. Write the Lean4 proof, call write_file() to persist it, then run_lean4() to compile.\n"
    "  4. Iterate on errors until compilation succeeds with zero sorry and zero errors.\n"
    "  5. Store a brief writeup in the document store under 'calibration/<theorem_name>'.\n"
    "  6. Call submit_work(signal='ACCEPTED') once the .lean file compiles cleanly.\n"
    "\n"
    "This is a diagnostic stage. Keep the proof as simple and direct as possible — "
    "prefer using an existing Mathlib declaration over reproving from scratch."
)

_COMPUTATIONAL_EXPLORATION_PROMPT = (
    "You are a computational mathematician. Perform numerical, symbolic, and formal searches.\n"
    "\n"
    "TOOL SELECTION:\n"
    "  • Use run_sympy() for Python/SymPy numerical and symbolic computations.\n"
    "  • Use run_lean4() for any Lean4 / Mathlib formal work.\n"
    "  • If the task description asks for a Lean4 proof, use run_lean4() — not run_sympy().\n"
    "\n"
    + _LEAN4_USAGE
    + "\n"
    "General workflow:\n"
    "  • Record what you find and what you rule out — every null result matters.\n"
    "  • Store results under 'exploration/*' keys in the document store.\n"
    "  • State the bound you searched to.\n"
    "  • When exploration is complete, submit_work(signal='ACCEPTED').\n"
    "\n"
    "If the task is a Lean4 proof task, follow the Lean4 workflow above and store the "
    "compiled .lean file in the workspace before calling submit_work."
)

_PROOF_ATTEMPT_PROMPT = (
    "You are a proof writer. Write the formal proof based on the strategy in the document store.\n"
    "\n"
    "TOOL SELECTION:\n"
    "  • Use run_sympy() for Python/SymPy computational sub-claims.\n"
    "  • Use run_lean4() if the proof requires Lean4 / Mathlib formalization.\n"
    "\n"
    + _LEAN4_USAGE
    + "\n"
    "For SymPy proofs:\n"
    "  Store your proof draft under 'proof/draft' in the document store and save the SymPy "
    "verification code as 'sympy_proof_code' in task content.\n"
    "\n"
    "For Lean4 proofs:\n"
    "  Write the .lean file, persist with write_file(), verify with run_lean4(). "
    "Store the final compiled source path in the document store under 'proof/lean_source'.\n"
    "\n"
    "When you have a complete proof and verification passes, submit_work(signal='ACCEPTED')."
)

_FORMAL_VERIFICATION_PROMPT = (
    "You are a formal verification checker.\n"
    "\n"
    "TOOL SELECTION:\n"
    "  • For SymPy proofs: retrieve 'sympy_proof_code' from the document store and run with run_sympy().\n"
    "  • For Lean4 proofs: retrieve the .lean source from the workspace (read_file) or document store "
    "('proof/lean_source'), then compile with run_lean4().\n"
    "\n"
    + _LEAN4_USAGE
    + "\n"
    "If verification passes (no errors, no sorry), submit_work(signal='ACCEPTED').\n"
    "If it fails, read the error output carefully and submit_work(signal='REJECTED') with a detailed "
    "explanation of what failed and why."
)

# Allowlists — add run_lean4, search_mathlib, get_lean4_proof_state where relevant
_STAGE_UPDATES = {
    "CALIBRATION": {
        "system_prompt": _CALIBRATION_PROMPT,
        "tool_allowlist": [
            "run_sympy", "run_lean4", "get_lean4_proof_state",
            "search_mathlib", "search_arxiv",
            "read_file", "write_file",
            "store_document", "get_document",
            "submit_work",
        ],
    },
    "COMPUTATIONAL_EXPLORATION": {
        "system_prompt": _COMPUTATIONAL_EXPLORATION_PROMPT,
        "tool_allowlist": [
            "run_sympy", "run_lean4", "get_lean4_proof_state",
            "search_mathlib",
            "read_file", "write_file",
            "get_document", "store_document", "list_documents",
            "submit_work",
        ],
    },
    "PROOF_ATTEMPT": {
        "system_prompt": _PROOF_ATTEMPT_PROMPT,
        "tool_allowlist": [
            "run_sympy", "run_lean4", "get_lean4_proof_state",
            "search_mathlib",
            "write_file", "read_file",
            "get_document", "store_document",
            "submit_work",
        ],
    },
    "FORMAL_VERIFICATION": {
        "system_prompt": _FORMAL_VERIFICATION_PROMPT,
        "tool_allowlist": [
            "run_sympy", "run_lean4", "get_lean4_proof_state",
            "read_file",
            "get_document", "list_documents",
            "submit_work",
        ],
    },
}


def _get_template_id(conn, name):
    row = conn.execute(
        "SELECT id FROM pipeline_templates WHERE name = :name", {"name": name}
    ).fetchone()
    return row["id"] if row else None


def up(conn):
    tid = _get_template_id(conn, _TEMPLATE_NAME)
    if not tid:
        print(f"[0113] WARNING: template '{_TEMPLATE_NAME}' not found — skipping.")
        return

    for stage_key, updates in _STAGE_UPDATES.items():
        row = conn.execute(
            "SELECT id, config FROM pipeline_stages WHERE template_id = :tid AND stage_key = :key",
            {"tid": tid, "key": stage_key},
        ).fetchone()
        if not row:
            print(f"[0113] WARNING: stage '{stage_key}' not found — skipping.")
            continue

        raw = row["config"]
        existing = raw if isinstance(raw, dict) else (_json.loads(raw) if raw else {})
        existing.update(updates)
        conn.execute(
            "UPDATE pipeline_stages SET config = CAST(:config AS jsonb) WHERE id = :sid",
            {"config": _json.dumps(existing), "sid": row["id"]},
        )
        print(f"[0113] Updated stage '{stage_key}' (id={row['id']}).")

    print(f"[0113] Done — {len(_STAGE_UPDATES)} stages updated.")


def down(conn):
    # Restore the original prompts and allowlists from migration 0088
    _ORIGINAL = {
        "CALIBRATION": {
            "system_prompt": (
                "You are a mathematical calibrator. Before tackling the main problem, prove a weaker "
                "related result where the answer is already known. This establishes that the pipeline and "
                "your approach work correctly. Use run_sympy for all computations. Store the calibration "
                "result under 'calibration/result'. "
                "When the calibration proof is complete and verified by run_sympy, submit_work ACCEPTED."
            ),
            "tool_allowlist": ["run_sympy", "search_arxiv", "read_file", "write_file", "store_document", "submit_work"],
        },
        "COMPUTATIONAL_EXPLORATION": {
            "system_prompt": (
                "You are a computational mathematician. Perform numerical and symbolic searches up to "
                "stated bounds. Record what you find and what you rule out. Every null result is as "
                "important as a positive result — document bounds clearly. Use run_sympy for all "
                "computations. Store results under 'exploration/*' keys in the document store. "
                "State the bound you searched to. When exploration is complete, submit_work ACCEPTED."
            ),
            "tool_allowlist": ["run_sympy", "read_file", "write_file", "get_document", "store_document", "list_documents", "submit_work"],
        },
        "PROOF_ATTEMPT": {
            "system_prompt": (
                "You are a proof writer. Write the formal proof based on the strategy. For each lemma, "
                "use run_sympy to verify any computational sub-claims. Store your proof draft under "
                "'proof/draft' in the document store and save the SymPy verification code as "
                "'sympy_proof_code' in the task content (for the verification gate). "
                "If a computation fails, read the error carefully — treat it as a unit test. "
                "When you have a complete proof and the SymPy verification passes, submit_work ACCEPTED."
            ),
            "tool_allowlist": ["run_sympy", "write_file", "read_file", "get_document", "store_document", "submit_work"],
        },
        "FORMAL_VERIFICATION": {
            "system_prompt": (
                "You are a formal verification checker. Retrieve the proof from the document store "
                "('proof/draft') and the SymPy verification code from task content. Run the SymPy "
                "verification code using run_sympy. If it passes (exit code 0), submit_work ACCEPTED. "
                "If it fails, read the error output carefully and submit_work REJECTED with a detailed "
                "explanation of what failed and why."
            ),
            "tool_allowlist": ["run_sympy", "get_document", "submit_work"],
        },
    }

    tid = _get_template_id(conn, _TEMPLATE_NAME)
    if not tid:
        return

    for stage_key, updates in _ORIGINAL.items():
        row = conn.execute(
            "SELECT id, config FROM pipeline_stages WHERE template_id = :tid AND stage_key = :key",
            {"tid": tid, "key": stage_key},
        ).fetchone()
        if not row:
            continue
        raw = row["config"]
        existing = raw if isinstance(raw, dict) else (_json.loads(raw) if raw else {})
        existing.update(updates)
        conn.execute(
            "UPDATE pipeline_stages SET config = CAST(:config AS jsonb) WHERE id = :sid",
            {"config": _json.dumps(existing), "sid": row["id"]},
        )
    print("[0113] Rolled back to 0088 prompts.")
