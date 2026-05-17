"""
CRUD operations for Malleable Pipeline configuration tables.
"""

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from .session import SessionLocal
from .models import (
    PipelineTemplate, PipelineStage, PipelineTransition,
    PipelineStageGroup, PipelineArchCategory,
    CustomAgentDefinition,
)

logger = logging.getLogger(__name__)

_VALID_CONDITIONS = {"pass", "fail", "reject", "always", "skip"}

# ---------------------------------------------------------------------------
# PipelineTemplate CRUD
# ---------------------------------------------------------------------------

def get_all_templates() -> List[PipelineTemplate]:
    db = SessionLocal()
    try:
        return db.query(PipelineTemplate).order_by(PipelineTemplate.name).all()
    finally:
        db.close()


def get_template(template_id: int) -> Optional[PipelineTemplate]:
    db = SessionLocal()
    try:
        return db.query(PipelineTemplate).filter(PipelineTemplate.id == template_id).first()
    finally:
        db.close()


def get_template_by_name(name: str) -> Optional[PipelineTemplate]:
    db = SessionLocal()
    try:
        return db.query(PipelineTemplate).filter(PipelineTemplate.name == name).first()
    finally:
        db.close()


def get_default_template() -> Optional[PipelineTemplate]:
    db = SessionLocal()
    try:
        return db.query(PipelineTemplate).filter(PipelineTemplate.is_default == True).first()
    finally:
        db.close()


def create_template(
    name: str,
    description: Optional[str] = None,
    is_default: bool = False,
    is_builtin: bool = False,
) -> Optional[PipelineTemplate]:
    db = SessionLocal()
    try:
        if db.query(PipelineTemplate).filter(PipelineTemplate.name == name).first():
            return None  # name collision
        t = PipelineTemplate(
            name=name,
            description=description,
            is_default=is_default,
            is_builtin=is_builtin,
            version=1,
        )
        db.add(t)
        db.commit()
        db.refresh(t)
        return t
    except Exception:
        db.rollback()
        logger.exception("create_template failed for name=%r", name)
        return None
    finally:
        db.close()


def update_template(
    template_id: int,
    name: Optional[str] = None,
    description: Optional[str] = None,
    is_default: Optional[bool] = None,
    version_bump: bool = False,
) -> Optional[PipelineTemplate]:
    db = SessionLocal()
    try:
        t = db.query(PipelineTemplate).filter(PipelineTemplate.id == template_id).first()
        if not t:
            return None
        if name is not None:
            t.name = name
        if description is not None:
            t.description = description
        if is_default is not None:
            t.is_default = is_default
        if version_bump:
            t.version = (t.version or 1) + 1
        t.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(t)
        return t
    except Exception:
        db.rollback()
        logger.exception("update_template failed for id=%d", template_id)
        return None
    finally:
        db.close()


def delete_template(template_id: int, force: bool = False) -> bool:
    """Delete a template.  Blocked if builtin or any project uses it (unless force=True)."""
    db = SessionLocal()
    try:
        t = db.query(PipelineTemplate).filter(PipelineTemplate.id == template_id).first()
        if not t:
            return False

        if t.is_builtin:
            raise ValueError("template_is_builtin")

        if not force:
            from .models import Project
            if db.query(Project).filter(Project.pipeline_template_id == template_id).first():
                raise ValueError("template_in_use")

        # Cascade-delete children (transitions → stages → groups → arch_cats)
        # Order matters because of FKs: transitions reference stage IDs
        stage_ids = [
            row[0] for row in
            db.query(PipelineStage.id).filter(PipelineStage.template_id == template_id).all()
        ]
        if stage_ids:
            db.query(PipelineTransition).filter(
                PipelineTransition.from_stage_id.in_(stage_ids)
            ).delete(synchronize_session=False)
            db.query(PipelineTransition).filter(
                PipelineTransition.to_stage_id.in_(stage_ids)
            ).delete(synchronize_session=False)
            db.query(PipelineStage).filter(PipelineStage.template_id == template_id).delete(synchronize_session=False)

        db.query(PipelineStageGroup).filter(PipelineStageGroup.template_id == template_id).delete(synchronize_session=False)
        db.query(PipelineArchCategory).filter(PipelineArchCategory.template_id == template_id).delete(synchronize_session=False)
        db.delete(t)
        db.commit()
        return True
    except ValueError:
        raise
    except Exception:
        db.rollback()
        logger.exception("delete_template failed for id=%d", template_id)
        return False
    finally:
        db.close()


# ---------------------------------------------------------------------------
# PipelineStage CRUD
# ---------------------------------------------------------------------------

