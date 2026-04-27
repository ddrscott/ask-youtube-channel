"""Filesystem queue for Claude Code agent-based chunking.

Workflow:
    1. `ayc queue prepare`     — Python pulls transcripts from R2 via the API
       and writes them to queue/pending/<id>.json
    2. Claude Code dispatches  the `ayc-chunker` agent for each pending file.
       The agent writes      queue/completed/<id>.chunks.json  (or  queue/failed/<id>.error.txt)
    3. `ayc queue merge`       — Python reads queue/completed/*, posts chunks to
       /internal/chunks:bulk (which transactionally deletes prior chunks,
       inserts new ones, and marks the video 'chunked').

This keeps deterministic state (D1, R2, Vectorize) in the cloud and the
LLM-shaped work (extracting Q&A and objection moments) in Claude Code subagents,
which run against the user's Claude Code subscription rather than the API.
"""

from __future__ import annotations

import json
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from .config import REPO_ROOT, Config
from .db import ApiClient
from .transcripts import transcript_from_payload
from .verify import quarantine_invalid, verify_completed_dir


QUEUE_DIR = REPO_ROOT / "queue"
PENDING_DIR = QUEUE_DIR / "pending"
COMPLETED_DIR = QUEUE_DIR / "completed"
FAILED_DIR = QUEUE_DIR / "failed"
ARCHIVE_DIR = QUEUE_DIR / "archive"

PENDING_SCHEMA_VERSION = 1
COMPLETED_SCHEMA_VERSION = 1


@dataclass
class QueueCounts:
    pending: int
    completed: int
    failed: int


def ensure_dirs() -> None:
    for d in (PENDING_DIR, COMPLETED_DIR, FAILED_DIR, ARCHIVE_DIR):
        d.mkdir(parents=True, exist_ok=True)


def queue_counts() -> QueueCounts:
    ensure_dirs()
    return QueueCounts(
        pending=sum(1 for _ in PENDING_DIR.glob("*.json")),
        completed=sum(1 for _ in COMPLETED_DIR.glob("*.chunks.json")),
        failed=sum(1 for _ in FAILED_DIR.glob("*")),
    )


