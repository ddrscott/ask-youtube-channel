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

## Stand up a second deployment (separate data)

When you want a different channel to live in its own D1, its own Vectorize index, and its own browse UI — no chunk bleed-through, no shared dashboard, no shared service token. Same Cloudflare account is fine; the resources are namespaced.

Not what you want for *"just add another channel to the existing index"* — for that, `ayc init <new-channel-url>` against the existing deployment is the right answer (see [Add a new channel to the index](#add-a-new-channel-to-the-index)).

**Cost:** one extra Worker (free tier), one extra D1 (free tier covers up to 10), one extra Vectorize index (paid feature — check pricing), one extra R2 bucket (free tier). Two `.env` files and two repo clones on your laptop. **No code changes in either repo** — both are already designed for this; the binding from Python CLI to Worker is entirely env-driven.

### Worker side — ordering matters

The key safety rule: lock the new deployment's *identity* in `wrangler.toml` (Worker `name` + binding names) and scope your shell to the right Cloudflare account **before** running any `wrangler` command. Wrangler reads `wrangler.toml` and the ambient Cloudflare auth — if either still points at the original deployment, `wrangler deploy` could clobber production. IDs from `create` commands get pasted back in after; that's safe because by then the identity is already locked.

1. **Clone the Worker repo.**
   ```sh
   cd ~/code
   git clone <ayc.ljs.app remote> ayc-channel2.ljs.app
   cd ayc-channel2.ljs.app
   ```

2. **Scope wrangler to the right account.** Confirm with `wrangler whoami`, then export `CLOUDFLARE_ACCOUNT_ID` (and `CLOUDFLARE_API_TOKEN` if using API-token auth) in a per-clone `.envrc` (direnv) or a sourced script. If `wrangler.toml` hardcodes `account_id = "..."` for the original account, delete that line so the env var wins, or update it to the new account ID.

3. **Edit `wrangler.toml` FIRST — change identity, leave IDs blank:**
   - `name = "ayc-channel2"` (this is the line that, if wrong, overwrites the original Worker on deploy)
   - `[[d1_databases]].database_name = "ayc-channel2-db"`, `database_id = "TBD"` (placeholder — filled after step 4)
   - `[[vectorize]].index_name = "ayc-channel2-chunks"`
   - `[[r2_buckets]].bucket_name = "ayc-channel2-transcripts"`

   At this point any accidental `wrangler deploy` fails on the placeholder bindings rather than touching production.

4. **Provision Cloudflare resources.** Eyeball each command's output to confirm it created the `ayc-channel2-*` resource, not an `ayc-*` one.
   ```sh
   wrangler d1 create ayc-channel2-db
   # → copy returned database_id into wrangler.toml [[d1_databases]].database_id
   wrangler d1 execute ayc-channel2-db --remote --file=schema/001_init.sql
   wrangler d1 execute ayc-channel2-db --remote --file=schema/002_favorites.sql
   wrangler vectorize create ayc-channel2-chunks --dimensions 1536 --metric cosine
   wrangler vectorize create-metadata-index ayc-channel2-chunks --property-name kind --type string
   wrangler vectorize create-metadata-index ayc-channel2-chunks --property-name channel_id --type string
   wrangler r2 bucket create ayc-channel2-transcripts
   ```

5. **Set secrets** (full rationale in [`reference/env-and-secrets.md`](reference/env-and-secrets.md)):
   ```sh
   wrangler secret put JWT_SECRET            # same value as auth.ljs.app (shared cookie auth)
   wrangler secret put SERVICE_TOKEN_SALT    # ≥32 random bytes, new for this deployment
   wrangler secret put DISCORD_WEBHOOK_URL   # optional
   ```

6. **Deploy** and sanity-check:
   ```sh
   npm run deploy
   curl https://ayc-channel2.<subdomain>.workers.dev/healthz
   # → {"ok": true, ...}
   ```

7. **Mint a service token** for the new deployment as in [Mint a service token](#mint-a-service-token), but hit the new Worker URL. Save the `ayc_<hex>` value — you'll paste it into the Python clone's `.env` next.

### Python side — clone, configure, run

1. **Clone this repo to a separate directory.**
   ```sh
   cd ~/code
   git clone <ask-youtube-channel remote> ask-youtube-channel-channel2
   cd ask-youtube-channel-channel2
   uv sync
   ```

2. **Create `.env`** (gitignored) pointing at the new Worker:
   ```env
   AYC_API_BASE_URL=https://ayc-channel2.<subdomain>.workers.dev
   AYC_API_TOKEN=ayc_<minted above>
   OPENAI_API_KEY=<your existing key — fine to reuse>
   ```

3. **Customize the chunker prompt** for the new channel's voice (optional but worth it — better examples mean better extraction):
   ```sh
   cp config/examples.toml config/examples.local.toml
   # Edit with phrasings the new channel actually uses. Gitignored, repo-local.
   ```

4. **Drive ingestion exactly like the first channel.**
   ```sh
   uv run ayc init "https://www.youtube.com/@new-channel-handle"
   uv run ayc status   # confirms you're hitting the new backend — counts are isolated
   ```
   Then follow [Add a new channel to the index](#add-a-new-channel-to-the-index) from `/ayc-runbook` onward.

### Verifying isolation

- `ayc status` in the new clone shows only the new channel's counts.
- `ayc status` in the *original* clone still shows only the original channel's counts.
- The Cloudflare dashboard's Vectorize page lists two indexes (`ayc-chunks` and `ayc-channel2-chunks`) with independent row counts.

### Why clone, not `uv tool install`

The per-deployment prompt examples (`config/examples.local.toml`) are resolved relative to the package's repo root in `ayc/prompts.py`, and the built wheel only ships the `ayc/` package — not the `config/` directory. Installing as a tool would land the generic defaults with no override path. Cloning sidesteps this entirely; the Python repo doesn't change often, and `git pull` in each clone is cheap.

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