def get_stages_for_template(template_id: int) -> List[PipelineStage]:
    db = SessionLocal()
    try:
        return (
            db.query(PipelineStage)
            .filter(PipelineStage.template_id == template_id)
            .order_by(PipelineStage.position)
            .all()
        )
    finally:
        db.close()


def get_stage_by_key(template_id: int, stage_key: str) -> Optional[PipelineStage]:
    db = SessionLocal()
    try:
        return (
            db.query(PipelineStage)
            .filter(PipelineStage.template_id == template_id, PipelineStage.stage_key == stage_key)
            .first()
        )
    finally:
        db.close()


def get_stage_by_id(stage_id: int) -> Optional[PipelineStage]:
    db = SessionLocal()
    try:
        return db.query(PipelineStage).filter(PipelineStage.id == stage_id).first()
    finally:
        db.close()


def create_stage(
    template_id: int,
    stage_key: str,
    label: str,
    agent_type: str,
    position: int,
    group_id: Optional[int] = None,
    config: Optional[Dict] = None,
    color: Optional[str] = None,
) -> Optional[PipelineStage]:
    db = SessionLocal()
    try:
        s = PipelineStage(
            template_id=template_id,
            stage_key=stage_key,
            label=label,
            agent_type=agent_type,
            position=position,
            group_id=group_id,
            config=config,
            color=color,
        )
        db.add(s)
        db.commit()
        db.refresh(s)
        return s
    except Exception:
        db.rollback()
        logger.exception("create_stage failed for template=%d key=%r", template_id, stage_key)
        return None
    finally:
        db.close()


def update_stage(
    stage_id: int,
    label: Optional[str] = None,
    agent_type: Optional[str] = None,
    position: Optional[int] = None,
    group_id: Optional[int] = ...,  # type: ignore[assignment]
    config: Optional[Dict] = ...,   # type: ignore[assignment]
    color: Optional[str] = ...,     # type: ignore[assignment]
) -> Optional[PipelineStage]:
    db = SessionLocal()
    try:
        s = db.query(PipelineStage).filter(PipelineStage.id == stage_id).first()
        if not s:
            return None
        if label is not None:
            s.label = label
        if agent_type is not None:
            s.agent_type = agent_type
        if position is not None:
            s.position = position
        if group_id is not ...:
            s.group_id = group_id
        if config is not ...:
            s.config = config
        if color is not ...:
            s.color = color
        db.commit()
        db.refresh(s)
        return s
    except Exception:
        db.rollback()
        logger.exception("update_stage failed for id=%d", stage_id)
        return None
    finally:
        db.close()


def delete_stage(stage_id: int) -> Dict[str, Any]:
    """Delete a stage.  Returns {ok, task_count} — callers must check task_count == 0
    or use delete_stage_with_redirect when tasks exist."""
    from .session import SessionLocal as _SL
    db = _SL()
    try:
        s = db.query(PipelineStage).filter(PipelineStage.id == stage_id).first()
        if not s:
            return {"ok": False, "error": "stage_not_found"}

        # Check for tasks assigned to this stage
        from .models import Task
        task_count = db.query(Task).filter(Task.stage_key == s.stage_key, Task.is_active == True).count()
        if task_count:
            return {"ok": False, "error": "tasks_assigned", "task_count": task_count}

        # Remove incoming + outgoing transitions
        db.query(PipelineTransition).filter(
            (PipelineTransition.from_stage_id == stage_id) |
            (PipelineTransition.to_stage_id == stage_id)
        ).delete(synchronize_session=False)

        db.delete(s)
        db.commit()
        return {"ok": True, "task_count": 0}
    except Exception:
        db.rollback()
        logger.exception("delete_stage failed for id=%d", stage_id)
        return {"ok": False, "error": "internal_error"}
    finally:
        db.close()


def delete_stage_with_redirect(stage_id: int, redirect_stage_key: str) -> Dict[str, Any]:
    """Migrate tasks from a stage to redirect_stage_key, then delete the stage."""
    from .session import SessionLocal as _SL
    db = _SL()
    try:
        s = db.query(PipelineStage).filter(PipelineStage.id == stage_id).first()
        if not s:
            return {"ok": False, "error": "stage_not_found"}

        redirect = (
            db.query(PipelineStage)
            .filter(PipelineStage.template_id == s.template_id, PipelineStage.stage_key == redirect_stage_key)
            .first()
        )
        if not redirect:
            return {"ok": False, "error": "redirect_stage_not_found"}

        from .models import Task
        tasks = db.query(Task).filter(Task.stage_key == s.stage_key, Task.is_active == True).all()
        for task in tasks:
            task.stage_key = redirect_stage_key
            task.type = redirect_stage_key

        db.query(PipelineTransition).filter(
            (PipelineTransition.from_stage_id == stage_id) |
            (PipelineTransition.to_stage_id == stage_id)
        ).delete(synchronize_session=False)

        db.delete(s)
        db.commit()
        return {"ok": True, "migrated_tasks": len(tasks)}
    except Exception:
        db.rollback()
        logger.exception("delete_stage_with_redirect failed for id=%d", stage_id)
        return {"ok": False, "error": "internal_error"}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# PipelineTransition CRUD
