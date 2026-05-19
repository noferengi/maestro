"""
Tests for pipeline_router — Phase 2 stage transition and dispatch.
"""
import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def router_db():
    """Seed a pipeline template into the shared test DB for router tests.

    The _db_rollback fixture in conftest wraps each test in a rolled-back
    transaction, so this data is automatically cleaned up after the test.
    """
    import app.database as db_mod

    db = db_mod.SessionLocal()
    try:
        # Template
        template = db_mod.PipelineTemplate(name="RouterTest", is_default=True, is_builtin=False)
        db.add(template)
        db.flush()

        # Stages: idea → planning → indev
        s_idea     = db_mod.PipelineStage(template_id=template.id, stage_key="idea",     label="Idea",     agent_type="intake_agent",         position=0)
        s_planning = db_mod.PipelineStage(template_id=template.id, stage_key="planning", label="Planning", agent_type="planning_agent",        position=1)
        s_indev    = db_mod.PipelineStage(template_id=template.id, stage_key="indev",    label="InDev",    agent_type="implementation_agent",  position=2)
        for s in (s_idea, s_planning, s_indev):
            db.add(s)
        db.flush()

        # Transitions
        db.add(db_mod.PipelineTransition(template_id=template.id, from_stage_id=s_idea.id,     to_stage_id=s_planning.id, condition="pass",   priority=0))
        db.add(db_mod.PipelineTransition(template_id=template.id, from_stage_id=s_planning.id, to_stage_id=s_indev.id,    condition="pass",   priority=0))
        db.add(db_mod.PipelineTransition(template_id=template.id, from_stage_id=s_indev.id,    to_stage_id=s_planning.id, condition="fail",   priority=0))
        db.add(db_mod.PipelineTransition(template_id=template.id, from_stage_id=s_indev.id,    to_stage_id=s_planning.id, condition="reject", priority=0))

        # Project linked to template
        project = db_mod.Project(name="RouterProj", pipeline_template_id=template.id)
        db.add(project)
        db.flush()

        # Task in idea stage
        task = db_mod.Task(
            id="task-router-1",
            title="Router test task",
            type="idea",
            stage_key="idea",
            project_id=project.id,
            history=[],
            prerequisites=[],
        )
        db.add(task)
        db.commit()

        yield db_mod, template.id, project.id, "task-router-1"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# get_next_stage
# ---------------------------------------------------------------------------

def test_get_next_stage_pass_from_idea(router_db):
    db_mod, _, _, task_id = router_db
    from app.agent.pipeline_router import get_next_stage
    assert get_next_stage(task_id, "pass") == "planning"


def test_get_next_stage_pass_from_planning(router_db):
    db_mod, _, _, task_id = router_db
    db_mod.update_task(task_id, stage_key="planning", type="planning")
    from app.agent.pipeline_router import get_next_stage
    assert get_next_stage(task_id, "pass") == "indev"


def test_get_next_stage_fail_from_indev(router_db):
    db_mod, _, _, task_id = router_db
    db_mod.update_task(task_id, stage_key="indev", type="indev")
    from app.agent.pipeline_router import get_next_stage
    assert get_next_stage(task_id, "fail") == "planning"


def test_get_next_stage_no_edge_returns_none(router_db):
    _, _, _, task_id = router_db
    from app.agent.pipeline_router import get_next_stage
    # "skip" condition has no edge in the test template
    assert get_next_stage(task_id, "skip") is None


def test_get_next_stage_missing_task_returns_none(router_db):
    from app.agent.pipeline_router import get_next_stage
    assert get_next_stage("task-does-not-exist", "pass") is None


# ---------------------------------------------------------------------------
# advance_stage
# ---------------------------------------------------------------------------

def test_advance_stage_updates_both_fields(router_db):
    db_mod, _, _, task_id = router_db
    from app.agent.pipeline_router import advance_stage

    result = advance_stage(task_id, "pass")
    assert result is True

    task = db_mod.get_task(task_id)
    assert task.type == "planning"
    assert task.stage_key == "planning"


def test_advance_stage_no_edge_returns_false_and_unchanged(router_db):
    db_mod, _, _, task_id = router_db
    from app.agent.pipeline_router import advance_stage

    result = advance_stage(task_id, "skip")
    assert result is False

    task = db_mod.get_task(task_id)
    assert task.type == "idea"      # unchanged
    assert task.stage_key == "idea"


def test_advance_stage_reject_condition(router_db):
    db_mod, _, _, task_id = router_db
    db_mod.update_task(task_id, stage_key="indev", type="indev")
    from app.agent.pipeline_router import advance_stage

    result = advance_stage(task_id, "reject")
    assert result is True

    task = db_mod.get_task(task_id)
    assert task.type == "planning"
    assert task.stage_key == "planning"


# ---------------------------------------------------------------------------
# get_stage_config
# ---------------------------------------------------------------------------

def test_get_stage_config_returns_correct_agent_type(router_db):
    _, _, _, task_id = router_db
    from app.agent.pipeline_router import get_stage_config

    cfg = get_stage_config(task_id)
    assert cfg is not None
    assert cfg.stage_key == "idea"
    assert cfg.agent_type == "intake_agent"
    assert cfg.position == 0


def test_get_stage_config_missing_task_returns_none(router_db):
    from app.agent.pipeline_router import get_stage_config
    assert get_stage_config("nonexistent-task") is None


# ---------------------------------------------------------------------------
# dispatch_task — handler registration
# ---------------------------------------------------------------------------

def test_dispatch_task_calls_registered_handler(router_db):
    db_mod, _, _, task_id = router_db
    from app.agent.pipeline_router import register_handler, dispatch_task

    called_with = {}

    def fake_handler(tid, base_url, model, ctx, lid, bid, ppath):
        called_with["task_id"] = tid
        called_with["stage"] = "idea"

    register_handler("idea", fake_handler)
    try:
        result = dispatch_task(
            task_id,
            llm_base_url="http://localhost:8008/v1",
            llm_model="test",
            max_context=None,
            llm_id=None,
            budget_id=None,
            project_path=None,
        )
        assert result is True
        assert called_with["task_id"] == task_id
    finally:
        # Restore — don't pollute other tests with fake handler
        register_handler("idea", lambda *a: None)


def test_dispatch_task_no_handler_returns_false(router_db):
    db_mod, _, _, task_id = router_db
    # Put task in a stage with no registered handler
    db_mod.update_task(task_id, stage_key="human_review", type="human_review")

    from app.agent.pipeline_router import dispatch_task
    result = dispatch_task(
        task_id,
        llm_base_url="http://localhost:8008/v1",
        llm_model="test",
        max_context=None,
        llm_id=None,
        budget_id=None,
        project_path=None,
    )
    assert result is False
