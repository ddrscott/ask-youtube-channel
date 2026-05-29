# Explanation

Why things are the way they are. Read this when something seems weird and you suspect there's a reason.

## Why a filesystem queue (instead of D1 work-queue rows)

The chunker is the expensive step. Running it as a Claude Code subagent shifts the cost from the Anthropic API to the operator's CC subscription — substantial savings on a long catalog.

But CC subagents are file-shaped. They don't authenticate against external services on the operator's behalf, and they can't easily call back into D1. They read files, write files, report a one-line summary.

So the queue is the IPC contract:

- Python writes `queue/pending/<id>.json` (transcript + optional gap metadata)
- The subagent reads it, writes `queue/completed/<id>.chunks.json`
- Python `ayc queue merge` reads the completed files and POSTs to the cloud

The chunker never knows about D1, service tokens, or auth. It does the one thing — extract chunks from a transcript — and stays in its lane. All persistence concerns are Python's job.

A side benefit: the queue is checkpoint-safe. Pending files survive across sessions. If you stop mid-run, picking up where you left off is just dispatching the next batch.

## Why gap-fill mode (instead of full re-chunking under updated rules)

When the chunker creed evolves, the natural impulse is to re-run from scratch on every video and replace existing chunks. We tried that on a smoke test of 3 podcasts/interviews — net result was −2, −1, +4 chunks. Not regression-free, and not a meaningful improvement.

The proven pattern (from the manual second-pass on `sTbtdokaRX0`, which went 16 → 29 chunks): give the agent **explicit gap timestamps** and tell it "find what's missed in these specific ranges." That worked because the agent had a concrete forcing function rather than abstract "be more comprehensive" guidance.

Generalizing that pattern → the gap-fill flow:

1. Augment each pending file with `existing_chunks`, `covered_ranges`, `gap_ranges`
2. Chunker reads `gap_fill: true` and (in theory) extracts only in gap ranges
3. Merge step **programmatically filters** new chunks to those overlapping a declared gap by ≥20 seconds — even if the agent drifts back to full-pass behavior, only true gap content lands
4. Insert with `replace=False`, preserving every existing chunk ID

The programmatic filter is the hard guarantee. The agent's gap-respecting behavior is best-effort, but the filter doesn't trust it.

Tradeoff: gap-fill never widens narrow chunks or fixes incorrect ones. It only adds chunks in genuinely uncovered windows. For everything else, you'd need a true full re-chunk — which we'd only do if the existing chunks were *worse* than what a fresh pass would produce, and so far that hasn't been the case.

## Why two repos

- TypeScript and Python toolchains stay clean (`uv` vs `npm`)
- The Worker can be cloned and deployed without the Python dependencies
- The Python pipeline can be developed offline against a mock without touching the Worker
- Different release cadences: the Worker deploys on every `npm run deploy`; the Python pipeline doesn't "deploy" — it just runs

They share state only through HTTP (`/internal/*` API) and a shared `JWT_SECRET` so both verify the same auth.ljs.app session cookies.

## Why service tokens (instead of JWTs for the pipeline too)

The pipeline runs on the operator's laptop and is essentially long-lived infrastructure. JWTs are short-lived by design — refreshing them constantly would be friction. Service tokens are long-lived but rotatable, audited via `last_used_at`, and revocable by setting `revoked_at`.

They're stored hashed (`sha256(SERVICE_TOKEN_SALT + plaintext)`) so a database leak doesn't compromise active tokens.

## Why the embed text format isn't stored

The text that gets embedded for a chunk is `"<kind>: <question>\n\n<answer>"` (see `_embed_text()` in `ayc/embed.py`). We don't store this string — we reconstruct it from the D1 row at re-embed time.

Tradeoff: changing the format invalidates every existing vector. The savings (no double-storage of long answer text) outweigh the cost given how rarely we'd change it.

If you do need to change the format, the safe migration path is:

1. Add the new format as an alternative
2. Re-embed every chunk under the new format (writing to a parallel index or tagging vectors with a `format_version`)
3. Cut over

We haven't had to do this yet.