# ---------------------------------------------------------------------------

def get_transitions_for_template(template_id: int) -> List[PipelineTransition]:
    db = SessionLocal()
    try:
        return (
            db.query(PipelineTransition)
            .filter(PipelineTransition.template_id == template_id)
            .all()
        )
    finally:
        db.close()


def get_transition_by_id(transition_id: int) -> Optional[PipelineTransition]:
    db = SessionLocal()
    try:
        return db.query(PipelineTransition).filter(PipelineTransition.id == transition_id).first()
    finally:
        db.close()


def create_transition(
    template_id: int,
    from_stage_id: int,
    to_stage_id: int,
    condition: str,
    priority: int = 0,
) -> Optional[PipelineTransition]:
    if condition not in _VALID_CONDITIONS:
        raise ValueError(f"condition must be one of {_VALID_CONDITIONS}")
    db = SessionLocal()
    try:
        tr = PipelineTransition(
            template_id=template_id,
            from_stage_id=from_stage_id,
            to_stage_id=to_stage_id,
            condition=condition,
            priority=priority,
        )
        db.add(tr)
        db.commit()
        db.refresh(tr)
        return tr
    except Exception:
        db.rollback()
        logger.exception("create_transition failed")
        return None
    finally:
        db.close()


def update_transition(
    transition_id: int,
    condition: Optional[str] = None,
    priority: Optional[int] = None,
) -> Optional[PipelineTransition]:
    if condition is not None and condition not in _VALID_CONDITIONS:
        raise ValueError(f"condition must be one of {_VALID_CONDITIONS}")
    db = SessionLocal()
    try:
        tr = db.query(PipelineTransition).filter(PipelineTransition.id == transition_id).first()
        if not tr:
            return None
        if condition is not None:
            tr.condition = condition
        if priority is not None:
            tr.priority = priority
        db.commit()
        db.refresh(tr)
        return tr
    except Exception:
        db.rollback()
        logger.exception("update_transition failed for id=%d", transition_id)
        return None
    finally:
        db.close()


def delete_transition(transition_id: int) -> bool:
    db = SessionLocal()
    try:
        tr = db.query(PipelineTransition).filter(PipelineTransition.id == transition_id).first()
        if not tr:
            return False
        db.delete(tr)
        db.commit()
        return True
    except Exception:
        db.rollback()
        logger.exception("delete_transition failed for id=%d", transition_id)
        return False
    finally:
        db.close()


# ---------------------------------------------------------------------------
# PipelineStageGroup CRUD
# ---------------------------------------------------------------------------

def get_stage_groups_for_template(template_id: int) -> List[PipelineStageGroup]:
    db = SessionLocal()
    try:
        return (
            db.query(PipelineStageGroup)
            .filter(PipelineStageGroup.template_id == template_id)
            .order_by(PipelineStageGroup.position)
            .all()
        )
    finally:
        db.close()


def get_group_by_id(group_id: int) -> Optional[PipelineStageGroup]:
    db = SessionLocal()
    try:
        return db.query(PipelineStageGroup).filter(PipelineStageGroup.id == group_id).first()
    finally:
        db.close()


def create_stage_group(
    template_id: int,
    name: str,
    position: int,
    color: Optional[str] = None,
) -> Optional[PipelineStageGroup]:
    db = SessionLocal()
    try:
        g = PipelineStageGroup(template_id=template_id, name=name, position=position, color=color)
        db.add(g)
        db.commit()
        db.refresh(g)
        return g
    except Exception:
        db.rollback()
        logger.exception("create_stage_group failed")
        return None
    finally:
        db.close()


def update_stage_group(
    group_id: int,
    name: Optional[str] = None,
    color: Optional[str] = ...,  # type: ignore[assignment]
    position: Optional[int] = None,
) -> Optional[PipelineStageGroup]:
    db = SessionLocal()
    try:
        g = db.query(PipelineStageGroup).filter(PipelineStageGroup.id == group_id).first()
        if not g:
            return None
        if name is not None:
            g.name = name
        if color is not ...:
            g.color = color
        if position is not None:
            g.position = position
        db.commit()
        db.refresh(g)
        return g
    except Exception:
        db.rollback()
        logger.exception("update_stage_group failed for id=%d", group_id)
        return None
    finally:
        db.close()


