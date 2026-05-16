"""
Tests for the Templates Gallery (Phase 10).

Covers: list templates, clone builtin, assign to project, delete-builtin blocked,
export/import round-trip.
"""

import json
import pytest

from app.database.crud_malleable import (
    get_all_templates,
    get_template,
    clone_template,
    get_stages_for_template,
    create_template,
    create_stage,
    delete_template,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _seed_builtin_templates():
    """Create minimal builtin templates for gallery tests (data is truncated by conftest)."""
    TEMPLATES = [
        {
            "name": "Software Development",
            "stages": [
                ("idea", "Idea", "intake_agent"),
                ("planning", "Planning", "planning_agent"),
                ("indev", "In Dev", "maestro_agent"),
            ],
        },
        {
            "name": "Bug Triage",
            "stages": [
                ("triage", "Triage", "intake_agent"),
                ("investigate", "Investigate", "maestro_agent"),
                ("resolve", "Resolve", "maestro_agent"),
            ],
        },
        {
            "name": "Novel Writing",
            "stages": [
                ("outline", "Outline", "planning_agent"),
                ("draft", "Draft", "maestro_agent"),
                ("revise", "Revise", "maestro_agent"),
            ],
        },
    ]
    for tpl in TEMPLATES:
        existing = None
        for t in get_all_templates():
            if t.name == tpl["name"] and t.is_builtin:
                existing = t
                break
        if existing is None:
            tmpl = create_template(tpl["name"], description=tpl["name"], is_builtin=True)
            if tmpl:
                for pos, (key, label, agent) in enumerate(tpl["stages"]):
                    create_stage(tmpl.id, key, label, agent, pos)


def _find_builtin(name: str = "Software Development"):
    templates = get_all_templates()
    for t in templates:
        if t.is_builtin and t.name == name:
            return t
    return None


def _make_project(proj_name: str = "gallery_test_proj"):
    from app.database import upsert_project
    upsert_project(proj_name)
    from app.database.session import SessionLocal
    from app.database.models import Project
    db = SessionLocal()
    try:
        return db.query(Project).filter(Project.name == proj_name).first()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestTemplatesGallery:
    @pytest.fixture(autouse=True)
    def seed_templates(self):
        _seed_builtin_templates()

    def test_list_templates_includes_builtins(self):
        templates = get_all_templates()
        assert len(templates) > 0, "Expected at least the Software Development builtin"
        builtin_names = {t.name for t in templates if t.is_builtin}
        assert "Software Development" in builtin_names

    def test_clone_builtin_sets_not_builtin(self):
        original = _find_builtin("Software Development")
        if original is None:
            pytest.skip("Software Development builtin not seeded")

        cloned = clone_template(original.id, new_name="SD Clone Gallery Test")
        assert cloned is not None
        assert cloned.is_builtin is False
        assert cloned.name == "SD Clone Gallery Test"

        # Stages should be copied
        orig_stages = get_stages_for_template(original.id)
        clone_stages = get_stages_for_template(cloned.id)
        assert len(clone_stages) == len(orig_stages)

    def test_delete_builtin_blocked(self):
        original = _find_builtin("Software Development")
        if original is None:
            pytest.skip("Software Development builtin not seeded")

        # delete_template raises ValueError("template_is_builtin") for builtins
        with pytest.raises(ValueError, match="template_is_builtin"):
            delete_template(original.id)

        # Template must still exist after the attempted delete
        still_there = get_template(original.id)
        assert still_there is not None

    def test_assign_template_to_project(self):
        from app.database import upsert_project
        from app.database.session import SessionLocal
        from app.database.models import Project

        proj = _make_project("assign_tmpl_test")
        original = _find_builtin("Novel Writing") or _find_builtin("Software Development")
        if original is None:
            pytest.skip("No builtin template found to assign")

        db = SessionLocal()
        try:
            row = db.query(Project).filter(Project.name == proj.name).first()
            row.pipeline_template_id = original.id
            db.commit()
            db.refresh(row)
            assert row.pipeline_template_id == original.id
        finally:
            db.close()

    def test_export_import_roundtrip(self):
        from app.database.crud_malleable import export_template, import_template

        original = _find_builtin("Bug Triage") or _find_builtin("Software Development")
        if original is None:
            pytest.skip("No builtin template available for export/import round-trip test")

        exported = export_template(original.id)
        assert isinstance(exported, dict)
        assert "stages" in exported
        orig_stage_count = len(exported["stages"])
        assert orig_stage_count > 0

        # Import under a different name so it doesn't conflict with the existing builtin
        blob = dict(exported)
        blob["name"] = "Roundtrip Test Import Gallery"
        imported = import_template(blob)
        assert imported is not None
        imported_stages = get_stages_for_template(imported.id)
        assert len(imported_stages) == orig_stage_count, \
            f"Expected {orig_stage_count} stages after import, got {len(imported_stages)}"
        assert imported.is_builtin is False
