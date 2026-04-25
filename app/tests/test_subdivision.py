"""
Tests for the SUBDIVIDE_IDEA verdict, tally_votes integration,
SubdivisionAgent parsing, and DAGResolver exclusions.
"""

import pytest
from app.agent.verdicts import Verdict, Vote, TallyResult, tally_votes


# ============================================================
# Verdict enum
# ============================================================

class TestSubdivideVerdict:
    """SUBDIVIDE_IDEA is a valid Verdict with full 0-100 confidence range."""

    def test_subdivide_idea_exists(self):
        assert Verdict.SUBDIVIDE_IDEA.value == "SUBDIVIDE_IDEA"

    def test_subdivide_idea_confidence_range(self):
        lo, hi = Verdict.SUBDIVIDE_IDEA.confidence_range
        assert lo == 0
        assert hi == 100

    def test_subdivide_idea_vote_any_confidence(self):
        """SUBDIVIDE_IDEA accepts any confidence 0-100."""
        for conf in [0, 25, 50, 75, 100]:
            v = Vote(
                stage="scope_analysis",
                verdict=Verdict.SUBDIVIDE_IDEA,
                confidence=conf,
                justification="Too big.",
            )
            assert v.confidence == conf


# ============================================================
# tally_votes - Rule 0
# ============================================================

class TestTallyVotesSubdivide:
    """Rule 0: majority of LLM stages (>=2 of 3) must vote SUBDIVIDE_IDEA."""

    def test_single_subdivide_vote_does_not_trigger(self):
        """A single SUBDIVIDE_IDEA vote is no longer sufficient on its own."""
        # 1/1 LLM stage; threshold = max(2, 1) = 2 → not met
        votes = [
            Vote(stage="scope", verdict=Verdict.SUBDIVIDE_IDEA, confidence=80, justification="Too large"),
        ]
        result = tally_votes(votes)
        assert result.outcome != "subdivide"

    def test_majority_subdivide_beats_rejected(self):
        """Rule 0 fires before Rule 1 when the majority threshold is met."""
        # 2/2 LLM stages; threshold = max(2, 2) = 2 → fires
        votes = [
            Vote(stage="scope", verdict=Verdict.SUBDIVIDE_IDEA, confidence=80, justification="Too large"),
            Vote(stage="feasibility", verdict=Verdict.SUBDIVIDE_IDEA, confidence=75, justification="Too big"),
            Vote(stage="conflict", verdict=Verdict.REJECTED, confidence=30, justification="Bad idea"),
        ]
        result = tally_votes(votes)
        assert result.outcome == "subdivide"

    def test_minority_subdivide_does_not_beat_passed(self):
        """1/3 SUBDIVIDE_IDEA votes (below threshold) falls through to normal rules."""
        # 1/3 LLM stages; threshold = max(2, 2) = 2 → not met → falls to 'passed'
        votes = [
            Vote(stage="scope", verdict=Verdict.SUBDIVIDE_IDEA, confidence=85, justification="Too big"),
            Vote(stage="feasibility", verdict=Verdict.LIKELY, confidence=95, justification="Feasible"),
            Vote(stage="conflict", verdict=Verdict.POSSIBLE, confidence=80, justification="No conflicts"),
        ]
        result = tally_votes(votes)
        assert result.outcome == "passed"

    def test_no_subdivide_passes_normally(self):
        """Without SUBDIVIDE_IDEA votes, normal rules apply."""
        votes = [
            Vote(stage="scope", verdict=Verdict.LIKELY, confidence=95, justification="Good"),
            Vote(stage="feasibility", verdict=Verdict.POSSIBLE, confidence=80, justification="OK"),
        ]
        result = tally_votes(votes)
        assert result.outcome == "passed"

    def test_subdivide_token_counts(self):
        """Token counts are accumulated correctly when Rule 0 fires."""
        # 2/2 LLM stages; threshold = max(2, 2) = 2 → fires
        votes = [
            Vote(stage="scope", verdict=Verdict.SUBDIVIDE_IDEA, confidence=80,
                 justification="Big", prompt_tokens=100, completion_tokens=50),
            Vote(stage="feasibility", verdict=Verdict.SUBDIVIDE_IDEA, confidence=85,
                 justification="Also too big", prompt_tokens=200, completion_tokens=100),
        ]
        result = tally_votes(votes)
        assert result.outcome == "subdivide"
        assert result.total_prompt_tokens == 300
        assert result.total_completion_tokens == 150


