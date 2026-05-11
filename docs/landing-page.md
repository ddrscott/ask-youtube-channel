# Landing Page Notes — ask-youtube-channel

Working notes for landing page copy and positioning. Hero is the **movement/podcast/creator video editor at a small shop**. Positioning rests on one inversion: competitors sell the synthesized answer with clips as footnotes; we sell the clips with synthesis as scaffolding.

## Competitive landscape

The "Q&A over a YouTube channel" idea is taken. Rather than pretending it isn't, this page exists to name where the field is crowded and where it isn't.

### Closest competitors

- **Transcribr.io** — almost identical elevator pitch. Chat with an entire YouTube channel, RAG, timestamped citations. Show HN'd July 2025. SaaS at $19.99/week or $49.99/month. Multi-tenant, not per-channel deploys.
- **FindInVideo** — per-channel indexing (creators pay an annual fee to opt in), returns timestamp deep links. Does not synthesize. Search engine, not answer engine.

### Adjacent

- **VERIDIVE / DeepContext** — cross-episode synthesis with timestamped citations + speaker ID. Podcast-only, centralized index over ~2,000 curated shows. Pre-launch.
- **Tapesearch** — cross-podcast transcript search + Q&A. Multi-tenant, not per-show.
- **ScreenApp Video Answer AI** — multi-video Q&A within user-built projects. Curated bundles, not channel-scoped.

### Single-video tools (not real competitors)

ChatTube, ChatPDF's YouTube mode, YouTLDR, Tactiq, Skimming.ai, Decopy, **YouTube's own conversational AI** (built into the watch page), vidIQ's AI Coach (creator-side analytics). All scoped to one video at a time.

### Where our design is actually different

1. **Per-channel deploy, owned by the channel.** Closer to Algolia DocSearch for a docs site than to anything in this list. Your subdomain, your D1, your branding.
2. **Q&A-shaped indexing, not generic chunking.** Every other tool does RAG over fixed-size chunks. Indexing on "moments where a question is asked and answered" is a different retrieval primitive — defensibly better for movement comms, sermon archives, and podcast back-catalogs that re-litigate the same questions.
3. **Clips-first UX, synthesis as scaffolding.** Everyone else leads with the AI answer and treats citations as footnotes. We lead with clip cards and demote synthesis to a paragraph above.
4. **"No match" as a feature.** None of the tools above emphasize refusing to answer when the channel didn't actually say it. Generative RAG happily synthesizes past the evidence. For movement comms (where wrong-but-confident answers are actively harmful), refusing to hallucinate is a product feature, not a guardrail.

If a piece of copy makes sense for Transcribr, delete it.

---

## StoryBrand: Personas considered

The competitors above all aim at the **viewer with a question**. That's the saturated lane. Three other heroes were considered before landing on the editor.

### Hero ranking

| Hero | Differentiation | Verdict |
|---|---|---|
| **Editor** | No competitor owns this lane. Quantifiable ROI: "one editor, one afternoon." Narrow audience but high-value. | **Lead with this.** |
| Activist (mid-conversation) | Emotionally compelling, demos beautifully ("clip in your pocket"). | **Use as the demo story**, not the lead. Activists don't buy software. |
| Channel owner / movement leader | They're the buyer for per-channel deploys. | **Use on sales/onboarding page**, not the homepage. Too inside-baseball for a hero. |
| New viewer | Largest TAM, easiest to explain. | **Avoid.** Saturated. Will get compared to Transcribr in 5 seconds. |

The villain across all four is the same: **linear video time**. Naming it explicitly lets all four heroes share one product without the homepage feeling like four homepages.

---

## BrandScript — Editor as Hero

### 1. Character (Hero)

The movement/podcast/creator video editor at a small shop. Often a team of one or two. Knows the channel's voice intimately — they've been hired *because* they know it. Time is the limiting reagent in everything they do.

**Wants:** To ship one more piece of content this week without sacrificing the weekend.

### 2. Problem

**Villain: The archive.** The asset that compounds *against* the editor instead of for them. Every important answer the team has ever given is locked in linear scrub-bar time, and the longer the channel produces, the bigger the villain gets. Past success becomes future drag.

| Layer | Statement |
|---|---|
| External | "I need three clips for a compilation on [topic] and I know we've answered it five times — but I'd have to scrub through hours to find the right 45 seconds in each." |
| Internal | "I'm the bottleneck. The talent can write and talk all day. Everything jams up at me." |
| Philosophical | "A small team with the right message shouldn't lose to bigger orgs because the people who *have* the words can't find their own words fast enough." |

### 3. Guide

