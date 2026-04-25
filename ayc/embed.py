"""Chunk embedding via OpenAI."""

from __future__ import annotations

import sqlite3

from openai import OpenAI

from .config import Config
from .db import encode_embedding


def _embed_text(chunk_kind: str, question: str, answer: str) -> str:
    """Format the embedding text for a chunk.

    Concatenate question + answer so search matches both the framing and the content.
    """
    label = "Question" if chunk_kind == "qa" else "Objection"
    rebuttal_label = "Answer" if chunk_kind == "qa" else "Rebuttal"
    return f"{label}: {question}\n{rebuttal_label}: {answer}"


def embed_pending_chunks(cfg: Config, conn: sqlite3.Connection, batch_size: int = 100) -> int:
    """Embed all chunks that don't yet have an embedding. Returns count embedded."""
    client = OpenAI(api_key=cfg.openai_api_key)

    rows = conn.execute(
        "SELECT id, kind, question, answer FROM chunks WHERE embedding IS NULL"
    ).fetchall()
    if not rows:
        return 0

    total = 0
    for i in range(0, len(rows), batch_size):
        batch = rows[i : i + batch_size]
        texts = [_embed_text(r["kind"], r["question"], r["answer"]) for r in batch]
        resp = client.embeddings.create(model=cfg.embed_model, input=texts)
        for row, item in zip(batch, resp.data):
            blob = encode_embedding(item.embedding)
            conn.execute(
                "UPDATE chunks SET embedding = ?, embed_text = ? WHERE id = ?",
                (blob, _embed_text(row["kind"], row["question"], row["answer"]), row["id"]),
            )
        conn.commit()
        total += len(batch)

    # Mark videos as 'embedded' once all their chunks have embeddings
    conn.execute(
        """
        UPDATE videos SET ingest_status = 'embedded', embedded_at = datetime('now')
        WHERE ingest_status = 'chunked'
          AND id IN (
            SELECT video_id FROM chunks
            GROUP BY video_id
            HAVING SUM(CASE WHEN embedding IS NULL THEN 1 ELSE 0 END) = 0
          )
        """
    )
    conn.commit()
    return total


def embed_query(cfg: Config, text: str) -> list[float]:
    """Embed a single query string."""
    client = OpenAI(api_key=cfg.openai_api_key)
    resp = client.embeddings.create(model=cfg.embed_model, input=[text])
    return resp.data[0].embedding
