# How-to guides

Recipes for common operational tasks. Each assumes you've done [`getting-started.md`](getting-started.md).

## Add a new channel to the index

1. Find the channel handle or URL.
2. Enumerate the catalog:
   ```sh
   uv run ayc init "https://www.youtube.com/@ChannelHandle"
   ```
   Idempotent if the channel is already in the database — re-running picks up new uploads.
3. Process the new videos:
   ```
   /ayc-runbook --form long --limit 25
   ```
   Repeat until `ayc status` shows the channel's `pending` count at 0 (or only `skipped_no_captions` remains).

## Re-chunk a single video

Useful after the chunker creed changes, or if you suspect a video's chunks are too sparse.

```sh
# Spot-check the bottom of /dashboard's "Under-extraction candidates" list,
# then pick a video id (e.g. sTbtdokaRX0).
```

```
/ayc-rechunker --video sTbtdokaRX0
```

The runbook will call `prepare_single`, augment with gap-fill metadata, dispatch one `ayc-chunker`, and merge with `replace=False`. Existing chunks are preserved; new chunks land in any uncovered gaps.

## Re-chunk under updated rules (whole catalog or a slice)

When you've tuned `.claude/agents/ayc-chunker.md` and want to apply the new rules to existing videos.

```sh
# 1. Make sure the queue is empty
ls queue/pending queue/completed 2>/dev/null

# 2. Pull transcripts for all already-chunked videos into the queue
uv run ayc queue prepare --rechunk --form long --limit 0

# 3. Augment with gap metadata (existing chunks + gap ranges per video)
uv run ayc queue augment-gapfill

# 4. Dispatch agents in parallel batches of 3
/ayc-rechunker --form long --limit 50    # or larger; cap at 3 parallel
```

After each batch the runbook periodically flushes via `ayc queue merge` and `ayc embed` so progress lands in D1/Vectorize incrementally and you can watch the dashboard tick up.

The merge step uses `replace=false` and programmatically filters new chunks to those overlapping a declared gap by ≥20s. Even if the agent drifts back to full-pass behavior, only true gap content lands. See [`explanation.md`](explanation.md) § *Why gap-fill*.

## Investigate a quarantined / failed chunking

`ayc queue merge` runs the verifier first. Files that fail verification get moved to `queue/failed/<id>.verify-error.txt` along with a description of what's wrong (timestamps out of range, missing `confidence`, malformed JSON, etc.).

```sh
ls queue/failed/                                    # what failed
cat queue/failed/<id>.verify-error.txt              # why
cat queue/pending/<id>.json | jq .duration_seconds  # what the agent had
```

To retry one specific video after fixing the agent's behavior:

```sh
# Move the bad completed file out of the way
mv queue/failed/<id>.verify-error.txt /tmp/
# Re-dispatch the agent on the still-present pending file
```

In Claude Code:

```
Process the file /Users/spierce/code/ask-youtube-channel/queue/pending/<id>.json
```

…via the `ayc-chunker` subagent.

## Deploy the Worker

```sh
cd ~/code/ayc.ljs.app
npm install
npm run typecheck                # `tsc --noEmit`; inline diagnostics may lie, this is the source of truth
npm run deploy                   # wrangler deploy
```

The deployed Worker shows up at:

- `https://ayc.ljs.app` (custom domain)
- `https://ayc-ljs-app.ddrscott.workers.dev` (workers.dev fallback)

Check `https://ayc.ljs.app/healthz` after deploy — should return `{"ok":true,"service":"ayc.ljs.app","environment":"production"}`.

## Run a D1 migration

```sh
cd ~/code/ayc.ljs.app
# Local D1 (for dev)
wrangler d1 execute ayc-ljs-db --file=schema/00X_my_migration.sql
# Remote D1 (production)
wrangler d1 execute ayc-ljs-db --remote --file=schema/00X_my_migration.sql
```

D1 doesn't accept `BEGIN`/`COMMIT` in SQL files — wrap multi-statement migrations as plain statements separated by `;` and let D1 batch them.

## Mint a service token

Sign in at `https://ayc.ljs.app` with an `admin`-scope account, then:

```sh
curl -X POST https://ayc.ljs.app/admin/service-tokens \
  -H "Cookie: session=<your-session-cookie>" \
  -H "Content-Type: application/json" \
  -d '{"label": "operator-laptop-2026-04", "scopes": ["pipeline:write"]}'
```

The response includes a `token` field — **copy it immediately, it is never shown again**. Save to `.env` as `AYC_API_TOKEN=ayc_<that-value>`.

To revoke an old token: query D1 directly:

```sh
wrangler d1 execute ayc-ljs-db --remote \
  --command "UPDATE service_tokens SET revoked_at = datetime('now') WHERE label = 'operator-laptop-old'"
```

## Run a manual SQL query against D1

```sh
cd ~/code/ayc.ljs.app
wrangler d1 execute ayc-ljs-db --remote --json \
  --command "SELECT ingest_status, COUNT(*) FROM videos GROUP BY ingest_status"
```

Use `--remote` for production, omit it for the local D1 (for dev).

## Pause / resume a long-running rechunk

The queue is checkpoint-safe. To pause:

1. Stop dispatching new agents (just stop in the conversation).
2. Run a final flush so completed chunks land in the cloud:
   ```sh
   uv run ayc queue merge
   uv run ayc embed
   ```
3. Leave `queue/pending/*.json` files in place — they represent the remaining work.

To resume in a future session, the pending files are still there. Dispatch the next batch the same way.

## Watch a long-running CLI command in the background

The Python CLI commands take a while when run on the full catalog. Patterns:

```sh
# In a separate terminal (keeps Claude Code free to dispatch agents)
uv run ayc transcripts --form long --limit 0 2>&1 | tee /tmp/ayc-transcripts.log

# Or via Claude Code's background bash
# (run_in_background=true on the Bash tool)
```

For polling progress, count files:

```sh
watch -n 30 'ls queue/pending | wc -l && ls queue/completed | wc -l'
```
