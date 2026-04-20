"""
app/agent/file_summary_agent.py
--------------------------------
FILE SUMMARY Agent - generates and DB-caches natural-language summaries
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

        Called by the scheduler worker thread.  Performs the LLM call(s),
        stores both summary and short_summary in file_summaries, returns
        token counts.

Summarization strategy
----------------------
Files are chunked by character count (~32 k chars ~= 8 k tokens per chunk).
Each chunk produces a 1-2 sentence section summary.  A final rollup call
combines all section summaries (plus verbatim content when the file is small
enough) and returns two outputs in a structured format:

    FULL_SUMMARY:
    <comprehensive description, length scaled to file size>

    SHORT_SUMMARY:
    <exactly 2 sentences - used in directory listings and agent snapshots>

Small files that fit in a single chunk skip straight to the rollup call.
Update calls (previous_summary set) also use a single call.
"""

from __future__ import annotations

import hashlib
import logging
import os
from typing import Any

from app.agent.llm_client import is_shutting_down, ShutdownError

logger = logging.getLogger(__name__)
AGENT_NAME = "File Summary Agent"

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Maximum characters stored in the DB job's file_content column.
# This is only a fallback used when the file cannot be re-read at execute
# time; the scheduler always tries to read the full file from disk first.
_MAX_CONTENT_CHARS = 32_000

# Fallback window size when max_context is unknown (~8 k tokens at 4 c/tok).
_CHUNK_CHARS_DEFAULT = 32_000

# If the full file fits within this many chars the verbatim content is
# appended to the rollup prompt alongside the section summaries.
_ROLLUP_MAX_CHARS = 400_000

# Chars-per-token estimate for code content (conservative: short identifiers,
# brackets, and keywords each burn a token → denser than prose).
_CHARS_PER_TOKEN = 3

# Window = 30% of LLM context in chars; step = 50% of window (50% overlap).
_WINDOW_FRACTION = 0.30
_STEP_FRACTION = 0.15


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _sha1_and_size(raw: bytes) -> tuple[str, int]:
    return hashlib.sha1(raw).hexdigest(), len(raw)


def _length_target(content: str) -> str:
    """Return a prose length description scaled to the file's line count."""
    line_count = content.count('\n') + 1
    if line_count <= 100:
        return "2-3 sentences"
    if line_count <= 500:
        return "a short paragraph (3-5 sentences)"
    return "2 concise paragraphs"


def _compute_window_step(max_context: int) -> tuple[int, int]:
    """Return (window_chars, step_chars) from the LLM's max context length.

    Window = 30% of context in chars (at 3 chars/token for code).
    Step   = 15% of context → 50% overlap between consecutive windows.
    Falls back to _CHUNK_CHARS_DEFAULT when max_context is 0 or unknown.
    """
    if max_context > 0:
        window = max(
            _CHUNK_CHARS_DEFAULT,
            int(max_context * _WINDOW_FRACTION * _CHARS_PER_TOKEN),
        )
        step = max(
            _CHUNK_CHARS_DEFAULT // 2,
            int(max_context * _STEP_FRACTION * _CHARS_PER_TOKEN),
        )
    else:
        window = _CHUNK_CHARS_DEFAULT
        step = _CHUNK_CHARS_DEFAULT
    return window, step


def _sliding_windows(content: str, window: int, step: int) -> list[tuple[int, int, str]]:
    """Return (start, end, text) windows over content with 50% overlap.

    Breaks at newline boundaries near each edge so windows align with complete
    lines rather than splitting in the middle of a statement.
    """
    windows: list[tuple[int, int, str]] = []
    n = len(content)
    start = 0
    while start < n:
        end = min(start + window, n)
        # Snap end to nearest newline (unless we're at EOF)
        if end < n:
            nl = content.rfind('\n', start, end)
            if nl > start:
                end = nl + 1
        windows.append((start, end, content[start:end]))
        if end >= n:
            break
        # Next window starts 'step' chars after current start, snapped to newline
        next_start = start + step
        nl2 = content.find('\n', next_start)
        start = (nl2 + 1) if (0 < nl2 < next_start + 200) else next_start
    return windows