def delete_stage_group(group_id: int) -> bool:
    """Dissolve group — stages become ungrouped (group_id set to NULL)."""
    db = SessionLocal()
    try:
        g = db.query(PipelineStageGroup).filter(PipelineStageGroup.id == group_id).first()
        if not g:
            return False
        # Unlink stages
        db.query(PipelineStage).filter(PipelineStage.group_id == group_id).update(
            {"group_id": None}, synchronize_session=False
        )
        db.delete(g)
        db.commit()
        return True
    except Exception:
        db.rollback()
        logger.exception("delete_stage_group failed for id=%d", group_id)
        return False
    finally:
        db.close()


# ---------------------------------------------------------------------------
# PipelineArchCategory CRUD
# ---------------------------------------------------------------------------

def get_arch_categories_for_template(template_id: int) -> List[PipelineArchCategory]:
    db = SessionLocal()
    try:
        return (
            db.query(PipelineArchCategory)
            .filter(PipelineArchCategory.template_id == template_id)
            .order_by(PipelineArchCategory.position)
            .all()
        )
    finally:
        db.close()


def get_arch_category_by_id(cat_id: int) -> Optional[PipelineArchCategory]:
    db = SessionLocal()
    try:
        return db.query(PipelineArchCategory).filter(PipelineArchCategory.id == cat_id).first()
    finally:
        db.close()


def create_arch_category(
    template_id: int,
    key: str,
    label: str,
    position: int,
    color: Optional[str] = None,
) -> Optional[PipelineArchCategory]:
    db = SessionLocal()
    try:
        c = PipelineArchCategory(template_id=template_id, key=key, label=label, position=position, color=color)
        db.add(c)
        db.commit()
        db.refresh(c)
        return c
    except Exception:
        db.rollback()
        logger.exception("create_arch_category failed for key=%r", key)
        return None
    finally:
        db.close()


def update_arch_category(
    cat_id: int,
    key: Optional[str] = None,
    label: Optional[str] = None,
    color: Optional[str] = ...,  # type: ignore[assignment]
    position: Optional[int] = None,
) -> Optional[PipelineArchCategory]:
    db = SessionLocal()
    try:
        c = db.query(PipelineArchCategory).filter(PipelineArchCategory.id == cat_id).first()
        if not c:
            return None
        if key is not None:
            c.key = key
        if label is not None:
            c.label = label
        if color is not ...:
            c.color = color
        if position is not None:
            c.position = position
        db.commit()
        db.refresh(c)
        return c
    except Exception:
        db.rollback()
        logger.exception("update_arch_category failed for id=%d", cat_id)
        return None
    finally:
        db.close()


def delete_arch_category(cat_id: int) -> bool:
    db = SessionLocal()
    try:
        c = db.query(PipelineArchCategory).filter(PipelineArchCategory.id == cat_id).first()
        if not c:
            return False
        db.delete(c)
        db.commit()
        return True
    except Exception:
        db.rollback()
        logger.exception("delete_arch_category failed for id=%d", cat_id)
        return False
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Clone
# ---------------------------------------------------------------------------

def clone_template(template_id: int, new_name: str) -> Optional[PipelineTemplate]:
    """Deep-copy a template under a new name.  The clone is never builtin."""
    db = SessionLocal()
    try:
        source = db.query(PipelineTemplate).filter(PipelineTemplate.id == template_id).first()
        if not source:
            return None
        if db.query(PipelineTemplate).filter(PipelineTemplate.name == new_name).first():
            return None  # name collision

        t = PipelineTemplate(
            name=new_name,
            description=source.description,
            is_default=False,
            is_builtin=False,
            version=1,
        )
        db.add(t)
        db.flush()

        # Clone groups (old_id -> new_id)
        group_id_map: Dict[int, int] = {}
        for g in db.query(PipelineStageGroup).filter(PipelineStageGroup.template_id == template_id).all():
            ng = PipelineStageGroup(template_id=t.id, name=g.name, color=g.color, position=g.position)
            db.add(ng)
            db.flush()
            group_id_map[g.id] = ng.id

        # Clone stages (old_id -> new_id)
        stage_id_map: Dict[int, int] = {}
        for s in (
            db.query(PipelineStage)
            .filter(PipelineStage.template_id == template_id)
            .order_by(PipelineStage.position)
            .all()
        ):
            ns = PipelineStage(
                template_id=t.id,
                stage_key=s.stage_key,
                label=s.label,
                agent_type=s.agent_type,
                position=s.position,
                group_id=group_id_map.get(s.group_id) if s.group_id else None,
                config=s.config,
                color=s.color,
            )
            db.add(ns)
            db.flush()
            stage_id_map[s.id] = ns.id

        # Clone transitions
        for tr in db.query(PipelineTransition).filter(PipelineTransition.template_id == template_id).all():
            fi = stage_id_map.get(tr.from_stage_id)
            ti = stage_id_map.get(tr.to_stage_id)
            if fi and ti:
                db.add(PipelineTransition(
                    template_id=t.id, from_stage_id=fi, to_stage_id=ti,
                    condition=tr.condition, priority=tr.priority,
                ))

        # Clone arch categories
        for ac in (
            db.query(PipelineArchCategory)
            .filter(PipelineArchCategory.template_id == template_id)
            .order_by(PipelineArchCategory.position)
            .all()
        ):
            db.add(PipelineArchCategory(
                template_id=t.id, key=ac.key, label=ac.label,
                color=ac.color, position=ac.position,
            ))

        db.commit()
        db.refresh(t)
        return t
    except Exception:
        db.rollback()
        logger.exception("clone_template failed for id=%d new_name=%r", template_id, new_name)
        return None
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Stage-map & Card Transfer
# ---------------------------------------------------------------------------