# ============================================================
# DAGResolver - cancelled/subdividing exclusions
# ============================================================

class TestDAGResolverExclusions:
    """Cancelled and subdividing tasks are excluded from ready-tasks."""

    def test_cancelled_task_not_ready(self):
        from app.agent.dag import DAGResolver

        tasks = [
            {"id": "t1", "type": "cancelled", "position": 0, "prerequisites": []},
            {"id": "t2", "type": "idea", "position": 0, "prerequisites": []},
        ]
        dag = DAGResolver(tasks)
        ready = dag.get_ready_tasks()
        ids = [t["id"] for t in ready]
        assert "t1" not in ids
        assert "t2" in ids

    def test_subdividing_task_not_ready(self):
        from app.agent.dag import DAGResolver

        tasks = [
            {"id": "t1", "type": "subdividing", "position": 0, "prerequisites": []},
            {"id": "t2", "type": "idea", "position": 0, "prerequisites": []},
        ]
        dag = DAGResolver(tasks)
        ready = dag.get_ready_tasks()
        ids = [t["id"] for t in ready]
        assert "t1" not in ids
        assert "t2" in ids

    def test_cancelled_prereq_blocks_dependent(self):
        """A cancelled prerequisite is NOT done, so it blocks dependents."""
        from app.agent.dag import DAGResolver

        tasks = [
            {"id": "t1", "type": "cancelled", "position": 0, "prerequisites": []},
            {"id": "t2", "type": "idea", "position": 1, "prerequisites": ["t1"]},
        ]
        dag = DAGResolver(tasks)
        ready = dag.get_ready_tasks()
        ids = [t["id"] for t in ready]
        assert "t2" not in ids


# ============================================================
# DAGResolver - Big Idea parent / child delegation
# ============================================================

