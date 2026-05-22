description = "FORMAL_VERIFICATION: add output_keys to store lean4_source and lean4_output in task.content"

import json

_PREV_SYSTEM_PROMPT = """\
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

_NEW_SYSTEM_PROMPT = """\
You are a formal verification checker for mathematical proofs.

Step 1 — Locate the proof file.
  Use list_directory to find .lean files in the workspace root.
  Then read the file with read_file.

Step 2 — Compile it with run_lean4.
  Pass the full source text to run_lean4.
  A passing compilation has ok=true, no "error" lines, and no "sorry" in stderr.

Step 3 — Report, including the source and compiler output in the payload.

  If compilation passes:
    submit_work with signal ACCEPTED and payload:
      {
        "lean4_source": "<full content of the .lean file>",
        "lean4_output": "<compiler stdout, or empty string if clean>"
      }

  If compilation fails:
    submit_work with signal REJECTED and payload:
      {
        "lean4_source": "<full content of the .lean file>",
        "lean4_output": "<full compiler stderr/stdout showing the errors>"
      }

Do not fall back to SymPy. run_lean4 is the gate. If the .lean file is
missing, submit_work REJECTED with message "no .lean file found in workspace"
and omit lean4_source/lean4_output from the payload.
"""

_TOOL_ALLOWLIST = ["list_directory", "read_file", "run_lean4", "submit_work"]

_NEW_CONFIG = {
    "gate_type": "llm_judge",
    "max_turns": 10,
    "verifier": "lean4",
    "required_tool_successes": ["run_lean4"],
    "output_keys": ["lean4_output", "lean4_source"],
    "system_prompt": _NEW_SYSTEM_PROMPT,
    "tool_allowlist": _TOOL_ALLOWLIST,
}

_PREV_CONFIG = {
    "gate_type": "llm_judge",
    "max_turns": 10,
    "verifier": "lean4",
    "required_tool_successes": ["run_lean4"],
    "system_prompt": _PREV_SYSTEM_PROMPT,
    "tool_allowlist": _TOOL_ALLOWLIST,
}


def up(conn) -> None:
    conn.execute(
        "UPDATE pipeline_stages SET config = :cfg WHERE stage_key = 'FORMAL_VERIFICATION'",
        {"cfg": json.dumps(_NEW_CONFIG)},
    )


def down(conn) -> None:
    conn.execute(
        "UPDATE pipeline_stages SET config = :cfg WHERE stage_key = 'FORMAL_VERIFICATION'",
        {"cfg": json.dumps(_PREV_CONFIG)},
    )
