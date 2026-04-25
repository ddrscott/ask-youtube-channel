"""Validate agent-produced chunks files against their source transcripts.

Catches the dominant agent-failure mode: hallucinated or off-by-thousands
timestamps that wouldn't match anything in the actual video. Also enforces
the chunk schema (kind values, confidence range, non-empty Q/A text, etc.).

Two levels of severity:
- error: chunk is unusable; verify will move the file to queue/failed/
- warn:  chunk is questionable but not broken; logged, kept

Issues block the chunks file as a whole — if ANY chunk has an error, the
entire file is moved to queue/failed/ rather than splitting it. The chunker
has full-transcript context per video, so a localized error is suspicious
about the rest of the file too.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .transcripts import load_transcript


# Tolerance: a chunk's start/end timestamp can be up to N seconds off from
# the closest transcript segment boundary before we flag it.
TIMESTAMP_TOLERANCE_SECONDS = 5.0


@dataclass
class Issue:
    severity: str  # 'error' | 'warn'
    chunk_index: int  # -1 for file-level issues
    field: str
    message: str


@dataclass
class FileVerification:
    path: Path
    video_id: str
    chunk_count: int
    issues: list[Issue] = field(default_factory=list)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "warn"]

    @property
    def is_valid(self) -> bool:
        return not self.errors


def verify_chunks_file(path: Path, transcripts_dir: Path) -> FileVerification:
    """Verify a single queue/completed/<id>.chunks.json file."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        return FileVerification(
            path=path,
            video_id=path.stem.removesuffix(".chunks"),
            chunk_count=0,
            issues=[Issue("error", -1, "<file>", f"invalid JSON: {e}")],
        )

    video_id = data.get("video_id") or path.stem.removesuffix(".chunks")
    chunks = data.get("chunks") or []
    fv = FileVerification(path=path, video_id=video_id, chunk_count=len(chunks))

    if "schema_version" not in data:
        fv.issues.append(Issue("warn", -1, "schema_version", "missing schema_version field"))

    if not isinstance(chunks, list):
        fv.issues.append(Issue("error", -1, "chunks", f"chunks must be a list, got {type(chunks).__name__}"))
        return fv

    transcript = load_transcript(transcripts_dir, video_id)
    if transcript is None:
        fv.issues.append(
            Issue("error", -1, "<transcript>", f"no transcript on disk for video {video_id} (looked in {transcripts_dir})")
        )
        return fv

    duration = transcript.duration_seconds
    # Build sorted lists for ±tolerance lookup
    segment_starts = sorted({s.start for s in transcript.segments})
    segment_ends = sorted({s.end for s in transcript.segments})

    for i, chunk in enumerate(chunks):
        _verify_chunk(fv, i, chunk, duration, segment_starts, segment_ends)

    return fv


