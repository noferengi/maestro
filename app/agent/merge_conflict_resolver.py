"""
app/agent/merge_conflict_resolver.py
-------------------------------------
MergeConflictResolver - LLM-assisted merger for parallel component file conflicts.

Used when two components in the same batch (after serialization failed to separate them,
or when manual conflict resolution is triggered) produce conflicting changes to the same file.
"""

from __future__ import annotations

import logging
import re

from app.agent.llm_client import call_llm, is_shutting_down, ShutdownError

logger = logging.getLogger(__name__)

_MAX_RETRIES = 2

_MERGE_PROMPT = """\
You are a code merge specialist. Two components were implemented in parallel and both \
modified the same file, producing conflicting versions. Produce a single merged file \
that preserves the intent of BOTH components.

=== BASE (original file before either component) ===
{base_content}

=== VERSION A (after component A) ===
Component A was implementing: {component_a_context}

{version_a}

=== VERSION B (after component B) ===
Component B was implementing: {component_b_context}

{version_b}

Output ONLY the complete merged file contents - no explanation, no markdown fences, \
no commentary. The merged file must include everything both components added or changed.\
"""


class MergeConflictResolver:
    """
    Takes two conflicting versions of a file plus context about what each
    component was trying to accomplish, and produces a merged version using an LLM.
    """

    async def resolve(
        self,
        file_path: str,
        base_content: str,
        version_a: str,
        version_b: str,
        component_a_context: str,
        component_b_context: str,
        task_id: int,
        llm_endpoint: str,
        llm_id: int,
        budget_id: int,
    ) -> dict:
        """Attempt to merge two conflicting file versions.

        Returns:
            {"success": bool, "merged_content": str, "explanation": str}
        """
        prompt = _MERGE_PROMPT.format(
            base_content=base_content,
            component_a_context=component_a_context,
            version_a=version_a,
            component_b_context=component_b_context,
            version_b=version_b,
        )
        messages = [
            {
                "role": "system",
                "content": "You are a code merge specialist. Output only the merged file contents.",
            },
            {"role": "user", "content": prompt},
        ]

        base_len = len(base_content.strip())

        for attempt in range(_MAX_RETRIES + 1):
            if is_shutting_down():
                raise ShutdownError("Server is shutting down")
            try:
                response = await call_llm(
                    messages,
                    base_url=llm_endpoint,
                    task_id=str(task_id),
                    llm_id=llm_id,
                    budget_id=budget_id,
                )
            except Exception as e:
                logger.warning(
                    "[merge_resolver] LLM call failed (attempt %d/%d) for '%s': %s",
                    attempt + 1, _MAX_RETRIES + 1, file_path, e,
                )
                if attempt == _MAX_RETRIES:
                    return {
                        "success": False,
                        "merged_content": "",
                        "explanation": f"LLM call failed after {_MAX_RETRIES + 1} attempts: {e}",
                    }
                continue

            choice = response.get("choices", [{}])[0]
            merged = choice.get("message", {}).get("content", "").strip()

            if self._is_valid_merge(merged, base_len, version_a, version_b):
                logger.info(
                    "[merge_resolver] Resolved conflict in '%s' on attempt %d",
                    file_path, attempt + 1,
                )
                return {
                    "success": True,
                    "merged_content": merged,
                    "explanation": (
                        f"Merged '{file_path}': "
                        f"component A ({component_a_context[:80]}) + "
                        f"component B ({component_b_context[:80]}) "
                        f"on attempt {attempt + 1}."
                    ),
                }

            logger.warning(
                "[merge_resolver] Attempt %d produced malformed merge for '%s' "
                "(len=%d, base_len=%d) - retrying",
                attempt + 1, file_path, len(merged), base_len,
            )

        return {
            "success": False,
            "merged_content": "",
            "explanation": (
                f"All {_MAX_RETRIES + 1} merge attempts for '{file_path}' produced "
                "malformed output. Manual review required."
            ),
        }

    def _is_valid_merge(
        self, merged: str, base_len: int, version_a: str, version_b: str
    ) -> bool:
        if not merged:
            return False
        if len(merged) < max(base_len, 1):
            return False
        a_tokens = self._key_tokens(version_a)
        b_tokens = self._key_tokens(version_b)
        if a_tokens and not any(tok in merged for tok in a_tokens):
            return False
        if b_tokens and not any(tok in merged for tok in b_tokens):
            return False
        return True

    @staticmethod
    def _key_tokens(content: str) -> list[str]:
        """Extract a few distinctive identifier-like tokens from file content."""
        tokens = re.findall(r"\b[A-Za-z_][A-Za-z0-9_]{4,}\b", content)
        seen: dict[str, int] = {}
        for t in tokens:
            seen[t] = seen.get(t, 0) + 1
        ranked = sorted(seen, key=lambda k: seen[k])
        return ranked[:5]
