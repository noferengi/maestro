description = "formal_verification gate: required_tool_successes, lean4 verifier, fixed system prompt and tool allowlist"

import json

_NEW_SYSTEM_PROMPT = """\
You are a formal verification checker for mathematical proofs.

Step 1 — Locate the proof file.
  Use list_directory to find .lean files in the workspace root.
  Then read the file with read_file.

Step 2 — Compile it with run_lean4.
  Pass the full source text to run_lean4.
  A passing compilation has ok=true, no "error" lines, and no "sorry" in stderr.

Step 3 — Report.
  If compilation passes: submit_work ACCEPTED.
  If compilation fails: submit_work REJECTED with the full compiler output so
  the proof author can fix it.

Do not fall back to SymPy. run_lean4 is the gate. If the .lean file is
missing, submit_work REJECTED with the message "no .lean file found in workspace".
"""

_NEW_TOOL_ALLOWLIST = [
    "list_directory",
    "read_file",
    "run_lean4",
    "submit_work",
]

_NEW_STAGE_CONFIG = {
    "gate_type": "llm_judge",
    "max_turns": 10,
    "verifier": "lean4",
    "required_tool_successes": ["run_lean4"],
    "system_prompt": _NEW_SYSTEM_PROMPT,
    "tool_allowlist": _NEW_TOOL_ALLOWLIST,
}

_OLD_STAGE_CONFIG = {
    "gate_type": "llm_judge",
    "max_turns": 10,
    "system_prompt": (
        "You are a formal verification checker for mathematical proofs.\n\n"
        "First, look for a .lean file in the workspace using read_file (try "
        "'infinitely_many_primes.lean' or list the directory). Compile it using "
        "run_lean4. Compilation success (no errors, no sorry in stderr) is the "
        "primary gate.\n\nIf no .lean file is found, fall back to running any "
        "SymPy verification code from the document store ('proof/draft' or task "
        "content 'sympy_proof_code') using run_sympy.\n\nIf the primary "
        "verification passes, submit_work ACCEPTED.\nIf it fails, submit_work "
        "REJECTED with the full compiler output so the proof author can fix it."
    ),
    "tool_allowlist": ["run_lean4", "run_sympy", "get_document", "read_file", "submit_work"],
}


def up(conn) -> None:
    conn.execute(
        "UPDATE pipeline_stages SET config = :cfg WHERE stage_key = :key",
        {"cfg": json.dumps(_NEW_STAGE_CONFIG), "key": "FORMAL_VERIFICATION"},
    )


def down(conn) -> None:
    conn.execute(
        "UPDATE pipeline_stages SET config = :cfg WHERE stage_key = :key",
        {"cfg": json.dumps(_OLD_STAGE_CONFIG), "key": "FORMAL_VERIFICATION"},
    )
