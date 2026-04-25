"""Transcript fetching via yt-dlp + JSON3 parsing with rolling-caption dedupe."""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .db import mark_video


@dataclass
class TranscriptSegment:
    start: float
    end: float
    text: str


@dataclass
class Transcript:
    video_id: str
    source: str  # 'auto' | 'manual'
    duration_seconds: float
    segments: list[TranscriptSegment]


def _run_yt_dlp_subs(video_id: str, out_dir: Path, manual: bool) -> Path | None:
    """Run yt-dlp to fetch subtitles. Returns the path to the downloaded JSON3 file, or None if no subs."""
    flag = "--write-subs" if manual else "--write-auto-subs"
    proc = subprocess.run(
        [
            "yt-dlp",
            "--skip-download",
            flag,
            "--sub-lang",
            "en.*,en",
            "--sub-format",
            "json3",
            "--output",
            str(out_dir / "%(id)s.%(ext)s"),
            f"https://www.youtube.com/watch?v={video_id}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return None
    candidates = list(out_dir.glob(f"{video_id}*.json3"))
    return candidates[0] if candidates else None


def _parse_json3(path: Path) -> list[TranscriptSegment]:
    """Parse a YouTube JSON3 caption file into segments, deduping rolling captions at the word level."""
    data = json.loads(path.read_text(encoding="utf-8"))
    events = data.get("events") or []

    # Emit (absolute_start_ms, text) for every non-empty utf8 segment.
    words: list[tuple[int, str]] = []
    for event in events:
        t_start = event.get("tStartMs") or 0
        for seg in event.get("segs") or []:
            text = seg.get("utf8")
            if not text:
                continue
            offset = seg.get("tOffsetMs") or 0
            words.append((t_start + offset, text))

    # Dedupe identical (start_ms, text) pairs — rolling captions repeat the same word
    # at the same timestamp across overlapping events.
    seen: set[tuple[int, str]] = set()
    deduped: list[tuple[int, str]] = []
    for w in words:
        if w not in seen:
            seen.add(w)
            deduped.append(w)

    deduped.sort(key=lambda w: w[0])

    # Group into ~5-second segments for chunker readability.
    segments: list[TranscriptSegment] = []
    current_start_ms: int | None = None
    current_text: list[str] = []
    last_word_ms = 0
    GROUP_MS = 5000

    for start_ms, text in deduped:
        if current_start_ms is None:
            current_start_ms = start_ms
            current_text = [text]
        elif start_ms - current_start_ms > GROUP_MS:
            segments.append(
                TranscriptSegment(
                    start=current_start_ms / 1000.0,
                    end=last_word_ms / 1000.0,
                    text="".join(current_text).strip(),
                )
            )
            current_start_ms = start_ms
            current_text = [text]
        else:
            current_text.append(text)
        last_word_ms = start_ms

    if current_start_ms is not None and current_text:
        segments.append(
            TranscriptSegment(
                start=current_start_ms / 1000.0,
                end=last_word_ms / 1000.0,
                text="".join(current_text).strip(),
            )
        )

    return [s for s in segments if s.text]


def fetch_transcript(video_id: str, transcripts_dir: Path) -> Transcript | None:
    """Fetch a transcript for a video. Tries manual captions first, then auto-subs."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        # Manual captions first (higher quality, line-level timestamps already)
        path = _run_yt_dlp_subs(video_id, tmp_path, manual=True)
        source = "manual"
        if not path:
            path = _run_yt_dlp_subs(video_id, tmp_path, manual=False)
            source = "auto"
        if not path:
            return None

        segments = _parse_json3(path)
        if not segments:
            return None

        # Persist normalized transcript
        out_path = transcripts_dir / f"{video_id}.json"
        duration = segments[-1].end
        out_path.write_text(
            json.dumps(
                {
                    "video_id": video_id,
                    "source": source,
                    "duration_seconds": duration,
                    "segments": [
                        {"start": s.start, "end": s.end, "text": s.text} for s in segments
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        # Also keep the raw JSON3 for debugging
        raw_dest = transcripts_dir / f"{video_id}.json3"
        shutil.copy2(path, raw_dest)

        return Transcript(
            video_id=video_id,
            source=source,
            duration_seconds=duration,
            segments=segments,
        )


def load_transcript(transcripts_dir: Path, video_id: str) -> Transcript | None:
    path = transcripts_dir / f"{video_id}.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    return Transcript(
        video_id=data["video_id"],
        source=data["source"],
        duration_seconds=data["duration_seconds"],
        segments=[TranscriptSegment(**s) for s in data["segments"]],
    )


def transcribe_video(conn, video_id: str, transcripts_dir: Path) -> Transcript | None:
    """Fetch the transcript and update the videos row. Returns the transcript or None on failure."""
    transcript = fetch_transcript(video_id, transcripts_dir)
    if transcript is None:
        mark_video(
            conn,
            video_id,
            "skipped_no_captions",
            error="no captions available (manual or auto)",
        )
        conn.commit()
        return None
    mark_video(
        conn,
        video_id,
        "transcribed",
        transcript_source=transcript.source,
        transcript_path=str(transcripts_dir / f"{video_id}.json"),
        error=None,
    )
    conn.commit()
    return transcript
