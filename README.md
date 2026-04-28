# ask-youtube-channel (AYC)

A searchable index of every question and every objection raised across abolitionist YouTube channels — paired with the exact moment in the video where it was answered. Live at [ayc.ljs.app](https://ayc.ljs.app).

For sidewalk counselors, video editors, researchers, and anyone who's ever lost a conversation to *"I know there's a clip about this somewhere…"*

## What's in this repo

This is the **operator-side Python pipeline**. It enumerates channels, fetches transcripts, dispatches Claude Code subagents to extract Q&A and objection-rebuttal moments, and POSTs the results to the cloud backend.

The cloud backend (Cloudflare Worker, browse UI, admin dashboard, suggest form) lives in a sibling repo at `~/code/ayc.ljs.app/`.

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

## Status

Live, indexed across 6 abolitionist channels:

- @AbolitionistsRising
- @AbolishHumanAbortion
- @abolish_abortion_canada
- @abolishabortionpa9847
- @abolishabortionnc
- @abolishabortionky

~2,300 videos enumerated, ~7,000+ chunks (and growing — see [`docs/how-tos.md`](docs/how-tos.md) § *Re-chunk under updated rules*).

## Personal / support

Made by [Scott Pierce](https://askscottpierce.com). Sponsored by Left Join Studio, Inc. Reach out via the personal site for questions or to suggest a channel for indexing.