class TestBigIdeaParentDelegation:
    """A subdivided Big Idea parent unblocks downstream tasks once all its
    active children complete, without the parent itself reaching 'completed'."""

    def _make_tasks(self, parent_type, child_types):
        """Helper: parent + children + one downstream task that prereqs the parent."""
        tasks = [
            {
                "id": "parent",
                "type": parent_type,
                "position": 0,
                "prerequisites": [],
                "parent_task_id": None,
            },
        ]
        for i, ct in enumerate(child_types):
            tasks.append({
                "id": f"child-{i}",
                "type": ct,
                "position": i,
                "prerequisites": [],
                "parent_task_id": "parent",
            })
        tasks.append({
            "id": "downstream",
            "type": "idea",
            "position": 0,
            "prerequisites": ["parent"],
            "parent_task_id": None,
        })
        return tasks

    def test_parent_not_dispatched_when_has_children(self):
        """A parent with children must never appear in get_ready_tasks."""
        from app.agent.dag import DAGResolver
        tasks = self._make_tasks("idea", ["idea", "idea"])
        dag = DAGResolver(tasks)
        ids = [t["id"] for t in dag.get_ready_tasks()]
        assert "parent" not in ids

    def test_downstream_blocked_while_children_pending(self):
        """Downstream prereq on parent stays blocked while children are not done."""
        from app.agent.dag import DAGResolver
        tasks = self._make_tasks("idea", ["idea", "idea"])
        dag = DAGResolver(tasks)
        ids = [t["id"] for t in dag.get_ready_tasks()]
        assert "downstream" not in ids

    def test_downstream_unblocked_when_all_children_completed(self):
        """Downstream unblocks once every active child reaches 'completed'."""
        from app.agent.dag import DAGResolver
        tasks = self._make_tasks("idea", ["completed", "completed"])
        dag = DAGResolver(tasks)
        ids = [t["id"] for t in dag.get_ready_tasks()]
        assert "downstream" in ids

    def test_downstream_blocked_when_one_child_still_pending(self):
        """One unfinished child keeps the parent (and downstream) blocked."""
        from app.agent.dag import DAGResolver
        tasks = self._make_tasks("idea", ["completed", "idea"])
        dag = DAGResolver(tasks)
        ids = [t["id"] for t in dag.get_ready_tasks()]
        assert "downstream" not in ids

    def test_all_cancelled_children_keeps_downstream_blocked(self):
        """All-cancelled children -> parent still blocked (conservative)."""
        from app.agent.dag import DAGResolver
        tasks = self._make_tasks("idea", ["cancelled", "cancelled"])
        dag = DAGResolver(tasks)
        ids = [t["id"] for t in dag.get_ready_tasks()]
        assert "downstream" not in ids

    def test_mixed_cancelled_and_completed_unblocks_downstream(self):
        """Cancelled children are ignored; all remaining active children done -> unblocks."""
        from app.agent.dag import DAGResolver
        tasks = self._make_tasks("idea", ["completed", "cancelled"])
        dag = DAGResolver(tasks)
        ids = [t["id"] for t in dag.get_ready_tasks()]
        assert "downstream" in ids

    def test_nested_big_idea_all_grandchildren_done_unblocks(self):
        """Two levels of subdivision: grandchildren all done -> outer downstream unblocks."""
        from app.agent.dag import DAGResolver
        tasks = [
            {"id": "grandparent", "type": "idea",      "position": 0, "prerequisites": [],              "parent_task_id": None},
            {"id": "parent",      "type": "idea",      "position": 0, "prerequisites": [],              "parent_task_id": "grandparent"},
            {"id": "child-0",     "type": "completed", "position": 0, "prerequisites": [],              "parent_task_id": "parent"},
            {"id": "child-1",     "type": "completed", "position": 1, "prerequisites": ["child-0"],     "parent_task_id": "parent"},
            {"id": "downstream",  "type": "idea",      "position": 0, "prerequisites": ["grandparent"], "parent_task_id": None},
        ]
        dag = DAGResolver(tasks)
        ids = [t["id"] for t in dag.get_ready_tasks()]
        assert "downstream" in ids
        assert "grandparent" not in ids
        assert "parent" not in ids

    def test_nested_big_idea_pending_grandchild_blocks(self):
        """Two levels: one pending grandchild keeps outer downstream blocked."""
        from app.agent.dag import DAGResolver
        tasks = [
            {"id": "grandparent", "type": "idea",      "position": 0, "prerequisites": [],              "parent_task_id": None},
            {"id": "parent",      "type": "idea",      "position": 0, "prerequisites": [],              "parent_task_id": "grandparent"},
            {"id": "child-0",     "type": "completed", "position": 0, "prerequisites": [],              "parent_task_id": "parent"},
            {"id": "child-1",     "type": "idea",      "position": 1, "prerequisites": ["child-0"],     "parent_task_id": "parent"},
            {"id": "downstream",  "type": "idea",      "position": 0, "prerequisites": ["grandparent"], "parent_task_id": None},
        ]
        dag = DAGResolver(tasks)
        ids = [t["id"] for t in dag.get_ready_tasks()]
        assert "downstream" not in ids


# ============================================================
# SubdivisionAgent result parsing
# ============================================================