def _parse_dual_summary(text: str) -> tuple[str, str]:
    """Parse a FULL_SUMMARY / SHORT_SUMMARY structured response.

    Returns (full_summary, short_summary).  Falls back gracefully when the
    LLM doesn't follow the format exactly.
    """
    if "FULL_SUMMARY:" in text and "SHORT_SUMMARY:" in text:
        after_full = text.split("FULL_SUMMARY:", 1)[1]
        full_part = after_full.split("SHORT_SUMMARY:", 1)[0].strip()
        short_part = after_full.split("SHORT_SUMMARY:", 1)[1].strip()
        return full_part, short_part
    # Fallback: whole text is the full summary; extract first 2 sentences as short
    full = text.strip()
    flat = full.replace('\n', ' ')
    sentences = [s.strip() for s in flat.split('. ') if s.strip()]
    short = '. '.join(sentences[:2])
    if short and not short.endswith('.'):
        short += '.'
    return full, short or full


def _build_static_json(abs_path: str) -> "str | None":
    """Run tree-sitter static analysis (Python files only). Returns JSON or None."""
    if not abs_path.endswith(".py"):
        return None
    try:
        import json as _json
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


def _extract_text(response: dict) -> str:
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

    completion_key == "" means the result is already in the DB cache - the
    caller should read it immediately without waiting.

    When completion_key is non-empty, the caller should block on
    wait_for_completion(completion_key, timeout) then read from
    get_file_summary(sha1, filesize).

    Raises ValueError if the file cannot be read.
    Returns ("", "", 0) silently for binary files, oversized files, or large logs.
    """
    from app.agent.config import SUMMARY_MAX_FILE_SIZE

    try:
        filesize = os.path.getsize(abs_path)
    except OSError as exc:
        raise ValueError(f"Cannot stat '{abs_path}': {exc}") from exc

    # Safety limits for summarization:
    # 1. Hard cap from config (default 1MB)
    if filesize > SUMMARY_MAX_FILE_SIZE:
        logger.debug("enqueue_file_summary: skipping oversized file '%s' (%d bytes)", abs_path, filesize)
        return "", "", 0

    # 2. Log file exclusion: skip .log and rotated log files (.log.1, .log.2, …)
    #    at any size — they are operational artifacts with no architectural value.
    import re as _re
    if _re.search(r'\.log(\.\d+)?$', abs_path, _re.IGNORECASE):
        logger.debug("enqueue_file_summary: skipping log file '%s'", abs_path)
        return "", "", 0

    try:
        with open(abs_path, "rb") as fh:
            # Only read the header to check for binary content first
            header = fh.read(8192)
            if b"\x00" in header:
                logger.debug("enqueue_file_summary: skipping binary file '%s'", abs_path)
                return "", "", 0
            
            # If not binary, we need the full content for SHA1 and preview
            fh.seek(0)
            raw = fh.read()
    except OSError as exc:
        raise ValueError(f"Cannot read '{abs_path}': {exc}") from exc

    sha1, filesize_actual = _sha1_and_size(raw)
    completion_key = f"file_summary:{sha1}:{filesize_actual}"

    # 1. DB cache hit - already summarised
    from app.database import get_file_summary
    if get_file_summary(sha1, filesize) is not None:
        logger.debug("file_summary cache hit (enqueue): %s (sha1=%s)", abs_path, sha1[:8])
        return "", sha1, filesize

    # 2. Dedup - existing pending/running job
    from app.database import get_file_summary_job_by_sha1
    existing_job = get_file_summary_job_by_sha1(sha1, filesize)

    # 3. Get-or-create completion event
    from app.agent.scheduler import get_or_create_completion_event
    _event, created = get_or_create_completion_event(completion_key)

    # 4. Create DB job only if no existing job and we just created the event.
    #    Store a small content snapshot as a fallback only - the scheduler
    #    always re-reads the full file from disk at execute time.
    if created and existing_job is None:
        content_preview = raw.decode("utf-8", errors="replace")[:_MAX_CONTENT_CHARS]
        static_json = _build_static_json(abs_path)
        from app.database import create_file_summary_job
        create_file_summary_job(
            sha1, filesize, abs_path, content_preview,
            static_analysis_json=static_json,
            llm_id=llm_id,
            budget_id=budget_id,
            task_id=task_id,
            priority=priority,
            previous_summary=previous_summary,
        )
        logger.debug("file_summary job created: %s (sha1=%s)", abs_path, sha1[:8])

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
    stream_idle_timeout: "float | None" = None,
    max_context: int = 0,
) -> dict:
    """Perform LLM call(s), store result in file_summaries, return token counts.

    Produces two outputs per file:
      summary       - comprehensive description (length scaled to file size)
      short_summary - exactly 2 sentences for directory listings / snapshots

    Three execution paths:
      1. Update   - previous_summary provided: single call with change context
      2. Small    - file fits in one chunk (<= _CHUNK_CHARS): single call
      3. Large    - multiple chunks: one call per chunk for section summaries,
                    then one rollup call combining all sections

    All paths use a structured FULL_SUMMARY / SHORT_SUMMARY response format.
    Called by _run_file_summary_job() in the scheduler worker thread.
    """
    from app.agent.llm_client import set_llm_session_context
    set_llm_session_context(AGENT_NAME)
    if is_shutting_down():
        raise ShutdownError("Server is shutting down")

    from app.agent.llm_client import call_llm
    from app.database import create_file_summary

    basename = os.path.basename(file_path)
    length_desc = _length_target(file_content)

    # Build a short architecture context preamble (Platform/Tooling/Data/General only).
    # This helps the agent understand what kind of project it is summarising,
    # so it can use domain-appropriate terminology and focus areas.
    _arch_preamble = ""
    if task_id:
        try:
            from app.database import get_task as _get_task
            from app.agent.project_snapshot import build_architecture_context
            _task_rec = _get_task(task_id)
            if _task_rec and _task_rec.project:
                _arch = build_architecture_context(
                    _task_rec.project, agent_type='file_summary'
                )
                if _arch:
                    _arch_preamble = f"{_arch}\n\n"
        except Exception:
            pass

    total_prompt = 0
    total_completion = 0

    # ── Helper: fire one LLM call ─────────────────────────────────────────
    async def _call(prompt_text: str) -> "tuple[str, int, int]":
        response = await call_llm(
            [{"role": "user", "content": prompt_text}],
            task_id=task_id,
            llm_id=llm_id,
            budget_id=budget_id,
            base_url=llm_base_url,
            model=llm_model,
            stream=stream_idle_timeout is not None,
            stream_idle_timeout=stream_idle_timeout,
            agent_name=AGENT_NAME,
        )
        text = _extract_text(response)
        usage = response.get("usage", {})
        return text, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)

    _DUAL_FORMAT = (
        "Respond with exactly this format (no extra text before or after):\n\n"
        "FULL_SUMMARY:\n"
        "<{length_desc} summary - overall purpose, main responsibilities, key patterns>\n\n"
        "SHORT_SUMMARY:\n"
        "<exactly 2 sentences suitable for a file directory listing>"
    ).format(length_desc=length_desc)

    # ── Path 1: update call - single call with change context ────────────
    if previous_summary:
        content_snippet = file_content[:_ROLLUP_MAX_CHARS]
        prompt = (
            f"{_arch_preamble}"
            f"A source file has been modified.\n\n"
            f"Previous summary: {previous_summary}\n\n"
            f"Current contents of {basename}:\n```\n{content_snippet}\n```\n\n"
            f"Update the summary to reflect any significant changes. "
            f"If the substance is unchanged you may reuse the previous summary verbatim.\n\n"
            + _DUAL_FORMAT
        )
        raw_text, pp, cp = await _call(prompt)
        total_prompt += pp
        total_completion += cp
        if not raw_text:
            logger.warning(f"[{AGENT_NAME}] LLM returned empty response for update call. Falling back to previous.")
            full_summary, short_summary = previous_summary, "(summary unchanged)"
        else:
            full_summary, short_summary = _parse_dual_summary(raw_text)

    # ── Path 2: small file - fits in one window ─────────────────────────
    elif len(file_content) <= _compute_window_step(max_context)[0]:
        prompt = (
            f"{_arch_preamble}"
            f"Analyze this source file.\n\n"
            f"File: {basename}\n\n```\n{file_content}\n```\n\n"
            + _DUAL_FORMAT
        )
        raw_text, pp, cp = await _call(prompt)
        total_prompt += pp
        total_completion += cp
        if not raw_text:
            logger.warning(f"[{AGENT_NAME}] LLM returned empty response for small-file call. Falling back to default.")
            full_summary, short_summary = "[Source file]", "[Source file]"
        else:
            full_summary, short_summary = _parse_dual_summary(raw_text)

    # ── Path 3: large file - sliding-window summaries + rollup ───────────
    # Window = 30% of LLM context (at 3 chars/token for code); step = 15% so
    # consecutive windows overlap by 50%.  This ensures code that straddles a
    # boundary always appears in full in at least one window.
    else:
        window_chars, step_chars = _compute_window_step(max_context)
        windows = _sliding_windows(file_content, window_chars, step_chars)
        window_count = len(windows)
        logger.debug(
            "file_summary sliding-window: %s - %d chars -> %d windows "
            "(window=%d, step=%d, ctx=%d)",
            basename, len(file_content), window_count,
            window_chars, step_chars, max_context,
        )

        # One concise summary per window
        section_summaries: list[str] = []
        for idx, (w_start, w_end, window_text) in enumerate(windows):
            if is_shutting_down():
                raise ShutdownError("Server is shutting down")

            prompt = (
                f"Summarize the following section of {basename} "
                f"(chars {w_start + 1}-{w_end} of {len(file_content)}) "
                f"in 2-3 sentences. Focus on the critical information flow: "
                f"what enters, what is decided or transformed, and what exits.\n\n"
                f"```\n{window_text}\n```"
            )
            section_text, pp, cp = await _call(prompt)
            total_prompt += pp
            total_completion += cp
            section_summaries.append(
                f"Window {idx + 1}/{window_count} "
                f"(chars {w_start + 1}-{w_end}): "
                f"{section_text or '(no summary)'}"
            )

        # Rollup - combine all window summaries into full + short summaries
        summaries_block = "\n".join(section_summaries)
        rollup_prompt = (
            f"{_arch_preamble}"
            f"You have read {basename} ({len(file_content):,} chars) in "
            f"{window_count} overlapping windows. Window summaries:\n\n"
            f"{summaries_block}\n\n"
        )
        if len(file_content) <= _ROLLUP_MAX_CHARS:
            rollup_prompt += f"Full file contents:\n```\n{file_content}\n```\n\n"
        rollup_prompt += _DUAL_FORMAT

        raw_text, pp, cp = await _call(rollup_prompt)
        total_prompt += pp
        total_completion += cp
        if not raw_text:
            logger.warning(f"[{AGENT_NAME}] LLM returned empty response for rollup call. Falling back to default.")
            full_summary, short_summary = "[Large source file]", "[Large source file]"
        else:
            full_summary, short_summary = _parse_dual_summary(raw_text)

    create_file_summary(
        sha1, filesize, file_path, full_summary,
        static_analysis_json,
        short_summary=short_summary,
    )
    logger.debug("file_summary stored: %s (sha1=%s)", file_path, sha1[:8])

    return {"prompt_tokens": total_prompt, "completion_tokens": total_completion}
