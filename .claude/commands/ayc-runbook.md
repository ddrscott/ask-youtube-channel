---
description: End-to-end Ask-YouTube-Channel pipeline. Resumes idempotently from current state. Dispatches the ayc-chunker agent in parallel for chunking; uses the Python CLI for everything else.
argument-hint: "[--form long|short|all] [--limit N] [--phase all|status|transcribe|prepare|chunk|merge|embed] [--parallel K]"
---

You are running the **Ask-YouTube-Channel (AYC) runbook**. This command is the single source of truth for ingesting and processing a YouTube channel into a queryable clip index.

## Why this command exists

The AYC pipeline has Python steps (deterministic — yt-dlp, sqlite, OpenAI embeddings, Anthropic API for query-time reformulation/synthesis) and an LLM-shaped step (chunking — extracting Q&A and objection-rebuttal moments from transcripts).

The chunking step is the recurring expensive operation. Running it as a Claude Code subagent shifts that cost from API tokens to the user's Claude Code subscription. The `ayc-chunker` agent (defined at `.claude/agents/ayc-chunker.md`) does this work.

This runbook orchestrates: figures out where the pipeline is, runs whichever phases need to run, dispatches agents in parallel, surfaces results.

## Pipeline phases

```
status      → show current state of DB and queue
transcribe  → uv run ayc transcripts (yt-dlp pulls captions, writes data/transcripts/)
prepare     → uv run ayc queue prepare (writes queue/pending/<id>.json per video)
chunk       → dispatch ayc-chunker agent per pending file (parallel, batched)
merge       → uv run ayc queue merge (inserts chunks into SQLite, archives queue file)
embed       → uv run ayc embed (OpenAI text-embedding-3-small, batched)
all         → run every phase in order (default)
```

## Argument parsing

Parse `$ARGUMENTS` for these flags. Use these defaults when not provided:

| Flag | Default | Meaning |
|------|---------|---------|
| `--form` | `long` | Restrict to `long`, `short`, or `all` videos. Long-form is the default since this tool is primarily for editors finding clips to remix into long-form videos. |
| `--limit` | `10` | Max videos to transcribe AND max pending files to chunk per invocation. `--limit 0` means no limit. Keep modest by default to avoid runaway agent dispatches. |
| `--phase` | `all` | Which phase to run. `all` runs the whole pipeline. |
| `--parallel` | `3` | How many `ayc-chunker` agents to dispatch in parallel. **Hard ceiling: 3** — running more risks hitting Claude Code usage limits. |

If `$ARGUMENTS` is empty, use all defaults.

## Pre-flight

1. **Verify env keys** — the Python CLI needs both `ANTHROPIC_API_KEY` and `OPENAI_API_KEY`. Run `uv run ayc status` and if it errors with a config message, tell the user to set them and stop.

2. **Verify the channel is enumerated** — run `uv run ayc status`. If the videos table is empty, tell the user to run `uv run ayc init "<channel-url>"` first and stop. Don't try to enumerate from the runbook (the channel URL is too important to guess).

3. **Verify the `ayc-chunker` agent is loaded** — try a no-op probe: `Bash: ls .claude/agents/ayc-chunker.md`. If the file exists but you (the parent) don't have `ayc-chunker` in your available `subagent_type` list, the user is in a session that started before the agent was added. **Tell them to restart Claude Code, then re-run this command.** Do not fall back to `general-purpose` agents — the named agent is the supported path.

## Phase: status (always run first)

Run these and show the user the output:

- `uv run ayc status`
- `uv run ayc queue status`

Then decide which subsequent phases need to run.

## Phase: transcribe

Run: `uv run ayc transcripts --form <form> --limit <limit>`. This is fully Python — no agents needed. Shows a progress bar.

## Phase: prepare

Run: `uv run ayc queue prepare --form <form> --limit <limit>`. Writes one `queue/pending/<id>.json` per transcribed-but-not-yet-chunked video. Idempotent.

## Phase: chunk (the agent loop)

This is the heart of the runbook.

1. **List pending files**: `Bash: ls queue/pending/*.json 2>/dev/null | head -<effective-limit>`. Sort lexicographically (matches the agent's "next pending" tiebreaker).

2. **If empty**, say "Queue is empty — nothing to chunk." and skip to the next phase.

3. **Dispatch in batches of `--parallel` (default 3)**. For each batch:
   - Send a single message containing N `Agent` tool calls (with `N <= --parallel`) — this runs them in parallel.
   - Each call: `subagent_type: "ayc-chunker"`, `prompt: "Process the file <ABSOLUTE-PATH-to-pending-file>"`.
   - The agent reads the spec at `.claude/agents/ayc-chunker.md`, processes the file, writes `queue/completed/<id>.chunks.json`, reports a one-line summary.
   - Wait for the entire batch to finish before dispatching the next one. (Each Agent tool call returns its own result — collect them all.)

4. **Track results**. After each batch, log the per-file result lines. If any agent reports a failure, surface it but continue with the next batch.

5. **`--parallel` ceiling: 3**. Even if the user passes a higher number, cap it at 3 — Claude Code usage limits make 4+ parallel agents risky for a long batch. Note this clamp to the user.

## Phase: merge

Run: `uv run ayc queue merge`. Inserts chunks into SQLite, deletes pending files, moves completed files to `queue/archive/`. Reports videos merged + total chunks + errors.

If errors > 0, point the user at `queue/failed/` for the malformed files.

## Phase: embed

Run: `uv run ayc embed`. Batches uncovered chunks through OpenAI's `text-embedding-3-small`. Cheap; ~$0.01 for 1000 chunks.

## End-of-run summary

After all requested phases run:

1. Show final `uv run ayc status` and `uv run ayc queue status`.
2. Note: how many new chunks were added this run (total_chunks_after − total_chunks_before, if known).
3. Suggest one query against the new content: `uv run ayc ask "<query>"` — pick something motivated by the just-ingested videos' titles if possible.

## Important guidance for the parent (you)

- **Be a coordinator, not a chunker**. The `ayc-chunker` agent does the LLM work. Your job is dispatching, collecting, merging. Don't analyze transcripts yourself.
- **Don't narrate the pipeline mid-run.** Show concise progress: phase started, batch dispatched, phase results. The user can read the CLI output.
- **Be honest about cost / billing**:
  - Chunker work: Claude Code subscription
  - Reformulator + synthesizer at query time: Anthropic API (~$0.02/query)
  - Embeddings: OpenAI API (negligible)
- **Failure modes**:
  - "no captions" video: marked `skipped_no_captions` by the transcript step. Skip it — don't retry unless `--redo-failed` (not yet implemented; mention if relevant).
  - Agent malformed JSON: lands in `queue/failed/<id>.error.txt`. Report at end; don't retry.
  - Merge step finds invalid timestamps: ditto — flagged in `queue/failed/`.
- **Don't over-eagerly enumerate the catalog** beyond `--limit`. The full long-form set on Abolitionists Rising is ~420 videos; running them all in one go is hours of agent time and tens of thousands of tokens of context. Default `--limit 10` per invocation, let the user iterate.

## Example invocations

- `/ayc-runbook` — process up to 10 long-form videos through every phase.
- `/ayc-runbook --form long --limit 25` — bigger long-form batch.
- `/ayc-runbook --phase chunk` — only dispatch agents on whatever's already in `queue/pending/`. Skip transcribe and prepare. Useful when you've already prepared a batch and just want it chunked.
- `/ayc-runbook --form short --limit 50` — work on shorts.
- `/ayc-runbook --phase status` — quick look at where things stand.

## User args

$ARGUMENTS
