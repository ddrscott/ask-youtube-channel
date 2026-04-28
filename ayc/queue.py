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


def augment_pending_for_gapfill(client: ApiClient) -> tuple[int, int]:
    """Mutate every queue/pending/<id>.json to include gap-fill metadata:

      * ``gap_fill: true``
      * ``existing_chunks``: list of {kind, start_seconds, end_seconds,
        question, topics} for chunks already in D1 — given to the agent so it
        knows what NOT to re-extract.
      * ``covered_ranges``: merged [(start, end), ...] in seconds.
      * ``gap_ranges``: list of [start, end, dur] tuples for any window of
        >=60 continuous seconds that no existing chunk overlaps.

    The chunker reads these and emits ONLY chunks that fall inside a gap
    range. The merge step's gap-fill mode then POSTs them with replace=False,
    so existing chunks (and Vectorize entries) are preserved and the run is
    purely additive.

    Returns (augmented, skipped). Skipped means the API didn't have chunks.
    """
    GAP_THRESHOLD = 60.0
    augmented = 0
    skipped = 0
    for path in sorted(PENDING_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            skipped += 1
            continue
        video_id = data.get("video_id")
        if not video_id:
            skipped += 1
            continue
        try:
            existing = client.list_video_chunks(video_id)
        except Exception:
            skipped += 1
            continue
        # Sort + merge overlapping ranges
        spans = sorted(
            [(float(c["start_seconds"]), float(c["end_seconds"])) for c in existing]
        )
        merged: list[tuple[float, float]] = []
        for s, e in spans:
            if merged and s <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], e))
            else:
                merged.append((s, e))
        # Compute gaps relative to the full duration.
        dur = float(data.get("duration_seconds") or 0)
        gaps: list[list[float]] = []
        prev = 0.0
        for s, e in merged:
            if s - prev >= GAP_THRESHOLD:
                gaps.append([round(prev, 2), round(s, 2), round(s - prev, 2)])
            prev = e
        if dur and dur - prev >= GAP_THRESHOLD:
            gaps.append([round(prev, 2), round(dur, 2), round(dur - prev, 2)])
        # Slim payload — drop the answer text since the agent shouldn't be
        # using it as a reference (it'd make the agent verbose). Keep
        # question + topics so it knows the topical territory already covered.
        existing_min = [
            {
                "kind": c["kind"],
                "start_seconds": float(c["start_seconds"]),
                "end_seconds": float(c["end_seconds"]),
                "question": c.get("question") or "",
                "topics": json.loads(c["topics"]) if c.get("topics") else [],
            }
            for c in existing
        ]
        data["gap_fill"] = True
        data["existing_chunks"] = existing_min
        data["covered_ranges"] = [[round(s, 2), round(e, 2)] for s, e in merged]
        data["gap_ranges"] = gaps
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        augmented += 1
    return augmented, skipped


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
            # Detect gap-fill mode by reading the pending file's gap_fill flag.
            # In that mode the merge is purely additive (replace=False) AND we
            # programmatically filter out any chunks that don't substantially
            # overlap a declared gap window. The chunker tends to do a fresh
            # full-pass when given the transcript, regardless of the gap_fill
            # instruction — this filter is the hard guarantee that the run
            # only ADDS chunks in genuinely uncovered territory.
            pending_file = PENDING_DIR / f"{video_id}.json"
            replace_mode = True
            gap_ranges: list[list[float]] = []
            if pending_file.exists():
                try:
                    pdata = json.loads(pending_file.read_text(encoding="utf-8"))
                    if pdata.get("gap_fill"):
                        replace_mode = False
                        gap_ranges = pdata.get("gap_ranges", [])
                except Exception:
                    pass
            if not replace_mode and gap_ranges:
                MIN_OVERLAP = 20.0  # seconds of chunk inside a gap to keep it
                kept: list[dict] = []
                for ch in payload_chunks:
                    s, e = ch["start_seconds"], ch["end_seconds"]
                    overlap_total = 0.0
                    for gs, ge, _ in gap_ranges:
                        overlap = max(0.0, min(e, ge) - max(s, gs))
                        overlap_total += overlap
                    if overlap_total >= MIN_OVERLAP:
                        kept.append(ch)
                payload_chunks = kept
            if payload_chunks:
                client.insert_chunks_bulk(video_id, payload_chunks, replace=replace_mode)
            videos += 1
            chunks += len(payload_chunks)
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
