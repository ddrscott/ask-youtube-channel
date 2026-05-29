"""Model battle harness for the AYC chunker.

Routes every candidate through one Cloudflare AI Gateway endpoint
(`https://gateway.ai.cloudflare.com/v1/<acct>/ayc/compat/chat/completions`),
giving us unified observability + a single auth token (`CF_AI_GATEWAY_TOKEN`).

Usage:
    uv run python eval/battle.py --video a7j1hYLe3dQ
    uv run python eval/battle.py --video a7j1hYLe3dQ --only grok/grok-4.3
    uv run python eval/battle.py --video a7j1hYLe3dQ --skip anthropic/claude-sonnet-4-6
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from rich.console import Console
from rich.table import Table

REPO_ROOT = Path(__file__).resolve().parent.parent
SYSTEM_PROMPT = (REPO_ROOT / "eval" / "system_prompt.md").read_text()

ACCOUNT_ID = "0223b96fe77599b23ff8ec7fcd32e2f1"
GATEWAY_NAME = "ayc"
GATEWAY_URL = (
    f"https://gateway.ai.cloudflare.com/v1/{ACCOUNT_ID}/{GATEWAY_NAME}/compat/chat/completions"
)


# ── Pricing (per million tokens) ────────────────────────────────────────────
# Used as fallback when the gateway response doesn't include cost.
PRICING: dict[str, tuple[float, float]] = {
    "google-ai-studio/gemini-3-flash-preview": (0.50, 3.00),
    "grok/grok-4.3":                           (1.25, 2.50),
    "anthropic/claude-haiku-4-5":              (1.00, 5.00),
    "anthropic/claude-sonnet-4-6":             (3.00, 15.00),
}


@dataclass
class CandidateResult:
    model: str
    ok: bool
    error: str | None = None
    raw_text: str = ""
    parsed: dict[str, Any] | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    cached_in: int = 0
    reasoning_tokens: int = 0
    latency_s: float = 0.0
    cost_usd: float = 0.0
    cost_source: str = ""  # "gateway" if reported, "computed" if derived from PRICING
    chunks: int = 0
    qa: int = 0
    objection: int = 0
    coverage_pct: float = 0.0
    mean_confidence: float = 0.0
    schema_errors: list[str] = field(default_factory=list)


def call_gateway(model: str, user_content: str, *, timeout: float = 600.0) -> dict[str, Any]:
    token = os.environ.get("CF_AI_GATEWAY_TOKEN")
    if not token:
        raise RuntimeError("CF_AI_GATEWAY_TOKEN env var not set")
    # Per-provider output-limit param. Empirically:
    #   - Anthropic via compat caps at 1024 if `max_tokens` is used, and now
    #     rejects having both `max_tokens` and `max_completion_tokens`. Use
    #     only `max_completion_tokens` for Anthropic.
    #   - Google Gemini 3 Flash via compat truncates mid-stream when given
    #     >= 32000 but completes when given 16000.
    #   - Grok handles `max_tokens` fine.
    max_out = 32000 if model.startswith("anthropic/") else 16000
    body: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
        "temperature": 0.2,
    }
    if model.startswith("anthropic/"):
        body["max_completion_tokens"] = max_out
    else:
        body["max_tokens"] = max_out
    # JSON-mode hint where supported — Anthropic ignores it, Grok and Gemini honor it.
    body["response_format"] = {"type": "json_object"}
    r = httpx.post(
        GATEWAY_URL,
        json=body,
        headers={
            "cf-aig-authorization": f"Bearer {token}",
            "cf-aig-skip-cache": "true",
            "Content-Type": "application/json",
        },
        timeout=timeout,
    )
    r.raise_for_status()
    return r.json()


def extract_response(resp: dict[str, Any]) -> tuple[str, dict[str, int]]:
    """Pull text + token counts from an OpenAI-compat response."""
    text = ""
    choices = resp.get("choices") or []
    if choices:
        text = choices[0].get("message", {}).get("content", "") or choices[0].get("text", "")
    usage = resp.get("usage") or {}
    tok_in = int(usage.get("prompt_tokens") or 0)
    tok_out = int(usage.get("completion_tokens") or 0)
    cached = int((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
    reasoning = int((usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0)
    # Cloudflare AI Gateway reports cost in micro-USD (ticks of 1e-6).
    cost_ticks = usage.get("cost_in_usd_ticks") or resp.get("cost_in_usd_ticks") or 0
    return text or "", {
        "tokens_in": tok_in,
        "tokens_out": tok_out,
        "cached_in": cached,
        "reasoning_tokens": reasoning,
        "cost_ticks": int(cost_ticks),
    }


def strip_json_fences(s: str) -> str:
    s = s.strip()
    if s.startswith("```"):
        # ```json\n...\n``` or ```\n...\n```
        s = s.split("\n", 1)[1] if "\n" in s else s[3:]
        if s.endswith("```"):
            s = s[:-3]
    return s.strip()


def coverage_pct(chunks: list[dict[str, Any]], duration: float) -> float:
    if duration <= 0 or not chunks:
        return 0.0
    spans = sorted(
        (float(c.get("start_seconds", 0.0)), float(c.get("end_seconds", 0.0))) for c in chunks
    )
    merged: list[list[float]] = []
    for s, e in spans:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    covered = sum(e - s for s, e in merged)
    return round(100.0 * covered / duration, 1)


def validate_chunks(parsed: dict[str, Any]) -> list[str]:
    """Return list of schema errors; empty == clean."""
    errs: list[str] = []
    if not isinstance(parsed, dict):
        return ["top-level not an object"]
    chunks = parsed.get("chunks")
    if not isinstance(chunks, list):
        return ["'chunks' missing or not array"]
    for i, c in enumerate(chunks):
        if not isinstance(c, dict):
            errs.append(f"chunk[{i}] not object")
            continue
        for key in ("kind", "start_seconds", "end_seconds", "question", "answer", "topics", "confidence"):
            if key not in c:
                errs.append(f"chunk[{i}].{key} missing")
        if c.get("kind") not in ("qa", "objection"):
            errs.append(f"chunk[{i}].kind invalid: {c.get('kind')!r}")
        conf = c.get("confidence")
        if not isinstance(conf, (int, float)):
            errs.append(f"chunk[{i}].confidence not numeric: {conf!r}")
        elif not 0.0 <= conf <= 1.0:
            errs.append(f"chunk[{i}].confidence out of range: {conf}")
        if not isinstance(c.get("topics"), list):
            errs.append(f"chunk[{i}].topics not array")
    return errs


def candidates() -> list[str]:
    return [
        "google-ai-studio/gemini-3-flash-preview",
        "grok/grok-4.3",
        "anthropic/claude-haiku-4-5",
        "anthropic/claude-sonnet-4-6",
    ]


def run_candidate(model: str, pending: dict[str, Any]) -> CandidateResult:
    res = CandidateResult(model=model, ok=False)
    user_content = json.dumps(pending, separators=(",", ":"))
    started = time.monotonic()
    try:
        resp = call_gateway(model, user_content)
    except httpx.HTTPStatusError as e:
        res.latency_s = time.monotonic() - started
        res.error = f"HTTP {e.response.status_code}: {e.response.text[:300]}"
        return res
    except Exception as e:
        res.latency_s = time.monotonic() - started
        res.error = f"{type(e).__name__}: {e}"
        return res

    res.latency_s = time.monotonic() - started
    text, u = extract_response(resp)
    res.raw_text = text
    res.tokens_in = u["tokens_in"]
    res.tokens_out = u["tokens_out"]
    res.cached_in = u["cached_in"]
    res.reasoning_tokens = u["reasoning_tokens"]

    if u["cost_ticks"] > 0:
        # AI Gateway reports cost as integer "ticks" of 1e-10 USD (not micro-USD).
        # Verified against published Grok 4.3 rates: 2,818,500 ticks ≈ $0.000282
        # for a 133-input + 100-output call, which matches $1.25/$2.50 per M.
        res.cost_usd = round(u["cost_ticks"] / 10_000_000_000, 6)
        res.cost_source = "gateway"
    else:
        rate_in, rate_out = PRICING.get(model, (0.0, 0.0))
        # Include reasoning tokens in output cost — they bill as output for Grok.
        billable_out = res.tokens_out + res.reasoning_tokens
        res.cost_usd = round(
            res.tokens_in * rate_in / 1_000_000 + billable_out * rate_out / 1_000_000,
            6,
        )
        res.cost_source = "computed"

    if not text:
        res.error = "empty response"
        return res

    cleaned = strip_json_fences(text)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as e:
        res.error = f"JSON decode failed: {e}"
        return res

    res.parsed = parsed
    res.schema_errors = validate_chunks(parsed)
    chunks = parsed.get("chunks", []) if isinstance(parsed, dict) else []
    res.chunks = len(chunks)
    res.qa = sum(1 for c in chunks if c.get("kind") == "qa")
    res.objection = sum(1 for c in chunks if c.get("kind") == "objection")
    confs = [
        c.get("confidence")
        for c in chunks
        if isinstance(c.get("confidence"), (int, float))
    ]
    res.mean_confidence = round(sum(confs) / len(confs), 2) if confs else 0.0
    res.coverage_pct = coverage_pct(chunks, pending.get("duration_seconds", 0.0))
    res.ok = not res.schema_errors
    return res


def render_scorecard(results: list[CandidateResult], pending: dict[str, Any]) -> None:
    console = Console()
    duration = pending.get("duration_seconds", 0)
    console.print(
        f"\n[bold]Battle for {pending.get('video_id')}[/bold] · "
        f"{duration:.0f}s ({duration/60:.1f}min) · {len(pending.get('segments', []))} segments\n"
    )

    table = Table(show_header=True, header_style="bold")
    table.add_column("Model", style="cyan")
    table.add_column("OK", justify="center")
    table.add_column("Chunks", justify="right")
    table.add_column("qa/obj", justify="right")
    table.add_column("Cov%", justify="right")
    table.add_column("MeanConf", justify="right")
    table.add_column("Tok in/out", justify="right")
    table.add_column("Reason", justify="right")
    table.add_column("$", justify="right")
    table.add_column("Latency", justify="right")
    table.add_column("Notes", style="dim")

    for r in results:
        ok = "✓" if r.ok else ("✗" if r.error else "⚠")
        notes = ""
        if r.error:
            notes = r.error[:60]
        elif r.schema_errors:
            notes = f"{len(r.schema_errors)} schema err"
        elif r.cost_source:
            notes = r.cost_source
        table.add_row(
            r.model,
            ok,
            str(r.chunks),
            f"{r.qa}/{r.objection}",
            f"{r.coverage_pct}%",
            f"{r.mean_confidence:.2f}",
            f"{r.tokens_in}/{r.tokens_out}",
            str(r.reasoning_tokens) if r.reasoning_tokens else "—",
            f"${r.cost_usd:.4f}",
            f"{r.latency_s:.1f}s",
            notes,
        )

    console.print(table)
    console.print()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True, help="video id (must have eval/inputs/<id>.pending.json)")
    ap.add_argument("--only", action="append", default=None, help="run only these model slugs")
    ap.add_argument("--skip", action="append", default=None, help="skip these model slugs")
    args = ap.parse_args()

    pending_path = REPO_ROOT / "eval" / "inputs" / f"{args.video}.pending.json"
    if not pending_path.exists():
        print(f"Missing input: {pending_path}", file=sys.stderr)
        return 2
    pending = json.loads(pending_path.read_text())

    out_dir = REPO_ROOT / "eval" / "results" / args.video
    out_dir.mkdir(parents=True, exist_ok=True)

    targets = candidates()
    if args.only:
        targets = [t for t in targets if t in args.only]
    if args.skip:
        targets = [t for t in targets if t not in args.skip]

    console = Console()
    results: list[CandidateResult] = []
    for model in targets:
        console.print(f"[bold]→ {model}[/bold]…")
        res = run_candidate(model, pending)
        results.append(res)
        slug = model.replace("/", "_").replace("@", "")
        (out_dir / f"{slug}.raw.txt").write_text(res.raw_text)
        if res.parsed is not None:
            (out_dir / f"{slug}.parsed.json").write_text(json.dumps(res.parsed, indent=2))
        meta = {
            "model": model,
            "ok": res.ok,
            "error": res.error,
            "chunks": res.chunks,
            "qa": res.qa,
            "objection": res.objection,
            "coverage_pct": res.coverage_pct,
            "mean_confidence": res.mean_confidence,
            "tokens_in": res.tokens_in,
            "tokens_out": res.tokens_out,
            "cached_in": res.cached_in,
            "reasoning_tokens": res.reasoning_tokens,
            "cost_usd": res.cost_usd,
            "cost_source": res.cost_source,
            "latency_s": res.latency_s,
            "schema_errors": res.schema_errors,
        }
        (out_dir / f"{slug}.meta.json").write_text(json.dumps(meta, indent=2))
        if res.error:
            console.print(f"  [red]✗[/red] {res.error}")
        else:
            console.print(
                f"  [green]✓[/green] {res.chunks} chunks · ${res.cost_usd:.4f} · {res.latency_s:.1f}s"
            )

    render_scorecard(results, pending)
    console.print(f"Saved raw + parsed outputs to: [cyan]{out_dir}/[/cyan]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
