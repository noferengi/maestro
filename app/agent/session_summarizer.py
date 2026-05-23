"""
app/agent/session_summarizer.py
---------------------------------
Cross-session learning via the 3+1 summarization pipeline.

Triggered from GenericStageAgent._on_max_turns() when a session exhausts its turns.

Windowing design
----------------
Each of the 3 parallel summarizers receives a chunk sized at 2/3 of the available
summarizer context window, with start points sliding by ~1/6 of that window:

    Chunk 1  [ 0%          → 67% of T ]
    Chunk 2  [ ~17% of T   → ~83% of T ]   (centered)
    Chunk 3  [ ~33% of T   → 100% of T ]

Every message is seen by at least 2 of the 3 summarizers. Messages in the middle
third are seen by all three. No event at a boundary is silently dropped.

For very long conversations (T >> max_context) the same formula degrades gracefully:
chunks cover start, middle, and end respectively with no gaps.

Chunk size = 2/3 * (max_context - OVERHEAD), where OVERHEAD reserves space for the
summarizer system prompt and its output tokens.  max_context is passed in from the
live LLM endpoint record so the window automatically adapts to whatever model is
serving the session.

Split points snap back to the nearest preceding assistant-role message so no
attempt cycle is cut mid-flight.

The merge agent folds in any prior accumulated learning from the document store,
producing a single master document that grows richer with each session.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)

# Tokens reserved for the summarizer's own system prompt + max_tokens output.
_SUMMARIZER_OVERHEAD = 2200

# Hard cap on a single rendered message to prevent one enormous tool result
# from consuming the entire chunk budget.  Expressed as chars (≈ tokens * 3).
_MSG_CHAR_CAP = 6000

# Minimum viable chunk budget (tokens).  Below this the conversation is too
# small to be worth the LLM cost of summarizing.
_MIN_BUDGET = 200


# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

_CHUNK_SYSTEM = """\
You are a session-learning analyst. You are reading a slice of a failed AI agent \
session — the agent exhausted its turns without finishing its task.

Write a concise but precise summary. A future agent will read this to avoid \
repeating the same mistakes. Be specific and technical: exact lemma names, exact \
error messages, exact tool arguments matter.

Output ONLY these four sections (use these exact headers):

ATTEMPTS:
Each distinct approach tried in this slice, with enough detail to recognise it again.

ERRORS:
Every concrete error or compilation failure — exact wording where available.

PROGRESS:
Partial wins: files written, code that compiled (even with sorry), correct API \
names confirmed, searches that returned useful results.

STATE_AT_END:
What the agent was actively working on at the very end of this slice.\
"""

_MERGE_SYSTEM = """\
You are a session-learning synthesizer. You have three summaries covering \
overlapping portions of the same failed agent session — early, middle, and \
late, each seeing roughly two-thirds of the full conversation. Multiple \
summarizers may have described the same events in different terms.

If prior accumulated learning from earlier sessions is provided, fold it in — \
do not discard knowledge from previous sessions. The output represents everything \
learned across ALL sessions so far.

SYNTHESIZE — DEDUPLICATE — do not concatenate. A future agent will read only \
this document before starting work.

Output ONLY these four sections (use these exact headers):

AVOID:
Specific approaches, API names, tactic patterns, or code snippets that FAILED — \
with exact error messages. The next agent must not waste turns on these.

KEEP:
Approaches, API names, partial proofs, file contents, or results that represent \
genuine progress and are worth building on directly.

CURRENT_STATE:
Precise description of where the work stands across all sessions: what exists, \
what compiles (even with sorry), what has been persisted to the workspace or \
document store.

