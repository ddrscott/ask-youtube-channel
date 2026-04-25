"""Ad-hoc helper: ingest a specific list of video IDs end-to-end (transcribe + chunk + embed)."""

from __future__ import annotations

import sys

from ayc.chunker import chunk_video, persist_chunks
from ayc.config import Config
from ayc.db import connect, mark_video
from ayc.embed import embed_pending_chunks
from ayc.transcripts import transcribe_video


def main(ids: list[str]) -> None:
    cfg = Config.load()
    conn = connect(cfg.db_path)

    for vid in ids:
        row = conn.execute("SELECT id, title, ingest_status FROM videos WHERE id = ?", (vid,)).fetchone()
        if not row:
            print(f"[skip] {vid}: not in DB (run `ayc init` first)")
            continue
        title = row["title"]
        status = row["ingest_status"]
        print(f"[{vid}] {title!r} (status={status})")

        # 1. Transcribe (idempotent — only runs if not already transcribed)
        if status in ("pending", "skipped_no_captions", "failed"):
            t = transcribe_video(conn, vid, cfg.transcripts_dir)
            if t is None:
                print(f"  [skip] no captions")
                continue
            print(f"  transcribed: {len(t.segments)} segments, {t.duration_seconds:.0f}s, source={t.source}")
            status = "transcribed"

        # 2. Chunk (re-chunk if already chunked, to allow iteration on prompts)
        if status == "transcribed" or "--rechunk" in sys.argv:
            from ayc.transcripts import load_transcript

            transcript = load_transcript(cfg.transcripts_dir, vid)
            if transcript is None:
                print(f"  [skip] transcript file missing on disk")
                continue
            try:
                result = chunk_video(cfg, transcript)
                count = persist_chunks(conn, result)
                print(
                    f"  chunked: {count} chunks "
                    f"(in={result.input_tokens} out={result.output_tokens} "
                    f"cache_read={result.cache_read_tokens} cache_creation={result.cache_creation_tokens})"
                )
            except Exception as e:
                mark_video(conn, vid, "failed", error=f"chunker: {e}")
                conn.commit()
                print(f"  [error] chunker: {e}")
                continue

    # 3. Embed everything that's pending
    n = embed_pending_chunks(cfg, conn)
    print(f"\nEmbedded: {n} chunks")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print("Usage: uv run python scripts/ingest_targeted.py <video_id> [<video_id> ...] [--rechunk]")
        sys.exit(1)
    main(args)
