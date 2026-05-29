"""Chunk embedding via Cloudflare Workers AI BGE-M3, into the V2 Vectorize index.

After Phase 3.4 cutover, all embedding flows through the Worker. Python is just
the orchestrator: list unembedded chunk IDs from D1, batch them, and POST to
`/internal/chunks/reembed:bulk`. The Worker does the BGE-M3 inference, the
Vectorize upsert, the embedded_at write, and the video status roll-up.

No OpenAI dependency.
"""

from __future__ import annotations

from .config import Config
from .db import ApiClient


def embed_pending_chunks(cfg: Config, client: ApiClient, batch_size: int = 100) -> int:
    """Embed all chunks that don't yet have an embedding. Returns count embedded.

    The Worker's reembed endpoint loads chunk text from D1, embeds via BGE-M3,
    and upserts to Vectorize V2 — Python just supplies the chunk IDs.
    """
    total = 0
    while True:
        chunks = client.unembedded_chunks(limit=batch_size)
        if not chunks:
            break
        ids = [c["id"] for c in chunks]
        result = client.reembed_bulk(ids)
        total += result.get("upserted", 0)
    return total


def embed_query(cfg: Config, text: str) -> list[float]:
    """LEGACY: embed a single query string client-side.

    Kept for any out-of-band tooling that still wants a local vector. The
    server-side `/api/v1/search` endpoint embeds queries via Workers AI BGE-M3
    in-Worker now — see src/services/embed.ts. Python-side `ayc ask` should
    just POST text to the search endpoint and let the Worker do the embedding.
    """
    raise NotImplementedError(
        "Phase 3.4: client-side query embedding removed — use the Worker's /api/v1/search "
        "endpoint which embeds via env.AI.run('@cf/baai/bge-m3', ...) server-side."
    )
