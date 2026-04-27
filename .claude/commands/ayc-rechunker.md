---
description: Re-chunk previously-chunked videos under the latest ayc-chunker rules. Replaces old chunks (and Vectorize entries) atomically per video. Dispatches the ayc-chunker agent in parallel.
argument-hint: "[--form long|short|all] [--limit N] [--parallel K] [--video <id>]"
---

You are running the **AYC re-chunker**. This command re-processes videos that were already chunked under earlier rules so they get the latest chunker creed (currently: "no good chunk left behind", with explicit density floors and a coverage audit pass).

## When to use this

Use this when the chunker prompt at `.claude/agents/ayc-chunker.md` has been tuned and you want to apply the new rules to previously-processed videos. **It is destructive per video**: the old chunks for each re-processed video are deleted from D1 and Vectorize before the new chunks land. Run it intentionally, in batches, with a `--limit` you can review.

It is NOT for new videos — use `/ayc-runbook` for those.

## Why a separate runbook

The regular `/ayc-runbook` filters to `status='transcribed'` and skips anything already chunked. The re-chunker explicitly walks the `chunked` and `embedded` statuses. The merge step's existing `replace=True` semantics (in `chunks:bulk`) handle the per-video swap atomically:

1. Capture prior chunk IDs for the video
2. Delete from D1 in the same batch as the new inserts
3. Best-effort `VECTOR.deleteByIds(...)` after the D1 batch lands

The result: no duplicate chunks, no Vectorize orphans, no broken state if a single video fails (others continue).

## Argument parsing

Parse `$ARGUMENTS` for these flags. Use these defaults when not provided:

| Flag | Default | Meaning |
|------|---------|---------|
| `--form` | `long` | Restrict to `long`, `short`, or `all`. Long-form is where the under-extraction problem lives. |
| `--limit` | `10` | Max videos to re-chunk this invocation. **Keep modest** — full re-chunks of large catalogs should be done in deliberate batches you can review. `--limit 0` means no limit (use sparingly). |
| `--parallel` | `3` | Concurrent `ayc-chunker` agent dispatches. **Hard ceiling: 3.** |
| `--video` | (none) | Re-chunk a single specific video by id. Skips the bulk prepare step; just queues that one video. Useful for spot-checks. |

If `$ARGUMENTS` is empty, use all defaults.

## Pre-flight

1. **Confirm intent.** This deletes prior chunks. Tell the user what's about to happen — e.g. "About to re-chunk up to 10 long-form videos under the latest ayc-chunker rules. Their existing chunks will be replaced." If `--limit 0`, be especially explicit. Do NOT pause for confirmation if the user explicitly passed `--limit` or `--video` (intent is already clear from the command); only confirm when the user typed bare `/ayc-rechunker` and we're about to consume budget on the default 10-video batch.

2. **Verify the agent is loaded** — the named `ayc-chunker` subagent_type must be available. If not, tell the user to restart Claude Code. **Do not** fall back to `general-purpose`.

3. **Check `ayc status`.** Note the current chunk count so you can report deltas at the end.

4. **Drain any existing queue/pending or queue/completed first.** If `ls queue/pending/*.json` or `ls queue/completed/*.chunks.json` is non-empty, tell the user — they may have an in-flight regular ingest. Do not proceed; the user should run `/ayc-runbook --phase merge` to drain it first.

## Phase: prepare

If `--video <id>` was passed, skip this phase — handle it inline (see "Single-video mode" below).

Otherwise: `uv run ayc queue prepare --rechunk --form <form> --limit <limit>`.

This walks the API for videos at `chunked` and `embedded` statuses (in that order — chunked-but-not-yet-embedded ones first since they're least invested) and writes a `queue/pending/<id>.json` per video.

## Phase: chunk (the agent loop)

Identical to the regular runbook's chunk phase:

1. List pending files: `Bash: ls queue/pending/*.json | head -<effective-limit>`.
2. If empty, say so and skip to merge.
3. **Dispatch in batches of `--parallel` (capped at 3)**. For each batch, send a single message containing N `Agent` tool calls, all with `subagent_type: "ayc-chunker"` and `prompt: "Process the file <ABSOLUTE-PATH-to-pending-file>"`. Wait for the entire batch before dispatching the next.
4. After each batch, log the per-file result lines. Surface failures but continue.
5. Cap `--parallel` at 3 and tell the user if their input was clamped.

The agent reads its own spec at `.claude/agents/ayc-chunker.md` — that file is the chunker creed. Don't paraphrase it in your dispatch prompt.

## Phase: verify + merge

Run: `uv run ayc queue merge`.

This calls `ayc queue verify --quarantine` internally first, then POSTs each chunks file to `/internal/chunks:bulk` with `replace=true` (the Python client's default). Each POST atomically:

- Captures the prior chunk IDs for the video (D1 lookup)
- Stages a `DELETE FROM chunks WHERE video_id = ?`
- Stages all new `INSERT INTO chunks ...`
- Stages the `UPDATE videos SET ingest_status='chunked'`
- Commits the D1 batch
- Best-effort `VECTOR.deleteByIds(prior_ids)`

If verify quarantines anything, those videos keep their old chunks (because the merge skips quarantined files entirely). Surface the quarantine count.

## Phase: embed

Run: `uv run ayc embed`. Embeds the new chunks via OpenAI text-embedding-3-small, upserts to Vectorize, rolls each video to `embedded` once its last chunk lands.

## Single-video mode (`--video <id>`)

When the user passes `--video <id>`, skip the bulk prepare phase and write a single pending file:

```bash
uv run python -c "
from ayc.config import Config
from ayc.db import ApiClient
from ayc.queue import prepare_single
cfg = Config.load()
with ApiClient(cfg) as client:
    ok = prepare_single(client, '<id>')
    print('wrote pending file' if ok else 'video not found or transcript fetch failed')
"
```

If `prepare_single` returns False, tell the user and stop. Otherwise dispatch one `ayc-chunker` agent on `queue/pending/<id>.json`, then run `uv run ayc queue merge` and `uv run ayc embed` as usual.

## End-of-run summary

1. Show final `uv run ayc status`.
2. Compare chunk count to the pre-run total: `Δ chunks: +N (was X, now Y)`.
3. Note any quarantines and where to find them.
4. Note coverage% if the chunker reported it (the new creed's audit step does).

## Important guidance

- **This is destructive per video.** Don't run unbounded (`--limit 0`) without the user explicitly asking for it.
- **Don't do the chunking work yourself.** Always dispatch `ayc-chunker`. The whole point of this runbook is to keep chunking on the Claude Code subscription, not on your context.
- **If verify quarantines a video, leave its old chunks in place.** Don't try to "clean up" — the old chunks are at least known-shape data; an empty index entry would be worse.
- **Failure modes**:
  - Agent malformed JSON → quarantined → old chunks preserved → next run can retry.
  - Vectorize cleanup transient failure → D1 is correct; orphans are harmless (browse JOIN filters them) → the next vector cleanup pass for that video will catch them.

## Example invocations

- `/ayc-rechunker` — re-chunk up to 10 long-form videos under the latest rules.
- `/ayc-rechunker --limit 50` — bigger batch.
- `/ayc-rechunker --video sTbtdokaRX0` — re-chunk one specific video.
- `/ayc-rechunker --form short --limit 25` — re-process shorts.

## User args

$ARGUMENTS
