"""
factory_sources — data source adapters for the Card Factory system (Phase 9).

Each adapter yields one dict per item.  Key names depend on source type and
are available as template variables in factory_card_template interpolation.
"""

from __future__ import annotations

import csv
import json
import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)


class DataSourceAdapter(ABC):
    @abstractmethod
    def items(self) -> Iterator[dict]:
        """Yield one dict per item."""


class FolderAdapter(DataSourceAdapter):
    def __init__(self, *, path: str, file_glob: str = "*", recursive: bool = False, **_):
        self.path = Path(path)
        self.file_glob = file_glob
        self.recursive = recursive

    def items(self) -> Iterator[dict]:
        if not self.path.is_dir():
            raise ValueError(f"Folder not found: {self.path}")
        glob_fn = self.path.rglob if self.recursive else self.path.glob
        for fp in sorted(glob_fn(self.file_glob)):
            if fp.is_file():
                yield {
                    "filepath": str(fp),
                    "filename": fp.name,
                    "stem": fp.stem,
                    "extension": fp.suffix,
                    "size_bytes": fp.stat().st_size,
                }


class FileListAdapter(DataSourceAdapter):
    def __init__(self, *, filepath: str, **_):
        self.filepath = Path(filepath)

    def items(self) -> Iterator[dict]:
        with open(self.filepath) as f:
            for line in f:
                line = line.strip()
                if line:
                    fp = Path(line)
                    yield {
                        "filepath": str(fp),
                        "filename": fp.name,
                        "stem": fp.stem,
                        "extension": fp.suffix,
                    }


class CSVAdapter(DataSourceAdapter):
    def __init__(self, *, filepath: str, **_):
        self.filepath = Path(filepath)

    def items(self) -> Iterator[dict]:
        with open(self.filepath, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                yield dict(row)


class JSONArrayAdapter(DataSourceAdapter):
    def __init__(self, *, filepath: str, **_):
        self.filepath = Path(filepath)

    def items(self) -> Iterator[dict]:
        with open(self.filepath, encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            raise ValueError(f"JSON file must contain a top-level array: {self.filepath}")
        for i, element in enumerate(data):
            if isinstance(element, dict):
                yield element
            else:
                yield {"value": element, "index": i}


class SQLiteQueryAdapter(DataSourceAdapter):
    """Reads from an *external* SQLite file as a data source — not the app DB."""
    def __init__(self, *, db_path: str, query: str, **_):
        self.db_path = db_path
        self.query = query

    def items(self) -> Iterator[dict]:
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            for row in conn.execute(self.query):
                yield dict(row)
        finally:
            conn.close()


class ManualPromptAdapter(DataSourceAdapter):
    """No external data source — yields the trigger card's content as a single item.

    LLM segmentation mode uses this to pass the card content to CardFactoryAgent.
    """
    def __init__(self, *, trigger_card_content: dict | None = None, **_):
        self.trigger_card_content = trigger_card_content or {}

    def items(self) -> Iterator[dict]:
        yield {"content": self.trigger_card_content}


class MaestroCardsAdapter(DataSourceAdapter):
    """Yields cards from a specified stage_key within the current project."""
    def __init__(self, *, project_name: str, stage_key: str, **_):
        self.project_name = project_name
        self.stage_key = stage_key

    def items(self) -> Iterator[dict]:
        from app.database.session import SessionLocal
        from app.database.models import Task, Project
        db = SessionLocal()
        try:
            project = db.query(Project).filter(Project.name == self.project_name).first()
            if not project:
                return
            tasks = (
                db.query(Task)
                .filter(Task.project_id == project.id, Task.is_active == True)
                .all()
            )
        finally:
            db.close()
        for t in tasks:
            sk = t.stage_key or t.type or ""
            if sk == self.stage_key:
                yield {
                    "task_id": t.id,
                    "title": t.title or "",
                    "description": t.description or "",
                    "stage_key": sk,
                }


ADAPTERS: dict[str, type[DataSourceAdapter]] = {
    "folder":        FolderAdapter,
    "file_list":     FileListAdapter,
    "csv":           CSVAdapter,
    "json_array":    JSONArrayAdapter,
    "sqlite_query":  SQLiteQueryAdapter,
    "manual_prompt": ManualPromptAdapter,
    "maestro_cards": MaestroCardsAdapter,
}


def build_adapter(source_type: str, source_config: dict) -> DataSourceAdapter:
    cls = ADAPTERS.get(source_type)
    if cls is None:
        raise ValueError(f"Unknown factory_source_type: {source_type!r}")
    return cls(**source_config)