def compute_stage_map(from_template_id: int, to_template_id: int) -> Dict[str, Any]:
    """Compare two templates and return a stage-mapping descriptor.

    Returns:
        {
          "auto_map":    {from_stage_key: to_stage_key, ...},   # keys present in both
          "unmapped":    [from_stage_key, ...],                  # keys only in source
          "dest_stages": [{"stage_key": ..., "label": ...}, ...]  # all dest stages
        }
    """
    db = SessionLocal()
    try:
        src_stages = (
            db.query(PipelineStage)
            .filter(PipelineStage.template_id == from_template_id)
            .order_by(PipelineStage.position)
            .all()
        )
        dst_stages = (
            db.query(PipelineStage)
            .filter(PipelineStage.template_id == to_template_id)
            .order_by(PipelineStage.position)
            .all()
        )
        dst_keys = {s.stage_key for s in dst_stages}
        auto_map: Dict[str, str] = {}
        unmapped: List[str] = []
        for s in src_stages:
            if s.stage_key in dst_keys:
                auto_map[s.stage_key] = s.stage_key
            else:
                unmapped.append(s.stage_key)
        dest_stages_list = [{"stage_key": s.stage_key, "label": s.label} for s in dst_stages]
        return {"auto_map": auto_map, "unmapped": unmapped, "dest_stages": dest_stages_list}
    except Exception:
        logger.exception("compute_stage_map failed from=%d to=%d", from_template_id, to_template_id)
        return {"auto_map": {}, "unmapped": [], "dest_stages": []}
    finally:
        db.close()


def transfer_cards(
    from_template_id: int,
    to_template_id: int,
    stage_map: Dict[str, str],
    project_id: "int | None" = None,
) -> int:
    """Move tasks from one pipeline template to another using the provided stage map.

    Only moves tasks whose stage_key appears as a key in ``stage_map`` (skipped
    stages are left untouched).  Updates both ``pipeline_template_id`` and
    ``stage_key``/``type`` on each task.

    Returns the number of tasks updated.
    """
    from .models import Task
    db = SessionLocal()
    try:
        q = db.query(Task).filter(
            Task.pipeline_template_id == from_template_id,
            Task.is_active == True,
        )
        if project_id is not None:
            q = q.filter(Task.project_id == project_id)
        tasks = q.all()
        count = 0
        for task in tasks:
            src_key = task.stage_key or task.type
            dest_key = stage_map.get(src_key)
            if dest_key is None:
                continue  # stage not in map — skip
            task.pipeline_template_id = to_template_id
            task.stage_key = dest_key
            task.type = dest_key  # keep in sync during phase-out period
            count += 1
        db.commit()
        return count
    except Exception:
        db.rollback()
        logger.exception("transfer_cards failed from=%d to=%d", from_template_id, to_template_id)
        return 0
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Serialization Helpers
# ---------------------------------------------------------------------------

def template_to_dict(template: PipelineTemplate) -> Dict[str, Any]:
    if not template:
        return {}
    stages = get_stages_for_template(template.id)
    transitions = get_transitions_for_template(template.id)
    groups = get_stage_groups_for_template(template.id)
    arch_cats = get_arch_categories_for_template(template.id)
    return {
        "id": template.id,
        "name": template.name,
        "description": template.description,
        "is_default": template.is_default,
        "is_builtin": template.is_builtin,
        "version": template.version,
        "stages": [
            {
                "id": s.id,
                "stage_key": s.stage_key,
                "label": s.label,
                "agent_type": s.agent_type,
                "position": s.position,
                "group_id": s.group_id,
                "config": s.config,
                "color": s.color,
            }
            for s in stages
        ],
        "transitions": [
            {
                "id": t.id,
                "from_stage_id": t.from_stage_id,
                "to_stage_id": t.to_stage_id,
                # Resolve stage keys so clients don't need a secondary lookup
                "from_stage_key": next(
                    (s.stage_key for s in stages if s.id == t.from_stage_id), None
                ),
                "to_stage_key": next(
                    (s.stage_key for s in stages if s.id == t.to_stage_id), None
                ),
                "condition": t.condition,
                "priority": t.priority,
            }
            for t in transitions
        ],
        "groups": [
            {"id": g.id, "name": g.name, "color": g.color, "position": g.position}
            for g in groups
        ],
        "arch_categories": [
            {"id": ac.id, "key": ac.key, "label": ac.label, "color": ac.color, "position": ac.position}
            for ac in arch_cats
        ],
    }


