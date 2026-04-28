# Getting started

End-to-end: clone, configure, ingest one video, see it in the browse UI. ~30 minutes if everything goes right.

By the end you'll have:

- The Python pipeline runnable on your laptop
- A service token to talk to the cloud
- One real video transcribed, chunked, embedded, and visible at [ayc.ljs.app/browse](https://ayc.ljs.app/browse)

This tutorial assumes you're cloning into `~/code/`. Adjust paths if not.

## What you need

- macOS or Linux
- [`uv`](https://github.com/astral-sh/uv) for Python (`brew install uv`)
- Node 20+ and npm (only if you'll deploy the Worker — ingest works without it)
- An OpenAI API key (chunk embeddings; <$1 for a small ingest)
- Claude Code installed and signed in (the chunker subagent runs on your CC subscription)
- A service token for `ayc.ljs.app/internal/*` — see [*Mint a service token*](how-tos.md#mint-a-service-token) if you don't have one

## 1. Clone

```sh
cd ~/code
git clone <ask-youtube-channel-url> ask-youtube-channel
git clone <ayc.ljs.app-url> ayc.ljs.app   # only if you'll touch the Worker
cd ask-youtube-channel
```

## 2. Install Python deps

```sh
uv sync
```

This creates `.venv/` and installs everything pinned in `uv.lock`. No global Python pollution.

## 3. Configure

Copy the example env and fill it in:

```sh
cp .env.example .env 2>/dev/null || touch .env
```

Edit `.env` to contain:

```sh
AYC_API_BASE_URL=https://ayc-ljs-app.ddrscott.workers.dev
AYC_API_TOKEN=ayc_<your-service-token>
OPENAI_API_KEY=sk-<your-openai-key>
```

You can use the `ayc.ljs.app` custom domain too — both work.

## 4. Confirm cloud connectivity

```sh
uv run ayc status
```

Should print a table of channels, videos by status, chunks by kind. If you get an auth error, the service token is wrong or revoked. If you get a connection error, check `AYC_API_BASE_URL`.

## 5. Restart Claude Code (one-time)

After cloning, **close and re-open Claude Code in this directory**. Project agents at `.claude/agents/*.md` are loaded at session start, so the `ayc-chunker` subagent type only becomes available after a restart. If you skip this, `/ayc-runbook` will tell you to restart anyway.

## 6. Ingest one channel

The full pipeline lives in a slash command:

```
/ayc-runbook --form long --limit 1
```

This runs every phase: transcribe one video, prepare a pending file, dispatch one `ayc-chunker` subagent, verify, merge, embed.

When it finishes, `ayc status` should show one new chunked + embedded video.

## 7. See it in the browse UI

Open [https://ayc.ljs.app/browse](https://ayc.ljs.app/browse) and sign in via the magic link. Your new video's chunks will be searchable immediately.

If you want a direct link to just that one video's chunks, the URL is `https://ayc.ljs.app/browse?video=<youtube_video_id>`.

## What you just exercised

| Step | What ran |
|---|---|
| 4. `ayc status` | API client → `/internal/stats` |
| 6. `/ayc-runbook` | `ayc transcripts` (yt-dlp + R2 upload) → `ayc queue prepare` (pull from R2 to disk) → `ayc-chunker` agent → `ayc queue merge` (POST `/internal/chunks:bulk`) → `ayc embed` (OpenAI + POST `/internal/chunks/embeddings:bulk`) |

You now have working knowledge of:

- The CLI (you just ran it)
- The runbook orchestration pattern
- The cloud API (every CLI command hits it)
- The chunker subagent (you watched one fire)

## Next steps

- **Common tasks**: see [`how-tos.md`](how-tos.md)
- **Re-chunk under updated rules** to widen coverage on existing videos: [`how-tos.md`](how-tos.md#re-chunk-under-updated-rules)
- **Deploy a Worker change**: see [`how-tos.md`](how-tos.md#deploy-the-worker)
- **Read the architecture overview** if you haven't: [`architecture.md`](architecture.md)
