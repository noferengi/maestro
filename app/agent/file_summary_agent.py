"""
app/agent/file_summary_agent.py
--------------------------------
FILE SUMMARY Agent — generates and DB-caches natural-language summaries
for source files, routed through the scheduler's job queue.

Cache key: SHA1(file bytes) + file size in bytes.
The same content at any path (renamed, copied) hits the same cache row.

Public API
----------
    enqueue_file_summary(abs_path, *, task_id, llm_id, budget_id)
        -> (completion_key, sha1, filesize)

        completion_key is "" on a cache hit (caller should read directly).
        Otherwise caller should await wait_for_completion(completion_key, timeout)
        then read from get_file_summary(sha1, filesize).

    execute_file_summary(*, sha1, filesize, file_path, file_content, ...)
        -> {"prompt_tokens": int, "completion_tokens": int}

        Called by the scheduler worker thread.  Performs the LLM call, stores
        the result in file_summaries, returns token counts.
"""

from __future__ import annotations

import hashlib
import json as _json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# Maximum characters of file content sent to the LLM in a single-call (small-file) path.
_MAX_CONTENT_CHARS = 32_000

# Lines per chunk for large-file chunked summarization.
_CHUNK_LINES = 250

# If the full file fits within this many chars (≈ 100k tokens at 4 chars/token),
# include it verbatim in the rollup call alongside the chunk summaries.
_ROLLUP_MAX_CHARS = 400_000


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _sha1_and_size(raw: bytes) -> tuple[str, int]:
    return hashlib.sha1(raw).hexdigest(), len(raw)


def _length_target(line_count: int) -> str:
    if line_count <= 100:
        return "2 sentences"
    if line_count <= 500:
        return "a short paragraph (3-5 sentences)"
    return "2 concise paragraphs"


def _build_static_json(abs_path: str) -> "str | None":
    """Run tree-sitter static analysis (Python files only). Returns JSON or None."""
    if not abs_path.endswith(".py"):
        return None
    try:
        from app.agent.static_analysis import analyze_file
        analysis = analyze_file(abs_path)
        return _json.dumps({
            "classes": [c.name for c in analysis.classes],
            "functions": [f.name for f in analysis.functions],
            "imports": analysis.imports,
        })
    except Exception as exc:
        logger.debug("static_analysis skipped for %s: %s", abs_path, exc)
        return None


def _extract_summary(response: dict) -> str:
    """Pull content string from an OpenAI-compatible response dict."""
    choices = response.get("choices", [])
    if choices:
        msg = choices[0].get("message", {})
        return msg.get("content", "").strip()
    return ""


# ---------------------------------------------------------------------------
# Public: enqueue
# ---------------------------------------------------------------------------

