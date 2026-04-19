"""
app/tests/test_dreamer_survey_integration.py
-------------------------------------------
Tests for DreamerAgent survey mode integration.
"""

import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from app.agent.dreamer import DreamerAgent, ProjectState

@pytest.fixture
def dreamer():
    return DreamerAgent(
        project_name="TestProj",
        project_path="/tmp/test",
        llm_id=1,
        budget_id=1,
        llm_base_url="http://localhost:8008/v1",
        llm_model="test-model"
    )

@pytest.mark.asyncio
async def test_decide_survey_initiates_survey(dreamer):
    """Verify that _decide_survey calls SurveyOrchestrator.ensure_project_surveyed."""
    state = ProjectState(
        project_name="TestProj",
        failed=[],
        arch_context="Platform: Linux",
        deleted_tasks=[]
    )
    
    with patch("app.agent.survey_orchestrator.SurveyOrchestrator") as mock_so_cls, \
         patch("app.agent.llm_client.call_llm", new_callable=AsyncMock) as mock_call, \
         patch("app.agent.tools.build_tool_schemas"), \
         patch("app.agent.tools.async_dispatch_tool"):
        
        mock_so = mock_so_cls.return_value
        mock_call.return_value = {"message": {"content": '{"new_cards": []}'}}
        
        await dreamer._decide_survey(state)
        
        # Verify ensure_project_surveyed was called
        mock_so.ensure_project_surveyed.assert_called_once_with(
            "TestProj", "/tmp/test", 1, 1
        )

@pytest.mark.asyncio
async def test_dreamer_uses_survey_tool(dreamer):
    """Verify that Dreamer can call a survey tool in the loop."""
    state = ProjectState(project_name="TestProj", failed=[], arch_context="", deleted_tasks=[])
    
    # 1st turn: LLM calls get_project_summary
    # 2nd turn: LLM returns final JSON
    mock_responses = [
        {
            "message": {
                "role": "assistant",
                "tool_calls": [{
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "get_project_summary",
                        "arguments": "{}"
                    }
                }]
            }
        },
        {
            "message": {
                "role": "assistant",
                "content": '{"new_cards": [{"title": "New Idea", "description": "...", "rationale": "..."}]}'
            }
        }
    ]
    
    async def _mock_call(*args, **kwargs):
        return mock_responses.pop(0)

    with patch("app.agent.survey_orchestrator.SurveyOrchestrator"), \
         patch("app.agent.llm_client.call_llm", side_effect=_mock_call), \
         patch("app.agent.tools.build_tool_schemas"), \
         patch("app.agent.tools.async_dispatch_tool", return_value="Project health is good.") as mock_dispatch:
        
        plan = await dreamer._decide_survey(state)
        
        assert len(plan.new_cards) == 1
        assert plan.new_cards[0]["title"] == "New Idea"
        
        # Verify tool was dispatched
        mock_dispatch.assert_called_once()
        args, kwargs = mock_dispatch.call_args
        assert args[0] == "get_project_summary"