# ---------------------------------------------------------------------------
# Export / Import
# ---------------------------------------------------------------------------

def export_template(template_id: int) -> Optional[Dict[str, Any]]:
    """Serialise a template to a portable JSON blob (schema_version=1)."""
    t = get_template(template_id)
    if not t:
        return None

    stages = get_stages_for_template(template_id)
    transitions = get_transitions_for_template(template_id)
    groups = get_stage_groups_for_template(template_id)
    arch_cats = get_arch_categories_for_template(template_id)

    # Build a lookup: stage_id → stage_key (for transition serialisation)
    id_to_key: Dict[int, str] = {s.id: s.stage_key for s in stages}
    # Group id → group name
    id_to_group: Dict[int, str] = {g.id: g.name for g in groups}

    return {
        "schema_version": 1,
        "name": t.name,
        "description": t.description,
        "arch_categories": [
            {"key": ac.key, "label": ac.label, "color": ac.color, "position": ac.position}
            for ac in arch_cats
        ],
        "groups": [
            {"name": g.name, "color": g.color, "position": g.position}
            for g in groups
        ],
        "stages": [
            {
                "stage_key": s.stage_key,
                "label": s.label,
                "agent_type": s.agent_type,
                "position": s.position,
                "group": id_to_group.get(s.group_id) if s.group_id else None,
                "color": s.color,
                "config": s.config,
            }
            for s in stages
        ],
        "transitions": [
            {
                "from": id_to_key.get(tr.from_stage_id, ""),
                "to": id_to_key.get(tr.to_stage_id, ""),
                "condition": tr.condition,
                "priority": tr.priority,
            }
            for tr in transitions
        ],
    }


def import_template(blob: Dict[str, Any]) -> Optional[PipelineTemplate]:
    """Create a new template from an export blob.  Never overwrites existing templates."""
    if blob.get("schema_version") != 1:
        raise ValueError("unsupported schema_version")

    base_name = blob.get("name") or "Imported Pipeline"
    db = SessionLocal()
    try:
        # Find a unique name
        name = base_name
        suffix = 2
        while db.query(PipelineTemplate).filter(PipelineTemplate.name == name).first():
            name = f"{base_name} ({suffix})"
            suffix += 1

        t = PipelineTemplate(
            name=name,
            description=blob.get("description"),
            is_default=False,
            is_builtin=False,
            version=1,
        )
        db.add(t)
        db.flush()  # get t.id

        # Groups (name → id)
        group_name_to_id: Dict[str, int] = {}
        for g_data in (blob.get("groups") or []):
            g = PipelineStageGroup(
                template_id=t.id,
                name=g_data["name"],
                color=g_data.get("color"),
                position=g_data.get("position", 0),
            )
            db.add(g)
            db.flush()
            group_name_to_id[g_data["name"]] = g.id

        # Stages (stage_key → id)
        key_to_id: Dict[str, int] = {}
        for s_data in (blob.get("stages") or []):
            grp_id = group_name_to_id.get(s_data.get("group")) if s_data.get("group") else None
            s = PipelineStage(
                template_id=t.id,
                stage_key=s_data["stage_key"],
                label=s_data["label"],
                agent_type=s_data["agent_type"],
                position=s_data.get("position", 0),
                group_id=grp_id,
                color=s_data.get("color"),
                config=s_data.get("config"),
            )
            db.add(s)
            db.flush()
            key_to_id[s_data["stage_key"]] = s.id

        # Transitions
        for tr_data in (blob.get("transitions") or []):
            from_id = key_to_id.get(tr_data.get("from", ""))
            to_id = key_to_id.get(tr_data.get("to", ""))
            cond = tr_data.get("condition", "pass")
            if from_id and to_id and cond in _VALID_CONDITIONS:
                db.add(PipelineTransition(
                    template_id=t.id,
                    from_stage_id=from_id,
                    to_stage_id=to_id,
                    condition=cond,
                    priority=tr_data.get("priority", 0),
                ))

        # Arch categories
        for ac_data in (blob.get("arch_categories") or []):
            db.add(PipelineArchCategory(
                template_id=t.id,
                key=ac_data["key"],
                label=ac_data["label"],
                color=ac_data.get("color"),
                position=ac_data.get("position", 0),
            ))

        db.commit()
        db.refresh(t)
        return t
    except Exception:
        db.rollback()
        logger.exception("import_template failed")
        return None
    finally:
        db.close()


