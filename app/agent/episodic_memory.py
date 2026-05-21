"""
Episodic memory — semantic store of past agent attempts, failures, and conclusions.

Storage: pgvector HNSW index on the episodic_memory table.
Embeddings: OpenAI-compatible /embeddings endpoint via an LLM record (embedding_llm_id).
Relevance:  cosine_similarity × recency_decay (exponential, half-life configurable).
Keepalive:  each retrieval extends expires_at; episodes that are never retrieved expire.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

import httpx
from sqlalchemy import text

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

_EXPIRES_YEARS = 5
_EMBED_TIMEOUT = 30.0  # seconds


# ---------------------------------------------------------------------------
# Embedding
# ---------------------------------------------------------------------------

def embed_text(text: str, llm_id: int, db) -> list[float]:
    """
    Call the /embeddings endpoint of the LLM record identified by llm_id.
    Returns a float list whose length equals the model's output dimension.
    Raises ValueError if the LLM record is missing or the call fails.
    """
    from app.database import get_llm as _get_llm

    llm = _get_llm(llm_id)
    if not llm:
        raise ValueError(f"embed_text: LLM record {llm_id} not found")

    base_url = (llm.base_url or "").rstrip("/")
    model = llm.model or ""

    try:
        resp = httpx.post(
            f"{base_url}/embeddings",
            json={"input": text, "model": model},
            timeout=_EMBED_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["data"][0]["embedding"]
    except Exception as exc:
        raise ValueError(f"embed_text: embedding call failed — {exc}") from exc


# ---------------------------------------------------------------------------
# Write path
# ---------------------------------------------------------------------------

def insert_episode(
    project_id: int,
    task_id: "str | None",
    episode_type: str,
    content: str,
    metadata: dict,
    settings,
) -> "int | None":
    """
    Embeds *content* and inserts one row into episodic_memory.
    Returns the new episode id, or None when embedding is unavailable (non-fatal).

    settings must expose:
        EPISODIC_MEMORY_ENABLED          bool
        EPISODIC_MEMORY_EMBEDDING_LLM_ID int | None
    """
    if not getattr(settings, "EPISODIC_MEMORY_ENABLED", False):
        return None

    llm_id = getattr(settings, "EPISODIC_MEMORY_EMBEDDING_LLM_ID", None)
    if not llm_id:
        logger.debug("[episodic_memory] embedding_llm_id not configured; skipping episode insert")
        return None

    try:
        embedding = embed_text(content, llm_id, db=None)
    except ValueError as exc:
        logger.warning("[episodic_memory] embed_text failed: %s", exc)
        return None

    embedding_str = "[" + ",".join(str(f) for f in embedding) + "]"
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=_EXPIRES_YEARS * 365)

    from app.database.session import SessionLocal
    db = SessionLocal()
    try:
        result = db.execute(
            text("""
            INSERT INTO episodic_memory
                (project_id, task_id, episode_type, content, embedding, metadata, created_at, expires_at)
            VALUES
                (:project_id, :task_id, :episode_type, :content,
                 CAST(:embedding AS vector), CAST(:metadata AS jsonb),
                 :created_at, :expires_at)
            RETURNING id
            """),
            {
                "project_id": project_id,
                "task_id": task_id,
                "episode_type": episode_type,
                "content": content,
                "embedding": embedding_str,
                "metadata": __import__("json").dumps(metadata),
                "created_at": now,
                "expires_at": expires_at,
            },
        )
        episode_id = result.fetchone()[0]
        db.commit()
        logger.debug(
            "[episodic_memory] inserted episode %d (type=%s, project=%d)",
            episode_id, episode_type, project_id,
        )
        return episode_id
    except Exception as exc:
        db.rollback()
        logger.warning("[episodic_memory] insert_episode DB error: %s", exc)
        return None
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Read path
# ---------------------------------------------------------------------------

def query_episodes(
    project_id: int,
    question: str,
    k: int,
    settings,
    episode_type: "str | None" = None,
) -> list[dict]:
    """
    Returns the top-k episodes by relevance_score = cosine_similarity × recency_weight.

    Strategy: fetch top-20 nearest neighbours by cosine distance from the HNSW index,
    then re-rank in Python using recency decay.  Updates expires_at for returned rows.

    Returns [] when episodic memory is disabled or embedding fails.
    """
    if not getattr(settings, "EPISODIC_MEMORY_ENABLED", False):
        return []

    llm_id = getattr(settings, "EPISODIC_MEMORY_EMBEDDING_LLM_ID", None)
    if not llm_id:
        return []

    k = max(1, min(k, 20))

    try:
        embedding = embed_text(question, llm_id, db=None)
    except ValueError as exc:
        logger.warning("[episodic_memory] query embed_text failed: %s", exc)
        return []

    embedding_str = "[" + ",".join(str(f) for f in embedding) + "]"
    half_life = getattr(settings, "EPISODIC_MEMORY_DECAY_HALF_LIFE_DAYS", 90)

    from app.database.session import SessionLocal
    import json as _json

    db = SessionLocal()
    try:
        type_filter = episode_type if episode_type else None
        rows = db.execute(
            text("""
            SELECT id, episode_type, content, metadata, created_at, expires_at,
                   1 - (embedding <=> CAST(:emb AS vector)) AS cosine_sim
            FROM episodic_memory
            WHERE project_id = :project_id
              AND expires_at > now()
              AND (:etype IS NULL OR episode_type = :etype)
            ORDER BY embedding <=> CAST(:emb AS vector)
            LIMIT 20
            """),
            {"emb": embedding_str, "project_id": project_id, "etype": type_filter},
        ).fetchall()
    except Exception as exc:
        logger.warning("[episodic_memory] query_episodes DB error: %s", exc)
        db.close()
        return []

    now = datetime.now(timezone.utc)
    scored: list[tuple[float, dict]] = []
    for row in rows:
        cosine_sim = float(row[6]) if row[6] is not None else 0.0
        created_at = row[4]
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        weight = _recency_weight(created_at, half_life, now)
        scored.append((cosine_sim * weight, {
            "id": row[0],
            "episode_type": row[1],
            "content": row[2],
            "metadata": _json.loads(row[3]) if isinstance(row[3], str) else (row[3] or {}),
            "created_at": created_at,
            "expires_at": row[5],
            "cosine_sim": cosine_sim,
            "relevance_score": cosine_sim * weight,
        }))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = [ep for _, ep in scored[:k]]

    if top:
        _extend_keepalive([ep["id"] for ep in top], db, settings)

    db.close()
    return top


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _recency_weight(
    created_at: datetime,
    half_life_days: int,
    now: "datetime | None" = None,
) -> float:
    """Exponential decay: weight = 2^(-age_days / half_life_days)."""
    if now is None:
        now = datetime.now(timezone.utc)
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    age_days = (now - created_at).total_seconds() / 86400.0
    return 2.0 ** (-age_days / max(half_life_days, 1))


def _extend_keepalive(episode_ids: list[int], db, settings) -> None:
    """Extend expires_at by keepalive_extension_days for each returned episode."""
    if not episode_ids:
        return
    extension = getattr(settings, "EPISODIC_MEMORY_KEEPALIVE_EXTENSION_DAYS", 14)
    try:
        db.execute(
            text(
                f"UPDATE episodic_memory "
                f"SET expires_at = GREATEST(expires_at, now() + INTERVAL '{extension} days'), "
                f"last_accessed = now() "
                f"WHERE id = ANY(:ids)"
            ),
            {"ids": episode_ids},
        )
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.warning("[episodic_memory] _extend_keepalive error: %s", exc)
