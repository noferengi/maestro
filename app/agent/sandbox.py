"""
app/agent/sandbox.py
--------------------
Docker sandbox execution for math tools (run_sympy, Lean4, Coq).

All code execution routes through this module so that agent-supplied code
never runs in the host Python process.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import uuid

logger = logging.getLogger(__name__)

SANDBOX_IMAGE = "sympy-lean4-sandbox:latest"
DEFAULT_TIMEOUT = 120
DEFAULT_MEMORY = "512m"

# Stdin-based entry commands (no volume mount required).
# Code is piped to the container's stdin so Windows temp paths never appear
# in the docker run command — required when DOCKER_HOST points to a remote daemon.
_LANG_STDIN_CMD: dict[str, list[str]] = {
    "python": ["python", "-"],
    "lean4":  ["sh", "-c", "cat > /mathlib-project/Verify.lean && cd /mathlib-project && lake env lean /mathlib-project/Verify.lean"],
    "coq":    ["sh", "-c", "cat > /tmp/main.v && coqc /tmp/main.v"],
}


def _get_memory_limit() -> str:
    try:
        from app.agent.config import _cfg
        mb = _cfg.getint("math", "sandbox_memory_mb", fallback=512)
        return f"{mb}m"
    except Exception:
        return DEFAULT_MEMORY


def _is_docker_available() -> bool:
    try:
        r = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=5,
        )
        return r.returncode == 0
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return False


def run_in_sandbox(
    code: str,
    lang: str = "python",
    timeout: int = DEFAULT_TIMEOUT,
) -> dict:
    """
    Run code in the isolated Docker container.

    lang: "python" | "lean4" | "coq"
    Returns {ok, stdout, stderr, timed_out} on normal exit.
    Returns {ok: False, error: str} when Docker is unavailable or misconfigured.
    """
    if lang not in _LANG_STDIN_CMD:
        return {
            "ok": False,
            "error": f"Unknown language {lang!r}. Supported: python, lean4, coq",
        }

    if not _is_docker_available():
        return {
            "ok": False,
            "error": "Docker is not available. Start Docker Desktop.",
        }

    cmd = _LANG_STDIN_CMD[lang]
    memory = _get_memory_limit()
    container_name = f"maestro-sandbox-{uuid.uuid4().hex[:12]}"

    try:
        docker_cmd = [
            "docker", "run", "--rm", "-i",
            "--name", container_name,
            "--network", "none",
            "--memory", memory,
            "--cpus", "1",
            SANDBOX_IMAGE,
        ] + cmd

        proc = subprocess.Popen(
            docker_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = proc.communicate(
                input=code.encode("utf-8"), timeout=timeout
            )
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                subprocess.run(
                    ["docker", "kill", container_name],
                    capture_output=True,
                    timeout=5,
                )
            except Exception:
                pass
            return {"ok": False, "stdout": "", "stderr": "", "timed_out": True}

        return {
            "ok": proc.returncode == 0,
            "stdout": stdout_b.decode("utf-8", errors="replace"),
            "stderr": stderr_b.decode("utf-8", errors="replace"),
            "timed_out": False,
        }

    except Exception as exc:
        logger.error("[sandbox] Error running sandbox (lang=%s): %s", lang, exc)
        return {
            "ok": False,
            "error": str(exc),
            "stdout": "",
            "stderr": "",
            "timed_out": False,
        }


# ---------------------------------------------------------------------------
# get_lean4_proof_state — Gap 12
# ---------------------------------------------------------------------------

def _build_lean_server_driver(lean_source: str, line: int, col: int) -> str:
    """
    Build a Python script that runs inside the sandbox container, starts lean
    --server, queries the proof state via JSON-RPC, and prints the result as
    a single JSON line to stdout.

    lean_source is embedded as a raw string; line/col are 0-indexed (LSP).
    """
    # Embed lean_source safely: use repr() so all special chars are escaped.
    escaped = repr(lean_source)
    return f"""\
import subprocess, json, time, sys, os

# elan installs the toolchain under /root; ensure HOME points there.
os.environ.setdefault("HOME", "/root")

LEAN_SOURCE = {escaped}
LINE = {line}
COL = {col}

proc = subprocess.Popen(
    ["lake", "env", "lean", "--server"],
    cwd="/mathlib-project",
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    text=True,
    bufsize=1,
)