# ---------------------------------------------------------------------------
# CustomAgentDefinition CRUD
# ---------------------------------------------------------------------------

def get_all_custom_agent_definitions() -> List[CustomAgentDefinition]:
    db = SessionLocal()
    try:
        return db.query(CustomAgentDefinition).order_by(CustomAgentDefinition.name).all()
    finally:
        db.close()


def get_custom_agent_definition_by_id(defn_id: int) -> Optional[CustomAgentDefinition]:
    db = SessionLocal()
    try:
        return db.query(CustomAgentDefinition).filter(CustomAgentDefinition.id == defn_id).first()
    finally:
        db.close()


def get_custom_agent_definition_by_name(name: str) -> Optional[CustomAgentDefinition]:
    db = SessionLocal()
    try:
        return db.query(CustomAgentDefinition).filter(CustomAgentDefinition.name == name).first()
    finally:
        db.close()


def create_custom_agent_definition(
    name: str,
    display_name: str,
    description: str = "",
    intent: str = "",
    system_prompt: str = "",
    allowed_tools: Optional[List[str]] = None,
    gate_type: str = "llm_judge",
    verifier: str = "none",
    verifier_cmd: Optional[str] = None,
    max_turns: Optional[int] = None,
    max_tokens: Optional[int] = None,
    user_prompt_template: Optional[str] = None,
    behavior_type: Optional[str] = None,
    behavior_config: Optional[Dict[str, Any]] = None,
    is_builtin: bool = False,
) -> Optional[CustomAgentDefinition]:
    db = SessionLocal()
    try:
        defn = CustomAgentDefinition(
            name=name,
            display_name=display_name,
            description=description,
            intent=intent,
            system_prompt=system_prompt,
            allowed_tools=allowed_tools or [],
            gate_type=gate_type,
            verifier=verifier,
            verifier_cmd=verifier_cmd,
            max_turns=max_turns,
            max_tokens=max_tokens,
            user_prompt_template=user_prompt_template,
            behavior_type=behavior_type,
            behavior_config=behavior_config,
            is_builtin=is_builtin,
        )
        db.add(defn)
        db.commit()
        db.refresh(defn)
        return defn
    except Exception:
        db.rollback()
        logger.exception("create_custom_agent_definition failed for name=%r", name)
        return None
    finally:
        db.close()


_SENTINEL = object()


def update_custom_agent_definition(
    defn_id: int,
    name=_SENTINEL,
    display_name=_SENTINEL,
    description=_SENTINEL,
    intent=_SENTINEL,
    system_prompt=_SENTINEL,
    allowed_tools=_SENTINEL,
    gate_type=_SENTINEL,
    verifier=_SENTINEL,
    verifier_cmd=_SENTINEL,
    max_turns=_SENTINEL,
    max_tokens=_SENTINEL,
    user_prompt_template=_SENTINEL,
    behavior_type=_SENTINEL,
    behavior_config=_SENTINEL,
) -> Optional[CustomAgentDefinition]:
    db = SessionLocal()
    try:
        defn = db.query(CustomAgentDefinition).filter(CustomAgentDefinition.id == defn_id).first()
        if not defn:
            return None
        if getattr(defn, "is_builtin", False):
            return None  # built-in definitions are immutable; caller should clone first
        if name is not _SENTINEL:
            defn.name = name
        if display_name is not _SENTINEL:
            defn.display_name = display_name
        if description is not _SENTINEL:
            defn.description = description
        if intent is not _SENTINEL:
            defn.intent = intent
        if system_prompt is not _SENTINEL:
            defn.system_prompt = system_prompt
        if allowed_tools is not _SENTINEL:
            defn.allowed_tools = allowed_tools
        if gate_type is not _SENTINEL:
            defn.gate_type = gate_type
        if verifier is not _SENTINEL:
            defn.verifier = verifier
        if verifier_cmd is not _SENTINEL:
            defn.verifier_cmd = verifier_cmd
        if max_turns is not _SENTINEL:
            defn.max_turns = max_turns
        if max_tokens is not _SENTINEL:
            defn.max_tokens = max_tokens
        if user_prompt_template is not _SENTINEL:
            defn.user_prompt_template = user_prompt_template
        if behavior_type is not _SENTINEL:
            defn.behavior_type = behavior_type
        if behavior_config is not _SENTINEL:
            defn.behavior_config = behavior_config
        db.commit()
        db.refresh(defn)
        return defn
    except Exception:
        db.rollback()
        logger.exception("update_custom_agent_definition failed for id=%d", defn_id)
        return None
    finally:
        db.close()


