"""Map-reduce chunker runner.

Splits a transcript into overlapping windows (~6k tokens of content each),
runs the chunker model on each window concurrently, then deduplicates by
timestamp proximity. The goal: keep attention dense so the model can find
every needle in the haystack instead of summarizing.

Usage:
    uv run python eval/mapreduce.py --video a7j1hYLe3dQ --model anthropic/claude-haiku-4-5
    uv run python eval/mapreduce.py --video x1iNGaNsaeo --model anthropic/claude-haiku-4-5
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from rich.console import Console
from rich.table import Table

REPO_ROOT = Path(__file__).resolve().parent.parent
SYSTEM_PROMPT = (REPO_ROOT / "eval" / "system_prompt.md").read_text()

ACCOUNT_ID = "0223b96fe77599b23ff8ec7fcd32e2f1"
GATEWAY_URL = f"https://gateway.ai.cloudflare.com/v1/{ACCOUNT_ID}/ayc/compat/chat/completions"

PRICING = {
    "google-ai-studio/gemini-3-flash-preview": (0.50, 3.00),
    "grok/grok-4.3":                           (1.25, 2.50),
    "anthropic/claude-haiku-4-5":              (1.00, 5.00),
    "anthropic/claude-sonnet-4-6":             (3.00, 15.00),
}

WINDOW_TARGET_CHARS = 24_000   # ≈ 6k tokens of transcript per window
OVERLAP_SECONDS = 30.0
DEDUP_GAP_SECONDS = 15.0


@dataclass
class WindowResult:
    index: int
    start_t: float
    end_t: float
    segments: int
    chunks: list[dict] | None
    tokens_in: int
    tokens_out: int
    cost_usd: float
    latency_s: float
    error: str | None


def split_into_windows(pending: dict) -> list[dict]:
    """Return a list of windows, each shaped like a pending file with a subset of segments."""
    segments = pending["segments"]
    if not segments:
        return []

    windows: list[list[dict]] = []
    cur: list[dict] = []
    cur_chars = 0

    for s in segments:
        cur.append(s)
        cur_chars += len(s["text"])
        if cur_chars >= WINDOW_TARGET_CHARS:
            windows.append(cur)
            # Set up overlap for the next window
            end_t = cur[-1]["end"]
            overlap_start = end_t - OVERLAP_SECONDS
            cur = [s2 for s2 in cur if s2["start"] >= overlap_start]
            cur_chars = sum(len(s2["text"]) for s2 in cur)

    if cur:
        if windows and cur[0]["start"] >= windows[-1][-1]["end"] - OVERLAP_SECONDS and len(cur) <= 3:
            # Trailing scrap — fold into the last window
            windows[-1].extend(s for s in cur if s["start"] > windows[-1][-1]["start"])
        else:
            windows.append(cur)

    out = []
    for i, segs in enumerate(windows):
        out.append({
            "schema_version": 1,
            "video_id": pending["video_id"],
            "title": pending.get("title", ""),
            "form": pending.get("form", "long"),
            "duration_seconds": pending["duration_seconds"],
            "transcript_source": pending.get("transcript_source", "auto"),
            "window_index": i,
            "window_of": len(windows),
            "window_start": segs[0]["start"],
            "window_end": segs[-1]["end"],
            "segments": segs,
        })
    return out


def window_user_prompt(window: dict) -> str:
    """Frame the window so the model knows it's seeing a slice."""
    header = (
        f"This is window {window['window_index']+1} of {window['window_of']} "
        f"from a longer transcript (full duration {window['duration_seconds']:.0f}s).\n"
        f"This window covers t={window['window_start']:.1f}s to t={window['window_end']:.1f}s.\n"
        f"Extract every Q&A and objection-rebuttal moment that occurs WITHIN this window. "
        f"Use only timestamps from the segments below.\n\n"
    )
    return header + json.dumps({
        "schema_version": window["schema_version"],
        "video_id": window["video_id"],
        "duration_seconds": window["duration_seconds"],
        "segments": window["segments"],
    }, separators=(",", ":"))


