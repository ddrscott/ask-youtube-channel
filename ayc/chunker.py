"""LLM chunker — extracts qa and objection chunks from a transcript in a single Opus 4.7 call."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

import anthropic
from pydantic import BaseModel, Field, ValidationError, field_validator

from .config import Config
from .db import insert_chunk, mark_video
from .prompts import CHUNKER_SYSTEM
from .transcripts import Transcript


CHUNK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "chunks": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "kind": {"type": "string", "enum": ["qa", "objection"]},
                    "start_seconds": {"type": "number"},
                    "end_seconds": {"type": "number"},
                    "question": {"type": "string"},
                    "answer": {"type": "string"},
                    "speaker": {"type": ["string", "null"]},
                    "topics": {"type": "array", "items": {"type": "string"}},
                    "confidence": {"type": "number"},
                },
                "required": [
                    "kind",
                    "start_seconds",
                    "end_seconds",
                    "question",
                    "answer",
                    "speaker",
                    "topics",
                    "confidence",
                ],
            },
        }
    },
    "required": ["chunks"],
}


class Chunk(BaseModel):
    kind: str
    start_seconds: float
    end_seconds: float
    question: str
    answer: str
    speaker: str | None = None
    topics: list[str] = Field(default_factory=list)
    confidence: float

    @field_validator("kind")
    @classmethod
    def _kind_ok(cls, v: str) -> str:
        if v not in {"qa", "objection"}:
            raise ValueError(f"invalid kind: {v}")
        return v


class ChunkerOutput(BaseModel):
    chunks: list[Chunk]


@dataclass
class ChunkerResult:
    video_id: str
    chunks: list[Chunk]
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int


def _format_transcript_for_prompt(transcript: Transcript) -> str:
    lines = []
    for seg in transcript.segments:
        # Single decimal place is enough; keeps the prompt compact.
        lines.append(f"[{seg.start:.1f}] {seg.text}")
    return "\n".join(lines)


def _clamp_to_transcript(chunks: list[Chunk], transcript: Transcript) -> list[Chunk]:
    """Clamp chunk timestamps to the actual transcript range, drop any that don't make sense."""
    if not transcript.segments:
        return []
    min_t = transcript.segments[0].start
    max_t = transcript.duration_seconds
    cleaned: list[Chunk] = []
    for c in chunks:
        if c.end_seconds < c.start_seconds:
            continue
        if c.end_seconds < min_t or c.start_seconds > max_t:
            continue
        c.start_seconds = max(min_t, c.start_seconds)
        c.end_seconds = min(max_t, c.end_seconds)
        cleaned.append(c)
    return cleaned


def chunk_video(
    cfg: Config,
    transcript: Transcript,
    *,
    max_tokens: int = 32000,
) -> ChunkerResult:
    """Run the chunker on a transcript. Returns the parsed chunks (does not persist)."""
    client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)

    transcript_block = _format_transcript_for_prompt(transcript)
    user_message = (
        f"Transcript of video `{transcript.video_id}`. Extract every qa and objection "
        f"moment per the rules. Use timestamps verbatim from the [seconds] markers.\n\n"
        f"```\n{transcript_block}\n```"
    )

    with client.messages.stream(
        model=cfg.chunker_model,
        max_tokens=max_tokens,
        system=[
            {
                "type": "text",
                "text": CHUNKER_SYSTEM,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        thinking={"type": "adaptive"},
        output_config={
            "format": {"type": "json_schema", "schema": CHUNK_SCHEMA},
            "effort": "high",
        },
        messages=[{"role": "user", "content": user_message}],
    ) as stream:
        final = stream.get_final_message()

    # The structured-output response: first text block contains valid JSON
    text = next((b.text for b in final.content if b.type == "text"), None)
    if not text:
        raise RuntimeError(
            f"chunker returned no text content for video {transcript.video_id} "
            f"(stop_reason={final.stop_reason})"
        )

    try:
        raw = json.loads(text)
        parsed = ChunkerOutput.model_validate(raw)
    except (json.JSONDecodeError, ValidationError) as e:
        raise RuntimeError(
            f"chunker returned invalid JSON for video {transcript.video_id}: {e}\n\n"
            f"Raw text (first 500 chars):\n{text[:500]}"
        ) from e

    cleaned = _clamp_to_transcript(parsed.chunks, transcript)

    usage = final.usage
    return ChunkerResult(
        video_id=transcript.video_id,
        chunks=cleaned,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
        cache_creation_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
    )


def persist_chunks(conn, result: ChunkerResult) -> int:
    """Persist a chunker result to the DB. Returns the number of chunks inserted."""
    # Delete any prior chunks for this video (idempotent re-chunking)
    conn.execute("DELETE FROM chunks WHERE video_id = ?", (result.video_id,))
    for c in result.chunks:
        chunk_id = uuid.uuid4().hex
        insert_chunk(
            conn,
            chunk_id=chunk_id,
            video_id=result.video_id,
            kind=c.kind,
            start_seconds=c.start_seconds,
            end_seconds=c.end_seconds,
            question=c.question,
            answer=c.answer,
            speaker=c.speaker,
            topics=c.topics,
            confidence=c.confidence,
        )
    mark_video(conn, result.video_id, "chunked", error=None)
    conn.commit()
    return len(result.chunks)
