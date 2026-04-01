"""
app/agent/arch_gen_agent.py
---------------------------
Generates a single architecture card for a given category by making one
LLM call whose context is the project's existing file summaries (2 sentences
each).  No tool use, no multi-turn — just prompt → card.

Called by the scheduler's _run_arch_gen_job() worker thread.
"""

from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger(__name__)

# Canonical list — must match ARCH_CATEGORY_COLORS keys in kanban.js
ARCH_CATEGORIES: list[str] = [
    "Platform", "Design", "Testing", "Security", "Performance",
    "API", "Tooling", "Data", "UX", "Accessibility",
    "Compliance", "Deployment", "Observability", "General",
]

_SYSTEM_PROMPT = (
    "You are an architecture advisor documenting a software project. "
    "Your notes are injected verbatim into AI agents as authoritative project constraints. "
    "Be specific, concrete, and accurate — your note will be used by agents to make "
    "implementation decisions. "
    "Write only the note text. No preamble, no title, no headings, no bullet points."
)

_USER_TEMPLATE = """\
Project: {project}
Category: {category}

Below are short summaries of source files in this project (one line each):

{summaries}

Write a concise 2–3 sentence architecture note about the **{category}** aspects of \
this project. The note will be injected as a constraint into all AI agents working \
on the codebase. Focus only on {category} — be specific and actionable.\
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
) -> dict:
    """Fetch file summaries, call the LLM, create the architecture card.

    Returns ``{"prompt_tokens": int, "completion_tokens": int}``.
    Raises on LLM error or empty response so the scheduler can mark the job failed.
    """
    from app.database import get_file_summaries_for_project_root, create_task
    from app.agent.llm_client import call_llm

    summaries = get_file_summaries_for_project_root(project_root)
    if not summaries:
        logger.warning(
            "[arch_gen] No file summaries found for project '%s' (root=%s). "
            "Run a prewarm first.",
            project, project_root,
        )
        raise RuntimeError(
            f"No file summaries found for project '{project}'. "
            "Run a prewarm/file-summary pass first."
        )

    lines: list[str] = []
    for row in summaries:
        rel = _rel_path(row.file_path, project_root)
        text = _two_sentences(
            (getattr(row, 'short_summary', None) or row.summary or "").strip()
        )
        if text:
            lines.append(f"- {rel}: {text}")

    if not lines:
        raise RuntimeError("All file summaries were empty — nothing to synthesise.")

    summary_block = "\n".join(lines)

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
        max_tokens=256,
        task_id=None,
        llm_id=llm_id,
        budget_id=budget_id,
    )

    body = (
        response.get("choices", [{}])[0]
        .get("message", {})
        .get("content", "")
        .strip()
    )
    usage = response.get("usage", {})
    prompt_tokens = usage.get("prompt_tokens", 0)
    completion_tokens = usage.get("completion_tokens", 0)

    if not body:
        raise ValueError("LLM returned empty content for arch gen job")

    create_task(
        title=f"{category} Architecture",
        task_type="architecture",
        description=body,
        content={"category": category, "priority": "normal"},
        project=project,
    )
    logger.info(
        "[arch_gen] Created '%s' arch card for project '%s' (%d prompt / %d completion tokens).",
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
