"""Arc-merge post-processor.

Takes a chunker output (from battle.py or mapreduce.py) and consolidates
adjacent same-kind chunks into the longer-arc shape that the Opus subagent
produces. Goal: keep Haiku's comprehensive extraction but restyle to match
the existing 12k-chunk index.

Rules:
  - Sort chunks by start_seconds.
  - Walk through; if the next chunk has the same kind AND its start is within
    GAP_SECONDS of the previous chunk's end, merge them.
  - Merged span = (min start, max end).
  - Merged question = " | ".join(unique questions) — preserves searchability
    while showing the moment is multi-part.
  - Merged answer = " | ".join(unique answers).
  - Topics = union (dedup case-insensitive).
  - Confidence = mean of source confidences.
  - Speaker = first non-null source speaker.

Usage:
    uv run python eval/arc_merge.py --in eval/results/<id>/<file>.parsed.json --out -
    uv run python eval/arc_merge.py --in eval/results/a7j1hYLe3dQ/anthropic_claude-haiku-4-5.mapreduce.parsed.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

GAP_SECONDS = 15.0  # max gap between end of prev chunk and start of next to merge


def merge_chunks(chunks: list[dict]) -> list[dict]:
    if not chunks:
        return []
    sorted_chunks = sorted(chunks, key=lambda c: c.get("start_seconds", 0.0))
    merged: list[dict] = []
    for c in sorted_chunks:
        if not merged:
            merged.append(dict(c))
            continue
        prev = merged[-1]
        same_kind = c.get("kind") == prev.get("kind")
        gap = c.get("start_seconds", 0.0) - prev.get("end_seconds", 0.0)
        if same_kind and gap <= GAP_SECONDS:
            merged[-1] = _merge_two(prev, c)
        else:
            merged.append(dict(c))
    return merged


def _merge_two(a: dict, b: dict) -> dict:
    qs = _join_unique(a.get("question"), b.get("question"))
    ans = _join_unique(a.get("answer"), b.get("answer"))
    topics_a = a.get("topics") or []
    topics_b = b.get("topics") or []
    topics = _dedup_case_insensitive(topics_a + topics_b)
    conf_a = a.get("confidence") or 0.0
    conf_b = b.get("confidence") or 0.0
    confs = [v for v in (conf_a, conf_b) if isinstance(v, (int, float))]
    conf = round(sum(confs) / len(confs), 2) if confs else 0.0
    speaker = a.get("speaker") or b.get("speaker") or None
    return {
        "kind": a["kind"],
        "start_seconds": min(a["start_seconds"], b["start_seconds"]),
        "end_seconds": max(a["end_seconds"], b["end_seconds"]),
        "question": qs,
        "answer": ans,
        "speaker": speaker,
        "topics": topics,
        "confidence": conf,
    }


def _join_unique(s1, s2) -> str:
    s1 = (s1 or "").strip()
    s2 = (s2 or "").strip()
    if not s1:
        return s2
    if not s2 or s1 == s2:
        return s1
    # If one contains the other, prefer the longer
    if s2 in s1:
        return s1
    if s1 in s2:
        return s2
    return f"{s1} | {s2}"


def _dedup_case_insensitive(items: list[str]) -> list[str]:
    seen = set()
    out = []
    for x in items:
        if not x:
            continue
        key = x.lower().strip()
        if key not in seen:
            seen.add(key)
            out.append(x)
    return out


def coverage_pct(chunks: list[dict], duration: float) -> float:
    if duration <= 0 or not chunks:
        return 0.0
    spans = sorted((float(c["start_seconds"]), float(c["end_seconds"])) for c in chunks)
    merged: list[list[float]] = []
    for s, e in spans:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    covered = sum(e - s for s, e in merged)
    return round(100.0 * covered / duration, 1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", required=True)
    ap.add_argument("--out", default=None, help="output path; '-' for stdout; default: <in>.arcmerged.json")
    ap.add_argument("--duration", type=float, default=None, help="video duration for coverage calc")
    args = ap.parse_args()

    inp = Path(args.inp)
    d = json.loads(inp.read_text())
    src_chunks = d.get("chunks", [])

    merged = merge_chunks(src_chunks)

    out_doc = {
        "schema_version": d.get("schema_version", 1),
        "video_id": d.get("video_id"),
        "chunks": merged,
    }

    duration = args.duration
    if duration is None:
        # Try to find the pending file for this video
        pending = Path(__file__).resolve().parent / "inputs" / f"{d.get('video_id')}.pending.json"
        if pending.exists():
            duration = json.loads(pending.read_text()).get("duration_seconds", 0.0)

    src_qa = sum(1 for c in src_chunks if c.get("kind") == "qa")
    src_obj = sum(1 for c in src_chunks if c.get("kind") == "objection")
    new_qa = sum(1 for c in merged if c.get("kind") == "qa")
    new_obj = sum(1 for c in merged if c.get("kind") == "objection")
    src_cov = coverage_pct(src_chunks, duration) if duration else "—"
    new_cov = coverage_pct(merged, duration) if duration else "—"

    print(f"Source: {len(src_chunks)} chunks (qa={src_qa} obj={src_obj}, coverage={src_cov}%)", file=sys.stderr)
    print(f"Merged: {len(merged)} chunks (qa={new_qa} obj={new_obj}, coverage={new_cov}%)", file=sys.stderr)
    print(f"Reduced {len(src_chunks) - len(merged)} adjacent same-kind chunks into arcs", file=sys.stderr)

    if args.out == "-":
        json.dump(out_doc, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        outp = Path(args.out) if args.out else inp.with_suffix(".arcmerged.json")
        outp.write_text(json.dumps(out_doc, indent=2))
        print(f"Wrote: {outp}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
