"""Chunk embedding via OpenAI, persisted to Cloudflare Vectorize."""

from __future__ import annotations

from openai import OpenAI

from .config import Config
from .db import ApiClient


def _embed_text(chunk_kind: str, question: str, answer: str) -> str:
    """Format the embedding text for a chunk.

    Concatenate question + answer so search matches both the framing and the content.
    Byte-identical to the previous local-SQLite implementation.
    """
    label = "Question" if chunk_kind == "qa" else "Objection"
    rebuttal_label = "Answer" if chunk_kind == "qa" else "Rebuttal"
    return f"{label}: {question}\n{rebuttal_label}: {answer}"


def embed_pending_chunks(cfg: Config, client: ApiClient, batch_size: int = 100) -> int:
    """Embed all chunks that don't yet have an embedding. Returns count embedded.

    Pulls the next batch from the API (`/internal/chunks/unembedded`), embeds them
    in one OpenAI call, then POSTs the vectors to `/internal/chunks/embeddings:bulk`,
    which upserts to Vectorize, sets `chunks.embedded_at`, and rolls videos to
    `embedded` when their last chunk lands.
    """
    openai_client = OpenAI(api_key=cfg.openai_api_key)
    total = 0

    while True:
        chunks = client.unembedded_chunks(limit=batch_size)
        if not chunks:
            break

        texts = [_embed_text(c["kind"], c["question"], c["answer"]) for c in chunks]
        resp = openai_client.embeddings.create(model=cfg.embed_model, input=texts)

        items = [
            {"id": chunks[i]["id"], "vector": resp.data[i].embedding}
            for i in range(len(chunks))
        ]
        client.embeddings_bulk(items)
        total += len(chunks)

    return total


def embed_query(cfg: Config, text: str) -> list[float]:
    """Embed a single query string."""
    openai_client = OpenAI(api_key=cfg.openai_api_key)
    resp = openai_client.embeddings.create(model=cfg.embed_model, input=[text])
    return resp.data[0].embedding
