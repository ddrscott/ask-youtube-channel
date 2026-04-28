# AYC documentation

Organized per the [Diataxis](https://diataxis.fr) framework: tutorial, how-to, reference, explanation.

| Type | When to read |
|---|---|
| [`getting-started.md`](getting-started.md) (tutorial) | New to the project — clone, configure, first end-to-end run |
| [`architecture.md`](architecture.md) (explanation) | Want the whole system at a glance |
| [`how-tos.md`](how-tos.md) (how-to) | "I need to do X right now" |
| [`reference/`](reference/) (reference) | Exact CLI flags, API shapes, schema, env vars |
| [`explanation.md`](explanation.md) (explanation) | "Why is it like this?" — design rationale |
| [`landing-page.md`](landing-page.md) | Working notes on positioning / competitive landscape (not user docs) |

## Reference index

| File | What it covers |
|---|---|
| [`reference/cli.md`](reference/cli.md) | `ayc` CLI commands (init, transcripts, embed, status, queue *) |
| [`reference/api.md`](reference/api.md) | Worker HTTP endpoints — `/internal/*`, `/api/*`, `/admin/*`, `/dashboard`, `/browse` |
| [`reference/schema.md`](reference/schema.md) | D1 tables + indexes; Vectorize metadata; R2 key conventions |
| [`reference/env-and-secrets.md`](reference/env-and-secrets.md) | Every env var and Worker secret, where it's set, what consumes it |

## For AI agents picking this up cold

Start with [`architecture.md`](architecture.md) — it answers the four questions that matter:
1. Which storage layer owns what?
2. Where does the chunking work happen?
3. How does the Python repo talk to the Cloud repo?
4. What's the data flow for a brand-new video → searchable chunk?

Then skim [`reference/cli.md`](reference/cli.md) and [`reference/api.md`](reference/api.md) so you know the verbs available to you. The how-tos chain those verbs into recipes.

The chunker subagent spec lives at [`/.claude/agents/ayc-chunker.md`](../.claude/agents/ayc-chunker.md) — read that whenever you're about to dispatch a chunking run, since the rules there govern output quality.
