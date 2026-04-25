"""Query reformulation, vector retrieval, and answer synthesis."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

import anthropic
import numpy as np
from pydantic import BaseModel, ValidationError

from .config import Config
from .db import decode_embedding
from .embed import embed_query
from .prompts import REFORMULATOR_SYSTEM, SYNTHESIZER_SYSTEM


REFORMULATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "reformulations": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "phrasing": {"type": "string"},
                    "kind": {"type": "string", "enum": ["qa", "objection", "either"]},
                },
                "required": ["phrasing", "kind"],
            },
        }
    },
    "required": ["reformulations"],
}


class Reformulation(BaseModel):
    phrasing: str
    kind: str  # 'qa' | 'objection' | 'either'


class ReformulationOutput(BaseModel):
    reformulations: list[Reformulation]


@dataclass
class Clip:
    chunk_id: str
    video_id: str
    video_title: str
    video_form: str
    kind: str
    start_seconds: float
    end_seconds: float
    question: str
    answer: str
    speaker: str | None
    topics: list[str]
    confidence: float
    score: float
    matched_via: str  # the reformulation phrasing that matched best
    youtube_url: str


def reformulate(cfg: Config, user_query: str) -> list[Reformulation]:
    """Use the LLM to expand the user's input into 2-4 search variants."""
    client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
    response = client.messages.create(
        model=cfg.query_model,
        max_tokens=2000,
        system=[
            {
                "type": "text",
                "text": REFORMULATOR_SYSTEM,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        thinking={"type": "adaptive"},
        output_config={
            "format": {"type": "json_schema", "schema": REFORMULATION_SCHEMA},
            "effort": "low",
        },
        messages=[{"role": "user", "content": f"User input: {user_query!r}"}],
    )
    text = next((b.text for b in response.content if b.type == "text"), None)
    if not text:
        raise RuntimeError(
            f"reformulator returned no text content (stop_reason={response.stop_reason})"
        )
    try:
        parsed = ReformulationOutput.model_validate(json.loads(text))
    except (json.JSONDecodeError, ValidationError) as e:
        raise RuntimeError(f"reformulator returned invalid JSON: {e}\n\nRaw: {text[:500]}") from e
    return parsed.reformulations


def _load_chunk_matrix(
    conn: sqlite3.Connection,
    kind_filter: list[str] | None,
    form_filter: str | None = None,
) -> tuple[np.ndarray, list[sqlite3.Row]]:
    """Load all embedded chunks into a (N, D) matrix + the corresponding rows.

    `form_filter` restricts to videos with the given form (`long`, `short`); None means all.
    """
    sql = (
        "SELECT c.id AS chunk_id, c.video_id, c.kind, c.start_seconds, c.end_seconds, "
        "       c.question, c.answer, c.speaker, c.topics, c.confidence, c.embedding, "
        "       v.title AS video_title, v.form AS video_form "
        "FROM chunks c JOIN videos v ON v.id = c.video_id "
        "WHERE c.embedding IS NOT NULL"
    )
    params: list[Any] = []
    if kind_filter:
        placeholders = ",".join("?" * len(kind_filter))
        sql += f" AND c.kind IN ({placeholders})"
        params.extend(kind_filter)
    if form_filter and form_filter != "all":
        sql += " AND v.form = ?"
        params.append(form_filter)
    rows = conn.execute(sql, params).fetchall()
    if not rows:
        return np.zeros((0, 0), dtype=np.float32), []
    matrix = np.stack([decode_embedding(r["embedding"]) for r in rows])
    return matrix, rows


def _cosine_scores(query_vec: np.ndarray, matrix: np.ndarray) -> np.ndarray:
    qn = query_vec / (np.linalg.norm(query_vec) + 1e-9)
    mnorm = np.linalg.norm(matrix, axis=1, keepdims=True) + 1e-9
    mn = matrix / mnorm
    return mn @ qn


def retrieve(
    cfg: Config,
    conn: sqlite3.Connection,
    user_query: str,
    *,
    top_k: int = 5,
    confidence_floor: float = 0.5,
    form_filter: str = "all",
) -> tuple[list[Reformulation], list[Clip]]:
    """Reformulate, embed, search, and return top clips deduped by chunk_id."""
    reforms = reformulate(cfg, user_query)
    if not reforms:
        return reforms, []

    # Score each reformulation against the index, restricted to its preferred kind and form.
    best_score: dict[str, tuple[float, sqlite3.Row, str]] = {}
    for r in reforms:
        kind_filter = None if r.kind == "either" else [r.kind]
        matrix, rows = _load_chunk_matrix(conn, kind_filter, form_filter)
        if not rows:
            continue
        qvec = np.asarray(embed_query(cfg, r.phrasing), dtype=np.float32)
        scores = _cosine_scores(qvec, matrix)
        # Take top 20 from this reformulation, then merge across reforms.
        top_idx = np.argsort(-scores)[: max(top_k * 4, 20)]
        for i in top_idx:
            row = rows[i]
            score = float(scores[i])
            chunk_id = row["chunk_id"]
            prior = best_score.get(chunk_id)
            if prior is None or score > prior[0]:
                best_score[chunk_id] = (score, row, r.phrasing)

    # Filter by confidence floor and rank by score
    ranked = sorted(
        (
            (score, row, matched_via)
            for score, row, matched_via in best_score.values()
            if (row["confidence"] or 0.0) >= confidence_floor
        ),
        key=lambda x: -x[0],
    )

    clips: list[Clip] = []
    for score, row, matched_via in ranked[:top_k]:
        topics = json.loads(row["topics"]) if row["topics"] else []
        clips.append(
            Clip(
                chunk_id=row["chunk_id"],
                video_id=row["video_id"],
                video_title=row["video_title"],
                video_form=row["video_form"],
                kind=row["kind"],
                start_seconds=row["start_seconds"],
                end_seconds=row["end_seconds"],
                question=row["question"],
                answer=row["answer"],
                speaker=row["speaker"],
                topics=topics,
                confidence=row["confidence"],
                score=score,
                matched_via=matched_via,
                youtube_url=f"https://www.youtube.com/watch?v={row['video_id']}&t={int(row['start_seconds'])}s",
            )
        )
    return reforms, clips


def synthesize_answer(cfg: Config, user_query: str, clips: list[Clip]) -> str:
    """Produce a 2-3 paragraph cited answer from the top clips."""
    if not clips:
        return "No clips in this channel address that question."

    excerpts = []
    for i, c in enumerate(clips, start=1):
        kind_label = "Q&A" if c.kind == "qa" else "Objection"
        excerpts.append(
            f"[{i}] {kind_label} from \"{c.video_title}\" "
            f"({c.start_seconds:.0f}s)\n"
            f"  {'Question' if c.kind == 'qa' else 'Objection'}: {c.question}\n"
            f"  {'Answer' if c.kind == 'qa' else 'Rebuttal'}: {c.answer}"
        )
    excerpt_block = "\n\n".join(excerpts)

    client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
    response = client.messages.create(
        model=cfg.query_model,
        max_tokens=2000,
        system=[
            {
                "type": "text",
                "text": SYNTHESIZER_SYSTEM,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        thinking={"type": "adaptive"},
        output_config={"effort": "low"},
        messages=[
            {
                "role": "user",
                "content": f"User question: {user_query}\n\nClip excerpts:\n\n{excerpt_block}",
            }
        ],
    )
    return next((b.text for b in response.content if b.type == "text"), "").strip()
