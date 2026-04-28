# Architecture

AYC is two repos that act as one system.

## Two repos, one system

```
┌──────────────────────────────────────────┐    ┌────────────────────────────────────────────┐
│  ask-youtube-channel  (this repo)        │    │  ayc.ljs.app  (sibling repo)               │
│                                          │    │                                            │
│  Python pipeline + Claude Code agents    │    │  Cloudflare Worker (Hono + TypeScript)     │
│                                          │    │                                            │
│  Runs on: operator's laptop              │    │  Runs on: Cloudflare global edge           │
│  Drives:  yt-dlp, OpenAI, CC subagents   │    │  Drives:  D1, Vectorize, R2                │
│                                          │    │                                            │
│  Output:  HTTP POSTs to /internal/*  ────┼──→ │  Persistent state + browse/admin UI        │
│                                          │    │                                            │
└──────────────────────────────────────────┘    └────────────────────────────────────────────┘
```

The Python repo is **stateless** — every fact lives in the cloud. If you wipe the laptop and clone fresh, you can resume where you left off by hitting `/internal/stats` and `/internal/videos?status=...`.

The only on-disk state on the laptop is a transient filesystem queue (`queue/pending/`, `queue/completed/`, `queue/failed/`, `queue/archive/`) used to hand work to and from the `ayc-chunker` Claude Code subagent.

## Storage layers

| What | Where | Why |
|---|---|---|
| Channels, videos, chunks, submissions, service tokens, favorites | **D1** (`ayc-ljs-db`) | Relational, transactional, cheap reads at the edge |
| Chunk embeddings (1536d cosine) | **Vectorize** (`ayc-chunks`) | Filtered cosine similarity for "more like this" + future query reformulation |
| Transcript JSONs (yt-dlp captions) | **R2** (`ayc-transcripts`) | Large blobs, key shape `transcripts/<channel_id>/<video_id>.json` |
| User favorites | D1 (`user_favorites` table, keyed by email) | Per-user state |

D1 is the source of truth. Vectorize is derived from D1 (chunks rows → embeddings). R2 is opaque content (transcripts the chunker reads).

## Auth surfaces

| Surface | How it's gated |
|---|---|
| `/internal/*` (pipeline → cloud) | Bearer service token (`Authorization: Bearer ayc_…`). Hashed at rest with `SERVICE_TOKEN_SALT`. |
| `/api/*` user endpoints (browse, suggest, favorites) | Magic-link cookie issued by [auth.ljs.app](https://auth.ljs.app). Verified locally with shared `JWT_SECRET`. |
| `/admin/*` + `/dashboard` | Same cookie + `admin` scope in the JWT |
| `/healthz`, `/`, `/about`, `/privacy`, `/terms` | Public |

## Data flow: a brand-new video → searchable chunk

```
  yt-dlp                           Python                          Worker / Cloud
  ──────                           ──────                          ──────────────
  1. resolve channel handle  →  upsert channel row (D1)
  2. enumerate /videos /shorts → upsert ≤500 videos at a time, status='pending'
  3. fetch transcript JSON   →  PUT /internal/videos/:id/transcript (R2)
                                status → 'transcribed'
  4. write queue/pending/<id>.json (transcript on disk for the agent)
  5. dispatch ayc-chunker subagent
       ↓
       agent reads pending file, emits queue/completed/<id>.chunks.json
  6. ayc queue merge       →  POST /internal/chunks:bulk
                                D1 inserts chunks, status → 'chunked'
                                (replace=true also deletes Vectorize entries)
  7. ayc embed             →  GET /internal/chunks/unembedded
                                POST /internal/chunks/embeddings:bulk
                                Vectorize upserts vectors
                                status → 'embedded' once last chunk lands
```

Status values for `videos.ingest_status`: `pending → transcribed → chunked → embedded`. (Plus `skipped_no_captions` for videos yt-dlp couldn't transcribe — usually shorts with auto-captions disabled.)

## Two chunking modes

The same `ayc-chunker` subagent supports two modes, selected by what's inside the pending file:

- **Default mode** — extract every Q&A and objection-rebuttal moment from scratch. Merge replaces existing chunks. Used by `/ayc-runbook` for new videos.
- **Gap-fill mode** (`gap_fill: true` in the pending file) — pending file also includes `existing_chunks`, `covered_ranges`, and `gap_ranges`. Agent extracts only new chunks in the gaps. Merge appends with `replace=false` and the merge step **programmatically filters** new chunks to those overlapping a declared gap by ≥20s. Used by `/ayc-rechunker` to widen coverage on already-chunked videos without losing existing chunk IDs or favorites.

See [`explanation.md`](explanation.md) § *Why gap-fill* for the rationale.

## Why a filesystem queue?

The chunker is the expensive step. Running it as a Claude Code subagent shifts that cost from the Anthropic API to the operator's CC subscription. But subagents don't have a way to be "given a transcript and asked to write to D1" — they read files, write files, and report a one-liner.

So the queue is the contract:
- Python writes `queue/pending/<id>.json` with the transcript
- Subagent writes `queue/completed/<id>.chunks.json` with the extracted chunks
- Python `ayc queue merge` reads the completed files, POSTs to `/internal/chunks:bulk`, archives the originals

This means the chunker never knows about D1 or service tokens. It does one thing: read transcript, write chunks. Everything else is plain Python orchestration.

## Why two repos, not one

The Worker is small (~6k lines of TypeScript), is deployed via wrangler, and has a different release cadence than the Python pipeline. Keeping them separate means:

- TypeScript and Python toolchains don't pollute each other (`uv` vs `npm`)
- The Worker can be cloned and deployed standalone (someone forking just the API can ignore the Python stuff)
- The Python pipeline can be developed offline against a local mock without touching the Worker

They share state only through HTTP (the `/internal/*` API) and a shared `JWT_SECRET` env var (so both verify the same auth.ljs.app session cookies).