class TestSubdivisionResultParsing:
    """Test the SubdivisionAgent's JSON extraction logic."""

    def test_parse_valid_result(self):
        from app.agent.subdivide import SubdivisionAgent
        agent = SubdivisionAgent(
            parent_task_id="test-1",
            parent_title="Test",
            parent_description="Test description",
            llm_id=1,
            budget_id=1,
        )

        content = '''
        {
          "sub_ideas": [
            {
              "title": "Part A",
              "description": "First part",
              "prerequisites": [],
              "estimated_scope": "small",
              "rationale": "It's small"
            },
            {
              "title": "Part B",
              "description": "Second part",
              "prerequisites": ["sub-0"],
              "estimated_scope": "medium",
              "rationale": "Depends on A"
            }
          ],
          "decomposition_rationale": "Split by concern",
          "coverage_check": "A + B = full task",
          "confidence": 85
        }
        '''
        result = agent._extract_result(content)
        assert result is not None
        assert len(result.sub_ideas) == 2
        assert result.sub_ideas[0].title == "Part A"
        assert result.sub_ideas[1].prerequisites == ["sub-0"]
        assert result.confidence == 85
        assert result.decomposition_rationale == "Split by concern"

    def test_parse_empty_sub_ideas_returns_none(self):
        from app.agent.subdivide import SubdivisionAgent
        agent = SubdivisionAgent(
            parent_task_id="test-1",
            parent_title="Test",
            parent_description="Test",
            llm_id=1,
            budget_id=1,
        )
        content = '{"sub_ideas": [], "confidence": 30}'
        result = agent._extract_result(content)
        assert result is None

    def test_parse_no_json_returns_none(self):
        from app.agent.subdivide import SubdivisionAgent
        agent = SubdivisionAgent(
            parent_task_id="test-1",
            parent_title="Test",
            parent_description="Test",
            llm_id=1,
            budget_id=1,
        )
        result = agent._extract_result("I need more information to decompose this task.")
        assert result is None

    def test_parse_fenced_json(self):
        from app.agent.subdivide import SubdivisionAgent
        agent = SubdivisionAgent(
            parent_task_id="test-1",
            parent_title="Test",
            parent_description="Test",
            llm_id=1,
            budget_id=1,
        )
        content = '''Here's my decomposition:

```json
{
  "sub_ideas": [
    {"title": "X", "description": "Do X", "prerequisites": [], "estimated_scope": "small", "rationale": "Small"}
  ],
  "decomposition_rationale": "Simple split",
  "coverage_check": "Covers everything",
  "confidence": 90
}
```'''
        result = agent._extract_result(content)
        assert result is not None
        assert len(result.sub_ideas) == 1
        assert result.confidence == 90


# ============================================================
# Config - subdivision settings
# ============================================================

class TestSubdivisionConfig:
    """Subdivision config values are loaded from maestro.ini."""

    def test_max_depth_loaded(self):
        from app.agent.config import SUBDIVISION_MAX_DEPTH
        assert isinstance(SUBDIVISION_MAX_DEPTH, int)
        assert SUBDIVISION_MAX_DEPTH > 0

    def test_max_retries_loaded(self):
        from app.agent.config import SUBDIVISION_MAX_RETRIES
        assert isinstance(SUBDIVISION_MAX_RETRIES, int)
        assert SUBDIVISION_MAX_RETRIES > 0

    def test_max_total_sub_ideas_loaded(self):
        from app.agent.config import SUBDIVISION_MAX_TOTAL_SUB_IDEAS
        assert isinstance(SUBDIVISION_MAX_TOTAL_SUB_IDEAS, int)
        assert SUBDIVISION_MAX_TOTAL_SUB_IDEAS > 0

    def test_tools_loaded(self):
        from app.agent.config import SUBDIVISION_AGENT_TOOLS
        assert isinstance(SUBDIVISION_AGENT_TOOLS, list)
        assert len(SUBDIVISION_AGENT_TOOLS) > 0
        assert "read_file" in SUBDIVISION_AGENT_TOOLS


class TestBigIdeaConfig:
    """Verify the updated subdivision config values."""

    def test_doubled_depth(self):
        from app.agent.config import SUBDIVISION_MAX_DEPTH
        assert SUBDIVISION_MAX_DEPTH == 6

    def test_doubled_retries(self):
        from app.agent.config import SUBDIVISION_MAX_RETRIES
        assert SUBDIVISION_MAX_RETRIES == 4

    def test_doubled_total(self):
        from app.agent.config import SUBDIVISION_MAX_TOTAL_SUB_IDEAS
        assert SUBDIVISION_MAX_TOTAL_SUB_IDEAS == 30

    def test_context_budget_ratio(self):
        from app.agent.config import SUBDIVISION_CONTEXT_BUDGET_RATIO
        assert SUBDIVISION_CONTEXT_BUDGET_RATIO == 0.60

    def test_planning_tools_loaded(self):
        from app.agent.config import SUBDIVISION_PLANNING_TOOLS
        assert isinstance(SUBDIVISION_PLANNING_TOOLS, list)
        assert "write_arch_doc" in SUBDIVISION_PLANNING_TOOLS
        assert "spawn_research_agent" in SUBDIVISION_PLANNING_TOOLS