async def call_one(client: httpx.AsyncClient, model: str, window: dict) -> WindowResult:
    body: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": window_user_prompt(window)},
        ],
        "temperature": 0.2,
    }
    if model.startswith("anthropic/"):
        body["max_completion_tokens"] = 16_000
    else:
        body["max_tokens"] = 16_000
        body["response_format"] = {"type": "json_object"}

    token = os.environ["CF_AI_GATEWAY_TOKEN"]
    started = time.monotonic()
    try:
        r = await client.post(
            GATEWAY_URL,
            json=body,
            headers={
                "cf-aig-authorization": f"Bearer {token}",
                "cf-aig-skip-cache": "true",
                "Content-Type": "application/json",
            },
            timeout=600.0,
        )
        r.raise_for_status()
        resp = r.json()
    except httpx.HTTPStatusError as e:
        return WindowResult(
            index=window["window_index"],
            start_t=window["window_start"], end_t=window["window_end"],
            segments=len(window["segments"]),
            chunks=None, tokens_in=0, tokens_out=0, cost_usd=0.0,
            latency_s=time.monotonic() - started,
            error=f"HTTP {e.response.status_code}: {e.response.text[:200]}",
        )
    latency = time.monotonic() - started

    text = ""
    choices = resp.get("choices") or []
    if choices:
        text = choices[0].get("message", {}).get("content", "") or ""
    usage = resp.get("usage") or {}
    tin = int(usage.get("prompt_tokens") or 0)
    tout = int(usage.get("completion_tokens") or 0)
    rate_in, rate_out = PRICING.get(model, (0.0, 0.0))
    cost = round(tin * rate_in / 1_000_000 + tout * rate_out / 1_000_000, 6)

    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
    try:
        parsed = json.loads(text.strip())
        chunks = parsed.get("chunks", []) if isinstance(parsed, dict) else []
    except json.JSONDecodeError as e:
        return WindowResult(
            index=window["window_index"],
            start_t=window["window_start"], end_t=window["window_end"],
            segments=len(window["segments"]),
            chunks=None, tokens_in=tin, tokens_out=tout, cost_usd=cost,
            latency_s=latency, error=f"JSON decode: {e}",
        )

    return WindowResult(
        index=window["window_index"],
        start_t=window["window_start"], end_t=window["window_end"],
        segments=len(window["segments"]),
        chunks=chunks, tokens_in=tin, tokens_out=tout, cost_usd=cost,
        latency_s=latency, error=None,
    )


async def run_all_windows(model: str, windows: list[dict]) -> list[WindowResult]:
    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(*(call_one(client, model, w) for w in windows))
    return list(results)


