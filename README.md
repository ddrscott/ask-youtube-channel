# ask-youtube-channel

Ask a question, get an answer backed by cited clips from a YouTube channel's back catalog.

A deployable-per-channel search and Q&A app. Point it at any channel, let it ingest every public video, and you get a site where users can ask freeform questions and receive short, synthesized answers alongside the exact timestamped clips that support the claim.

**First deployment target:** Abolitionists Rising (`UCopqtPoYi92ZMdXEGWXaPTA`, https://www.youtube.com/@AbolitionistsRising).

## Status

- **Vision (this README, below):** Deployable-per-channel Cloudflare app with subdomain per channel.
- **What exists today:** Local CLI prototype (Python + uv + sqlite). Validates the chunker quality, the query-reformulation pattern, and the end-to-end retrieval flow before we commit to the full CF stack. See [Local CLI Prototype](#local-cli-prototype).

## Local CLI Prototype

A `uv`-managed Python package (`ayc/`) that ingests a channel into a local sqlite DB and answers queries against it.

### What it does differently from the vision

- **Two chunk kinds** in a single pass per video: `qa` (answered questions) and `objection` (objections/claims with rebuttals). Abolitionist content is overwhelmingly objection-handling, so the original "answered questions" spec was missing the dominant pattern.
- **Long-form vs. shorts classification.** `init` enumerates `/videos`, `/streams`, and `/shorts` separately and tags each row's `form`. Editors looking for clips to remix into long-form videos default to `--form long` on `ask`; `--form short` is one-flag away when the editor wants Shorts; `--form all` searches both.
- **Two chunker paths**:
  - `ayc chunk` — calls the Anthropic API. Self-contained, scriptable, billed per token. Use for unattended batch ingest, automated tests, or when you don't have a Claude Code session open.
  - **Claude Code agent** (`ayc-chunker`) — runs against your Claude Code subscription rather than the API. Use for the bulk of the catalog. Workflow is `ayc queue prepare` → dispatch the agent → `ayc queue merge`.
- **Query-time reformulation.** An LLM expands the raw user input ("but a fetus isn't human") into 2–4 search variants tagged as `qa`, `objection`, or `either`. Each variant gets embedded and scored separately; the union is deduped and ranked. Lets editors type natural inputs that won't directly match index entries.
- **Storage.** One `data/ayc.db` sqlite file. Embeddings are float32 BLOBs; cosine similarity is brute-force numpy at query time. Fine for any single-channel scale (≤100k chunks).
- **Models.** Claude Opus 4.7 (1M context, adaptive thinking, structured outputs via `output_config.format`) for chunking, reformulating, synthesizing; OpenAI `text-embedding-3-small` for embeddings.

### Quick start

```sh
# 1. Install deps
uv sync

# 2. Set keys (or use .env)
export ANTHROPIC_API_KEY=...
export OPENAI_API_KEY=...

# 3. Enumerate every video on the channel (long-form, streams, shorts)
uv run ayc init "https://www.youtube.com/@AbolitionistsRising"

# 4. Pull transcripts. --form is optional: 'all' (default), 'long', or 'short'.
uv run ayc transcripts --form long

# 5a. Chunk via the Claude Code agent (preferred for bulk ingest):
uv run ayc queue prepare --form long
# → in your Claude Code session:
#    /ayc-process-queue
# → after the agent batches finish:
uv run ayc queue merge

# 5b. Or chunk via the Anthropic API (unattended / automated):
uv run ayc chunk --form long

# 6. Embed every chunk
uv run ayc embed

# 7. Ask. Defaults to --form long (editor's reflex). Use --form short or --form all to widen.
uv run ayc ask "but a fetus isn't human"
uv run ayc ask "what about cases of rape" --form all
```

### Long-form vs. shorts

`init` runs three yt-dlp passes against the channel and tags each row's `form`:

| URL | Tagged as | Editor's typical use |
|-----|-----------|----------------------|
| `<channel>/videos` | `long` | Primary — clips to remix into a long-form edit |
| `<channel>/streams` | `long` | Long debates / livestreams |
| `<channel>/shorts` | `short` | Sometimes the cleanest 30-second street-debate exchanges |

Defaults:

| Command | `--form` default | Reasoning |
|---------|-----|-----------|
| `transcripts` | `all` | Slow batch step — ingest both so the editor doesn't wait when they change their mind. |
| `chunk` | `all` | Same. |
| `queue prepare` | `all` | Same. |
| `ask` | `long` | Editors are usually looking for clips to remix into long-form videos. |

### Claude Code agent flow (preferred for bulk chunking)

The chunker is the recurring expensive operation — re-run anytime the prompt iterates. Keeping it on the Anthropic API means every prompt change costs real tokens against the catalog. Moving it to a Claude Code subagent shifts that cost to your Claude Code subscription.

Architecture:

```
Python (deterministic state)            Claude Code (LLM work)
─────────────────────────────           ────────────────────────────
ayc transcripts                  ─►     [ no LLM needed ]
ayc queue prepare                ─►     queue/pending/<id>.json
                                            │
                                            ▼
                                        Agent: ayc-chunker
                                        (reads pending/, writes completed/)
                                            │
                                            ▼
ayc queue merge                  ◄─     queue/completed/<id>.chunks.json
ayc embed                        ─►     [ OpenAI embeddings ]
ayc ask                          ─►     [ Anthropic API for reformulation+synthesis ]
```

The chunker agent is defined at `.claude/agents/ayc-chunker.md`. Project-level agents are loaded at Claude Code session start, so a fresh session is needed after pulling the agent file the first time.

Dispatching:

- **Manual one-off:** in Claude Code, ask "process queue/pending/<id>.json with the ayc-chunker agent" — Claude dispatches it.
- **Batch:** `/ayc-process-queue` (slash command at `.claude/commands/ayc-process-queue.md`) loops over `queue/pending/*.json`, dispatches the agent in parallel batches of 5, and runs `ayc queue merge` at the end.

The agent reads ONE pending transcript per invocation, extracts chunks, writes `queue/completed/<id>.chunks.json`. Failures land in `queue/failed/<id>.error.txt`. The Python merge step archives completed files into `queue/archive/` once their chunks are in the DB.

### Layout

```
ask-youtube-channel/
├── pyproject.toml
├── ayc/                        # Python — deterministic state
│   ├── cli.py                  # Typer entry — exposes `ayc` and `ayc queue ...`
│   ├── config.py
│   ├── db.py                   # sqlite schema + migrations
│   ├── enumerate.py            # /videos + /streams + /shorts passes
│   ├── transcripts.py          # yt-dlp auto-subs JSON3 → deduped segments
│   ├── chunker.py              # API-based chunker (anthropic SDK)
│   ├── queue.py                # filesystem queue for the agent flow
│   ├── embed.py                # OpenAI embeddings (batched)
│   ├── search.py               # reformulate → embed → cosine → synthesize
│   └── prompts.py              # all LLM system prompts
├── .claude/
│   ├── agents/
│   │   └── ayc-chunker.md      # the chunker agent definition
│   └── commands/
│       └── ayc-process-queue.md
├── queue/                      # gitignored
│   ├── pending/                # transcripts ready to chunk
│   ├── completed/              # chunks written by the agent (pre-merge)
│   ├── failed/                 # agent failures
│   └── archive/                # post-merge archive of completed files
├── scripts/
│   └── ingest_targeted.py      # ingest specific video IDs end-to-end (API path)
└── data/                       # gitignored: ayc.db + transcripts/
```

### Other commands

- `uv run ayc status` — counts of videos by `form × ingest_status`, chunk counts by kind.
- `uv run ayc queue status` — count of pending, completed, failed queue files.
- `uv run ayc ask "<query>" --json` — machine-readable output (clips + reformulations + synthesized answer).
- `uv run ayc ask "<query>" --no-synthesis` — clips only, skip the synthesized answer.
- `uv run python scripts/ingest_targeted.py <video_id> [<video_id> ...]` — ingest specific videos by ID end-to-end via the API path. Useful for iterating on the chunker prompt against a curated set.

## Vision (full architecture)

## Why

Long-form YouTube content is a goldmine of answers buried under hours of runtime. Movements, podcasts, and creators routinely answer the same questions dozens of times across their catalog, but nobody can find the right 45 seconds when they need it — mid-conversation, in a sermon, on the street, on a podcast.

This tool turns a channel's full video library into a **citeable knowledge base**:

- Ask: *"What's the biblical case for immediate abolition?"*
- Get: A two-paragraph synthesized answer, plus three clip cards linking to the exact moments in three different videos where the speaker actually makes that case.

The answer is **never un-cited**. Every claim in the synthesized response maps to a timestamped clip, and the UI leads with the clips, not the synthesis.

## Design Principles

1. **Clips are the product, synthesis is the scaffolding.** If the app can't find a clip that supports a claim, it doesn't make the claim. No "based on general knowledge" answers — if the channel never said it, the app doesn't say it.
2. **Deep-link, never re-host.** Every clip is a YouTube URL with `?t=start`. We don't mirror video or audio. Fair-use is clearly on our side and creators keep their views.
3. **Deployable per channel.** One codebase, many deployments. A single channel's index lives in its own D1/Vectorize namespace with its own subdomain. No multi-tenant query surface — the scope of what the app knows is always exactly one channel's output.
4. **Ingest is batch, query is instant.** Scraping, transcription, and indexing happen on background queues. End-user queries hit a pre-built vector index and return in under a second.
5. **Q&A-shaped indexing beats generic chunking.** The indexer's job isn't to split transcripts into 500-token chunks — it's to find moments where a **question is posed or implied and an answer is given**, and tag those moments with the question they answer. That's the unit of retrieval.
6. **The chunker sees the whole transcript in one LLM call.** No sliding windows, no map-reduce stitching. A 256k-context model comfortably fits any realistic video transcript (a 10-hour livestream transcribes to ~120k tokens), and single-pass chunking eliminates the dominant failure mode of windowed approaches — Q&A moments split across window boundaries. Full-transcript context also lets the model resolve pronouns, callbacks, and multi-turn exchanges that local chunking can't see.

## What It Does (Vision)

### Ingest Pipeline

1. **Channel enumeration** — given a channel handle, list every video (YouTube Data API, or `yt-dlp` for channels without API quota).
2. **Transcript capture** — pull official captions when available; fall back to `yt-dlp --write-auto-subs` or Whisper for videos with no captions. Store word-level timestamps.
3. **Q&A chunking** — a **single LLM call per video** with the **entire transcript** in context (256k-context model, structured output). The model extracts **answered-question moments**: spans where a question is asked (by host, guest, audience, or rhetorically) and answered. One pass, full document context, no windowing. Each chunk gets:
   - The inferred question (normalized, canonicalized)
   - The answer text (verbatim or near-verbatim span from the transcript)
   - Video ID + `start_seconds` / `end_seconds` (derived from the word-level timestamps in the input)
   - Speaker attribution (best effort)
   - Topic tags
   - Confidence score

   The prompt hands the model the transcript as `[start_seconds] speaker?: text` lines so timestamps flow through structured output without hallucination. If a transcript ever exceeds the context window (extremely long livestream), fall back to a deterministic section split on long silence gaps and run the chunker once per section — never chunk mid-conversation.
4. **Embedding + indexing** — chunks embedded and upserted into Cloudflare Vectorize. Raw transcripts stored in R2, structured metadata in D1.
5. **Re-index on new uploads** — a scheduled worker checks for new videos on each channel daily and incrementally ingests them.

### Query Experience

- Single search box. User types a question.
- Backend does hybrid retrieval (vector + keyword on the question field).
- Top clips are rendered as cards: thumbnail, inferred question, speaker, duration, **"Play 0:45 → 1:32"** deep link.
- Above the clips: a short LLM-synthesized answer that stitches the clip content together, with inline citations `[1][2][3]` pointing to the clip cards.
- A "No match" state when nothing in the channel actually answers the question — this is a feature, not a failure.

### Admin / Curation (Later)

- Flag a bad chunk (wrong question inferred, missed the answer).
- Merge duplicate questions across clips ("Canonical question: X").
- Featured / pinned answers for the most-asked questions.

## Tech Stack

Mirrors [`~/code/justright.fm`](../justright.fm/) — full Cloudflare stack.

| Layer | Choice | Why |
|-------|--------|-----|
| Runtime | Cloudflare Workers | Edge, cheap, already the house stack |
| Framework | Hono | Same as justright.fm, JSX SPA pattern |
| Database | Cloudflare D1 (SQLite) | Channel metadata, videos, chunks, question index |
| Vector DB | Cloudflare Vectorize | Semantic retrieval over Q&A chunks |
| Object Store | Cloudflare R2 | Raw transcripts, thumbnails (cached), optional audio for Whisper fallback |
| Queues | Cloudflare Queues | Ingest pipeline: enumerate → transcribe → chunk → embed |
| Durable Objects | CF DOs | Per-channel ingest coordinator with SQLite state |
| LLM (chunker) | Long-context model (≥256k ctx) with structured output — e.g. Claude Sonnet, Gemini 2.x, GPT-4.1 | Single-pass Q&A extraction over full transcript |
| LLM (synth) | Fast cheap model — Groq Llama / Haiku / Workers AI | Answer synthesis with citations (short output, small input — cheap model fine) |
| Embeddings | Workers AI (bge-small) or OpenAI | Per-chunk vectors |
| Transcription fallback | Whisper via fal.ai or OpenAI | Only when YouTube captions are missing |
| YouTube data | YouTube Data API v3 + `yt-dlp` fallback | Caption fetch + metadata |
| Frontend | Hono JSX SPA | Single search box + clip cards |
| Analytics | Plausible | Same pattern as justright.fm |

## Per-Channel Deployment Model

Each channel deployment is a separate Worker with its own D1, Vectorize index, R2 prefix, and subdomain. Same codebase, different bindings.

```
abolitionist.ask-youtube.app   → abolitionist-db, abolitionist-vectors, r2://ask-yt/abolitionist/
podcast-x.ask-youtube.app      → podcast-x-db,    podcast-x-vectors,    r2://ask-yt/podcast-x/
```

Config lives in `wrangler.toml` per env, and a `channels/{slug}/config.yml` holds the channel-specific bits (channel handle, display name, branding, question prompts).

## Repository Layout (Planned)

```
ask-youtube-channel/
├── README.md                         # This file
├── wrangler.toml                     # Base config + env-per-channel
├── package.json
├── tsconfig.json
├── schema/
│   ├── migrations.sql                # videos, chunks, questions, ingest_runs
│   └── seed-*.sql                    # Per-channel seed data
├── channels/
│   └── abolitionist-rising/
│       ├── config.yml                # Channel ID, display name, branding
│       └── branding/                 # Logo, colors, favicon
├── src/
│   ├── index.ts                      # Hono app entry
│   ├── routes/
│   │   ├── ui.ts                     # SPA shell
│   │   ├── search.ts                 # POST /api/ask → retrieval + synth
│   │   ├── ingest.ts                 # POST /api/ingest/run (admin)
│   │   └── admin.ts                  # Curation, flags
│   ├── ingest/
│   │   ├── enumerate.ts              # Channel → video list
│   │   ├── transcript.ts             # Captions → word-level JSON
│   │   ├── whisper.ts                # Fallback transcription
│   │   ├── chunk.ts                  # Transcript → Q&A chunks (LLM)
│   │   └── embed.ts                  # Chunks → Vectorize
│   ├── search/
│   │   ├── retrieve.ts               # Hybrid vector + keyword
│   │   └── synthesize.ts             # LLM answer with citations
│   ├── durable-objects/
│   │   └── channel-ingester.ts       # Per-channel pipeline orchestrator
│   ├── client/
│   │   ├── app.tsx                   # Hono JSX SPA
│   │   ├── pages/
│   │   │   ├── Ask.tsx               # Search box + answer view
│   │   │   └── Clip.tsx              # Single clip detail
│   │   └── main.css
│   └── types/
│       ├── env.ts                    # CF bindings
│       └── models.ts                 # Video, Chunk, Question, Answer
└── scripts/
    ├── ingest-channel.ts             # Local CLI: trigger a channel ingest
    └── reindex.ts                    # Rebuild vectors from D1
```

## Data Model (Sketch)

```sql
-- Channels this instance knows about (usually one, but multi-channel possible)
CREATE TABLE channels (
  id TEXT PRIMARY KEY,
  slug TEXT UNIQUE NOT NULL,
  youtube_channel_id TEXT NOT NULL,
  display_name TEXT NOT NULL,
  created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE videos (
  id TEXT PRIMARY KEY,                   -- YouTube video ID
  channel_id TEXT NOT NULL REFERENCES channels(id),
  title TEXT NOT NULL,
  published_at TEXT,
  duration_seconds INTEGER,
  thumbnail_url TEXT,
  transcript_source TEXT,                -- 'captions' | 'auto' | 'whisper'
  ingest_status TEXT,                    -- 'pending' | 'transcribed' | 'chunked' | 'indexed' | 'failed'
  ingested_at TEXT
);

CREATE TABLE chunks (
  id TEXT PRIMARY KEY,
  video_id TEXT NOT NULL REFERENCES videos(id),
  start_seconds REAL NOT NULL,
  end_seconds REAL NOT NULL,
  inferred_question TEXT NOT NULL,       -- The normalized question this clip answers
  answer_text TEXT NOT NULL,             -- Verbatim or near-verbatim transcript span
  speaker TEXT,                          -- Best-effort attribution
  topic_tags TEXT,                       -- JSON array
  confidence REAL,                       -- LLM confidence this is a real Q&A moment
  created_at TEXT DEFAULT (datetime('now'))
);

-- Vectorize index 'chunks-v1': chunk_id → embedding(inferred_question + answer_text)

CREATE TABLE ingest_runs (
  id TEXT PRIMARY KEY,
  channel_id TEXT NOT NULL REFERENCES channels(id),
  started_at TEXT DEFAULT (datetime('now')),
  finished_at TEXT,
  videos_added INTEGER DEFAULT 0,
  chunks_added INTEGER DEFAULT 0,
  status TEXT                            -- 'running' | 'complete' | 'failed'
);
```

## Milestones

### v0 — Single-channel prototype
- [ ] Scaffold Hono + Workers + D1 + R2 + Vectorize
- [ ] Enumerate all videos for one channel
- [ ] Pull captions for every video with captions
- [ ] Whisper-fallback for one video without captions to prove the path
- [ ] LLM chunker that extracts Q&A moments from a transcript
- [ ] Embed + upsert into Vectorize
- [ ] `/ask` endpoint: retrieve top-5 chunks, return raw JSON
- [ ] Minimal UI: search box + clip cards with YouTube deep links
- [ ] Ship to `abolitionist.ask-youtube.app` (or similar)

### v1 — Synthesis + polish
- [ ] Cited-answer synthesis above the clip cards
- [ ] Hybrid retrieval (vector + BM25 on question field)
- [ ] "No match" state with explicit honesty
- [ ] Daily scheduled re-ingest for new uploads
- [ ] Plausible analytics
- [ ] Per-channel branding via `channels/{slug}/config.yml`

### v2 — Curation + multi-channel
- [ ] Admin flag-a-chunk UX
- [ ] Canonical question merging
- [ ] Second channel deployment to prove the per-channel model
- [ ] Export/share: "Here's the answer + 3 clips" as an embeddable card

### v3 — Productize (optional)
- [ ] Self-serve channel onboarding
- [ ] Tiered pricing for larger channels / movement orgs
- [ ] API access for partners

## Open Questions

- **Transcription cost control.** Whisper fallback costs real money per hour of video. Cap per-channel ingest budgets and prioritize videos with existing captions.
- **Chunker model choice.** The Q&A chunker is the whole ballgame, and it must be a ≥256k-context model with reliable structured output. Bake off on a 10-video sample: Claude Sonnet vs. Gemini 2.x vs. GPT-4.1. Score on (a) did it find every real Q&A moment, (b) did it invent any, (c) are the timestamps accurate. The cheapest model that passes wins. Note: a single one-hour transcript ≈ 12k tokens in, maybe 4–8k tokens out — this is not the expensive path, even on the priciest model.
- **Licensing.** Deep-linking to YouTube is clearly fair use. Showing transcript excerpts in the answer synthesis is where we need to be careful — keep excerpts short, always link to source, and add a creator opt-out path if a channel owner asks.
- **Speaker attribution.** Multi-speaker channels (podcasts, debates) make attribution messy. v0 can skip it; v1 should take a swing using metadata + heuristics.
- **"Confidence" threshold.** If the best retrieved chunk is below some similarity threshold, return "no match" instead of a synthesized hallucination. Calibrate this threshold on real queries, not intuition.

## References

- [`~/code/justright.fm`](../justright.fm/) — parent architecture pattern for CF Worker + Hono + D1 + R2 + DO + Queues
- YouTube Data API v3 — channel + video enumeration
- `yt-dlp` — fallback for caption and metadata fetch
- Cloudflare Vectorize — vector search on edge
- fal.ai Whisper — transcription fallback

## License

Private — all rights reserved.
