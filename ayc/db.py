"""SQLite schema and connection helpers."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import numpy as np

SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    id TEXT PRIMARY KEY,
    handle TEXT NOT NULL,
    display_name TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS videos (
    id TEXT PRIMARY KEY,
    channel_id TEXT NOT NULL REFERENCES channels(id),
    title TEXT NOT NULL,
    duration_seconds INTEGER,
    thumbnail_url TEXT,
    published_at TEXT,
    form TEXT NOT NULL DEFAULT 'unknown',  -- 'long' | 'short' | 'unknown'
    transcript_source TEXT,           -- 'auto' | 'manual' | 'whisper'
    transcript_path TEXT,             -- path to JSON file under data/transcripts/
    ingest_status TEXT NOT NULL,      -- 'pending' | 'transcribed' | 'chunked' | 'embedded' | 'failed' | 'skipped_no_captions'
    error TEXT,
    enumerated_at TEXT DEFAULT (datetime('now')),
    transcribed_at TEXT,
    chunked_at TEXT,
    embedded_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_videos_status ON videos(ingest_status);
CREATE INDEX IF NOT EXISTS idx_videos_form ON videos(form);

CREATE TABLE IF NOT EXISTS chunks (
    id TEXT PRIMARY KEY,
    video_id TEXT NOT NULL REFERENCES videos(id),
    kind TEXT NOT NULL,               -- 'qa' | 'objection'
    start_seconds REAL NOT NULL,
    end_seconds REAL NOT NULL,
    question TEXT NOT NULL,           -- qa: the question; objection: the objection/claim
    answer TEXT NOT NULL,             -- qa: the answer; objection: the rebuttal
    speaker TEXT,
    topics TEXT,                      -- JSON array
    confidence REAL,
    embedding BLOB,                   -- float32 bytes when embedded
    embed_text TEXT,                  -- the text that was embedded (for debugging)
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_chunks_video ON chunks(video_id);
CREATE INDEX IF NOT EXISTS idx_chunks_kind ON chunks(kind);
CREATE INDEX IF NOT EXISTS idx_chunks_unembedded ON chunks(id) WHERE embedding IS NULL;
"""


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    # Migrations must run before SCHEMA: the SCHEMA's indexes reference columns
    # added by the migration on already-existing DBs.
    _migrate_pre_schema(conn)
    conn.executescript(SCHEMA)
    return conn


def _migrate_pre_schema(conn: sqlite3.Connection) -> None:
    """Apply column-add migrations for DBs created before a column was introduced."""
    has_videos = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='videos'"
    ).fetchone()
    if not has_videos:
        return
    cols = {row[1] for row in conn.execute("PRAGMA table_info(videos)").fetchall()}
    if "form" not in cols:
        conn.execute("ALTER TABLE videos ADD COLUMN form TEXT NOT NULL DEFAULT 'unknown'")
        conn.commit()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def encode_embedding(vec: list[float] | np.ndarray) -> bytes:
    arr = np.asarray(vec, dtype=np.float32)
    return arr.tobytes()


def decode_embedding(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


def upsert_channel(conn: sqlite3.Connection, channel_id: str, handle: str, display_name: str | None) -> None:
    conn.execute(
        "INSERT INTO channels (id, handle, display_name) VALUES (?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET handle=excluded.handle, display_name=excluded.display_name",
        (channel_id, handle, display_name),
    )


def upsert_video(
    conn: sqlite3.Connection,
    *,
    video_id: str,
    channel_id: str,
    title: str,
    duration_seconds: int | None,
    thumbnail_url: str | None,
    published_at: str | None,
    form: str = "unknown",
) -> bool:
    """Insert or update a video row. Returns True if it was newly inserted (vs. updated)."""
    existed = conn.execute("SELECT 1 FROM videos WHERE id = ?", (video_id,)).fetchone() is not None
    conn.execute(
        "INSERT INTO videos (id, channel_id, title, duration_seconds, thumbnail_url, published_at, form, ingest_status) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending') "
        "ON CONFLICT(id) DO UPDATE SET "
        "  title=excluded.title, "
        "  duration_seconds=COALESCE(excluded.duration_seconds, videos.duration_seconds), "
        "  thumbnail_url=COALESCE(excluded.thumbnail_url, videos.thumbnail_url), "
        "  published_at=COALESCE(excluded.published_at, videos.published_at), "
        "  form=CASE WHEN excluded.form != 'unknown' THEN excluded.form ELSE videos.form END",
        (video_id, channel_id, title, duration_seconds, thumbnail_url, published_at, form),
    )
    return not existed


def mark_video(conn: sqlite3.Connection, video_id: str, status: str, **fields: object) -> None:
    """Update a video's status plus any extra columns. Sets the matching <status>_at timestamp when applicable."""
    cols = ["ingest_status = ?"]
    vals: list[object] = [status]
    for k, v in fields.items():
        cols.append(f"{k} = ?")
        vals.append(v)
    if status in {"transcribed", "chunked", "embedded"}:
        cols.append(f"{status}_at = datetime('now')")
    vals.append(video_id)
    conn.execute(f"UPDATE videos SET {', '.join(cols)} WHERE id = ?", vals)


def insert_chunk(
    conn: sqlite3.Connection,
    *,
    chunk_id: str,
    video_id: str,
    kind: str,
    start_seconds: float,
    end_seconds: float,
    question: str,
    answer: str,
    speaker: str | None,
    topics: list[str] | None,
    confidence: float | None,
) -> None:
    conn.execute(
        "INSERT INTO chunks (id, video_id, kind, start_seconds, end_seconds, question, answer, speaker, topics, confidence) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            chunk_id,
            video_id,
            kind,
            start_seconds,
            end_seconds,
            question,
            answer,
            speaker,
            json.dumps(topics) if topics is not None else None,
            confidence,
        ),
    )