def _verify_chunk(
    fv: FileVerification,
    i: int,
    chunk: Any,
    duration: float,
    segment_starts: list[float],
    segment_ends: list[float],
) -> None:
    if not isinstance(chunk, dict):
        fv.issues.append(Issue("error", i, "<chunk>", f"chunk must be a dict, got {type(chunk).__name__}"))
        return

    kind = chunk.get("kind")
    if kind not in ("qa", "objection"):
        fv.issues.append(Issue("error", i, "kind", f"kind must be 'qa' or 'objection', got {kind!r}"))

    start = chunk.get("start_seconds")
    end = chunk.get("end_seconds")
    if not isinstance(start, (int, float)):
        fv.issues.append(Issue("error", i, "start_seconds", f"must be numeric, got {type(start).__name__}"))
        return
    if not isinstance(end, (int, float)):
        fv.issues.append(Issue("error", i, "end_seconds", f"must be numeric, got {type(end).__name__}"))
        return

    if start < 0 or start > duration + TIMESTAMP_TOLERANCE_SECONDS:
        fv.issues.append(
            Issue(
                "error",
                i,
                "start_seconds",
                f"out of range: start={start:.2f} not in [0, {duration:.2f}+{TIMESTAMP_TOLERANCE_SECONDS}]",
            )
        )
    if end < 0 or end > duration + TIMESTAMP_TOLERANCE_SECONDS:
        fv.issues.append(
            Issue(
                "error",
                i,
                "end_seconds",
                f"out of range: end={end:.2f} not in [0, {duration:.2f}+{TIMESTAMP_TOLERANCE_SECONDS}]",
            )
        )
    if end < start:
        fv.issues.append(
            Issue("error", i, "end_seconds", f"end={end:.2f} is before start={start:.2f}")
        )

    # Tolerance check: start should be near some segment.start; end near some segment.end
    if not _within_tolerance(start, segment_starts):
        fv.issues.append(
            Issue(
                "warn",
                i,
                "start_seconds",
                f"start={start:.2f} not within ±{TIMESTAMP_TOLERANCE_SECONDS}s of any segment boundary",
            )
        )
    if not _within_tolerance(end, segment_ends):
        fv.issues.append(
            Issue(
                "warn",
                i,
                "end_seconds",
                f"end={end:.2f} not within ±{TIMESTAMP_TOLERANCE_SECONDS}s of any segment boundary",
            )
        )

    question = chunk.get("question")
    answer = chunk.get("answer")
    if not isinstance(question, str) or not question.strip():
        fv.issues.append(Issue("error", i, "question", "must be a non-empty string"))
    if not isinstance(answer, str) or not answer.strip():
        fv.issues.append(Issue("error", i, "answer", "must be a non-empty string"))

    speaker = chunk.get("speaker", None)
    if speaker is not None and not isinstance(speaker, str):
        fv.issues.append(Issue("warn", i, "speaker", f"must be string or null, got {type(speaker).__name__}"))

    topics = chunk.get("topics")
    if not isinstance(topics, list) or not all(isinstance(t, str) for t in topics):
        fv.issues.append(Issue("warn", i, "topics", "must be a list of strings"))

    confidence = chunk.get("confidence")
    if not isinstance(confidence, (int, float)) or not (0.0 <= confidence <= 1.0):
        fv.issues.append(
            Issue("error", i, "confidence", f"must be number in [0, 1], got {confidence!r}")
        )


def _within_tolerance(value: float, sorted_targets: list[float]) -> bool:
    """Is value within TIMESTAMP_TOLERANCE_SECONDS of any element in the sorted list?"""
    if not sorted_targets:
        return False
    # Binary search for closest neighbor
    import bisect

    idx = bisect.bisect_left(sorted_targets, value)
    candidates: list[float] = []
    if idx < len(sorted_targets):
        candidates.append(sorted_targets[idx])
    if idx > 0:
        candidates.append(sorted_targets[idx - 1])
    return any(abs(value - c) <= TIMESTAMP_TOLERANCE_SECONDS for c in candidates)


def verify_completed_dir(
    completed_dir: Path, transcripts_dir: Path
) -> list[FileVerification]:
    """Verify every queue/completed/*.chunks.json. Doesn't move anything; just reports."""
    return [
        verify_chunks_file(p, transcripts_dir)
        for p in sorted(completed_dir.glob("*.chunks.json"))
    ]


def quarantine_invalid(
    verifications: list[FileVerification], failed_dir: Path
) -> int:
    """Move chunks files with errors to queue/failed/, with a sibling .verify-error.txt explaining why.

    Returns the number of files quarantined.
    """
    failed_dir.mkdir(parents=True, exist_ok=True)
    moved = 0
    for fv in verifications:
        if fv.is_valid:
            continue
        # Move the chunks file out of completed/
        target = failed_dir / fv.path.name
        shutil.move(str(fv.path), str(target))
        # Write a sibling error file
        report_path = failed_dir / f"{fv.video_id}.verify-error.txt"
        lines = [f"Verification failed for {fv.video_id} ({fv.chunk_count} chunks)\n"]
        for issue in fv.issues:
            loc = f"chunk[{issue.chunk_index}].{issue.field}" if issue.chunk_index >= 0 else f"<file>.{issue.field}"
            lines.append(f"  [{issue.severity:5s}] {loc}: {issue.message}\n")
        report_path.write_text("".join(lines), encoding="utf-8")
        moved += 1
    return moved
