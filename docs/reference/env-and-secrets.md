# Env vars and secrets

Two repos, two configuration surfaces. Keep them in sync.

## Python repo (`ask-youtube-channel`)

Local-only. Loaded from `.env` via `python-dotenv` in `ayc/config.py`.

| Variable | Required | What it does |
|---|---|---|
| `AYC_API_BASE_URL` | yes | Cloud API base — `https://ayc.ljs.app` or `https://ayc-ljs-app.ddrscott.workers.dev`. Both work. |
| `AYC_API_TOKEN` | yes | Bearer service token (`ayc_<hex>`). Mint via `POST /admin/service-tokens` (see [*Mint a service token*](../how-tos.md#mint-a-service-token)). |
| `OPENAI_API_KEY` | yes for `ayc embed` | OpenAI key for `text-embedding-3-small` |

There used to be `ANTHROPIC_API_KEY` (for an old API-based chunker) and `db_path` / `transcripts_dir` (for local SQLite/disk caches). Both are gone — chunking runs on the Claude Code subscription via the `ayc-chunker` subagent, and all persistent data lives in D1/R2/Vectorize.

`.env` is gitignored. Rotate the service token by minting a new one and replacing the value, then `POST /admin/service-tokens` to revoke the old.

## Worker repo (`ayc.ljs.app`)

Two kinds of config: plain vars (in `wrangler.toml`) and secrets (set via `wrangler secret put`).

### Vars (`wrangler.toml [vars]`)

| Variable | Value | What it does |
|---|---|---|
| `ENVIRONMENT` | `production` | Echoed in `/healthz` for sanity |
| `AUTH_URL` | `https://auth.ljs.app` | Where unauthenticated browsers go for sign-in |
| `PLAUSIBLE_HOST` | `plausible.ljs.app` | Self-hosted Plausible. Empty/unset = no tracking script (useful for dev/preview) |

Edit and `npm run deploy` to change.

### Secrets (`wrangler secret put <NAME>`)

| Secret | Required | Source |
|---|---|---|
| `JWT_SECRET` | yes | **Same value** as auth.ljs.app's `JWT_SECRET`. Both services verify the same magic-link cookies. |
| `SERVICE_TOKEN_SALT` | yes | Random ≥32 bytes, generated once. Used as `sha256(salt + plaintext)` to hash service tokens at rest in `service_tokens.token_hash`. **Rotating this invalidates every minted token.** |
| `DISCORD_WEBHOOK_URL` | optional | If set, `/api/suggest` posts an embed to Discord on every submission. |

Set/rotate with:

```sh
cd ~/code/ayc.ljs.app
wrangler secret put JWT_SECRET           # paste at the prompt
wrangler secret put SERVICE_TOKEN_SALT
wrangler secret put DISCORD_WEBHOOK_URL
```

List configured secrets:

```sh
wrangler secret list
```

You can't read a secret's value back from Cloudflare — only re-set it.

### Bindings (`wrangler.toml [[d1_databases]] / [[vectorize]] / [[r2_buckets]]`)

These are not "secrets" but they are environment-coupled identifiers:

| Binding | Resource | ID / name |
|---|---|---|
| `DB` | D1 | `ayc-ljs-db` (`7251d8d8-4e69-4509-a466-119e581dda2d`) |
| `VECTOR` | Vectorize | `ayc-chunks` |
| `BUCKET` | R2 | `ayc-transcripts` |

These were provisioned once. To rebuild them in another account, you'd run:

```sh
wrangler d1 create ayc-ljs-db
wrangler d1 execute ayc-ljs-db --remote --file=schema/001_init.sql
wrangler d1 execute ayc-ljs-db --remote --file=schema/002_favorites.sql
wrangler vectorize create ayc-chunks --dimensions 1536 --metric cosine
wrangler vectorize create-metadata-index ayc-chunks --property-name kind --type string
wrangler vectorize create-metadata-index ayc-chunks --property-name channel_id --type string
wrangler r2 bucket create ayc-transcripts
```

Then update the IDs in `wrangler.toml` and redeploy.

## Sanity-check checklist

If `ayc status` works → `AYC_API_BASE_URL` + `AYC_API_TOKEN` are good.

If `/healthz` returns `{"ok": true}` but `/browse` 401s in the browser → `JWT_SECRET` mismatch with auth.ljs.app.

If `/admin/*` 403s after sign-in → your magic-link session doesn't have the `admin` scope. Grant in the auth.ljs.app admin tooling.

If `/api/suggest` succeeds but nothing appears in Discord → `DISCORD_WEBHOOK_URL` is unset or wrong (the endpoint silently no-ops if it's empty).
