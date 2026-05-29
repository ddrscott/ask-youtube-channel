"""Backfill BGE-M3 embeddings into the V2 Vectorize index.

Pages through every video, fetches its chunk IDs, and submits batches to the
Worker's `POST /internal/chunks/reembed:bulk` endpoint. The Worker does the
text formatting + BGE-M3 inference + Vectorize upsert in one call — Python
is just the orchestrator.

No OpenAI dependency. Costs (Workers AI native, against your account credit)
are negligible: ~12k chunks × few thousand tokens each ≈ pennies total.
"""

from __future__ import annotations

import time
from typing import Iterator

import httpx
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, MofNCompleteColumn, TimeElapsedColumn

from .config import Config
from .db import ApiClient

BATCH_SIZE = 100
MAX_RETRIES = 3


def _iter_all_chunk_ids(client: ApiClient) -> Iterator[tuple[str, list[str]]]:
    """Yield (video_id, [chunk_ids]) for every video that has at least one chunk."""
    for status in ("chunked", "embedded"):
        for video in client.list_videos(status=status, limit=100):
            # Use the existing GET /internal/videos/:id/chunks endpoint
            r = client._client.get(f"/internal/videos/{video['id']}/chunks")
            r.raise_for_status()
            chunks = r.json().get("chunks", [])
            ids = [c["id"] for c in chunks]
            if ids:
                yield video["id"], ids


def _post_reembed(client: ApiClient, ids: list[str]) -> dict:
    """POST a batch of chunk IDs to the reembed endpoint, with retries."""
    last_err: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            r = client._client.post(
                "/internal/chunks/reembed:bulk",
                json={"ids": ids},
                timeout=120.0,
            )
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as e:
            # 4xx — don't retry, surface the error
            if 400 <= e.response.status_code < 500:
                raise RuntimeError(
                    f"HTTP {e.response.status_code}: {e.response.text[:300]}"
                )
            last_err = e
        except httpx.RequestError as e:
            last_err = e
        time.sleep(2 ** attempt)
    raise RuntimeError(f"Backfill batch failed after {MAX_RETRIES} attempts: {last_err}")


def backfill_bge(
    cfg: Config,
    client: ApiClient,
    batch_size: int = BATCH_SIZE,
    only_video_id: str | None = None,
) -> dict:
    """Walk all chunks and reembed via the Worker. Returns counts."""
    console = Console()

    # Collect all (video_id, chunk_ids) pairs up front so we can show progress.
    console.print("[bold]Enumerating chunks…[/bold]")
    pairs: list[tuple[str, list[str]]] = []
    if only_video_id:
        r = client._client.get(f"/internal/videos/{only_video_id}/chunks")
        r.raise_for_status()
        chunks = r.json().get("chunks", [])
        ids = [c["id"] for c in chunks]
        if ids:
            pairs.append((only_video_id, ids))
    else:
        for vid, ids in _iter_all_chunk_ids(client):
            pairs.append((vid, ids))

    total_chunks = sum(len(ids) for _, ids in pairs)
    console.print(
        f"  [dim]{len(pairs)} videos · {total_chunks} chunks to backfill[/dim]"
    )

    if total_chunks == 0:
        return {"videos": 0, "chunks": 0, "upserted": 0, "failed": 0}

    # Flatten and batch
    all_ids: list[str] = [cid for _, ids in pairs for cid in ids]
    upserted = 0
    failed: list[str] = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Embedding chunks", total=total_chunks)
        for i in range(0, len(all_ids), batch_size):
            chunk_batch = all_ids[i : i + batch_size]
            try:
                resp = _post_reembed(client, chunk_batch)
                upserted += resp.get("upserted", 0)
            except Exception as e:
                console.print(f"[red]Batch {i // batch_size} failed:[/red] {e}")
                failed.extend(chunk_batch)
            progress.update(task, advance=len(chunk_batch))

    return {
        "videos": len(pairs),
        "chunks": total_chunks,
        "upserted": upserted,
        "failed": len(failed),
        "failed_ids": failed[:20],  # first 20 for triage
    }