**Empathy:**
> We've watched editors burn weekends scrubbing 200-hour archives for a quote the founder *definitely* said but nobody can locate. We've watched them lose another hour chasing a quote that an AI summary invented. We built this for the part of the job no one sees — between *"this idea would make a great clip reel"* and *"where on earth did he say that."*

**Authority:**
- **Q&A-shaped indexing.** We don't chunk transcripts into 500-token slop. We extract the moments where someone *asks* a question and *answers* it. That's the unit of retrieval — because that's the unit of your workflow.
- **Per-channel deploy.** Your archive lives at *your* subdomain, in *your* index, with *your* branding. The model only knows what your channel said.
- **Refuses to fabricate.** If the channel never made the claim, the app says "no match." No phantom quotes that send you on a 90-minute hunt for tape that doesn't exist.

### 4. Plan

Three steps. Your actual workflow:

1. **Ask the question.** Paste the objection, topic, or question your compilation will answer.
2. **Pick the clips.** Get a ranked stack of clip cards — thumbnail, timestamp, verbatim transcript span, deep link.
3. **Ship the cut.** Drop the `?t=` links into your script. Paste the verbatim transcript into the teleprompter or the YouTube description.

### 5. Calls to Action

| Type | Copy |
|---|---|
| Direct | **Index my channel** |
| Transitional | **See it run on a live archive** → (links to a movement-specific deployment) |

### 6. Success

- Friday's compilation reel cut by Thursday afternoon. Saturday is yours again.
- Thematic compilations go from *"once a quarter when we have time"* to *"every week."*
- The back catalog starts compounding *for* you. Every week's reel pulls from five years of material. New content is the seed; the archive is the soil.
- The talent stops giving the same answer for the 51st time on camera — because the editor can now make their past answer the *current* answer.

### 7. Failure

- Another weekend lost to the scrub bar.
- Another compilation idea killed because the deadline came faster than the search did.
- The library keeps growing. The production rate stays flat. You stay the bottleneck.
- Talented people answering the same five questions on camera over and over, while the previous 50 versions sit unsearched in the archive.

---

## Derived assets

### Refined one-liner

> Small content teams burn whole days scrubbing past streams to find clips they know exist. ask-youtube-channel turns your channel's back catalog into a question-indexed clip library, so one editor can cut a thematic compilation in an afternoon instead of a week.

### Homepage hero (curiosity → enlightenment → CTA)

Used on the *generic / sales* landing page (editor as hero, "Index my channel" as direct CTA):

> ### Your archive is your biggest content asset — and your slowest.
>
> Ask a question. Get the three best clips from your channel that answer it — with deep links, verbatim transcript, and exact timestamps. Cut your next compilation in an afternoon, not a weekend.
>
> **[ Index my channel ]**   See it on a live archive →

### Movement-specific landing — pattern

When the page is for a *specific deployment* (your audience already knows the channel exists and just wants the clip), the hook shifts from positioning to recognition. The CTA shifts from "Index my channel" (already done) to "Try a question."

Pattern:

> ### Ever have a hard time finding that one thing he said about [a distinctive, in-group reference]?
>
> Or [a second, slightly different example that signals breadth]. Or [a third example pointing at long-form content]: that thing you know was somewhere on a livestream last spring.
>
> It's in there. You know which channel. You'd find it if you had three hours.
>
> You don't.
>
> **Ask the question. Get the clip** — with the timestamp, the verbatim transcript, and a deep link straight to the moment.
>
> **[ Try a question ]**

H1 example notes:
- Pick references that feel **confessional or intellectual**, not confrontational. The page is public — bystanders shouldn't feel ambushed even though they aren't the target.
- Use **distinctive in-group vocabulary** (book titles, episode shorthand, recurring themes) so the right reader feels "this is for me" within the first six words.
- Avoid the most polarizing question on the channel as your example, even if it's a real and recurring conversation. Save it for the search box, not the H1.

### Anti-copy — what *not* to say

Because this is what every competitor says:

- ~~"Chat with your YouTube channel"~~ — frames the user as a viewer, not an editor; collapses us onto Transcribr.
- ~~"AI-powered video search"~~ — utility framing, no villain, no stakes.
- ~~"Get instant answers from any video"~~ — wrong hero, wrong job-to-be-done.

---

## Open questions for the page

- Do we lead a movement-specific deployment with the editor, or ship a generic homepage that pivots based on `?role=` or referrer?
- Is "Index my channel" the right primary CTA for a deployment whose channels are already indexed? Probably the deployment page should have **"Try a question"** as primary and **"Deploy this for your channel"** as secondary.
- Demo questions to seed on the deployment landing page — pick 3–5 that show the clips-first UX at its best (questions where 3+ great clips exist across different videos).
