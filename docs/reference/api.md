# API reference

The Cloudflare Worker at `ayc.ljs.app` (also `ayc-ljs-app.ddrscott.workers.dev`) exposes three families of routes. Source: `~/code/ayc.ljs.app/src/routes/`.

## Auth gates

| Family | Auth | Set in |
|---|---|---|
| `/internal/*` | `Authorization: Bearer ayc_<token>` | `src/middleware/service.ts` |
| `/api/*`, `/browse`, `/suggest` | Magic-link cookie (`session=`) issued by auth.ljs.app | `src/middleware/auth.ts` (`requireAuth`, `requireAuthRedirect`) |
| `/admin/*`, `/dashboard` | Same cookie + `admin` scope | `src/middleware/auth.ts` (`requireScope`) |
| `/healthz`, `/`, `/about`, `/privacy`, `/terms` | None | — |

## `/internal/*` — pipeline ↔ cloud

Service-token gated. Used by the Python CLI (`ayc/db.py`) and migration scripts.

| Method | Path | Body / params | Returns |
|---|---|---|---|
| GET | `/internal/stats` | — | `{channels, videos, chunks, embedded, submissions_pending, videos_by_status: [...], chunks_by_kind: [...]}` |
| POST | `/internal/channels` | `{id, handle, display_name?, submitted_by_email?}` | `{ok, id}` |
| POST | `/internal/videos:bulk` | `{videos: [{id, channel_id, title, duration_seconds?, thumbnail_url?, published_at?, form, ...}]}` (≤500/call) | `{ok, inserted}` |
| GET | `/internal/videos?channel_id=&status=&form=&limit=&cursor=` | query | `{videos: [...], cursor?: ""}` (paginated) |
| POST | `/internal/videos/:id/transcript` | request body = transcript JSON (streamed to R2) | `{ok, id, transcript_r2_key}` |
| GET | `/internal/videos/:id/transcript` | — | transcript JSON (streamed from R2) |
| GET | `/internal/videos/:id/chunks` | — | `{video_id, chunks: [{id, kind, start_seconds, end_seconds, question, topics, confidence}]}` |
| POST | `/internal/videos/:id/mark` | `{status, error?}` | `{ok}` |
| POST | `/internal/chunks:bulk` | `{video_id, replace: bool, chunks: [{id, kind, start_seconds, end_seconds, question, answer, speaker?, topics?, confidence?}]}` (≤500/call) | `{ok, video_id, inserted, vector_orphans_removed?}` |
| GET | `/internal/chunks/unembedded?limit=100` | — | `{chunks: [{id, kind, question, answer}]}` |
| POST | `/internal/chunks/embeddings:bulk` | `{items: [{id, vector}]}` | `{ok, upserted, videos_rolled}` |
| POST | `/internal/vector/query` | `{vector, top_k, filter?: {channel_id?, kind?}}` | `{matches: [{id, score}]}` |

### `chunks:bulk` semantics

- **`replace: true` (default)** — captures prior chunk IDs, deletes them from D1 in a batch with the new inserts, then best-effort `VECTOR.deleteByIds(prior)` so no orphan vectors remain. Marks the video `chunked`.
- **`replace: false`** — appends new chunks; existing chunks unchanged in both D1 and Vectorize. Used by gap-fill mode.

The atomic D1 batch wraps DELETE + INSERTs + UPDATE so the video is never in a half-state.

### `embeddings:bulk` semantics

For each `{id, vector}`:
1. Look up the chunk's `kind` and `channel_id` in D1
2. Upsert to Vectorize with metadata `{kind, channel_id, confidence}` (these are the only metadata fields used for filtering)
3. Set `chunks.embedded_at = datetime('now')`

After the batch, scan affected videos: any video whose every chunk now has `embedded_at` flips to `ingest_status='embedded'`.

## `/api/*` — user-facing JSON

Cookie-gated. Browser uses these from the browse UI.

| Method | Path | Returns |
|---|---|---|
| GET | `/api/browse/chunks` | `{chunks: [...]}` — every chunk for client-side filtering |
| GET | `/api/browse/similar/:id` | `{source, neighbors: [{...chunk, score}]}` — Vectorize-backed similarity, kind-filtered |
| GET | `/api/browse/favorites` | `{chunk_ids: [...]}` — current user's saved chunks |
| POST | `/api/browse/favorites/:id` | `{ok, favorited: true}` |
| DELETE | `/api/browse/favorites/:id` | `{ok, favorited: false}` |
| GET | `/api/auth/callback?token=...&returnTo=...` | sets cookie, redirects |
| POST | `/api/auth/logout` | clears cookie |
| GET | `/api/auth/me` | `{email}` |
| POST | `/api/submissions` | `{channel_url}` → row in `channel_submissions`. Rate-limited per email. |
| POST | `/api/suggest` | `{type, channel_url?, message, wants_business}` → Discord webhook + (if type=channel) row in `channel_submissions` |

## `/admin/*` + `/dashboard` — admin

Cookie + `admin` scope.

| Method | Path | Purpose |
|---|---|---|
| GET | `/dashboard` | Server-rendered HTML with KPIs, per-channel table, under-extraction candidates, recent activity, suggestion queue |
| GET | `/admin/whoami` | Sanity check — `{email, scopes}` |
| POST | `/admin/service-tokens` | Mint a new bearer token. Plaintext returned ONCE. |
| GET | `/admin/service-tokens` | List active tokens (no plaintext) |
| GET | `/admin/submissions` | List pending suggestions |
| POST | `/admin/submissions/:id/accept` | Insert into `channels`, mark accepted |

## HTML routes

| Path | Auth | Notes |
|---|---|---|
| `/` | none | Landing — redirects to `/browse` if signed in |
| `/about`, `/privacy`, `/terms` | none | Static, share Plausible script |
| `/browse` | cookie (redirect on missing) | Discovery + filter UI |
| `/browse/similar/:id` | cookie | Single-chunk view + 20 same-kind neighbors |
| `/suggest` | cookie | Suggest a channel / feature / bug |
| `/dashboard` | cookie + admin scope | (see above) |
| `/login` | none | Redirect to auth.ljs.app/login |

## Rate limits + quotas

- **D1**: ~5M rows/db, 50k writes/day on free, more on paid. Current usage well under.
- **Vectorize**: 5M vectors/index. ~7k currently. 1000 vectors/upsert call cap (we use 500).
- **R2**: storage essentially free at this volume.
- **Worker request body**: 100MB hard cap; transcript JSONs are well under.