class TestInterfaceContractParsing:
    """Test _try_parse with provides/consumes fields."""

    def test_parse_with_provides_consumes(self):
        from app.agent.subdivide import SubdivisionAgent
        agent = SubdivisionAgent(
            parent_task_id="test-1",
            parent_title="Test",
            parent_description="Test",
            llm_id=1,
            budget_id=1,
        )
        content = '''
        {
          "sub_ideas": [
            {
              "title": "Part A",
              "description": "First part",
              "prerequisites": [],
              "estimated_scope": "small",
              "rationale": "Foundation",
              "provides": [{"name": "DataModel", "type": "class"}],
              "consumes": []
            },
            {
              "title": "Part B",
              "description": "Second part",
              "prerequisites": ["sub-0"],
              "estimated_scope": "medium",
              "rationale": "Depends on A",
              "provides": [],
              "consumes": [{"name": "DataModel", "type": "class", "source": "sub-0"}]
            }
          ],
          "interface_contracts": [
            {"component": "Part A", "provides": [{"name": "DataModel"}], "consumes": []}
          ],
          "decomposition_rationale": "Split by concern",
          "coverage_check": "A + B = full task",
          "confidence": 85
        }
        '''
        result = agent._extract_result(content)
        assert result is not None
        assert len(result.sub_ideas) == 2
        assert result.sub_ideas[0].provides == [{"name": "DataModel", "type": "class"}]
        assert result.sub_ideas[1].consumes == [{"name": "DataModel", "type": "class", "source": "sub-0"}]
        assert len(result.interface_contracts) == 1
        assert result.interface_contracts[0]["component"] == "Part A"


class TestContextAwareToolSelection:
    """Verify tool selection for greenfield vs existing code."""

    def test_greenfield_gets_planning_tools(self):
        from app.agent.subdivide import _build_context_aware_schemas
        schemas, names = _build_context_aware_schemas(has_source=False)
        assert "write_arch_doc" in names
        assert "spawn_research_agent" in names
        assert "read_list_dir" in names
        assert "find_files" in names
        # Codebase tools should NOT be present
        assert "read_file" not in names
        assert "read_git_blame" not in names

    def test_existing_code_gets_all_tools(self):
        from app.agent.subdivide import _build_context_aware_schemas
        schemas, names = _build_context_aware_schemas(has_source=True)
        assert "write_arch_doc" in names
        assert "read_file" in names
        assert "read_git_status" in names


class TestSubdivisionStrategyGuidance:
    """Verify prompt no longer says 'Prefer vertical slices'."""

    def test_no_prefer_vertical_slices(self):
        from app.agent.subdivide import _SUBDIVISION_SYSTEM_PROMPT
        assert "Prefer vertical slices" not in _SUBDIVISION_SYSTEM_PROMPT

    def test_has_greenfield_guidance(self):
        from app.agent.subdivide import _SUBDIVISION_SYSTEM_PROMPT
        assert "greenfield" in _SUBDIVISION_SYSTEM_PROMPT.lower()

    def test_has_planning_tools_guidance(self):
        from app.agent.subdivide import _SUBDIVISION_SYSTEM_PROMPT
        assert "generate_architecture_doc" in _SUBDIVISION_SYSTEM_PROMPT


# ============================================================
# Intake pipeline - _build_tally with SUBDIVIDE_IDEA
# ============================================================

class TestIntakeBuildTallySubdivide:
    """The IntakePipeline._build_tally() method handles SUBDIVIDE_IDEA."""

    def test_build_tally_subdivide(self):
        from app.agent.intake import IntakePipeline

        pipeline = IntakePipeline(
            task_id="test-1",
            task_description="Big task",
            task_title="Big task",
            all_tasks=[],
            budget_id=1,
            llm_id=1,
            project="TheMaestro",  # Required for static analysis
        )
        # Two LLM stages vote SUBDIVIDE_IDEA; threshold = max(2, 2) = 2 → fires
        pipeline.votes = [
            {
                "stage": "scope_analysis",
                "verdict": "SUBDIVIDE_IDEA",
                "confidence": 0.85,
                "justification": "Too large",
                "raw_response": {},
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "model": "test",
            },
            {
                "stage": "feasibility_analysis",
                "verdict": "SUBDIVIDE_IDEA",
                "confidence": 0.80,
                "justification": "Also too large",
                "raw_response": {},
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "model": "test",
            },
        ]

        tally = pipeline._build_tally()
        assert tally["outcome"] == "subdivide"
        assert "SUBDIVIDE_IDEA" in tally.get("summary", "")
