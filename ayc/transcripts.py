"""Transcript fetching via yt-dlp + JSON3 parsing with rolling-caption dedupe.

Transcripts are streamed to R2 via the cloud API; nothing persists locally
beyond the temp dir used during yt-dlp invocation.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import dataclass, asdict
from pathlib import Path

from .db import ApiClient


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

    def to_payload(self) -> dict[str, object]:
        return {
            "video_id": self.video_id,
            "source": self.source,
            "duration_seconds": self.duration_seconds,
            "segments": [asdict(s) for s in self.segments],
        }


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

    words: list[tuple[int, str]] = []
    for event in events:
        t_start = event.get("tStartMs") or 0
        for seg in event.get("segs") or []:
            text = seg.get("utf8")
            if not text:
                continue
            offset = seg.get("tOffsetMs") or 0
            words.append((t_start + offset, text))

    seen: set[tuple[int, str]] = set()
    deduped: list[tuple[int, str]] = []
    for w in words:
        if w not in seen:
            seen.add(w)
            deduped.append(w)
    deduped.sort(key=lambda w: w[0])

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


def fetch_transcript(video_id: str) -> Transcript | None:
    """Fetch a transcript for a video. Tries manual captions first, then auto-subs.
    Nothing persists locally — caller is responsible for shipping it to R2."""
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

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

        return Transcript(
            video_id=video_id,
            source=source,
            duration_seconds=segments[-1].end,
            segments=segments,
        )


def transcribe_video(client: ApiClient, video_id: str) -> Transcript | None:
    """Fetch a transcript via yt-dlp and PUT it to R2 via the API.

    Updates the video status to 'transcribed' (set server-side by the put_transcript
    endpoint) or 'skipped_no_captions' if nothing was available.
    """
    transcript = fetch_transcript(video_id)
    if transcript is None:
        client.mark_video(
            video_id,
            "skipped_no_captions",
            error="no captions available (manual or auto)",
        )
        return None
    client.put_transcript(video_id, transcript.to_payload(), source=transcript.source)
    return transcript


def transcript_from_payload(data: dict[str, object]) -> Transcript:
    """Reconstruct a Transcript from the JSON returned by GET /internal/videos/:id/transcript."""
    return Transcript(
        video_id=data["video_id"],  # type: ignore[arg-type]
        source=data["source"],  # type: ignore[arg-type]
        duration_seconds=data["duration_seconds"],  # type: ignore[arg-type]
        segments=[TranscriptSegment(**s) for s in data["segments"]],  # type: ignore[arg-type]
    )
