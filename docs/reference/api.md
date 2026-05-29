# API reference

The Cloudflare Worker at `ayc.ljs.app` (also `ayc-ljs-app.ddrscott.workers.dev`) exposes several families of routes — the pipeline API, the channel-scoped read API, the cookie-gated browse API, admin, and HTML pages. Source: `~/code/ayc.ljs.app/src/routes/`.

## Auth gates

| Family | Auth | Set in |
|---|---|---|
| `/internal/*` | `Authorization: Bearer ayc_<token>` (pipeline tokens) | `src/middleware/service.ts` |
| `/api/v1/*` | `Authorization: Bearer ayc_<token>` with the `read` scope | `src/middleware/read-token.ts` |
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

## `/api/v1/*` — channel-scoped read API

The shared, public read layer that any number of frontends build on. Service-token gated, requiring the `read` scope (`src/middleware/read-token.ts`). Source: `src/routes/api.ts`.

Every request is scoped to a set of channel ids. The **effective** set is `requested ∩ token grant`:

| Token `channel_scope` | Caller omits `channels` | Caller sends `channels` |
|---|---|---|
| `null` (unrestricted) | `400 channels_required` — never dumps the catalog | the requested set, verbatim |
| `["UC_a", …]` (restricted) | defaults to the full grant | the intersection; `403 no_permitted_channels` if empty |
| `[]` (empty grant) | `403 no_permitted_channels` | `403 no_permitted_channels` |

Scoping is enforced in both the authoritative D1 query (`channel_id IN (…)`) and the Vectorize filter (`channel_id $in […]`), so a token can never reach a channel outside its grant.

| Method | Path | Body / params | Returns |
|---|---|---|---|
| GET | `/api/v1/chunks?channels=&kind=&topic=&cursor=&limit=` | `channels` = comma-separated ids; `limit` ≤ 500 (default 200) | `{chunks: [...], next_cursor: string\|null}` — id-ordered, cursor-paginated |
| GET | `/api/v1/chunks/:id?channels=` | query | `{chunk}`, or `404 chunk_not_found` (also for in-DB but out-of-scope ids) |
| GET | `/api/v1/chunks/:id/similar?channels=&kind=&top_k=` | `top_k` 1–100 (default 20) | `{source, neighbors: [{...chunk, score}]}` |
| POST | `/api/v1/search` | `{query, channels?, kind?, top_k?}`; `query` ≤ 500 chars | `{matches: [{...chunk, score}]}` |

`/search` embeds the query text server-side with OpenAI `text-embedding-3-small` (1536d, matching the pipeline's `AYC_EMBED_MODEL`), then runs a scoped Vectorize query. The `:id/similar` endpoint reuses the source chunk's stored vector (no embedding call) and confirms the source is in scope before returning neighbors.

**Error codes:** `401 missing_bearer_token` / `invalid_or_revoked_token`; `403 insufficient_scope` (token lacks `read`); `400 channels_required` / `query_required` / `query_too_long`; `403 no_permitted_channels`; `502 embedding_failed` (OpenAI error on `/search`).

## `/admin/*` + `/dashboard` — admin

Cookie + `admin` scope.

| Method | Path | Purpose |
|---|---|---|
| GET | `/dashboard` | Server-rendered HTML with KPIs, per-channel table, under-extraction candidates, recent activity, suggestion queue |
| GET | `/dashboard/tokens` | Token management UI — mint (scope checklist + channel-scope picker), list, and revoke service tokens |
| GET | `/admin/whoami` | Sanity check — `{email, scopes}` |
| POST | `/admin/service-tokens` | Mint a bearer token. Body `{label, scopes?, channel_scope?}`: `scopes` defaults to `["pipeline:write"]`; `channel_scope` (array of channel ids) locks a `read` token to those channels — omit for unrestricted. Plaintext returned ONCE. |
| GET | `/admin/service-tokens` | List active tokens (no plaintext) |
| POST | `/admin/service-tokens/:id/revoke` | Revoke a token — immediate, permanent |
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
| `/dashboard/tokens` | cookie + admin scope | Token management UI |
| `/login` | none | Redirect to auth.ljs.app/login |

## Rate limits + quotas

- **D1**: ~5M rows/db, 50k writes/day on free, more on paid. Current usage well under.
- **Vectorize**: 5M vectors/index. ~7k currently. 1000 vectors/upsert call cap (we use 500).
- **R2**: storage essentially free at this volume.
- **Worker request body**: 100MB hard cap; transcript JSONs are well under.
