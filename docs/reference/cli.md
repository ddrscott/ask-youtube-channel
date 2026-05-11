# CLI reference

The `ayc` CLI is a thin Python wrapper around the cloud API. Run with `uv run ayc <command>`.

## Top-level commands

```
ayc init <channel-url>        Resolve a YouTube channel and enumerate every video to D1.
ayc transcripts [opts]        Fetch transcripts for every video that doesn't have one yet (uploaded to R2).
ayc embed [opts]              Embed every chunk that doesn't have an embedding yet (Vectorize).
ayc status                    Show ingest progress (queries the cloud).
ayc queue <subcommand>        Filesystem queue for Claude Code agent-based chunking.
```

## `ayc init`

```sh
uv run ayc init "https://www.youtube.com/@SomeChannel"
```

Resolves the channel handle/URL via yt-dlp, upserts a row in `channels`, then enumerates `/videos`, `/streams`, and `/shorts` separately and writes (or upserts) every video row at `ingest_status='pending'` with the `form` set to `long` or `short`.

Idempotent. Re-running on the same channel is safe and cheap (the videos:bulk endpoint is upsert-on-id).

## `ayc transcripts`

```sh
uv run ayc transcripts --form long --limit 10
```

| Flag | Default | Meaning |
|---|---|---|
| `--form` | `all` | `long`, `short`, or `all` |
| `--limit` | `0` (no limit) | Max videos to attempt this run |

For each candidate (status=`pending`), pulls the JSON3 captions via yt-dlp, dedupes the rolling-caption pattern, and `PUT`s the resulting transcript JSON to `/internal/videos/:id/transcript` (which streams to R2). Server then sets `ingest_status='transcribed'`.

Videos with no captions get `skipped_no_captions`.

## `ayc embed`

```sh
uv run ayc embed
```

Pulls unembedded chunks via `/internal/chunks/unembedded?limit=100`, computes OpenAI `text-embedding-3-small` (1536d) embeddings in batches, POSTs to `/internal/chunks/embeddings:bulk`. Server upserts to Vectorize, sets `embedded_at` on each chunk, and rolls each video to `ingest_status='embedded'` once its last chunk lands.

Cheap: ~$0.02 per million tokens, so a typical batch of 100 chunks is well under a cent.

## `ayc status`

```sh
uv run ayc status
```

Prints a Rich-formatted table from `/internal/stats`:

- Total channels / videos / chunks / embedded / submissions
- Videos by `(form, ingest_status)` breakdown
- Chunks by `kind` (qa vs objection)

## `ayc queue` subcommands

The filesystem queue is the contract between Python and the `ayc-chunker` Claude Code subagent. Layout:

```
queue/
├── pending/<id>.json          ← Python writes; agent reads
├── completed/<id>.chunks.json ← agent writes; Python merges
├── failed/<id>.error.txt      ← Python or verifier writes; never read by agent
└── archive/<id>.chunks.json   ← Python moves successfully-merged completed files here
```

### `ayc queue prepare`

```sh
uv run ayc queue prepare --form long --limit 25                   # default mode
uv run ayc queue prepare --rechunk --form long --limit 0          # rechunk mode
```

| Flag | Default | Meaning |
|---|---|---|
| `--form` | `all` | Restrict by long/short |
| `--limit` | `0` | Cap files written this run |
| `--rechunk` | off | Include videos at status in `{chunked, embedded}`, not just `transcribed` |

Default mode walks `status='transcribed'` videos and writes one `queue/pending/<id>.json` per video. The pending file contains the transcript segments for the agent to read.

`--rechunk` widens the search to already-chunked content. Pair with `ayc queue augment-gapfill` to switch into additive gap-fill mode (see below).

### `ayc queue augment-gapfill`

```sh
uv run ayc queue augment-gapfill
```

For every existing `queue/pending/<id>.json`, fetches the video's existing chunks from `/internal/videos/:id/chunks`, computes covered ranges (merged) and gap ranges (≥60s with no chunk overlap), and rewrites the pending file with these added fields:

- `gap_fill: true`
- `existing_chunks: [...]`
- `covered_ranges: [[start, end], ...]`
- `gap_ranges: [[start, end, dur], ...]`

The chunker reads the `gap_fill` flag and switches to its second-pass mode (extract only in gap ranges). The merge step honors the same flag and uses `replace=False` plus a programmatic gap-overlap filter (≥20s overlap required) to guarantee additivity.

Run this only after `ayc queue prepare --rechunk`. Running it on a default-mode pending file would trigger gap-fill behavior but with empty `existing_chunks` (effectively no-op vs default).

### `ayc queue merge`

```sh
uv run ayc queue merge
```

The reverse of `prepare`. For each `queue/completed/*.chunks.json`:

1. **Verify** against the source transcript at `queue/pending/<id>.json` (timestamps within ±5s of real segment boundaries, schema-valid, every chunk has numeric `confidence`). Files that fail verification are moved to `queue/failed/<id>.verify-error.txt` and **not merged**.
2. **POST** to `/internal/chunks:bulk` with `replace=True` by default, or `replace=False` if the pending file has `gap_fill: true`.
3. In gap-fill mode, **filter** the agent's chunks to those overlapping a declared gap range by ≥20 seconds.
4. **Archive** the completed file to `queue/archive/` and **delete** the matching `queue/pending/<id>.json`.

Reports `{videos, chunks, errors, quarantined}`.

### `ayc queue verify`

```sh
uv run ayc queue verify [--quarantine]
```

Defense-in-depth: same verifier as merge, but standalone. Without `--quarantine`, just reports issues. With `--quarantine`, moves bad files to `queue/failed/` so the next merge skips them.

`ayc queue merge` runs verify automatically — you only need this command for diagnostic dry-runs.

### `ayc queue status`

```sh
uv run ayc queue status
```

File counts in pending/completed/failed.

## Slash commands (Claude Code)

These are project commands at `.claude/commands/*.md`. They orchestrate the CLI commands above plus dispatching the `ayc-chunker` agent. Use them when you have a CC session open.

| Command | What it does |
|---|---|
| `/ayc-runbook` | End-to-end ingest of new videos: status → transcribe → prepare → chunk (parallel agents, capped at 3) → verify → merge → embed |
| `/ayc-rechunker` | Re-chunk already-processed videos under updated rules. Calls `prepare --rechunk` + `augment-gapfill`, then dispatches agents the same way. |

See the `.md` files for full flag references.