def enqueue_file_summary(
    abs_path: str,
    *,
    task_id: "str | None" = None,
    llm_id: "int | None" = None,
    budget_id: "int | None" = None,
    previous_summary: "str | None" = None,
    priority: float = -1.0,
) -> "tuple[str, str, int]":
    """Enqueue a file summary job for scheduler dispatch.

    Returns (completion_key, sha1, filesize).

    completion_key == "" means the result is already in the DB cache — the
    caller should read it immediately without waiting.

    When completion_key is non-empty, the caller should block on
    wait_for_completion(completion_key, timeout) then read from
    get_file_summary(sha1, filesize).

    Raises ValueError if the file cannot be read.
    """
    try:
        with open(abs_path, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        raise ValueError(f"Cannot read '{abs_path}': {exc}") from exc

    sha1, filesize = _sha1_and_size(raw)
    completion_key = f"file_summary:{sha1}:{filesize}"

    # 1. DB cache hit — already summarised
    from app.database import get_file_summary
    if get_file_summary(sha1, filesize) is not None:
        logger.debug("file_summary cache hit (enqueue): %s (sha1=%s…)", abs_path, sha1[:8])
        return "", sha1, filesize

    # 2. Dedup — existing pending/running job
    from app.database import get_file_summary_job_by_sha1
    existing_job = get_file_summary_job_by_sha1(sha1, filesize)

    # 3. Get-or-create completion event
    from app.agent.scheduler import get_or_create_completion_event
    _event, created = get_or_create_completion_event(completion_key)

    # 4. Create DB job only if no existing job and we just created the event
    if created and existing_job is None:
        content = raw.decode("utf-8", errors="replace")
        static_json = _build_static_json(abs_path)
        from app.database import create_file_summary_job
        create_file_summary_job(
            sha1, filesize, abs_path, content[:_MAX_CONTENT_CHARS],
            static_analysis_json=static_json,
            llm_id=llm_id,
            budget_id=budget_id,
            task_id=task_id,
            priority=priority,
            previous_summary=previous_summary,
        )
        logger.debug("file_summary job created: %s (sha1=%s…)", abs_path, sha1[:8])

    return completion_key, sha1, filesize


# ---------------------------------------------------------------------------
# Public: execute (called by scheduler worker thread)
# ---------------------------------------------------------------------------

async def execute_file_summary(
    *,
    sha1: str,
    filesize: int,
    file_path: str,
    file_content: str,
    static_analysis_json: "str | None" = None,
    task_id: "str | None" = None,
    llm_id: "int | None" = None,
    budget_id: "int | None" = None,
    llm_base_url: "str | None" = None,
    llm_model: "str | None" = None,
    previous_summary: "str | None" = None,
) -> dict:
    """Perform LLM call(s), store result in file_summaries, return token counts.

    Small files (≤ 250 lines) and update calls (previous_summary set) use a
    single LLM call.  Large files are processed in 250-line chunks — one call
    per chunk to produce section summaries — then a final rollup call combines
    all section summaries into a single file-level summary.  If the full file
    content fits within ~100k tokens (400k chars) it is included verbatim in
    the rollup prompt.

    Called by _run_file_summary_job() in the scheduler worker thread.
    """
    from app.agent.llm_client import call_llm
    from app.database import create_file_summary

    basename = os.path.basename(file_path)
    lines = file_content.splitlines()
    line_count = len(lines)
    length_desc = _length_target(line_count)

    # ── Helper: fire one call and accumulate tokens ───────────────────────
    async def _call(prompt_text: str) -> "tuple[str, int, int]":
        response = await call_llm(
            [{"role": "user", "content": prompt_text}],
            temperature=0.1,
            task_id=task_id,
            llm_id=llm_id,
            budget_id=budget_id,
            base_url=llm_base_url,
            model=llm_model,
        )
        text = _extract_summary(response)
        usage = response.get("usage", {})
        return text, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)

    total_prompt = 0
    total_completion = 0

    # ── Path 1: previous_summary (update) or small file — single call ────
    if previous_summary or line_count <= _CHUNK_LINES:
        if previous_summary:
            content_snippet = file_content[:_ROLLUP_MAX_CHARS]
            prompt = (
                f"A source file has been modified. Previous summary:\n\n"
                f"> {previous_summary}\n\n"
                f"Current contents of {basename}:\n"
                f"```\n{content_snippet}\n```\n\n"
                f"Update the summary if the changes are significant. "
                f"Keep it to exactly 2 sentences. "
                f"If the substance is unchanged, you may return the previous summary verbatim."
            )
        else:
            prompt = (
                f"Summarize the following source file in {length_desc}. "
                f"Focus on what the file does, its main responsibilities, and key patterns.\n\n"
                f"File: {basename}\n\n"
                f"```\n{file_content}\n```"
            )
        summary_text, pp, cp = await _call(prompt)
        total_prompt += pp
        total_completion += cp
        if not summary_text:
            raise ValueError("LLM returned an empty summary")

    # ── Path 2: large file — chunked section summaries + rollup ──────────
    else:
        chunk_summaries: list[str] = []
        chunk_count = (line_count + _CHUNK_LINES - 1) // _CHUNK_LINES
        logger.debug(
            "file_summary chunked: %s — %d lines → %d chunks",
            basename, line_count, chunk_count,
        )

        for chunk_idx in range(chunk_count):
            start_line = chunk_idx * _CHUNK_LINES          # 0-based
            chunk_lines = lines[start_line: start_line + _CHUNK_LINES]
            chunk_text = "\n".join(chunk_lines)
            display_start = start_line + 1                 # 1-based for the prompt
            display_end = start_line + len(chunk_lines)

            prompt = (
                f"Summarize lines {display_start}-{display_end} of {basename} "
                f"in 1-2 sentences. Focus on what this section does.\n\n"
                f"```\n{chunk_text}\n```"
            )
            section_text, pp, cp = await _call(prompt)
            total_prompt += pp
            total_completion += cp
            chunk_summaries.append(
                f"Lines {display_start}-{display_end}: {section_text or '(no summary)'}"
            )

        # Rollup — combine all section summaries into one file-level summary.
        summaries_block = "\n".join(chunk_summaries)
        rollup_prompt = (
            f"You have read {basename} ({line_count} lines) in "
            f"{_CHUNK_LINES}-line sections. Section summaries:\n\n"
            f"{summaries_block}\n\n"
        )
        if len(file_content) <= _ROLLUP_MAX_CHARS:
            rollup_prompt += (
                f"Full file contents:\n```\n{file_content}\n```\n\n"
            )
        rollup_prompt += (
            f"Write a comprehensive summary of the complete file in {length_desc}. "
            f"Focus on overall purpose, main responsibilities, and key patterns."
        )

        summary_text, pp, cp = await _call(rollup_prompt)
        total_prompt += pp
        total_completion += cp
        if not summary_text:
            raise ValueError("LLM returned an empty rollup summary")

    create_file_summary(sha1, filesize, file_path, summary_text, static_analysis_json)
    logger.debug("file_summary stored: %s (sha1=%s…)", file_path, sha1[:8])

    return {"prompt_tokens": total_prompt, "completion_tokens": total_completion}
