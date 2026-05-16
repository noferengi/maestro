"""
app/agent/stage_executors.py
------------------------------
Generic pipeline node executors registered via register_agent_type_executor().

Three executor types:
  circuit_breaker  — configurable attempt counter; parks or fails when exhausted
  voting_panel     — N-voter LLM panel with tally strategy
  fan_out_judge    — best-of-N parallel agents + LLM judge picks the winner

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

    Stage config shape:
        voter_count          — number of voters (default 3)
        voter_system_prompt  — system prompt for each voter
        voter_tools          — tool allowlist for voters (default: submit_work only)
        voter_max_turns      — max turns per voter (default 10)
        tally                — "majority" (default) — uses verdicts.tally_votes()
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
        user_msg = (
            f"Task ID: {task_id}\n"
            f"Title: {task.title if task else '(unknown)'}\n"
            f"Description:\n{task.description or '' if task else ''}\n\n"
            "Vote on whether this task should proceed. Call submit_work with:\n"
            "  signal='ACCEPTED' or 'REJECTED'\n"
            "  summary='your reasoning'\n"
            "  payload={'verdict': 'ACCEPTED'|'REJECTED', 'confidence': 0-100, 'justification': '...'}"
        )

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

    Stage config shape:
        n                    — number of parallel agents (default 3)
        agent_system_prompt  — system prompt for proposal agents
        agent_tools          — tool allowlist for proposal agents
        agent_max_turns      — max turns per proposal agent (default 30)
        judge_system_prompt  — system prompt for the judge
        judge_max_turns      — max turns for judge (default 10)
        output_key           — task.content key to write winning proposal (default "winning_proposal")
    """
    from app.database import (
        create_agent_session, close_agent_session, get_task, update_task,
    )

    cfg = stage_config.config or {}
    n                   = int(cfg.get("n", 3))
    agent_system_prompt = cfg.get("agent_system_prompt", "Produce a proposal for the task. Submit it via submit_work.")
    agent_tools         = list(cfg.get("agent_tools") or [])
    agent_max_turns     = int(cfg.get("agent_max_turns", 30))
    judge_system_prompt = cfg.get("judge_system_prompt", "Compare the proposals below and pick the best one.")
    judge_max_turns     = int(cfg.get("judge_max_turns", 10))
    output_key          = cfg.get("output_key", "winning_proposal")

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

        proposer_user_msg = (
            f"Task ID: {task_id}\n"
            f"Title: {task_title}\n"
            f"Description:\n{task_desc}\n\n"
            "Produce your best proposal. When done, call submit_work with:\n"
            "  signal='ACCEPTED'\n"
            "  summary='brief summary'\n"
            "  payload={'proposal': '<your full proposal text>'}"
        )

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
                user_message=proposer_user_msg,
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
            f"=== Proposal {i} ===\n{str(p)[:_MAX_CHARS]}"
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
