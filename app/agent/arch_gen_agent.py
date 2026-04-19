"""
app/agent/arch_gen_agent.py
---------------------------
Generates a single architecture card for a given category by making one
LLM call whose context is the project's existing file summaries (2 sentences
each).  No tool use, no multi-turn - just prompt -> card.

Called by the scheduler's _run_arch_gen_job() worker thread.
"""

from __future__ import annotations

import logging
import os
import re

from app.agent.llm_client import is_shutting_down, ShutdownError, extract_text_response

logger = logging.getLogger(__name__)
AGENT_NAME = "Arch Gen Agent"

# Canonical list - must match ARCH_CATEGORY_COLORS keys in kanban.js
ARCH_CATEGORIES: list[str] = [
    "Platform", "Design", "Testing", "Security", "Performance",
    "API", "Tooling", "Data", "UX", "Accessibility",
    "Compliance", "Deployment", "Observability", "General",
]

_SYSTEM_PROMPT = (
    "/no_think\n"
    "You are an architecture advisor documenting a software project. "
    "Your notes are injected verbatim into AI agents as authoritative project constraints. "
    "Be specific, concrete, and accurate - your note will be used by agents to make "
    "implementation decisions. "
    "Write only the note text. No preamble, no title, no headings, no bullet points."
)

_USER_TEMPLATE = """
Project: {project}
Category: {category}

Below are short summaries of source files in this project (one line each):

{summaries}

Write a concise 2-3 sentence architecture note about the **{category}** aspects of
this project. The note will be injected as a constraint into all AI agents working
on the codebase. Focus only on {category} - be specific and actionable.
"""


async def execute_arch_gen_job(
    *,
    project: str,
    category: str,
    project_root: str,
    llm_id: int,
    budget_id: int,
    llm_base_url: str,
    llm_model: str,
    max_context: int | None = None,
) -> dict:
    """Fetch file summaries, call the LLM, create the architecture card.

    Returns ``{"prompt_tokens": int, "completion_tokens": int}``.
    Raises on LLM error or empty response so the scheduler can mark the job failed.
    """
    from app.agent.llm_client import set_llm_session_context
    set_llm_session_context(AGENT_NAME)
    if is_shutting_down():
        raise ShutdownError("Server is shutting down")

    from app.database import get_file_summaries_for_project_root, create_task
    from app.agent.llm_client import call_llm
    from app.agent.config import SUMMARY_CONTEXT_RATIO
    from app.agent.path_filter import is_ignored

    summaries = get_file_summaries_for_project_root(project_root)
    if not summaries:
        logger.warning(
            f"[{AGENT_NAME}] No file summaries found for project '%s' (root=%s). "
            "Run a prewarm first.",
            project, project_root,
        )
        raise RuntimeError(
            f"No file summaries found for project '{project}'. "
            "Run a prewarm/file-summary pass first."
        )

    # Deduplicate: multiple summary rows may exist per file (re-runs, stale cache).
    # Keep only the most recent row per file path (summaries are ordered by path,
    # but may have multiple rows; pick the one with the best content).
    seen_paths: dict[str, str] = {}  # rel_path -> best text so far
    for row in summaries:
        fp = row.file_path
        if is_ignored(fp, project_root):
            continue
        rel = _rel_path(fp, project_root)
        text = _two_sentences(
            (getattr(row, 'short_summary', None) or row.summary or "").strip()
        )
        if text and (rel not in seen_paths or len(text) > len(seen_paths[rel])):
            seen_paths[rel] = text

    lines: list[str] = [f"- {rel}: {text}" for rel, text in seen_paths.items()]

    if not lines:
        raise RuntimeError("All file summaries were empty - nothing to synthesise.")

    # Cap the summary block to avoid overwhelming the LLM's context window.
    # We use a fraction of the context window (default 10%) to allow space for 
    # other architectural notes and the generated response.
    _ratio = SUMMARY_CONTEXT_RATIO if max_context else 0.10
    _MAX_SUMMARY_CHARS = max(2000, int((max_context or 12000) * _ratio) * 3)

    summary_block = "\n".join(lines)
    if len(summary_block) > _MAX_SUMMARY_CHARS:
        # Truncate to the last complete line within the cap.
        truncated = summary_block[:_MAX_SUMMARY_CHARS]
        last_nl = truncated.rfind("\n")
        summary_block = truncated[:last_nl] if last_nl != -1 else truncated
        kept = summary_block.count("\n") + 1
        logger.warning(
            "[%s] Summary block truncated to %d lines (%d chars, cap=%d) — "
            "%d lines omitted.",
            AGENT_NAME, kept, len(summary_block), _MAX_SUMMARY_CHARS, len(lines) - kept,
        )

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": _USER_TEMPLATE.format(
            project=project,
            category=category,
            summaries=summary_block,
        )},
    ]

    response = await call_llm(
        messages,
        base_url=llm_base_url,
        model=llm_model,
        temperature=0.4,
        max_tokens=512,
        max_retries=5,
        task_id=None,
        llm_id=llm_id,
        budget_id=budget_id,
        agent_name=AGENT_NAME,
    )

    raw_body = extract_text_response(response).strip()
    usage = response.get("usage", {})
    prompt_tokens = usage.get("prompt_tokens", 0)
    completion_tokens = usage.get("completion_tokens", 0)

    # Strip Qwen3 <think>...</think> blocks in case /no_think was ignored.
    body = re.sub(r"<think>.*?</think>", "", raw_body, flags=re.DOTALL).strip()

    if not body:
        detail = "contained only <think> block" if raw_body else "was empty"
        raise ValueError(f"LLM returned no usable content for arch gen job (response {detail})")

    create_task(
        title=f"{category} Architecture",
        task_type="architecture",
        description=body,
        content={"category": category, "priority": "normal"},
        project=project,
    )
    logger.info(
        f"[{AGENT_NAME}] Created '%s' arch card for project '%s' (%d prompt / %d completion tokens).",
        category, project, prompt_tokens, completion_tokens,
    )

    return {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rel_path(abs_path: str, root: str) -> str:
    """Return abs_path relative to root using forward slashes."""
    try:
        return os.path.relpath(abs_path, root).replace("\\", "/")
    except ValueError:
        return os.path.basename(abs_path)


def _two_sentences(text: str) -> str:
    """Return at most the first two sentences of text."""
    if not text:
        return ""
    sentences = re.split(r'(?<=[.!?])\s+', text.strip())
    return " ".join(sentences[:2])
