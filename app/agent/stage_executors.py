"""
app/agent/stage_executors.py
------------------------------
Generic pipeline node executors registered via register_agent_type_executor().

Five executor types:
  circuit_breaker        — configurable attempt counter; parks or fails when exhausted
  voting_panel           — N-voter LLM panel with tally strategy
  fan_out_judge          — best-of-N parallel agents + LLM judge picks the winner
  reflection_agent       — skeptical post-stage reviewer; stores confidence report
  static_analysis_widget — deterministic tree-sitter analysis; no LLM; injects JSON into task.content

Each executor has a public runner function (_run_*) that is registered in
scheduler.py at import time.  The function signature matches the agent-type
executor contract:

    fn(task_id, stage_config, llm_base_url, llm_model, max_context,
       llm_id, budget_id, project_path) -> None

A helper _CollectorAgent class (local to this module) runs an AgentLoop turn
loop but suppresses advance_stage — it just returns the submit_work payload.
This is used by voting_panel (voter agents) and fan_out_judge (proposer agents
and the judge).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.agent.agent_loop import AgentLoop
from app.agent.pipeline_router import StageConfig, advance_stage
from app.agent.tools import build_tool_schemas

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# _CollectorAgent — minimal AgentLoop that returns submit_work payload
# ---------------------------------------------------------------------------

class _CollectorAgent(AgentLoop):
    """
    Runs a turn loop and returns the submit_work payload without advancing stage.
    Used as individual voters (voting_panel) and proposers/judge (fan_out_judge).
    """

    def __init__(
        self,
        *,
        task_id: str,
        system_prompt: str,
        tool_allowlist: list[str],
        max_turns: int,
        llm_id: int | None,
        budget_id: int | None,
        llm_base_url: str | None,
        llm_model: str | None,
        max_context: int | None,
        user_message: str,
        agent_name: str = "collector",
    ) -> None:
        super().__init__(
            task_id=task_id,
            llm_id=llm_id,
            budget_id=budget_id,
            max_turns=max_turns,
            llm_base_url=llm_base_url,
            llm_model=llm_model,
            max_context=max_context,
        )
        self._sys_prompt = system_prompt
        self._user_msg = user_message
        self._agent_name = agent_name
        allowed = list(tool_allowlist or [])
        if "submit_work" not in allowed:
            allowed.append("submit_work")
        self._tool_schemas_list = build_tool_schemas(allowed)

    def _build_messages(self) -> list[dict]:
        return [
            {"role": "system", "content": self._sys_prompt},
            {"role": "user",   "content": self._user_msg},
        ]

    def _get_tool_schemas(self) -> list[dict]:
        return self._tool_schemas_list

    async def _on_terminal(self) -> dict | None:
        return self._terminal_signal.get("payload") or {}

    async def _on_max_turns(self) -> None:
        logger.warning("[collector] task '%s' agent '%s': max turns reached.", self.task_id, self._agent_name)
        return None

    async def _on_error(self, reason: str) -> None:
        logger.error("[collector] task '%s' agent '%s': error — %s", self.task_id, self._agent_name, reason)
        return None


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _send_inbox_message(task_id: str, task: Any, message: str, source_type: str = "circuit_breaker") -> None:
    try:
        from app.database import create_inbox_message
        create_inbox_message(
            subject=message[:120],
            source_type=source_type,
            task_id=task_id,
            project_id=task.project if task else None,
            task_title=task.title if task else None,
            outcome="parked",
        )
    except Exception:
        logger.exception("[stage_executors] Failed to send inbox message for task '%s'", task_id)


# ---------------------------------------------------------------------------
# Circuit Breaker
# ---------------------------------------------------------------------------

def _run_circuit_breaker(
    task_id: str,
    stage_config: StageConfig,
    llm_base_url: str,
    llm_model: str,
    max_context: int | None,
    llm_id: int | None,
    budget_id: int | None,
    project_path: str | None,
) -> None:
    """
    Configurable attempt counter.  Parks or fails the task when exhausted.

    Stage config shape:
        counter_key      — key in task.content._counters (fallback counter)
        max_attempts     — trigger threshold (default 3)
        count_source     — "transition_results" | "content_counter"
        count_transition — transition key to count in transition_results
        count_outcome    — outcome value to count (default "rejected")
        on_exhaust       — "park" | "fail" | "notify_only" (default "park")
        notify_inbox     — bool (default False)
        exhaust_message  — human-readable message written to history + inbox
    """
    from app.database import get_task, update_task, append_task_history
    from app.database.session import SessionLocal
    from app.database.models import TransitionResult

    cfg = stage_config.config or {}
    counter_key      = cfg.get("counter_key", "circuit_break_count")
    max_attempts     = int(cfg.get("max_attempts", 3))
    count_source     = cfg.get("count_source", "transition_results")
    count_transition = cfg.get("count_transition", "")
    count_outcome    = cfg.get("count_outcome", "rejected")
    on_exhaust       = cfg.get("on_exhaust", "park")
    notify_inbox     = bool(cfg.get("notify_inbox", False))
    exhaust_message  = cfg.get(
        "exhaust_message",
        f"Circuit breaker exhausted at stage '{stage_config.stage_key}' — manual intervention required.",
    )

    task = get_task(task_id)
    if not task:
        return

    blob = dict(task.content or {})

    # Already parked at this stage — skip silently.
    if blob.get("_parked_at_stage") == stage_config.stage_key:
        logger.debug("[circuit_breaker] task '%s' already parked at '%s'.", task_id, stage_config.stage_key)
        return

    # Count attempts.
    count = 0
    if count_source == "transition_results" and count_transition:
        db = SessionLocal()
        try:
            count = (
                db.query(TransitionResult)
                .filter(
                    TransitionResult.task_id == task_id,
                    TransitionResult.transition == count_transition,
                    TransitionResult.outcome == count_outcome,
                )
                .count()
            )
        finally:
            db.close()
    else:
        counters = blob.get("_counters") or {}
        count = int(counters.get(counter_key, 0))

    logger.info(
        "[circuit_breaker] task '%s' stage '%s': count=%d / max=%d.",
        task_id, stage_config.stage_key, count, max_attempts,
    )

    if count < max_attempts:
        advance_stage(task_id, "pass")
        return

    # Exhausted — apply on_exhaust policy.
    if on_exhaust == "park":
        blob["_parked_at_stage"] = stage_config.stage_key
        blob["_parked_reason"] = exhaust_message
        update_task(task_id, content=blob)
        append_task_history(task_id, "circuit_breaker_parked", message=exhaust_message)
        if notify_inbox:
            _send_inbox_message(task_id, task, exhaust_message)
        logger.info("[circuit_breaker] task '%s' parked at stage '%s'.", task_id, stage_config.stage_key)

    elif on_exhaust == "notify_only":
        if notify_inbox:
            _send_inbox_message(task_id, task, exhaust_message)
        advance_stage(task_id, "pass")

    else:  # "fail" or unknown
        logger.info("[circuit_breaker] task '%s' exhausted → fail.", task_id)
        advance_stage(task_id, "fail")


# ---------------------------------------------------------------------------
# Veto tally helper
# ---------------------------------------------------------------------------

def _tally_veto(votes: list) -> str:
    """Returns 'fail' if any vote is REJECTED or NOT_SUITABLE, else 'pass'."""
    from app.agent.verdicts import Verdict
    for v in votes:
        if v.verdict in (Verdict.REJECTED, Verdict.NOT_SUITABLE):
            return "fail"
    return "pass"


def _build_required_keys_preamble(task_id: str, required_input_keys: list[str]) -> str:
    """Fetch task.content and build a preamble block for required_input_keys."""
    if not required_input_keys:
        return ""
    try:
        from app.database import get_task as _get_task
        t = _get_task(task_id)
        blob = (t.content or {}) if t else {}
        lines = ["\n== Prior Stage Outputs =="]
        for key in required_input_keys:
            if key in blob:
                lines.append(f"{key}: {blob[key]}")
        return "\n".join(lines) + "\n\n"
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Voting Panel
# ---------------------------------------------------------------------------

def _run_voting_panel(
    task_id: str,
    stage_config: StageConfig,
    llm_base_url: str,
    llm_model: str,
    max_context: int | None,
    llm_id: int | None,
    budget_id: int | None,
    project_path: str | None,
) -> None:
    """
    Spawn voter_count LLM agents concurrently, tally their votes, advance stage.

    Stage config shape (legacy — single shared prompt):
        voter_count          — number of voters (default 3)
        voter_system_prompt  — system prompt for each voter
        voter_tools          — tool allowlist for voters (default: submit_work only)
        voter_max_turns      — max turns per voter (default 10)

    Stage config shape (per-reviewer — takes precedence when present):
        reviewers            — list of {name, system_prompt, tools?, max_turns?}
        tally_strategy       — "majority" (default) | "veto" (any REJECTED/NOT_SUITABLE blocks)
        required_input_keys  — list of task.content keys to inject into voter user message preamble
        on_tie               — "reject" | "pass" (default "reject")
        output_key           — task.content key to write tally result (default "vote_result")
    """
    from app.database import (
        create_agent_session, close_agent_session, get_task, update_task,
    )
    from app.database.session import SessionLocal
    from app.database.models import TransitionVote
    from app.agent.verdicts import Vote, Verdict, tally_votes

    cfg = stage_config.config or {}
    reviewers_cfg       = cfg.get("reviewers")  # list of {name, system_prompt, tools?, max_turns?}
    tally_strategy      = cfg.get("tally_strategy", "majority")
    required_input_keys = cfg.get("required_input_keys") or []
    if isinstance(required_input_keys, str):
        required_input_keys = [k.strip() for k in required_input_keys.split(",") if k.strip()]

    # Legacy single-prompt config
    voter_count         = int(cfg.get("voter_count", 3))
    voter_system_prompt = cfg.get("voter_system_prompt", "Review the task and vote ACCEPTED or REJECTED.")
    voter_tools         = list(cfg.get("voter_tools") or [])
    voter_max_turns     = int(cfg.get("voter_max_turns", 10))
    on_tie              = cfg.get("on_tie", "reject")
    output_key          = cfg.get("output_key", "vote_result")

    session_id = create_agent_session(
        task_id=task_id,
        agent_type=f"voting_panel:{stage_config.stage_key}",
        llm_id=llm_id,
        budget_id=budget_id,
        scheduler_reason="scheduler",
    )
    exit_reason = "error"
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        task = get_task(task_id)
        keys_preamble = _build_required_keys_preamble(task_id, required_input_keys)
        user_msg = (
            keys_preamble
            + f"Task ID: {task_id}\n"
            f"Title: {task.title if task else '(unknown)'}\n"
            f"Description:\n{task.description or '' if task else ''}\n\n"
            "Vote on whether this task should proceed. Call submit_work with:\n"
            "  signal='ACCEPTED' or 'REJECTED'\n"
            "  summary='your reasoning'\n"
            "  payload={'verdict': 'ACCEPTED'|'REJECTED', 'confidence': 0-100, 'justification': '...'}"
        )

        if reviewers_cfg:
            voters = [
                _CollectorAgent(
                    task_id=task_id,
                    system_prompt=r.get("system_prompt", voter_system_prompt),
                    tool_allowlist=list(r.get("tools") or voter_tools),
                    max_turns=int(r.get("max_turns") or voter_max_turns),
                    llm_id=llm_id,
                    budget_id=budget_id,
                    llm_base_url=llm_base_url,
                    llm_model=llm_model,
                    max_context=max_context,
                    user_message=user_msg,
                    agent_name=f"voter_{r.get('name', i)}:{stage_config.stage_key}",
                )
                for i, r in enumerate(reviewers_cfg)
            ]
        else:
            voters = [
                _CollectorAgent(
                    task_id=task_id,
                    system_prompt=voter_system_prompt,
                    tool_allowlist=voter_tools,
                    max_turns=voter_max_turns,
                    llm_id=llm_id,
                    budget_id=budget_id,
                    llm_base_url=llm_base_url,
                    llm_model=llm_model,
                    max_context=max_context,
                    user_message=user_msg,
                    agent_name=f"voter_{i}:{stage_config.stage_key}",
                )
                for i in range(voter_count)
            ]

        payloads: list[dict | None] = loop.run_until_complete(
            asyncio.gather(*[v.run() for v in voters], return_exceptions=False)
        )

        # Convert payloads to Vote objects (map ACCEPTED→LIKELY, REJECTED→REJECTED).
        votes: list[Vote] = []
        for i, payload in enumerate(payloads):
            if not payload:
                continue
            verdict_str = str(payload.get("verdict", "REJECTED")).upper()
            raw_conf    = int(payload.get("confidence", 50))
            justification = str(payload.get("justification", ""))

            if verdict_str == "ACCEPTED":
                confidence = max(92, min(100, raw_conf if raw_conf >= 76 else 92))
                verdict    = Verdict.LIKELY
            else:
                confidence = max(0, min(50, raw_conf if raw_conf <= 50 else 25))
                verdict    = Verdict.REJECTED

            try:
                votes.append(Vote(
                    stage=f"voter_{i}",
                    verdict=verdict,
                    confidence=confidence,
                    justification=justification,
                ))
            except Exception:
                logger.warning("[voting_panel] task '%s': voter_%d produced invalid vote — skipped.", task_id, i)

        if not votes:
            condition    = "fail"
            tally_outcome = "rejected"
        elif tally_strategy == "veto":
            condition    = _tally_veto(votes)
            tally_outcome = "rejected" if condition == "fail" else "passed"
        else:
            tally        = tally_votes(votes)
            tally_outcome = tally.outcome
            if tally_outcome in ("passed", "conditional_pass", "warned"):
                condition = "pass"
            elif tally_outcome == "tie":
                condition = "fail" if on_tie == "reject" else "pass"
            else:
                condition = "fail"

        # Persist tally result in task.content.
        task = get_task(task_id)
        blob = dict((task.content or {}) if task else {})
        blob[output_key] = {
            "outcome": tally_outcome,
            "votes": [
                {
                    "stage": v.stage,
                    "verdict": v.verdict.value,
                    "confidence": v.confidence,
                    "justification": v.justification,
                }
                for v in votes
            ],
        }
        update_task(task_id, content=blob)

        # Persist TransitionVote rows.
        db = SessionLocal()
        try:
            for v in votes:
                db.add(TransitionVote(
                    task_id=task_id,
                    transition=f"{stage_config.stage_key}_voting_panel",
                    stage=v.stage,
                    verdict=v.verdict.value,
                    confidence=v.confidence,
                    justification=v.justification,
                    llm_id=llm_id,
                    budget_id=budget_id,
                ))
            db.commit()
        except Exception:
            db.rollback()
            logger.exception("[voting_panel] task '%s': failed to save TransitionVote rows.", task_id)
        finally:
            db.close()

        advance_stage(task_id, condition)
        exit_reason = condition

    except Exception:
        logger.exception("[voting_panel] task '%s' stage '%s' raised.", task_id, stage_config.stage_key)
    finally:
        close_agent_session(session_id, exit_reason, "")
        try:
            loop.run_until_complete(asyncio.wait_for(loop.shutdown_asyncgens(), timeout=5.0))
        except Exception:
            pass
        try:
            loop.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Fan-Out + Judge
# ---------------------------------------------------------------------------

def _run_fan_out_judge(
    task_id: str,
    stage_config: StageConfig,
    llm_base_url: str,
    llm_model: str,
    max_context: int | None,
    llm_id: int | None,
    budget_id: int | None,
    project_path: str | None,
) -> None:
    """
    Run N parallel proposal agents, then a judge picks the best one.

    Stage config shape (legacy — single shared prompt):
        n                    — number of parallel agents (default 3)
        agent_system_prompt  — system prompt for all proposal agents
        agent_tools          — tool allowlist for proposal agents
        agent_max_turns      — max turns per proposal agent (default 30)

    Stage config shape (per-persona — takes precedence when present):
        personas             — list of {name, system_prompt}; N = len(personas)
        required_input_keys  — list of task.content keys to inject into proposer user message preamble
        agent_tools          — tool allowlist shared across all proposers
        agent_max_turns      — max turns per proposer (default 30)

    Shared keys:
        judge_system_prompt  — system prompt for the judge
        judge_max_turns      — max turns for judge (default 10)
        output_key           — task.content key to write winning proposal (default "winning_proposal")
    """
    from app.database import (
        create_agent_session, close_agent_session, get_task, update_task,
    )

    cfg = stage_config.config or {}
    personas_cfg        = cfg.get("personas")  # list of {name, system_prompt}
    required_input_keys = cfg.get("required_input_keys") or []
    if isinstance(required_input_keys, str):
        required_input_keys = [k.strip() for k in required_input_keys.split(",") if k.strip()]

    # Legacy single-prompt config
    n                   = int(cfg.get("n", 3))
    agent_system_prompt = cfg.get("agent_system_prompt", "Produce a proposal for the task. Submit it via submit_work.")
    agent_tools         = list(cfg.get("agent_tools") or [])
    agent_max_turns     = int(cfg.get("agent_max_turns", 30))
    judge_system_prompt = cfg.get("judge_system_prompt", "Compare the proposals below and pick the best one.")
    judge_max_turns     = int(cfg.get("judge_max_turns", 10))
    output_key          = cfg.get("output_key", "winning_proposal")

    if personas_cfg:
        n = len(personas_cfg)

    session_id = create_agent_session(
        task_id=task_id,
        agent_type=f"fan_out_judge:{stage_config.stage_key}",
        llm_id=llm_id,
        budget_id=budget_id,
        scheduler_reason="scheduler",
    )
    exit_reason = "error"
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        task = get_task(task_id)
        task_title = task.title if task else "(unknown)"
        task_desc  = task.description or "" if task else ""

        keys_preamble = _build_required_keys_preamble(task_id, required_input_keys)
        proposer_user_msg_base = (
            keys_preamble
            + f"Task ID: {task_id}\n"
            f"Title: {task_title}\n"
            f"Description:\n{task_desc}\n\n"
            "Produce your best proposal. When done, call submit_work with:\n"
            "  signal='ACCEPTED'\n"
            "  summary='brief summary'\n"
            "  payload={'proposal': '<your full proposal text>'}"
        )

        if personas_cfg:
            proposers = [
                _CollectorAgent(
                    task_id=task_id,
                    system_prompt=p.get("system_prompt", agent_system_prompt),
                    tool_allowlist=agent_tools,
                    max_turns=agent_max_turns,
                    llm_id=llm_id,
                    budget_id=budget_id,
                    llm_base_url=llm_base_url,
                    llm_model=llm_model,
                    max_context=max_context,
                    user_message=proposer_user_msg_base,
                    agent_name=f"proposer_{p.get('name', i)}:{stage_config.stage_key}",
                )
                for i, p in enumerate(personas_cfg)
            ]
        else:
            proposers = [
                _CollectorAgent(
                    task_id=task_id,
                    system_prompt=agent_system_prompt,
                    tool_allowlist=agent_tools,
                    max_turns=agent_max_turns,
                    llm_id=llm_id,
                    budget_id=budget_id,
                    llm_base_url=llm_base_url,
                    llm_model=llm_model,
                    max_context=max_context,
                    user_message=proposer_user_msg_base,
                    agent_name=f"proposer_{i}:{stage_config.stage_key}",
                )
                for i in range(n)
            ]

        payloads: list[dict | None] = loop.run_until_complete(
            asyncio.gather(*[p.run() for p in proposers], return_exceptions=False)
        )
        proposals = [p for p in payloads if p]

        if not proposals:
            logger.warning("[fan_out_judge] task '%s': no proposals — advancing fail.", task_id)
            advance_stage(task_id, "fail")
            exit_reason = "fail"
            return

        # Build judge prompt (truncate long proposals to fit context).
        _MAX_CHARS = 2000
        proposals_text = "\n\n".join(
            f"=== Proposal {i} ({personas_cfg[i]['name'] if personas_cfg and i < len(personas_cfg) else ''}) ===\n{str(p)[:_MAX_CHARS]}"
            for i, p in enumerate(proposals)
        )
        judge_user_msg = (
            f"Task: {task_title}\n\n"
            f"You have {len(proposals)} proposal(s):\n\n{proposals_text}\n\n"
            "Pick the best one. Call submit_work with:\n"
            "  signal='ACCEPTED'\n"
            "  summary='your rationale'\n"
            "  payload={'selected_index': N, 'rationale': '...'}"
        )

        judge = _CollectorAgent(
            task_id=task_id,
            system_prompt=judge_system_prompt,
            tool_allowlist=[],
            max_turns=judge_max_turns,
            llm_id=llm_id,
            budget_id=budget_id,
            llm_base_url=llm_base_url,
            llm_model=llm_model,
            max_context=max_context,
            user_message=judge_user_msg,
            agent_name=f"judge:{stage_config.stage_key}",
        )

        judgment: dict | None = loop.run_until_complete(judge.run())

        selected_idx = 0
        if judgment:
            try:
                selected_idx = int(judgment.get("selected_index", 0))
                selected_idx = max(0, min(selected_idx, len(proposals) - 1))
            except (TypeError, ValueError):
                selected_idx = 0

        winning = proposals[selected_idx]

        # Persist result in task.content.
        task = get_task(task_id)
        blob = dict((task.content or {}) if task else {})
        blob[output_key] = winning
        if judgment:
            blob[f"{output_key}_rationale"] = judgment.get("rationale", "")
        update_task(task_id, content=blob)

        advance_stage(task_id, "pass")
        exit_reason = "pass"

    except Exception:
        logger.exception("[fan_out_judge] task '%s' stage '%s' raised.", task_id, stage_config.stage_key)
    finally:
        close_agent_session(session_id, exit_reason, "")
        try:
            loop.run_until_complete(asyncio.wait_for(loop.shutdown_asyncgens(), timeout=5.0))
        except Exception:
            pass
        try:
            loop.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Reflection Agent
# ---------------------------------------------------------------------------

def _run_reflection_agent(
    task_id: str,
    stage_config: StageConfig,
    llm_base_url: str,
    llm_model: str,
    max_context: int | None,
    llm_id: int | None,
    budget_id: int | None,
    project_path: str | None,
) -> None:
    """
    Run ReflectionAgent for a pipeline stage of agent_type 'reflection_agent'.

    Stores a structured JSON confidence report at
    reflection:{task_id}:{stage_key} in the project document store, then
    advances the stage unconditionally (condition='pass').  Maestro reads
    the report on its next tick and decides consequence.

    Stage config keys (all optional):
        system_prompt                — override default skeptical-reviewer prompt
        reflection_llm_id            — specific LLM for this reflection stage
        reflection_max_history_turns — cap on get_task_history_recent (default 20)
        max_turns                    — agent turn limit (default 150)
    """
    from app.database import create_agent_session, close_agent_session
    from app.agent.reflection_agent import ReflectionAgent

    session_id = create_agent_session(
        task_id=task_id,
        agent_type=f"reflection_agent:{stage_config.stage_key}",
        llm_id=llm_id,
        budget_id=budget_id,
        scheduler_reason="scheduler",
    )
    exit_reason = "error"
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        agent = ReflectionAgent(
            task_id=task_id,
            stage_config=stage_config,
            llm_id=llm_id,
            budget_id=budget_id,
            llm_base_url=llm_base_url,
            llm_model=llm_model,
            max_context=max_context,
        )
        result = loop.run_until_complete(agent.run())
        exit_reason = (result or {}).get("condition", "pass") if isinstance(result, dict) else "pass"

    except Exception:
        logger.exception("[reflection_agent] task '%s' stage '%s' raised.", task_id, stage_config.stage_key)
    finally:
        close_agent_session(session_id, exit_reason, "")
        try:
            loop.run_until_complete(asyncio.wait_for(loop.shutdown_asyncgens(), timeout=5.0))
        except Exception:
            pass
        try:
            loop.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Static Analysis Widget
# ---------------------------------------------------------------------------

def _run_static_analysis_widget(
    task_id: str,
    stage_config: StageConfig,
    llm_base_url: str,
    llm_model: str,
    max_context: int | None,
    llm_id: int | None,
    budget_id: int | None,
    project_path: str | None,
) -> None:
    """
    Deterministic non-LLM node. Runs tree-sitter on the project folder and writes
    structured JSON to task.content[output_key]. Advances with 'pass' immediately.

    Stage config shape:
        output_key    — task.content key to write result (default "static_analysis")
        file_pattern  — glob filter applied to relative file paths (default "**/*.py")
        max_files     — cap to avoid large projects (default 50)
    """
    import fnmatch
    import os

    from app.database import get_task, update_task
    from app.agent.path_filter import walk_safe

    cfg         = stage_config.config or {}
    output_key  = cfg.get("output_key", "static_analysis")
    file_pattern = cfg.get("file_pattern", "**/*.py")
    max_files   = int(cfg.get("max_files", 50))

    if not project_path:
        logger.warning("[static_analysis_widget] task '%s': no project_path — skipping analysis.", task_id)
        advance_stage(task_id, "pass")
        return

    # Collect matching files
    file_paths: list[str] = []
    # Normalise pattern for per-filename matching (last component of a glob)
    _basename_pattern = file_pattern.split("/")[-1] if "/" in file_pattern else file_pattern
    for root, dirs, files in walk_safe(project_path):
        for fname in files:
            full = os.path.join(root, fname)
            rel  = os.path.relpath(full, project_path).replace("\\", "/")
            if fnmatch.fnmatch(rel, file_pattern) or fnmatch.fnmatch(fname, _basename_pattern):
                file_paths.append(full)
            if len(file_paths) >= max_files:
                break
        if len(file_paths) >= max_files:
            break

    if not file_paths:
        logger.info("[static_analysis_widget] task '%s': no files matched pattern '%s'.", task_id, file_pattern)
        task = get_task(task_id)
        blob = dict((task.content or {}) if task else {})
        blob[output_key] = {"file_count": 0, "files": {}, "import_graph": {}, "reverse_import_graph": {}}
        update_task(task_id, content=blob)
        advance_stage(task_id, "pass")
        return

    try:
        from app.agent.static_analysis import analyze_project, _file_analysis_to_dict
        analysis = analyze_project(file_paths)
        result = {
            "file_count": len(analysis.files),
            "files": {
                path: _file_analysis_to_dict(fa)
                for path, fa in analysis.files.items()
            },
            "import_graph": analysis.import_graph,
            "reverse_import_graph": analysis.reverse_import_graph,
        }
    except Exception:
        logger.exception("[static_analysis_widget] task '%s': analyze_project failed.", task_id)
        result = {"error": "analysis failed", "file_count": len(file_paths)}

    task = get_task(task_id)
    blob = dict((task.content or {}) if task else {})
    blob[output_key] = result
    update_task(task_id, content=blob)

    logger.info(
        "[static_analysis_widget] task '%s': analysed %d file(s) → '%s'.",
        task_id, result.get("file_count", 0), output_key,
    )
    advance_stage(task_id, "pass")


# ---------------------------------------------------------------------------
# dangerous_edit_llm_agent — wraps MaestroLoop with stage-config overrides
# ---------------------------------------------------------------------------

def _record_demotion(task_id: str, from_stage: str, to_stage: str, reason: str) -> None:
    """Local copy of scheduler._record_demotion_inline — avoids circular import."""
    import asyncio
    from datetime import datetime, timezone
    from app.database import get_task, update_task
    from app.agent.pip_agent import generate_pip

    task = get_task(task_id)
    if not task:
        return
    history = task.demotion_history or []
    history.append({
        "from": from_stage,
        "to": to_stage,
        "reason": reason[:500],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    update_task(task_id, demotion_count=(task.demotion_count or 0) + 1, demotion_history=history)

    review_stages = {"conceptual_review", "optimization", "security", "human_review"}
    if from_stage in review_stages:
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(generate_pip(task_id, from_stage, reason))
        except RuntimeError:
            asyncio.run(generate_pip(task_id, from_stage, reason))


def _run_dangerous_edit_llm_agent(
    task_id: str,
    stage_config: "StageConfig",
    llm_base_url: str,
    llm_model: str,
    max_context: int | None,
    llm_id: int | None,
    budget_id: int | None,
    project_path: str | None,
) -> None:
    """
    Executor for dangerous_edit_llm_agent — wraps MaestroLoop with worktree-isolated
    writes and per-stage overrides for system_prompt, agent_tools, max_turns, and
    required_input_keys.

    Stage config shape:
        system_prompt        — override MAESTRO_SYSTEM_PROMPT (empty/absent = default)
        max_turns            — integer cap (default from maestro.ini)
        agent_tools          — comma-separated string or list of tool names (absent = INDEV_AGENT_TOOLS)
        required_input_keys  — comma-separated string or list; values injected from task.content
    """
    import asyncio
    import json as _json

    from app.agent.loop import MaestroLoop
    from app.agent.config import MAX_TURNS as _DEFAULT_MAX_TURNS
    from app.database import (
        get_task,
        update_task,
        create_agent_session,
        close_agent_session,
        create_inbox_message,
    )
    from app.agent.pipeline_router import advance_stage

    cfg = stage_config.config or {}
    system_prompt = cfg.get("system_prompt") or None  # empty string → None (use default)
    max_turns = int(cfg.get("max_turns", _DEFAULT_MAX_TURNS))
    stage_key = stage_config.stage_key

    # agent_tools: stored as JSON list or comma-sep string from the pipeline editor
    _raw_tools = cfg.get("agent_tools")
    if isinstance(_raw_tools, list):
        agent_tools: list[str] | None = [t.strip() for t in _raw_tools if t.strip()] or None
    elif isinstance(_raw_tools, str) and _raw_tools.strip():
        agent_tools = [t.strip() for t in _raw_tools.split(",") if t.strip()] or None
    else:
        agent_tools = None  # falls back to INDEV_AGENT_TOOLS

    # required_input_keys: same dual-format handling
    _raw_keys = cfg.get("required_input_keys", [])
    if isinstance(_raw_keys, list):
        required_keys: list[str] = [k.strip() for k in _raw_keys if k.strip()]
    elif isinstance(_raw_keys, str) and _raw_keys.strip():
        required_keys = [k.strip() for k in _raw_keys.split(",") if k.strip()]
    else:
        required_keys = []

    _session_id = None
    _exit_reason = "error"
    _exit_summary = ""

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        _session_id = create_agent_session(
            task_id=task_id,
            agent_type="dangerous_edit_llm_agent",
            llm_id=llm_id,
            budget_id=budget_id,
            scheduler_reason="scheduler",
            max_turns=max_turns,
        )

        maestro = MaestroLoop(
            task_id=task_id,
            max_turns=max_turns,
            llm_base_url=llm_base_url,
            llm_model=llm_model,
            max_context=max_context,
            llm_id=llm_id,
            budget_id=budget_id,
            project_path=project_path,
            system_prompt=system_prompt,
            agent_tools=agent_tools,
            required_input_keys=required_keys,
        )
        result = loop.run_until_complete(maestro.run())
        _exit_summary = result.final_message or ""

        if result.status == "ACCEPTED":
            _exit_reason = "completed"
            advance_stage(task_id, "pass", from_stage=stage_key)

        elif result.status == "NEEDS_HUMAN":
            _exit_reason = "needs_human"
            advance_stage(task_id, "pass", from_stage=stage_key)
            task_obj = get_task(task_id)
            create_inbox_message(
                subject=f"Human review needed: {(task_obj.title if task_obj else task_id)[:60]}",
                source_type="needs_human",
                task_id=task_id,
                project_id=task_obj.project if task_obj else None,
                task_title=task_obj.title if task_obj else None,
                outcome="needs_human",
                data_json=_json.dumps({"summary": _exit_summary}),
            )

        elif result.status == "CONSULTING":
            _exit_reason = "consulting"
            update_task(task_id, consultation_payload=_json.dumps({
                "question": result.consultation_question,
                "hint": None,
                "source": None,
            }))
            task_obj = get_task(task_id)
            create_inbox_message(
                subject=f"Consultation needed: {(task_obj.title if task_obj else task_id)[:60]}",
                source_type="consultation",
                task_id=task_id,
                project_id=task_obj.project if task_obj else None,
                task_title=task_obj.title if task_obj else None,
                outcome="consultation",
                data_json=_json.dumps({
                    "question": result.consultation_question,
                    "summary": _exit_summary,
                }),
            )

        elif result.status in ("REVERT_TO_DESIGN", "REJECTED"):
            _exit_reason = "rejected"
            advance_stage(task_id, "fail", from_stage=stage_key)
            _record_demotion(task_id, stage_key, "planning",
                             result.final_message or "Agent requested revert")

        elif result.status in ("MAX_TURNS", "ERROR"):
            _exit_reason = result.status.lower()
            advance_stage(task_id, "fail", from_stage=stage_key)
            _record_demotion(task_id, stage_key, "planning",
                             f"{result.status} in dangerous_edit_llm_agent stage.")

    finally:
        loop.close()
        if _session_id is not None:
            close_agent_session(_session_id, _exit_reason, _exit_summary)


# ---------------------------------------------------------------------------
# parallel_agents — fan-out creator
# ---------------------------------------------------------------------------

def _run_parallel_agents(
    task_id: str,
    stage_config: "StageConfig",
    llm_base_url: str,
    llm_model: str,
    max_context: int | None,
    llm_id: int | None,
    budget_id: int | None,
    project_path: str | None,
) -> None:
    """
    Fan-out creator for the parallel_agents node.

    Creates N child _psubagent tasks plus one _psubagent_join aggregator, then
    appends the aggregator ID to the parent's prerequisites to block re-dispatch
    until all children complete.  Does NOT call advance_stage — the aggregator
    drives the parent forward.
    """
    from app.database import get_task, update_task, create_task

    cfg: dict = stage_config.config or {}
    agents_cfg: list[dict] = cfg.get("agents", [])
    output_key: str = cfg.get("output_key", "parallel_agents_output")
    max_turns: int = int(cfg.get("max_turns", 30))

    parent = get_task(task_id)
    if not parent:
        return

    # Idempotency guard: skip if children already created
    if (parent.content or {}).get("_psubagent_child_ids"):
        return

    content = dict(parent.content or {})
    content["_psubagent_waiting"] = True
    update_task(task_id, content=content)

    child_ids: list[str] = []
    for i, agent in enumerate(agents_cfg):
        name = agent.get("name", f"agent_{i}")
        tg_id = agent.get("tool_grouping_id")
        subagent_type: str = agent.get("subagent_type", "collector")
        child_task_type = "_psubagent_dangerous" if subagent_type == "dangerous_edit" else "_psubagent"
        child = create_task(
            title=f"[PA] {parent.title[:50]} — {name}",
            task_type=child_task_type,
            stage_key=child_task_type,
            project_id=parent.project_id,
            pipeline_template_id=None,
            llm_id=llm_id,
            budget_id=budget_id,
            content={"_subagent_cfg": {
                "name": name,
                "system_prompt": agent.get("system_prompt", "Complete the task and call submit_work."),
                "max_turns": agent.get("max_turns", max_turns),
                "output_key": output_key,
                "parent_task_id": task_id,
                "parent_stage_key": stage_config.stage_key,
                "tool_grouping_id": tg_id,
                "agent_tools": agent.get("agent_tools"),
            }},
        )
        if child:
            child_ids.append(child.id)

    agg = create_task(
        title=f"[PA-join] {parent.title[:50]}",
        task_type="_psubagent_join",
        stage_key="_psubagent_join",
        project_id=parent.project_id,
        pipeline_template_id=None,
        llm_id=llm_id,
        budget_id=budget_id,
        prerequisites=child_ids,
        content={"_subagent_cfg": {
            "parent_task_id": task_id,
            "output_key": output_key,
            "parent_stage_key": stage_config.stage_key,
            "child_ids": child_ids,
        }},
    )

    content["_psubagent_child_ids"] = child_ids
    content["_psubagent_agg_id"] = agg.id if agg else None
    existing_prereqs = list(parent.prerequisites or [])
    update_task(task_id, content=content,
                prerequisites=existing_prereqs + ([agg.id] if agg else []))

    logger.info(
        "[parallel_agents] task '%s': created %d children + aggregator '%s'.",
        task_id, len(child_ids), (agg.id if agg else "None"),
    )


# ---------------------------------------------------------------------------
# _psubagent — runs one parallel sub-agent child
# ---------------------------------------------------------------------------

def _run_parallel_subagent(
    task_id: str,
    stage_config: "StageConfig",
    llm_base_url: str,
    llm_model: str,
    max_context: int | None,
    llm_id: int | None,
    budget_id: int | None,
    project_path: str | None,
) -> None:
    """
    Runs a single parallel sub-agent child.  Config comes from task.content._subagent_cfg
    (injected by _run_parallel_agents at creation time).
    """
    from app.database import get_task, update_task, create_agent_session, close_agent_session

    task = get_task(task_id)
    if not task:
        return

    cfg: dict = (task.content or {}).get("_subagent_cfg", {})
    name: str = cfg.get("name", "subagent")
    system_prompt: str = cfg.get("system_prompt", "Complete the task and call submit_work.")
    max_turns: int = int(cfg.get("max_turns", 30))
    parent_task_id: str | None = cfg.get("parent_task_id")
    tg_id: int | None = cfg.get("tool_grouping_id")

    # Resolve tool allowlist from tool grouping
    tool_allowlist: list[str] = ["submit_work"]
    if tg_id is not None:
        try:
            from app.database.crud_malleable import get_tool_grouping
            tg = get_tool_grouping(tg_id)
            if tg:
                tool_allowlist = list(tg.get("tools", ["submit_work"]))
        except Exception:
            logger.exception("[parallel_subagent] task '%s': failed to load tool grouping %s.", task_id, tg_id)

    parent = get_task(parent_task_id) if parent_task_id else None
    user_msg = (
        f"Task: {(parent.title if parent else task.title)}\n"
        f"Description:\n{(parent.description or '') if parent else (task.description or '')}\n\n"
        "Complete your assigned work. Use submit_work with:\n"
        "  signal='ACCEPTED', payload={'output': '<your full output>'}"
    )

    session_id = create_agent_session(
        task_id=task_id,
        agent_type=f"parallel_subagent:{name}",
        llm_id=llm_id,
        budget_id=budget_id,
        scheduler_reason="scheduler",
    )
    exit_reason = "error"
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        agent = _CollectorAgent(
            task_id=task_id,
            system_prompt=system_prompt,
            tool_allowlist=tool_allowlist,
            max_turns=max_turns,
            llm_id=llm_id,
            budget_id=budget_id,
            llm_base_url=llm_base_url,
            llm_model=llm_model,
            max_context=max_context,
            user_message=user_msg,
            agent_name=f"subagent:{name}",
        )
        payload = loop.run_until_complete(agent.run())
        output = (payload or {}).get("output", str(payload or ""))
        blob = dict(task.content or {})
        blob["output"] = output
        update_task(task_id, content=blob, type="completed", stage_key="completed")
        exit_reason = "completed"
    except Exception:
        logger.exception("[parallel_subagent] task '%s' agent '%s' raised.", task_id, name)
        fresh = get_task(task_id)
        blob = dict((fresh.content or {}) if fresh else {})
        blob["output"] = f"ERROR: subagent '{name}' failed."
        blob["_subagent_failed"] = True
        update_task(task_id, content=blob, type="completed", stage_key="completed")
        # Still complete so the aggregator can fire
    finally:
        close_agent_session(session_id, exit_reason, "")
        try:
            loop.run_until_complete(asyncio.wait_for(loop.shutdown_asyncgens(), timeout=5.0))
        except Exception:
            pass
        try:
            loop.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# _psubagent_dangerous — write-capable parallel subagent (MaestroLoop)
# ---------------------------------------------------------------------------

def _run_parallel_subagent_dangerous(
    task_id: str,
    stage_config: "StageConfig",
    llm_base_url: str,
    llm_model: str,
    max_context: int | None,
    llm_id: int | None,
    budget_id: int | None,
    project_path: str | None,
) -> None:
    """
    Write-capable parallel subagent — runs a scoped MaestroLoop for one component.
    Config comes from task.content._subagent_cfg (injected by _run_parallel_agents).

    Unlike _run_parallel_subagent (_CollectorAgent, read-only), this variant has a
    worktree and full write access.  It does NOT call advance_stage; the aggregator
    drives the parent forward once all children complete.
    """
    import json as _json
    from app.agent.loop import MaestroLoop
    from app.agent.config import MAX_TURNS as _DEFAULT_MAX_TURNS
    from app.database import get_task, update_task, create_agent_session, close_agent_session

    task = get_task(task_id)
    if not task:
        return

    cfg: dict = (task.content or {}).get("_subagent_cfg", {})
    name: str = cfg.get("name", "subagent")
    system_prompt: str | None = cfg.get("system_prompt") or None
    max_turns: int = int(cfg.get("max_turns", _DEFAULT_MAX_TURNS))

    # agent_tools: list or comma-sep string; None falls back to INDEV_AGENT_TOOLS inside MaestroLoop
    _raw_tools = cfg.get("agent_tools")
    if isinstance(_raw_tools, list):
        agent_tools: list[str] | None = [t.strip() for t in _raw_tools if t.strip()] or None
    elif isinstance(_raw_tools, str) and _raw_tools.strip():
        agent_tools = [t.strip() for t in _raw_tools.split(",") if t.strip()] or None
    else:
        agent_tools = None

    session_id = create_agent_session(
        task_id=task_id,
        agent_type=f"parallel_subagent_dangerous:{name}",
        llm_id=llm_id,
        budget_id=budget_id,
        scheduler_reason="scheduler",
        max_turns=max_turns,
    )
    exit_reason = "error"
    exit_summary = ""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        maestro = MaestroLoop(
            task_id=task_id,
            max_turns=max_turns,
            llm_base_url=llm_base_url,
            llm_model=llm_model,
            max_context=max_context,
            llm_id=llm_id,
            budget_id=budget_id,
            project_path=project_path,
            system_prompt=system_prompt,
            agent_tools=agent_tools,
        )
        result = loop.run_until_complete(maestro.run())
        exit_summary = result.final_message or ""
        blob = dict(task.content or {})
        if result.status == "ACCEPTED":
            exit_reason = "completed"
            blob["output"] = exit_summary
        else:
            exit_reason = result.status.lower()
            blob["output"] = f"subagent '{name}' ended with status {result.status}: {exit_summary}"
            blob["_subagent_failed"] = True
        update_task(task_id, content=blob, type="completed", stage_key="completed")
    except Exception:
        logger.exception("[parallel_subagent_dangerous] task '%s' agent '%s' raised.", task_id, name)
        fresh = get_task(task_id)
        blob = dict((fresh.content or {}) if fresh else {})
        blob["output"] = f"ERROR: subagent '{name}' failed."
        blob["_subagent_failed"] = True
        update_task(task_id, content=blob, type="completed", stage_key="completed")
    finally:
        close_agent_session(session_id, exit_reason, exit_summary)
        try:
            loop.run_until_complete(asyncio.wait_for(loop.shutdown_asyncgens(), timeout=5.0))
        except Exception:
            pass
        try:
            loop.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# _psubagent_join — aggregator: merges outputs and advances parent
# ---------------------------------------------------------------------------

def _run_parallel_subagent_aggregator(
    task_id: str,
    stage_config: "StageConfig",
    llm_base_url: str,
    llm_model: str,
    max_context: int | None,
    llm_id: int | None,
    budget_id: int | None,
    project_path: str | None,
) -> None:
    """
    Aggregator for parallel_agents.  Merges child outputs into the parent task's
    content and calls advance_stage on the parent so it proceeds to the next stage.
    """
    from app.database import get_task, update_task, create_agent_session, close_agent_session

    task = get_task(task_id)
    if not task:
        return

    cfg: dict = (task.content or {}).get("_subagent_cfg", {})
    parent_task_id: str | None = cfg.get("parent_task_id")
    output_key: str = cfg.get("output_key", "parallel_agents_output")
    parent_stage_key: str = cfg.get("parent_stage_key", "")
    child_ids: list[str] = cfg.get("child_ids", [])

    session_id = create_agent_session(
        task_id=task_id,
        agent_type="parallel_subagent_aggregator",
        llm_id=llm_id,
        budget_id=budget_id,
        scheduler_reason="scheduler",
    )
    exit_reason = "error"
    try:
        merged: dict[str, str] = {}
        for cid in child_ids:
            child = get_task(cid)
            if not child:
                continue
            child_cfg = (child.content or {}).get("_subagent_cfg", {})
            name = child_cfg.get("name", cid)
            merged[name] = (child.content or {}).get("output", "")

        parent = get_task(parent_task_id) if parent_task_id else None
        if parent:
            parent_blob = dict(parent.content or {})
            parent_blob[output_key] = merged
            parent_blob.pop("_psubagent_waiting", None)
            update_task(parent_task_id, content=parent_blob)

        update_task(task_id, type="completed", stage_key="completed")
        advance_stage(parent_task_id, "pass", from_stage=parent_stage_key)
        exit_reason = "completed"
        logger.info(
            "[parallel_subagent_aggregator] task '%s': merged %d outputs → parent '%s' stage '%s'.",
            task_id, len(merged), parent_task_id, parent_stage_key,
        )
    except Exception:
        logger.exception("[parallel_subagent_aggregator] task '%s' raised.", task_id)
    finally:
        close_agent_session(session_id, exit_reason, "")