def dedup_chunks(all_chunks: list[dict]) -> tuple[list[dict], int]:
    """Sort by start_seconds; collapse chunks within DEDUP_GAP_SECONDS of an earlier one.
    When collapsed, keep the chunk with the higher confidence (ties → keep first).
    Returns (deduped_chunks, num_removed).
    """
    if not all_chunks:
        return [], 0
    chunks = sorted(all_chunks, key=lambda c: c.get("start_seconds", 0))
    kept: list[dict] = []
    removed = 0
    for c in chunks:
        if not kept:
            kept.append(c)
            continue
        prev = kept[-1]
        same_kind = c.get("kind") == prev.get("kind")
        close_start = abs(c.get("start_seconds", 0) - prev.get("start_seconds", 0)) <= DEDUP_GAP_SECONDS
        if same_kind and close_start:
            # Same moment — keep higher confidence
            if (c.get("confidence") or 0) > (prev.get("confidence") or 0):
                kept[-1] = c
            removed += 1
        else:
            kept.append(c)
    return kept, removed


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
    ap.add_argument("--video", required=True)
    ap.add_argument("--model", default="anthropic/claude-haiku-4-5")
    args = ap.parse_args()

    pending_path = REPO_ROOT / "eval" / "inputs" / f"{args.video}.pending.json"
    if not pending_path.exists():
        print(f"Missing input: {pending_path}", file=sys.stderr)
        return 2
    pending = json.loads(pending_path.read_text())

    console = Console()
    windows = split_into_windows(pending)
    console.print(
        f"[bold]Map-reduce on {args.video}[/bold] · {args.model} · "
        f"split into {len(windows)} windows (~{WINDOW_TARGET_CHARS//1000}k chars each, "
        f"{OVERLAP_SECONDS}s overlap)"
    )
    for w in windows:
        console.print(
            f"  window {w['window_index']+1}: t={w['window_start']:.0f}-{w['window_end']:.0f}s "
            f"({len(w['segments'])} segments)"
        )

    results = asyncio.run(run_all_windows(args.model, windows))
    total_cost = sum(r.cost_usd for r in results)
    total_tin = sum(r.tokens_in for r in results)
    total_tout = sum(r.tokens_out for r in results)
    max_latency = max(r.latency_s for r in results)

    console.print()
    table = Table(show_header=True, header_style="bold")
    table.add_column("Window", justify="right")
    table.add_column("Range", justify="right")
    table.add_column("Chunks", justify="right")
    table.add_column("Tok in/out", justify="right")
    table.add_column("$", justify="right")
    table.add_column("Latency", justify="right")
    table.add_column("Error", style="dim")
    all_chunks: list[dict] = []
    for r in results:
        table.add_row(
            str(r.index + 1),
            f"{r.start_t:.0f}-{r.end_t:.0f}",
            str(len(r.chunks)) if r.chunks is not None else "—",
            f"{r.tokens_in}/{r.tokens_out}",
            f"${r.cost_usd:.4f}",
            f"{r.latency_s:.1f}s",
            (r.error or "")[:50],
        )
        if r.chunks:
            all_chunks.extend(r.chunks)
    console.print(table)

    deduped, removed = dedup_chunks(all_chunks)
    duration = pending.get("duration_seconds", 0.0)
    qa = sum(1 for c in deduped if c.get("kind") == "qa")
    obj = sum(1 for c in deduped if c.get("kind") == "objection")
    cov = coverage_pct(deduped, duration)
    confs = [c["confidence"] for c in deduped if isinstance(c.get("confidence"), (int, float))]
    mean_conf = round(sum(confs) / len(confs), 2) if confs else 0.0

    console.print()
    console.print(f"[bold]Merge:[/bold] {len(all_chunks)} raw → {len(deduped)} unique ({removed} merged)")
    console.print(f"[bold]Result:[/bold] {len(deduped)} chunks · qa={qa} obj={obj} · "
                  f"{cov}% coverage · mean conf {mean_conf}")
    console.print(f"[bold]Cost:[/bold] ${total_cost:.4f} ({total_tin} in / {total_tout} out tokens) · "
                  f"wall-clock {max_latency:.1f}s")

    out_dir = REPO_ROOT / "eval" / "results" / args.video
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = args.model.replace("/", "_") + ".mapreduce"
    (out_dir / f"{slug}.parsed.json").write_text(
        json.dumps({"schema_version": 1, "video_id": args.video, "chunks": deduped}, indent=2)
    )
    (out_dir / f"{slug}.meta.json").write_text(json.dumps({
        "model": args.model,
        "windows": len(windows),
        "raw_chunks": len(all_chunks),
        "deduped": len(deduped),
        "removed_dups": removed,
        "qa": qa, "objection": obj,
        "coverage_pct": cov,
        "mean_confidence": mean_conf,
        "tokens_in": total_tin,
        "tokens_out": total_tout,
        "cost_usd": round(total_cost, 6),
        "max_latency_s": max_latency,
    }, indent=2))
    console.print(f"Saved to: [cyan]{out_dir}/{slug}.parsed.json[/cyan]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