RECOMMENDED_START:
A concrete, step-by-step starting point. Specific enough that the next agent \
can execute immediately without re-exploring. Include exact code snippets or \
API names where known.\
"""


# ---------------------------------------------------------------------------
# Token-weight helpers
# ---------------------------------------------------------------------------

def _token_weight(m: dict) -> int:
    """Rough token estimate for a single message (chars // 3)."""
    parts = [str(m.get("content") or "")]
    for tc in (m.get("tool_calls") or []):
        parts.append(str(tc.get("function", {}).get("arguments", "")))
    return max(1, sum(len(p) for p in parts) // 3)


def _extract_prefix(messages: list[dict]) -> tuple[list[dict], list[dict]]:
    """
    Separate the context prefix (system + first user message) from the
    conversation body.  The prefix is prepended to every chunk so every
    summarizer has full task context.
    """
    prefix: list[dict] = []
    for i, m in enumerate(messages):
        if m.get("role") in ("system", "user") and i <= 2:
            prefix.append(m)
        else:
            break
    return prefix, messages[len(prefix):]


# ---------------------------------------------------------------------------
# Overlapping window splitter
# ---------------------------------------------------------------------------

def _snap_to_assistant(body: list[dict], raw_idx: int) -> int:
    """
    Walk backward from raw_idx to the nearest assistant-role message.
    Ensures we never start a chunk mid-tool-result, keeping attempt
    cycles intact.
    """
    for i in range(raw_idx, -1, -1):
        if body[i].get("role") == "assistant":
            return i
    return 0


def _take_from(body: list[dict], start: int, budget: int) -> list[dict]:
    """Collect messages from start until the cumulative token budget is consumed."""
    chunk: list[dict] = []
    used = 0
    for m in body[start:]:
        w = _token_weight(m)
        # Always admit the first message so we never produce an empty chunk.
        if chunk and used + w > budget:
            break
        chunk.append(m)
        used += w
    return chunk


def _split_overlapping(
    messages: list[dict],
    max_context: int,
) -> tuple[list[dict], list[dict], list[dict]]:
    """
    Produce three overlapping chunks from a conversation.

    Each chunk is sized at 2/3 of the available summarizer budget, with start
    points spaced to cover the full conversation with ~1/3 overlap between
    adjacent chunks:

        Chunk 1  starts at token position 0
        Chunk 3  starts at token position  T - chunk_budget   (ends at T)
        Chunk 2  starts at token position (T - chunk_budget) / 2  (centered)

    This layout means:
      • When T ≤ chunk_budget    → all three chunks are identical (full coverage).
      • When T ≈ 3*chunk_budget/2 → the elegant 17%-slide case.
      • When T >> chunk_budget   → start / middle / end coverage without gaps.

    Split points snap back to the nearest assistant-role boundary.
    """
    prefix, body = _extract_prefix(messages)

    if not body:
        return prefix[:], prefix[:], prefix[:]

    prefix_cost = sum(_token_weight(m) for m in prefix)
    available   = max_context - _SUMMARIZER_OVERHEAD
    chunk_budget = max(_MIN_BUDGET, available * 2 // 3 - prefix_cost)

    # Build cumulative token-weight index for the body.
    cumulative: list[int] = []
    total = 0
    for m in body:
        total += _token_weight(m)
        cumulative.append(total)

    def _token_pos_to_idx(token_pos: int) -> int:
        """Convert a token position to a message index, snapped to assistant boundary."""
        if token_pos <= 0:
            return 0
        for i, c in enumerate(cumulative):
            if c >= token_pos:
                return _snap_to_assistant(body, i)
        return _snap_to_assistant(body, len(body) - 1)

    idx1 = 0
    idx3 = _token_pos_to_idx(max(0, total - chunk_budget))
    idx2 = _token_pos_to_idx(max(0, (total - chunk_budget) // 2))

    c1 = prefix + _take_from(body, idx1, chunk_budget)
    c2 = prefix + _take_from(body, idx2, chunk_budget)
    c3 = prefix + _take_from(body, idx3, chunk_budget)

    return c1, c2, c3


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _render_msg(m: dict) -> str:
    """Render a message to plain text, capped at _MSG_CHAR_CAP."""
    content = m.get("content") or ""
    if isinstance(content, list):
        content = " ".join(
            c.get("text", "") for c in content if isinstance(c, dict)
        )
    content = str(content)
    if len(content) > _MSG_CHAR_CAP:
        content = content[:_MSG_CHAR_CAP] + f"\n[...+{len(content) - _MSG_CHAR_CAP} chars truncated]"
    return content


def _chunk_to_text(chunk: list[dict]) -> str:
    lines: list[str] = []
    for m in chunk:
        role = m.get("role", "unknown").upper()
        if role == "SYSTEM":
            continue  # system is the summarizer's own prompt
        content = _render_msg(m)
        if content.strip():
            lines.append(f"[{role}]: {content}")
    return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# LLM calls
# ---------------------------------------------------------------------------

async def _summarize_chunk(
    chunk: list[dict],
    label: str,
    llm_id: int | None,
    budget_id: int | None,
    task_id: str,
    base_url: str | None,
    model: str | None,
) -> str:
    from app.agent.llm_client import call_llm

    body_text = _chunk_to_text(chunk)
    if not body_text.strip():
        return f"[{label}: empty slice — no content to summarize]"

    try:
        resp = await call_llm(
            messages=[
                {"role": "system", "content": _CHUNK_SYSTEM},
                {"role": "user",   "content": f"[Session slice — {label}]\n\n{body_text}"},
            ],
            base_url=base_url,
            model=model,
            max_tokens=1500,
            llm_id=llm_id,
            budget_id=budget_id,
            task_id=task_id,
            agent_name=f"session_summarizer:{label}",
        )
        return (resp.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
    except Exception as exc:
        logger.warning("[session_summarizer] %s failed: %s", label, exc)
        return f"[summarizer {label} failed: {exc}]"


async def _merge_summaries(
    summaries: list[str],
    prior_learning: str | None,
    llm_id: int | None,
    budget_id: int | None,
    task_id: str,
    base_url: str | None,
    model: str | None,
) -> str:
    from app.agent.llm_client import call_llm

    parts: list[str] = []
    if prior_learning:
        parts.append(
            "=== ACCUMULATED LEARNING FROM PRIOR SESSIONS ===\n"
            f"{prior_learning}\n"
            "=== END PRIOR LEARNING ==="
        )
    for i, s in enumerate(summaries, 1):
        parts.append(f"=== SUMMARY PART {i}/3 ===\n{s}")

    try:
        resp = await call_llm(
            messages=[
                {"role": "system", "content": _MERGE_SYSTEM},
                {"role": "user",   "content": "\n\n".join(parts)},
            ],
            base_url=base_url,
            model=model,
            max_tokens=2500,
            llm_id=llm_id,
            budget_id=budget_id,
            task_id=task_id,
            agent_name="session_summarizer:merge",
        )
        return (resp.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
    except Exception as exc:
        logger.warning("[session_summarizer] merge failed: %s", exc)
        # Degrade gracefully: concatenate so at least something is persisted.
        return "\n\n".join(s for s in summaries if s.strip())


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

_DOC_KEY_PREFIX = "session_learning"


def _doc_key(task_id: str) -> str:
    return f"{_DOC_KEY_PREFIX}/{task_id}"


async def summarize_session(
    *,
    task_id: str,
    messages: list[dict],
    llm_id: int | None,
    budget_id: int | None,
    project_name: str,
    max_context: int,
    base_url: str | None = None,
    model: str | None = None,
) -> None:
    """
    Run the 3+1 pipeline and persist the master learning document.

    Called from GenericStageAgent._on_max_turns() before advance_stage(fail).
    Non-fatal: all exceptions are logged and swallowed.

    max_context must be the live endpoint's context window (tokens) so the
    chunk windows are correctly sized for whatever model is serving this session.
    """
    if len(messages) < 6:
        logger.debug(
            "[session_summarizer] task '%s': only %d messages — skipping.",
            task_id, len(messages),
        )
        return

    key = _doc_key(task_id)

    # Load any prior learning to fold into the merge step.
    prior: str | None = None
    try:
        from app.agent.doc_store import get_document
        result = get_document(project_name, key)
        if result and result.get("content"):
            prior = result["content"]
    except Exception:
        pass

    chunk1, chunk2, chunk3 = _split_overlapping(messages, max_context)

    logger.info(
        "[session_summarizer] task '%s': 3 parallel summarizers "
        "(max_ctx=%d, chunks=%d/%d/%d msgs).",
        task_id, max_context, len(chunk1), len(chunk2), len(chunk3),
    )

    summaries = list(await asyncio.gather(
        _summarize_chunk(chunk1, "part_1_of_3", llm_id, budget_id, task_id, base_url, model),
        _summarize_chunk(chunk2, "part_2_of_3", llm_id, budget_id, task_id, base_url, model),
        _summarize_chunk(chunk3, "part_3_of_3", llm_id, budget_id, task_id, base_url, model),
    ))

    logger.info("[session_summarizer] task '%s': merging.", task_id)
    master = await _merge_summaries(
        summaries,
        prior_learning=prior,
        llm_id=llm_id,
        budget_id=budget_id,
        task_id=task_id,
        base_url=base_url,
        model=model,
    )

    if not master.strip():
        logger.warning(
            "[session_summarizer] task '%s': merge produced empty output — not persisting.",
            task_id,
        )
        return

    try:
        from app.agent.doc_store import store_document
        store_document(
            project_name=project_name,
            key=key,
            content=master,
            tags=["session_learning", "auto"],
            written_by_task_id=task_id,
        )
        logger.info(
            "[session_summarizer] task '%s': persisted %d-char learning doc → '%s'.",
            task_id, len(master), key,
        )
    except Exception as exc:
        logger.warning(
            "[session_summarizer] task '%s': persist failed: %s", task_id, exc,
        )


def get_session_learning(project_name: str, task_id: str) -> str | None:
    """
    Return the persisted session-learning string for a task, or None.
    Called from GenericStageAgent._build_messages() to inject prior context.
    """
    try:
        from app.agent.doc_store import get_document
        result = get_document(project_name, _doc_key(task_id))
        if result and result.get("content"):
            return result["content"]
        return None
    except Exception:
        return None
