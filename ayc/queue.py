"""Filesystem queue for Claude Code agent-based chunking.

Workflow:
    1. `ayc queue prepare`     — Python writes transcripts to queue/pending/<id>.json
    2. Claude Code dispatches  the `ayc-chunker` agent for each pending file.
       The agent writes      queue/completed/<id>.chunks.json  (or  queue/failed/<id>.error.txt)
    3. `ayc queue merge`       — Python reads queue/completed/*, inserts chunks into the DB.

This keeps deterministic state (DB, transcripts, embeddings) in Python and the
LLM-shaped work (extracting Q&A and objection moments) in Claude Code subagents,
which run against the user's Claude Code subscription rather than the API.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path

from .config import REPO_ROOT
from .db import insert_chunk, mark_video
from .transcripts import load_transcript


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
    conn: sqlite3.Connection,
    transcripts_dir: Path,
    *,
    form: str = "all",
    limit: int = 0,
) -> tuple[int, int]:
    """For every transcribed video without chunks yet, write its transcript into queue/pending/.

    Returns (written, skipped). Skipped means transcript file missing on disk.
    """
    sql = (
        "SELECT id, title, form, duration_seconds FROM videos "
        "WHERE ingest_status = 'transcribed'"
    )
    params: list[object] = []
    if form != "all":
        sql += " AND form = ?"
        params.append(form)
    sql += " ORDER BY id"
    rows = conn.execute(sql, params).fetchall()
    if limit:
        rows = rows[:limit]

    written = 0
    skipped = 0
    for row in rows:
        transcript = load_transcript(transcripts_dir, row["id"])
        if transcript is None:
            skipped += 1
            continue
        write_pending(
            video_id=row["id"],
            title=row["title"],
            form=row["form"],
            duration_seconds=transcript.duration_seconds,
            transcript_source=transcript.source,
            segments=[
                {"start": s.start, "end": s.end, "text": s.text} for s in transcript.segments
            ],
        )
        written += 1
    return written, skipped


def merge_completed(conn: sqlite3.Connection) -> tuple[int, int, int]:
    """Read every queue/completed/*.chunks.json and insert chunks into the DB.

    On success, the completed file is moved to queue/archive/ and the matching
    queue/pending/<id>.json is deleted.

    Returns (videos_merged, total_chunks_inserted, errors).
    """
    ensure_dirs()
    files = sorted(COMPLETED_DIR.glob("*.chunks.json"))
    if not files:
        return 0, 0, 0

    videos = 0
    chunks = 0
    errors = 0
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            video_id = data["video_id"]
            chunk_list = data["chunks"]
            # Validate schema version (forward compat)
            if data.get("schema_version", 1) > COMPLETED_SCHEMA_VERSION:
                raise ValueError(f"unknown schema_version {data['schema_version']}")
            # Replace any prior chunks for this video
            conn.execute("DELETE FROM chunks WHERE video_id = ?", (video_id,))
            for c in chunk_list:
                insert_chunk(
                    conn,
                    chunk_id=uuid.uuid4().hex,
                    video_id=video_id,
                    kind=c["kind"],
                    start_seconds=float(c["start_seconds"]),
                    end_seconds=float(c["end_seconds"]),
                    question=c["question"],
                    answer=c["answer"],
                    speaker=c.get("speaker"),
                    topics=c.get("topics") or [],
                    confidence=float(c.get("confidence", 0.5)),
                )
            mark_video(conn, video_id, "chunked", error=None)
            conn.commit()
            videos += 1
            chunks += len(chunk_list)
            # Archive the completed file and remove the pending file
            shutil.move(str(path), str(ARCHIVE_DIR / path.name))
            pending = PENDING_DIR / f"{video_id}.json"
            if pending.exists():
                pending.unlink()
        except Exception as e:  # noqa: BLE001
            errors += 1
            error_path = FAILED_DIR / f"{path.stem}.merge-error.txt"
            error_path.write_text(f"merge error: {e}\n\nfile: {path}\n", encoding="utf-8")
    return videos, chunks, errors