def delete_custom_agent_definition(defn_id: int) -> Dict[str, Any]:
    """
    Delete a custom agent definition.

    Blocked if the definition is a built-in, or if any pipeline_stages row
    references this definition's name as agent_type.
    Returns {"ok": True} or {"ok": False, "error": "...", "stage_count": N}.
    """
    db = SessionLocal()
    try:
        defn = db.query(CustomAgentDefinition).filter(CustomAgentDefinition.id == defn_id).first()
        if not defn:
            return {"ok": False, "error": "not found"}
        if getattr(defn, "is_builtin", False):
            return {"ok": False, "error": "built-in definitions cannot be deleted"}
        # Check for pipeline stages that use this agent_type
        stage_count = (
            db.query(PipelineStage)
            .filter(PipelineStage.agent_type == defn.name)
            .count()
        )
        if stage_count > 0:
            return {
                "ok": False,
                "error": f"used by {stage_count} pipeline stage(s)",
                "stage_count": stage_count,
            }
        db.delete(defn)
        db.commit()
        return {"ok": True}
    except Exception:
        db.rollback()
        logger.exception("delete_custom_agent_definition failed for id=%d", defn_id)
        return {"ok": False, "error": "internal error"}
    finally:
        db.close()


def clone_custom_agent_definition(defn_id: int) -> Optional[CustomAgentDefinition]:
    """
    Clone a custom agent definition (including built-ins).

    The clone gets is_builtin=False, a unique name prefixed with 'copy-of-',
    and a display name prefixed with 'Copy of '.  All other fields are copied verbatim.
    """
    db = SessionLocal()
    try:
        src = db.query(CustomAgentDefinition).filter(CustomAgentDefinition.id == defn_id).first()
        if not src:
            return None

        base_name = f"copy-of-{src.name}"
        name = base_name
        suffix = 2
        while db.query(CustomAgentDefinition).filter(CustomAgentDefinition.name == name).first():
            name = f"{base_name}-{suffix}"
            suffix += 1

        clone = CustomAgentDefinition(
            name=name,
            display_name=f"Copy of {src.display_name}",
            description=src.description,
            intent=src.intent,
            system_prompt=src.system_prompt,
            allowed_tools=list(src.allowed_tools or []),
            gate_type=src.gate_type,
            verifier=src.verifier,
            verifier_cmd=src.verifier_cmd,
            max_turns=src.max_turns,
            max_tokens=src.max_tokens,
            user_prompt_template=src.user_prompt_template,
            behavior_type=src.behavior_type,
            behavior_config=dict(src.behavior_config) if src.behavior_config else None,
            is_builtin=False,
        )
        db.add(clone)
        db.commit()
        db.refresh(clone)
        return clone
    except Exception:
        db.rollback()
        logger.exception("clone_custom_agent_definition failed for id=%d", defn_id)
        return None
    finally:
        db.close()


def custom_agent_definition_to_dict(defn: CustomAgentDefinition) -> Dict[str, Any]:
    return {
        "id": defn.id,
        "name": defn.name,
        "display_name": defn.display_name,
        "description": defn.description,
        "intent": defn.intent,
        "system_prompt": defn.system_prompt,
        "allowed_tools": defn.allowed_tools or [],
        "gate_type": defn.gate_type,
        "verifier": defn.verifier,
        "verifier_cmd": defn.verifier_cmd,
        "max_turns": defn.max_turns,
        "max_tokens": defn.max_tokens,
        "user_prompt_template": defn.user_prompt_template,
        "behavior_type": getattr(defn, "behavior_type", None),
        "behavior_config": getattr(defn, "behavior_config", None) or {},
        "is_builtin": bool(getattr(defn, "is_builtin", False)),
        "created_at": defn.created_at.isoformat() if defn.created_at else None,
    }


def load_custom_agents_into_registry() -> int:
    """
    Load all custom_agent_definitions rows and register them as AgentSpec entries
    in AGENT_REGISTRY.  Called at server startup.

    Returns the number of definitions loaded.
    """
    from app.agent.agent_registry import AGENT_REGISTRY, AgentSpec
    from app.agent.custom_llm_agent import CustomLLMAgent

    defns = get_all_custom_agent_definitions()
    for defn in defns:
        AGENT_REGISTRY[defn.name] = AgentSpec(
            cls=CustomLLMAgent,
            display_name=defn.display_name,
            description=defn.description or "",
            default_tools=list(defn.allowed_tools or []),
            gate_type=defn.gate_type or "llm_judge",
        )
    return len(defns)
