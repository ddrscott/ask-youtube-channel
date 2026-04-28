"""HTTP client for the AYC cloud API at ayc.ljs.app.

Replaces the previous local-SQLite backend. Every function that used to
operate on a `sqlite3.Connection` now goes through `ApiClient`, which speaks
to the Worker's `/internal/*` endpoints with a service-token bearer auth.

Configuration comes from `Config.api_base_url` and `Config.api_token`
(see `ayc/config.py`).

The `encode_embedding` / `decode_embedding` helpers are retained because the
embed phase still produces float32 arrays locally before posting them.
"""
from __future__ import annotations

import json
import time
from typing import Any, Iterator

import httpx
import numpy as np

from .config import Config


# ────────────────────────────────────────────────────────────────────────────
# Embedding helpers (still useful for client-side numpy work)
# ────────────────────────────────────────────────────────────────────────────


def encode_embedding(vec: list[float] | np.ndarray) -> bytes:
    arr = np.asarray(vec, dtype=np.float32)
    return arr.tobytes()


def decode_embedding(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


# ────────────────────────────────────────────────────────────────────────────
# ApiClient
# ────────────────────────────────────────────────────────────────────────────


class ApiError(RuntimeError):
    def __init__(self, status: int, body: str | dict[str, Any]):
        self.status = status
        self.body = body
        super().__init__(f"API {status}: {body}")


class ApiClient:
    """Thin wrapper over the AYC Worker's internal API.

    Construct once per CLI invocation (it owns an httpx.Client connection pool).
    Use as a context manager or call `close()` when done.
    """

    def __init__(self, cfg: Config, *, timeout: float = 60.0) -> None:
        self.base_url = cfg.api_base_url.rstrip("/")
        self.token = cfg.api_token
        if not self.token:
            raise RuntimeError("AYC_API_TOKEN is required (set in .env)")
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            headers={"Authorization": f"Bearer {self.token}"},
        )

    # ── lifecycle ──

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "ApiClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ── retry-aware request helper ──

    def _req(self, method: str, path: str, *, json_body: Any = None, content: bytes | None = None,
             headers: dict[str, str] | None = None, retries: int = 3) -> httpx.Response:
        last_err: Exception | None = None
        for attempt in range(retries):
            try:
                resp = self._client.request(
                    method,
                    path,
                    json=json_body,
                    content=content,
                    headers=headers,
                )
            except httpx.RequestError as e:
                last_err = e
                time.sleep(0.5 * (2 ** attempt))
                continue
            if resp.status_code == 429 or 500 <= resp.status_code < 600:
                last_err = ApiError(resp.status_code, resp.text)
                time.sleep(0.5 * (2 ** attempt))
                continue
            if resp.status_code >= 400:
                try:
                    body: Any = resp.json()
                except Exception:
                    body = resp.text
                raise ApiError(resp.status_code, body)
            return resp
        # Out of retries
        raise last_err if last_err else ApiError(0, "request failed without exception")

    # ── stats ──

    def stats(self) -> dict[str, Any]:
        return self._req("GET", "/internal/stats").json()

    # ── channels ──

    def upsert_channel(
        self,
        channel_id: str,
        handle: str,
        display_name: str | None,
        *,
        submitted_by_email: str | None = None,
    ) -> None:
        self._req(
            "POST",
            "/internal/channels",
            json_body={
                "id": channel_id,
                "handle": handle,
                "display_name": display_name,
                "submitted_by_email": submitted_by_email,
            },
        )

    # ── videos ──

    def upsert_videos_bulk(self, videos: list[dict[str, Any]]) -> int:
        """POST a batch of video dicts. Each row may contain id, channel_id, title,
        duration_seconds, thumbnail_url, published_at, form, ingest_status."""
        if not videos:
            return 0
        # Server caps at 500/call; chunk locally just in case.
        total = 0
        for i in range(0, len(videos), 500):
            batch = videos[i : i + 500]
            r = self._req("POST", "/internal/videos:bulk", json_body={"videos": batch})
            total += r.json().get("processed", 0)
        return total

    def list_videos(
        self,
        *,
        channel_id: str | None = None,
        status: str | None = None,
        form: str | None = None,
        limit: int = 100,
    ) -> Iterator[dict[str, Any]]:
        """Generator: pages through /internal/videos until exhausted."""
        cursor: str | None = None
        while True:
            params: dict[str, str] = {"limit": str(limit)}
            if channel_id:
                params["channel_id"] = channel_id
            if status:
                params["status"] = status
            if form:
                params["form"] = form
            if cursor:
                params["cursor"] = cursor
            r = self._client.get("/internal/videos", params=params)
            r.raise_for_status()
            data = r.json()
            for v in data.get("videos", []):
                yield v
            cursor = data.get("next_cursor")
            if not cursor:
                return

    def mark_video(self, video_id: str, status: str, error: str | None = None) -> None:
        self._req(
            "POST",
            f"/internal/videos/{video_id}/mark",
            json_body={"status": status, "error": error},
        )

    # ── transcripts (R2-backed) ──

    def put_transcript(self, video_id: str, payload: dict[str, Any], *, source: str = "auto") -> None:
        body = json.dumps(payload).encode("utf-8")
        self._req(
            "POST",
            f"/internal/videos/{video_id}/transcript",
            content=body,
            headers={
                "Content-Type": "application/json",
                "X-Transcript-Source": source,
            },
        )

    def get_transcript(self, video_id: str) -> dict[str, Any]:
        r = self._req("GET", f"/internal/videos/{video_id}/transcript")
        return r.json()

    def list_video_chunks(self, video_id: str) -> list[dict[str, Any]]:
        """Returns the existing chunks for a video, ordered by start_seconds.
        Used by the gap-fill prepare step."""
        r = self._req("GET", f"/internal/videos/{video_id}/chunks")
        return r.json().get("chunks", [])

    # ── chunks (relational) ──

    def insert_chunks_bulk(
        self,
        video_id: str,
        chunks: list[dict[str, Any]],
        *,
        replace: bool = True,
    ) -> int:
        """POST chunks for a video. Server deletes prior chunks (if replace) and
        marks the video 'chunked' transactionally."""
        # Server caps at 500/call.
        total = 0
        for i in range(0, len(chunks), 500):
            batch = chunks[i : i + 500]
            r = self._req(
                "POST",
                "/internal/chunks:bulk",
                json_body={
                    "video_id": video_id,
                    "replace": replace and i == 0,
                    "chunks": batch,
                },
            )
            total += r.json().get("inserted", 0)
        return total

    def unembedded_chunks(self, limit: int = 100) -> list[dict[str, Any]]:
        r = self._req("GET", f"/internal/chunks/unembedded?limit={limit}")
        return r.json().get("chunks", [])

    def embeddings_bulk(self, items: list[dict[str, Any]]) -> int:
        """POST {id, vector} items. Server upserts to Vectorize, sets embedded_at,
        rolls videos to 'embedded' as their last chunk lands."""
        if not items:
            return 0
        total = 0
        for i in range(0, len(items), 500):
            batch = items[i : i + 500]
            r = self._req(
                "POST",
                "/internal/chunks/embeddings:bulk",
                json_body={"items": batch},
            )
            total += r.json().get("upserted", 0)
        return total

    # ── vector query ──

    def vector_query(
        self,
        vector: list[float],
        *,
        top_k: int = 20,
        channel_id: str | None = None,
        kind: str | None = None,
    ) -> list[dict[str, Any]]:
        body: dict[str, Any] = {"vector": vector, "top_k": top_k}
        flt: dict[str, str] = {}
        if channel_id:
            flt["channel_id"] = channel_id
        if kind:
            flt["kind"] = kind
        if flt:
            body["filter"] = flt
        r = self._req("POST", "/internal/vector/query", json_body=body)
        return r.json().get("matches", [])
