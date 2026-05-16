"""
app/agent/verifiers.py
----------------------
Pluggable verifier framework for pipeline stage gates.

run_verifier(task_id, stage_config) dispatches to the configured verifier and
returns True (pass) or False (fail).  Called by CustomLLMAgent (and any future
agent that supports formal verification gates).

Supported verifiers:
  none          — always passes (default)
  python_sympy  — runs sympy_proof_code from task content as a Python subprocess
  lean4         — stub; logs a warning and returns False (Lean 4 not yet wired)
  coq           — stub; logs a warning and returns False (Coq not yet wired)
  custom_script — runs verifier_cmd (from stage config) with task content JSON as stdin

Security note: SymPy and custom_script run in a subprocess with a timeout.
For the current single-user local deployment this is acceptable.  For
multi-tenant or adversarial environments, proper sandboxing would be required.
"""

from __future__ import annotations

import json
import logging
import subprocess
import tempfile

logger = logging.getLogger(__name__)


def run_verifier(task_id: str, stage_config) -> bool:
    """
    Dispatch to the configured verifier for `stage_config`.

    `stage_config` is a StageConfig (from pipeline_router) or any object that
    has a `config` dict.  The verifier type is read from `config["verifier"]`.

    Returns True (pass) or False (fail).
    """
    config = getattr(stage_config, "config", None) or {}
    verifier = config.get("verifier", "none")

    if verifier == "none":
        return True
    elif verifier == "python_sympy":
        return _run_sympy(task_id, config)
    elif verifier == "lean4":
        return _run_lean4(task_id, config)
    elif verifier == "coq":
        return _run_coq(task_id, config)
    elif verifier == "custom_script":
        return _run_custom(task_id, config)
    else:
        raise ValueError(f"Unknown verifier: {verifier!r}")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_task_content(task_id: str) -> dict:
    from app.database import get_task
    task = get_task(task_id)
    if not task:
        return {}
    raw = task.content
    if not raw:
        return {}
    try:
        if isinstance(raw, str):
            return json.loads(raw)
        if isinstance(raw, dict):
            return raw
    except Exception:
        pass
    return {}


def _run_sympy(task_id: str, config: dict) -> bool:
    content = _get_task_content(task_id)
    proof_code = content.get("sympy_proof_code", "")
    if not proof_code:
        logger.warning("[verifiers] python_sympy: no sympy_proof_code in task content (task=%s)", task_id)
        return False
    try:
        result = subprocess.run(
            ["python", "-c", proof_code],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            logger.info(
                "[verifiers] python_sympy: proof failed (task=%s)\nstderr: %s",
                task_id, result.stderr[:500],
            )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        logger.warning("[verifiers] python_sympy: timeout after 30s (task=%s)", task_id)
        return False
    except Exception as exc:
        logger.error("[verifiers] python_sympy: subprocess error (task=%s): %s", task_id, exc)
        return False


def _run_lean4(task_id: str, config: dict) -> bool:
    # Lean 4 integration is deferred — the slot exists but is not yet wired.
    logger.warning(
        "[verifiers] lean4 verifier is a stub — Lean 4 not yet integrated (task=%s)", task_id
    )
    return False


def _run_coq(task_id: str, config: dict) -> bool:
    logger.warning(
        "[verifiers] coq verifier is a stub — Coq not yet integrated (task=%s)", task_id
    )
    return False


def _run_custom(task_id: str, config: dict) -> bool:
    verifier_cmd = config.get("verifier_cmd", "")
    if not verifier_cmd:
        logger.error(
            "[verifiers] custom_script: no verifier_cmd in stage config (task=%s)", task_id
        )
        return False
    content = _get_task_content(task_id)
    content_json = json.dumps(content)
    try:
        result = subprocess.run(
            verifier_cmd,
            shell=True,
            input=content_json,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            logger.info(
                "[verifiers] custom_script: verifier returned %d (task=%s)\nstderr: %s",
                result.returncode, task_id, result.stderr[:500],
            )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        logger.warning("[verifiers] custom_script: timeout after 60s (task=%s)", task_id)
        return False
    except Exception as exc:
        logger.error("[verifiers] custom_script: subprocess error (task=%s): %s", task_id, exc)
        return False
