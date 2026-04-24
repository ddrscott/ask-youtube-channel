# ask-youtube-channel

Ask a question, get an answer backed by cited clips from a YouTube channel's back catalog.

A deployable-per-channel search and Q&A app. Point it at any channel, let it ingest every public video, and you get a site where users can ask freeform questions and receive short, synthesized answers alongside the exact timestamped clips that support the claim.

**First deployment target:** Abolitionist Rising (ask @Grassley for channel list).

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

## What It Does (Vision)

### Ingest Pipeline

1. **Channel enumeration** — given a channel handle, list every video (YouTube Data API, or `yt-dlp` for channels without API quota).
2. **Transcript capture** — pull official captions when available; fall back to `yt-dlp --write-auto-subs` or Whisper for videos with no captions. Store word-level timestamps.
3. **Q&A chunking** — an LLM pass sweeps each transcript and extracts **answered-question moments**: spans where a question is asked (by host, guest, audience, or rhetorically) and answered. Each chunk gets:
   - The inferred question (normalized, canonicalized)
   - The answer text
   - Video ID + start/end timestamps
   - Speaker attribution (best effort)
   - Topic tags
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
| LLM (synth) | Groq / Workers AI / Anthropic | Answer synthesis with citations |
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
- **Chunk quality vs. cost.** The Q&A chunker is the whole ballgame. Test Groq Llama vs. GPT-4o-mini vs. Sonnet on a 10-video sample before committing. The cheapest model that produces acceptable chunks wins.
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