def _send(obj):
    body = json.dumps(obj)
    header = f"Content-Length: {{len(body.encode())}}\\r\\n\\r\\n"
    proc.stdin.write(header + body)
    proc.stdin.flush()

def _recv():
    header_lines = []
    while True:
        line = proc.stdout.readline()
        if not line:
            return None
        stripped = line.strip()
        if stripped == "":
            break
        header_lines.append(stripped)
    content_length = 0
    for h in header_lines:
        if h.lower().startswith("content-length:"):
            content_length = int(h.split(":", 1)[1].strip())
    body = proc.stdout.read(content_length)
    return json.loads(body)

try:
    _send({{"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {{"processId": None, "rootUri": None, "capabilities": {{}}}}}})
    _recv()

    _send({{"jsonrpc": "2.0", "method": "textDocument/didOpen",
            "params": {{"textDocument": {{
                "uri": "file:///mathlib-project/Verify.lean",
                "languageId": "lean4",
                "version": 1,
                "text": LEAN_SOURCE,
            }}}}}})

    # Give the elaborator time to process the file.
    time.sleep(6)

    _send({{"jsonrpc": "2.0", "id": 2, "method": "$/lean/plainGoal",
            "params": {{
                "textDocument": {{"uri": "file:///mathlib-project/Verify.lean"}},
                "position": {{"line": LINE, "character": COL}},
            }}}})

    goal_result = None
    deadline = time.time() + 25
    while time.time() < deadline:
        r = _recv()
        if r is None:
            break
        if r.get("id") == 2:
            goal_result = r
            break

    proc.terminate()

    if goal_result and "result" in goal_result and goal_result["result"]:
        res = goal_result["result"]
        goals = res.get("goals", [])
        goal_str = goals[0] if goals else ""
        hyps: list[str] = []
        # Lean infoview format: hypothesis lines precede "\\u22a2 ..." (⊢)
        if "\\n" in goal_str:
            parts = goal_str.split("\\n")
            for part in parts[:-1]:
                stripped = part.strip()
                if stripped and ":" in stripped:
                    hyps.append(stripped)
            goal_str = parts[-1].strip()
        print(json.dumps({{"ok": True, "goal": goal_str, "hypotheses": hyps, "messages": []}}))
    else:
        print(json.dumps({{"ok": False, "error": "No proof state from lean server",
                           "goal": None, "hypotheses": [], "messages": []}}))

except Exception as exc:
    try:
        proc.terminate()
    except Exception:
        pass
    print(json.dumps({{"ok": False, "error": str(exc), "goal": None,
                       "hypotheses": [], "messages": []}}))
"""


def get_lean4_proof_state(lean_source: str, line: int, col: int = 0) -> dict:
    """
    Return the Lean4 proof state (goal + hypotheses) at the given 1-indexed line.

    Runs the Lean language server inside the existing Docker sandbox via a
    Python driver script.  No new Docker image or mode required.

    Returns:
        {"ok": True,  "goal": "⊢ ...", "hypotheses": [...], "messages": [...]}
        {"ok": False, "error": "...",  "goal": None,  "hypotheses": [], "messages": []}
    """
    _err_base: dict = {"goal": None, "hypotheses": [], "messages": []}

    if not _is_docker_available():
        return {"ok": False, "error": "Docker unavailable", **_err_base}

    # LSP positions are 0-indexed; callers pass 1-indexed line numbers.
    lsp_line = max(0, int(line) - 1)
    lsp_col = max(0, int(col))

    driver = _build_lean_server_driver(lean_source, lsp_line, lsp_col)

    # 90 s: 6 s elaboration sleep + 25 s response window + Docker overhead.
    result = run_in_sandbox(driver, lang="python", timeout=90)

    stdout = result.get("stdout", "").strip()
    if not stdout:
        err = result.get("error") or result.get("stderr") or "no output from sandbox"
        return {"ok": False, "error": err, **_err_base}

    # The driver prints exactly one JSON line as its last line.
    last_line = stdout.split("\n")[-1].strip()
    try:
        return json.loads(last_line)
    except json.JSONDecodeError:
        return {"ok": False, "error": f"Could not parse driver output: {last_line[:400]}",
                **_err_base}
