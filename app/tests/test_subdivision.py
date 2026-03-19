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
# tally_votes — Rule 0
# ============================================================

class TestTallyVotesSubdivide:
    """Rule 0: any SUBDIVIDE_IDEA vote → outcome='subdivide'."""

    def test_single_subdivide_vote(self):
        votes = [
            Vote(stage="scope", verdict=Verdict.SUBDIVIDE_IDEA, confidence=80, justification="Too large"),
        ]
        result = tally_votes(votes)
        assert result.outcome == "subdivide"
        assert "SUBDIVIDE_IDEA" in result.summary

    def test_subdivide_beats_rejected(self):
        """Rule 0 fires before Rule 1 (REJECTED)."""
        votes = [
            Vote(stage="scope", verdict=Verdict.SUBDIVIDE_IDEA, confidence=80, justification="Too large"),
            Vote(stage="feasibility", verdict=Verdict.REJECTED, confidence=30, justification="Bad idea"),
        ]
        result = tally_votes(votes)
        assert result.outcome == "subdivide"

    def test_subdivide_beats_passed(self):
        """Even with LIKELY votes, a single SUBDIVIDE_IDEA triggers subdivision."""
        votes = [
            Vote(stage="scope", verdict=Verdict.SUBDIVIDE_IDEA, confidence=85, justification="Too big"),
            Vote(stage="feasibility", verdict=Verdict.LIKELY, confidence=95, justification="Feasible"),
            Vote(stage="conflict", verdict=Verdict.POSSIBLE, confidence=80, justification="No conflicts"),
        ]
        result = tally_votes(votes)
        assert result.outcome == "subdivide"

    def test_no_subdivide_passes_normally(self):
        """Without SUBDIVIDE_IDEA votes, normal rules apply."""
        votes = [
            Vote(stage="scope", verdict=Verdict.LIKELY, confidence=95, justification="Good"),
            Vote(stage="feasibility", verdict=Verdict.POSSIBLE, confidence=80, justification="OK"),
        ]
        result = tally_votes(votes)
        assert result.outcome == "passed"

    def test_subdivide_token_counts(self):
        """Token counts are accumulated correctly in subdivide outcome."""
        votes = [
            Vote(stage="scope", verdict=Verdict.SUBDIVIDE_IDEA, confidence=80,
                 justification="Big", prompt_tokens=100, completion_tokens=50),
            Vote(stage="feasibility", verdict=Verdict.LIKELY, confidence=92,
                 justification="OK", prompt_tokens=200, completion_tokens=100),
        ]
        result = tally_votes(votes)
        assert result.outcome == "subdivide"
        assert result.total_prompt_tokens == 300
        assert result.total_completion_tokens == 150


# ============================================================
# DAGResolver — cancelled/subdividing exclusions
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
# Config — subdivision settings
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

    def test_llm_temperature_loaded(self):
        from app.agent.config import SUBDIVISION_LLM_TEMPERATURE
        assert isinstance(SUBDIVISION_LLM_TEMPERATURE, float)
        assert 0.0 <= SUBDIVISION_LLM_TEMPERATURE <= 2.0

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
        assert SUBDIVISION_CONTEXT_BUDGET_RATIO == 0.30

    def test_planning_tools_loaded(self):
        from app.agent.config import SUBDIVISION_PLANNING_TOOLS
        assert isinstance(SUBDIVISION_PLANNING_TOOLS, list)
        assert "generate_architecture_doc" in SUBDIVISION_PLANNING_TOOLS
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
        assert "generate_architecture_doc" in names
        assert "spawn_research_agent" in names
        assert "list_directory" in names
        assert "find_files" in names
        # Codebase tools should NOT be present
        assert "read_file" not in names
        assert "git_blame" not in names

    def test_existing_code_gets_all_tools(self):
        from app.agent.subdivide import _build_context_aware_schemas
        schemas, names = _build_context_aware_schemas(has_source=True)
        assert "generate_architecture_doc" in names
        assert "read_file" in names
        assert "git_status" in names


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
# Intake pipeline — _build_tally with SUBDIVIDE_IDEA
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
        )
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
                "verdict": "LIKELY",
                "confidence": 0.95,
                "justification": "Feasible",
                "raw_response": {},
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "model": "test",
            },
        ]

        tally = pipeline._build_tally()
        assert tally["outcome"] == "subdivide"
        assert "SUBDIVIDE_IDEA" in tally.get("summary", "")