def write_pending(
    *,
    video_id: str,
    title: str,
    form: str,
    duration_seconds: float,
    transcript_source: str,
    segments: list[dict[str, float | str]],
) -> Path:
    """Write a single pending transcript file. Idempotent — overwrites any existing file."""
    ensure_dirs()
    path = PENDING_DIR / f"{video_id}.json"
    payload = {
        "schema_version": PENDING_SCHEMA_VERSION,
        "video_id": video_id,
        "title": title,
        "form": form,
        "duration_seconds": duration_seconds,
        "transcript_source": transcript_source,
        "segments": segments,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def prepare_pending(
    cfg: Config,
    client: ApiClient,
    *,
    form: str = "all",
    limit: int = 0,
    rechunk: bool = False,
) -> tuple[int, int]:
    """Fetch transcripts and write queue/pending/<id>.json files.

    By default: only videos at status='transcribed' (i.e. transcribed but not
    yet chunked).

    With ``rechunk=True``: include status in {'chunked', 'embedded'} as well —
    used by the rechunk runbook to re-process previously-chunked content under
    updated chunker rules. The merge step's ``replace=True`` semantics will
    delete the old chunks and their Vectorize entries before inserting fresh.

    Returns ``(written, skipped)``. Skipped means the transcript fetch failed.
    """
    statuses: list[str] = ["transcribed"]
    if rechunk:
        statuses.extend(["chunked", "embedded"])

    written = 0
    skipped = 0
    seen = 0
    target = limit if limit else None

    for status in statuses:
        if target is not None and seen >= target:
            break
        list_kwargs: dict[str, str | int] = {"status": status, "limit": 200}
        if form != "all":
            list_kwargs["form"] = form
        for video in client.list_videos(**list_kwargs):  # type: ignore[arg-type]
            if target is not None and seen >= target:
                break
            seen += 1
            try:
                payload = client.get_transcript(video["id"])
            except Exception:
                skipped += 1
                continue
            transcript = transcript_from_payload(payload)
            write_pending(
                video_id=video["id"],
                title=video["title"],
                form=video["form"],
                duration_seconds=transcript.duration_seconds,
                transcript_source=transcript.source,
                segments=[
                    {"start": s.start, "end": s.end, "text": s.text}
                    for s in transcript.segments
                ],
            )
            written += 1

    # cfg used to be the transcripts_dir source; keep it in the signature so
    # CLI callers don't break if they pass it positionally.
    _ = cfg
    return written, skipped


def prepare_single(client: ApiClient, video_id: str) -> bool:
    """Write a single pending file for one specific video. Returns True if
    written, False if the transcript could not be fetched.

    Used by the rechunk runbook's single-video mode. Looks the video up by
    iterating list_videos across statuses — costs ~one paginated API call,
    which is fine for a one-off."""
    target: dict | None = None
    for status in ("chunked", "embedded", "transcribed"):
        for v in client.list_videos(status=status, limit=200):
            if v["id"] == video_id:
                target = v
                break
        if target is not None:
            break
    if target is None:
        return False
    try:
        payload = client.get_transcript(video_id)
    except Exception:
        return False
    transcript = transcript_from_payload(payload)
    write_pending(
        video_id=video_id,
        title=target.get("title") or video_id,
        form=target.get("form") or "long",
        duration_seconds=transcript.duration_seconds,
        transcript_source=transcript.source,
        segments=[
            {"start": s.start, "end": s.end, "text": s.text}
            for s in transcript.segments
        ],
    )
    return True


def merge_completed(client: ApiClient) -> tuple[int, int, int, int]:
    """Read every queue/completed/*.chunks.json and POST chunks to the API.

    Verifies each file against its source transcript first. Files with errors
    are quarantined to queue/failed/ before merging starts.

    On successful merge, the completed file is moved to queue/archive/ and the
    matching queue/pending/<id>.json is deleted.

    Returns (videos_merged, total_chunks_inserted, merge_errors, quarantined).
    """
    ensure_dirs()

    # Defense-in-depth: verify every file before merging.
    # The verifier reads transcripts from queue/pending/ files (already on disk),
    # so it doesn't need a transcripts dir or API access.
    verifications = verify_completed_dir(COMPLETED_DIR, PENDING_DIR)
    quarantined = quarantine_invalid(verifications, FAILED_DIR)

    files = sorted(COMPLETED_DIR.glob("*.chunks.json"))
    if not files:
        return 0, 0, 0, quarantined

    videos = 0
    chunks = 0
    errors = 0
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            video_id = data["video_id"]
            raw_chunks = data["chunks"]
            if data.get("schema_version", 1) > COMPLETED_SCHEMA_VERSION:
                raise ValueError(f"unknown schema_version {data['schema_version']}")

            payload_chunks = [
                {
                    "id": uuid.uuid4().hex,
                    "kind": c["kind"],
                    "start_seconds": float(c["start_seconds"]),
                    "end_seconds": float(c["end_seconds"]),
                    "question": c["question"],
                    "answer": c["answer"],
                    "speaker": c.get("speaker"),
                    "topics": c.get("topics") or [],
                    "confidence": float(c.get("confidence", 0.5)),
                }
                for c in raw_chunks
            ]
            client.insert_chunks_bulk(video_id, payload_chunks, replace=True)
            videos += 1
            chunks += len(raw_chunks)
            # Archive the completed file and remove the pending file
            shutil.move(str(path), str(ARCHIVE_DIR / path.name))
            pending = PENDING_DIR / f"{video_id}.json"
            if pending.exists():
                pending.unlink()
        except Exception as e:  # noqa: BLE001
            errors += 1
            error_path = FAILED_DIR / f"{path.stem}.merge-error.txt"
            error_path.write_text(f"merge error: {e}\n\nfile: {path}\n", encoding="utf-8")
    return videos, chunks, errors, quarantined
