# Schema reference

## D1 (`ayc-ljs-db`)

Source of truth: `~/code/ayc.ljs.app/schema/001_init.sql` and `002_favorites.sql`.

### `channels`

| Column | Type | Notes |
|---|---|---|
| `id` | TEXT PK | YouTube channel id (e.g. `UCopqtPoYi92ZMdXEGWXaPTA`) |
| `handle` | TEXT NOT NULL | The `@handle` form (no leading `@`) |
| `display_name` | TEXT | Human-readable name |
| `submitted_by_email` | TEXT | If accepted from a `/suggest` submission |
| `added_at` | TEXT default now | When admin accepted / operator added |
| `created_at` | TEXT default now | Row creation time |

### `videos`

| Column | Type | Notes |
|---|---|---|
| `id` | TEXT PK | YouTube video id |
| `channel_id` | TEXT NOT NULL FK | → `channels.id` |
| `title` | TEXT NOT NULL | |
| `duration_seconds` | INTEGER | NULL for shorts when yt-dlp couldn't get it from `--flat-playlist` |
| `thumbnail_url` | TEXT | |
| `published_at` | TEXT | NULL for most existing rows; not backfilled |
| `form` | TEXT | `long`, `short`, or `unknown` |
| `transcript_source` | TEXT | `auto`, `manual`, or null |
| `transcript_r2_key` | TEXT | `transcripts/<channel_id>/<video_id>.json` once uploaded |
| `ingest_status` | TEXT NOT NULL | `pending`, `transcribed`, `chunked`, `embedded`, `skipped_no_captions`, or error string |
| `error` | TEXT | Free-form error if last action failed |
| `enumerated_at` | TEXT default now | When `ayc init` first saw it |
| `transcribed_at` | TEXT | When R2 upload completed |
| `chunked_at` | TEXT | When `chunks:bulk` last fired (with replace=true) |
| `embedded_at` | TEXT | When the last chunk got its embedding |

Indexes: `(channel_id, ingest_status)`, `(ingest_status)`, `(form)`.

### `chunks`

| Column | Type | Notes |
|---|---|---|
| `id` | TEXT PK | UUID4 hex assigned at merge time |
| `video_id` | TEXT NOT NULL FK | → `videos.id` |
| `channel_id` | TEXT NOT NULL FK | Denormalized so Vectorize metadata can filter by channel without a D1 round-trip |
| `kind` | TEXT NOT NULL | `qa` or `objection` |
| `start_seconds` | REAL NOT NULL | |
| `end_seconds` | REAL NOT NULL | |
| `question` | TEXT NOT NULL | The question or objection (canonicalized) |
| `answer` | TEXT NOT NULL | The answer or rebuttal |
| `speaker` | TEXT | Best-effort — usually null |
| `topics` | TEXT | JSON array of 1-4 short tags |
| `confidence` | REAL | 0.0-1.0, agent-assigned |
| `embedded_at` | TEXT | Non-null = vector exists in Vectorize for this id |
| `created_at` | TEXT default now | |

Indexes: `(video_id)`, `(channel_id, kind)`, `(id) WHERE embedded_at IS NULL` (partial — fast lookup of unembedded chunks).

### `channel_submissions`

| Column | Type | Notes |
|---|---|---|
| `id` | TEXT PK | UUID at insert time |
| `channel_url` | TEXT NOT NULL | As submitted, may be `@handle` or full URL |
| `resolved_channel_id` | TEXT FK | → `channels.id` once accepted |
| `submitted_by_email` | TEXT NOT NULL | From the JWT payload at submit time |
| `status` | TEXT default `pending` | `pending`, `accepted`, `rejected`, `duplicate` |
| `note` | TEXT | Admin note when triaging |
| `created_at` | TEXT default now | |

Index: `(status, created_at)`.

### `service_tokens`

| Column | Type | Notes |
|---|---|---|
| `id` | TEXT PK | UUID at mint time |
| `token_hash` | TEXT UNIQUE | `sha256(SERVICE_TOKEN_SALT + plaintext)` |
| `label` | TEXT NOT NULL | Human-readable identifier |
| `scopes` | TEXT default `[]` | JSON array (currently unused beyond presence) |
| `created_at` | TEXT default now | |
| `last_used_at` | TEXT | Updated on every successful auth |
| `revoked_at` | TEXT | Non-null = token rejected |

### `user_favorites`

| Column | Type | Notes |
|---|---|---|
| `user_email` | TEXT NOT NULL | Composite PK part 1 |
| `chunk_id` | TEXT NOT NULL FK | → `chunks.id` (composite PK part 2) |
| `created_at` | TEXT default now | |

PK is `(user_email, chunk_id)`. Index: `(user_email, created_at DESC)`.

## Vectorize (`ayc-chunks`)

- 1536 dimensions (OpenAI `text-embedding-3-small`)
- Cosine similarity
- Vector id = `chunks.id` (hex UUID)
- Metadata: `{kind, channel_id, confidence}` only — anything else is hydrated from D1 by id
- Supports filtered queries via `filter: {kind: 'qa'}` or `filter: {channel_id: '...'}`

Why so few metadata fields: changing metadata on Vectorize requires a full re-upsert. Keep what you'd actually filter by.

## R2 (`ayc-transcripts`)

- Key shape: `transcripts/<channel_id>/<video_id>.json`
- Content: yt-dlp JSON3 captions, deduped + normalized to `{schema_version, video_id, source, segments: [{start, end, text}]}`
- Average size: 10-100KB per video

## Embed text format

The text that gets embedded for each chunk is **not** just `answer`. See `_embed_text()` in `ayc/embed.py`. The format is:

```
<kind>: <question>

<answer>
```

This means re-embedding requires either reading the chunk from D1 and reconstructing, or storing the embed text alongside the vector. We chose to keep the format reproducible from the D1 row so we don't double-store text.

Migration scripts and gap-fill logic both rely on this — if you change the format, every existing vector is stale.
