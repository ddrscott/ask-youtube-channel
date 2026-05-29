# Phase 3 — Chunker on Cloudflare

End-state: chunking runs inside the existing Worker (`ayc.ljs.app`), triggered by a Workflow, billed via AI Gateway Unified Billing. No laptop dependency.

## Eval result summary

Decision-grade data from the bake-off (two 35-min street debate videos, see `eval/results/`):

| Architecture | Avg chunks | Avg coverage | Avg $/video | Variance |
|---|---|---|---|---|
| Single-pass Sonnet 4.6 | 36 | 60% | $0.135 | low |
| Single-pass Haiku 4.5 | 44 | 41% | $0.05 | **high** |
| Single-pass Gemini 3 Flash | 23 | 32% | $0.02 | low (low-floor) |
| Single-pass Grok 4.3 | 11 | 12% | $0.025 | low (low-floor) |
| **Map-reduce Haiku 4.5** | **53** | **63%** | **$0.07** | **low** |
| **+ arc-merge post-pass** | **31** | **68%** | **$0.07** | **low** |
| Map-reduce Sonnet 4.6 | 44 | 63%+ | $0.17 | low |
| Opus 4.7 (current subagent, baseline) | 35 | 83% | (subscription) | low |

**Pick:** Map-reduce + Haiku 4.5 + arc-merge post-processor. Same chunk shape as the existing Opus index, ~15-pt coverage gap below Opus but ≥ Sonnet, half the cost of single-pass Sonnet, attention-dense windows eliminate the Haiku single-pass variance.

## Target architecture

```
                    ┌─────────────────────────────────────────────┐
                    │            Cloudflare Worker                │
                    │                                             │
   Cron Trigger ───►│  Workflow: ingest                          │
   (daily 06:00 UTC)│   1. enum_channel (yt-dlp container)        │
                    │   2. transcribe_new (yt-dlp container)      │
                    │   3. chunk_pending (this doc)               │
                    │   4. embed (BGE-M3 via Workers AI)          │
                    │   5. health_summary (Discord webhook)       │
                    └────────┬─────────────────┬──────────────────┘
                             │                 │
                  ┌──────────▼───┐    ┌────────▼─────────┐
                  │  AI Gateway  │    │   D1 / R2 / Vec  │
                  │  Unified Bill│    │   (already exist)│
                  │  → Anthropic │    └──────────────────┘
                  │  Haiku 4.5   │
                  └──────────────┘
```

## Chunker step contract

For each video in `videos WHERE ingest_status='transcribed' AND channel.enabled=1`:

1. Load channel config from D1 (`channels` row).
2. Fetch transcript JSON from R2 (`transcript_r2_key`).
3. Split into windows per channel config (`window_chars`, `window_overlap_s`).
4. Fan out parallel `Anthropic` calls via AI Gateway, model from `channels.chunker_model`.
5. Dedupe windowed chunks by `(kind, start_seconds)` proximity (15s).
6. If `arc_merge_enabled`: collapse adjacent same-kind chunks (`arc_merge_gap_s`).
7. Validate schema. Reject the video on JSON/schema failure (status → `chunk_failed`).
8. Insert chunks into D1, mark `ingest_status='chunked'`, set `chunked_at`.

## Files to create / modify

### `schema/004_channels_chunker_config.sql` (✓ written)
Per-channel chunker tuning columns. Default values capture the Phase 3 pick.

### `src/services/chunker.ts` (skeleton in this commit, fill out next)
Pure TypeScript port of `eval/mapreduce.py`. Inputs: transcript object + channel config. Outputs: validated chunk array. No DB / R2 / network side effects except AI Gateway calls.

### `src/services/arc_merge.ts` (skeleton in this commit)
Pure TypeScript port of `eval/arc_merge.py`. Inputs: chunk array + config. Outputs: arc-merged chunk array.

### `src/workflows/ingest.ts` (NEW)
Workflow class orchestrating the pipeline phases. Uses `ctx.do(...)` per phase for durable retry. Triggered by Cron or admin endpoint.

### `src/routes/internal.ts` (modify)
Add `POST /internal/videos/:id/chunk` — synchronous trigger of the chunker step on a single video (admin-invoked retry path).

### `wrangler.toml` (modify)
- Add `[[workflows]]` binding for `IngestWorkflow`
- Add `[triggers] crons = ["0 6 * * *"]`
- Confirm `[ai]` binding for Workers AI (BGE-M3 embeddings)

### `src/routes/admin.ts` (modify)
- Channel CRUD UI (form + table)
- "Force chunk now" button per video
- Chunker A/B testing surface (model + pattern dropdowns, scorecard view)

## Open decisions for the user

1. **Cron cadence.** Daily? Hourly? Manual-only initially? My recommendation: daily at 06:00 UTC, can add manual trigger from admin UI.
2. **Container vs not for yt-dlp.** Workers Containers GA. Cost is per-second of container runtime. For a daily cron handling ~5–20 new videos, total container time is <5 minutes/day → cents/month. Approve building yt-dlp container in Phase 3.1?
3. **BGE-M3 cutover timing.** Re-embed of 12,162 existing chunks against a new `ayc-chunks-v2` Vectorize index. Do once, atomically cut over, decommission old index. Approve as part of Phase 3 or defer to Phase 4?
4. **Discord webhook.** Existing `DISCORD_WEBHOOK_URL` secret. Should the workflow ping you on completion + errors? Recommend yes.

## Cost projection (post-migration steady-state)

| Item | $/mo |
|---|---|
| Workers Paid plan | $5 |
| Chunker (Haiku via AI Gateway, ~15 videos/day × $0.07) | $32 |
| Embeddings (BGE-M3, ~free at this volume) | <$0.10 |
| Containers (yt-dlp, <5 min/day) | $1 |
| Workflows | <$1 |
| D1 + R2 + Vectorize | included in existing tier |
| AI Gateway Unified Billing 5% fee on top-ups | amortized |
| **Total** | **~$40/mo** |

340-video backlog drain: **$24 one-time** in chunker + a few minutes of container/workflow time.

## Phase 3 milestone breakdown

- **3.0** — This plan + scaffolds + migration. (this commit)
- **3.1** — yt-dlp Container Worker. Wraps `ayc transcripts` semantics, output to R2 + D1. Includes the channel-enum path.
- **3.2** — Chunker service + arc-merge port to TS. Unit tests against captured transcripts in `eval/inputs/`.
- **3.3** — Workflow orchestration + Cron trigger. End-to-end dry-run on one channel.
- **3.4** — BGE-M3 embedder + new Vectorize index. Dual-write + atomic cutover.
- **3.5** — Admin UI for channel CRUD + chunker config. Delete `/ayc-runbook` and `ayc-chunker` subagent.
