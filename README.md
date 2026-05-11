# ask-youtube-channel (AYC)

A searchable index of every question and every objection raised across one or more YouTube channels — paired with the exact moment in the video where it was answered.

For video editors, researchers, content teams, and anyone who's ever lost a conversation to *"I know there's a clip about this somewhere…"*

## What's in this repo

This is the **operator-side Python pipeline**. It enumerates channels, fetches transcripts, dispatches Claude Code subagents to extract Q&A and objection-rebuttal moments, and POSTs the results to a cloud backend.

The cloud backend (Cloudflare Worker, browse UI, admin dashboard, suggest form) lives in a sibling repo. Path is configurable per deployment; this repo's defaults assume `~/code/ayc.ljs.app/`.

## Quick orientation

| What | Where |
|---|---|
| Project pitch + status | this README |
| Get running on a fresh laptop | [`docs/getting-started.md`](docs/getting-started.md) |
| How the two repos relate | [`docs/architecture.md`](docs/architecture.md) |
| CLI commands | [`docs/reference/cli.md`](docs/reference/cli.md) |
| Internal HTTP API | [`docs/reference/api.md`](docs/reference/api.md) |
| D1 schema | [`docs/reference/schema.md`](docs/reference/schema.md) |
| Env vars + secrets | [`docs/reference/env-and-secrets.md`](docs/reference/env-and-secrets.md) |
| Common operational tasks | [`docs/how-tos.md`](docs/how-tos.md) |
| Why things are the way they are | [`docs/explanation.md`](docs/explanation.md) |

## At a glance

```
              YOU (operator)                       Cloudflare (cloud)
              ────────────                         ──────────────────
    ┌──────────────────────────────┐    ┌───────────────────────────────┐
    │  ayc CLI         (Python)    │ ←→ │  ayc.ljs.app Worker (Hono/TS) │
    │  ayc-chunker     (CC agent)  │    │  D1 + Vectorize + R2          │
    │  /ayc-runbook    (CC cmd)    │    │  /browse, /dashboard, /suggest│
    │  /ayc-rechunker  (CC cmd)    │    │  /internal/* (service-token)  │
    └──────────────────────────────┘    └───────────────────────────────┘
            ↓                                            ↑
       yt-dlp (YouTube)                       Browser users + admin
       OpenAI (embeddings)
       Anthropic / CC subscription (chunking)
```

The Python pipeline drives ingestion. The Worker owns persistent state and serves all human-facing surfaces.

## Per-deployment configuration

The chunker prompt embeds short illustrative examples ("the kind of objection your channel actually answers"). Defaults in [`config/examples.toml`](config/examples.toml) are deliberately generic so the platform stays domain-neutral. To customize for your own deployment, copy that file to `config/examples.local.toml` and edit — the `.local` file is gitignored and takes precedence at runtime. See [`config/README.md`](config/README.md) for details.

Channels are not configured in this repo. Add them at runtime with `ayc init <channel-url>` (see [`docs/reference/cli.md`](docs/reference/cli.md)).

## Personal / support

Made by [Scott Pierce](https://askscottpierce.com). Sponsored by Left Join Studio, Inc.