## Why `chunks.channel_id` is denormalized

Vectorize metadata can filter by `channel_id` directly. Without the denorm, every filtered query would need a D1 join after the vector search to filter by channel. With the denorm, the filter happens at vector-query time.

The cost: every `INSERT INTO chunks` writes 2 ids (video_id and channel_id) and a small risk of inconsistency if a video ever moved channels (it can't, on YouTube — channels own videos forever).

## Why the chunker spec lives in the Python repo

`.claude/agents/ayc-chunker.md` is the single file that controls extraction quality. It lives with the Python code because the Python `ayc queue prepare` writes the input file format and `ayc queue merge` reads the output. Changing the spec without updating the Python verifier or merge filter would break the contract.

If you change the spec, regenerate any in-flight queue and check that:
- The schema in the spec matches what `ayc/verify.py` expects
- New optional fields are documented in [`reference/schema.md`](reference/schema.md) under the queue file format

## Why the Plausible script is on `plausible.ljs.app` (self-hosted)

We didn't want to depend on plausible.io for analytics on a project whose privacy posture we publicly commit to. Self-hosting on our own infrastructure means:

- No third party sees user IPs
- We can keep the privacy page promise of "no third-party analytics that profile you across sites" honestly
- No surprise pricing changes

Plausible is cookieless and aggregate by design, so the tradeoffs are minimal.

## Why we don't sort by publish date in `/browse`

`videos.published_at` is NULL for most existing rows. yt-dlp's `--flat-playlist` mode doesn't return upload dates, and we used that for speed during initial enumeration.

Backfilling would require ~80 minutes of sequential yt-dlp metadata calls for the existing catalog. We've left it as a known limitation. Two paths forward, neither implemented yet:

1. One-shot script `scripts/backfill_publish_dates.py`
2. Capture opportunistically during the next `ayc transcripts` runs (yt-dlp returns it from the per-video metadata fetch)

Once `published_at` is populated, the browse UI's sort options can include it.

## Why the chunker creed reads like a manifesto

Earlier versions of the chunker were calibrated for monologue podcasts (1 chunk per few minutes, "be selective"). When applied to street debates with rapid back-and-forth, it under-extracted dramatically — 16 chunks on a 42-minute debate where the right answer was closer to 29.

The current creed reframes the agent around **editor trust**: "if the index misses real moments, the user goes back to scrubbing video by hand and never opens the app again." It includes:

- Density floors per content type (street debate: 1 chunk per 60-120s; podcast: 1 per 2-4 min; sermon: 1 per 3-6 min)
- A list of inclusion categories that previously got skipped (micro-objections, recurring objections, tone-policing-with-rebuttal, bystander interjections, long monologic objections)
- A coverage-audit Bash step that prints any 90+ second window with no chunk overlap and forces the agent to either fill it or justify the gap

The framing is intentional. Vague "be comprehensive" wording didn't move the LLM. Editor-trust framing (concrete consequence of failure) does.

## Why per-token channel scoping (one catalog, many frontends)

The catalog is one shared, deduped index — a channel is ingested once (the YouTube channel id is the primary key in `channels`, so the pipeline physically can't double-index it). But different audiences should see different subsets. Rather than run a separate D1 + Vectorize per audience — which would re-index any channel two audiences share — every audience is just a frontend holding a `read` token, and the token carries a fixed `channel_scope`. The [`/api/v1/*` read API](reference/api.md) enforces `effective = requested ∩ grant` on every call.

The tradeoff: isolation is **query-time, not physical**. A bug in the scope filter could leak another audience's channels. So the scope is funneled through one helper (`resolveChannelScope` in `src/services/channel-scope.ts`) and enforced in *both* the D1 query and the Vectorize filter — never just one, so a stale vector match can't surface an out-of-scope row. If an audience ever needs a hard security boundary (not just curation by interest), give it its own deployment and eat the duplicate index. For "different audiences by topic," query-time scoping is the right call.

This is the read-side mirror of [why `chunks.channel_id` is denormalized](#why-chunkschannel_id-is-denormalized): the denorm exists precisely so this channel filter is cheap at query time.
