"""Prompt templates. Kept stable so they're cache-friendly across many video calls.

Inline examples are loaded from `config/examples.toml` (or `examples.local.toml`
if present), so the platform ships domain-neutral and each deployment can
supply examples that match its own channel's voice.
"""

import tomllib
from pathlib import Path
from string import Template

_CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
_LOCAL = _CONFIG_DIR / "examples.local.toml"
_DEFAULT = _CONFIG_DIR / "examples.toml"


def _load_examples() -> dict:
    path = _LOCAL if _LOCAL.exists() else _DEFAULT
    with path.open("rb") as f:
        return tomllib.load(f)


_examples = _load_examples()


CHUNKER_SYSTEM = Template("""You are an expert at extracting question-answer and objection-rebuttal moments from long-form video transcripts.

Your input is the full transcript of a single video, formatted as time-stamped lines:
  [seconds] text...

Your job is to find every distinct moment where one of two things happens:

1. **qa** — Someone (host, guest, audience, caller, interlocutor, or the speaker themselves rhetorically) **asks a question**, and an answer is given. The unit is the exchange — find the question and the answer that addresses it.

2. **objection** — Someone **raises an objection, claim, or counter-position** (e.g. $chunker_objection_examples), and the speaker **rebuts, refutes, or addresses** it. The unit is the objection-plus-rebuttal exchange.

For each moment, extract:

- **kind**: "qa" or "objection"
- **start_seconds**: the timestamp where the question/objection begins (use a value from the transcript verbatim — do not invent timestamps)
- **end_seconds**: the timestamp where the answer/rebuttal effectively concludes (also verbatim from the transcript)
- **question**: for `qa`, the question being answered (normalize/canonicalize it — make it a clean, searchable question even if the original was rambling); for `objection`, the objection or claim being addressed (state it as the objector would, in 1-2 sentences)
- **answer**: for `qa`, the substantive answer (a near-verbatim or condensed paraphrase of what was actually said); for `objection`, the speaker's rebuttal/response (also near-verbatim or condensed)
- **speaker**: best-effort attribution of who gave the answer/rebuttal (e.g. "host", "guest"). Use null if unclear.
- **topics**: 1-4 short topic tags that would help someone retrieve this clip (e.g. $chunker_topic_examples)
- **confidence**: 0.0-1.0 — your confidence that this is a real, well-formed Q&A or objection moment with a substantive answer/rebuttal. Use 1.0 only when the question/objection is clear AND the answer/rebuttal is substantive. Use 0.5 or lower for partial or weak matches.

**Quality bar — be selective**:
- Skip moments where the question is asked but no real answer is given.
- Skip moments where someone briefly mentions a topic but doesn't actually address an objection.
- Skip rhetorical questions that aren't followed by substantive content.
- Skip pure transitions, banter, or topic shifts that aren't Q&A.
- A 30-minute video might have 10-30 real moments. A short clip might have 1-3. A long debate or stream might have 50+. Don't pad — only extract real moments.

**Timestamp accuracy**:
- start_seconds and end_seconds MUST be values that appear in the transcript's `[seconds]` markers (or close to them — within 5 seconds is fine if the exact moment falls between markers).
- Never invent timestamps. If you can't find a clear moment, omit it.

Output: a JSON object matching the provided schema. If the transcript contains no real Q&A or objection moments, return an empty `chunks` array.""").substitute(_examples)


REFORMULATOR_SYSTEM = Template("""You are a query reformulator for a YouTube clip search engine.

The search index stores two kinds of entries from a YouTube channel's transcripts:
- **qa entries**: a question and its answer
- **objection entries**: an objection/claim and its rebuttal

The user types a raw input that might be:
- A direct question ("$reformulator_qa_input_example")
- An objection or claim someone made to them ($reformulator_objection_input_examples)
- A topic ("core concept")
- A vague phrase

Your job is to produce 2-4 reformulations of the user's input that match how the entries are phrased in the index. For each reformulation, identify:
- The phrasing
- Whether it's best matched as a `qa` query (a question), an `objection` query (an objection/claim being raised), or `either`

Examples:

$reformulator_full_examples

Output: a JSON object with a `reformulations` array. Keep them short and sharp — these will be embedded for vector search. 2-4 entries is right; don't overdo it.""").substitute(_examples)


SYNTHESIZER_SYSTEM = """You are answering a viewer's question using ONLY the provided clip excerpts from a YouTube channel.

Each excerpt is numbered [1], [2], [3]... and includes the question/objection it addresses, the answer/rebuttal text, and the source video.

Rules:
1. Synthesize a 2-3 paragraph answer that draws ONLY from the excerpts. Do not add information from outside the excerpts.
2. Cite every claim with the bracket number it came from: "...the speaker argues X [1][3], while also noting Y [2]."
3. If the excerpts don't actually answer the user's question, say so honestly — do not pretend they do.
4. Keep it tight. The clips themselves are the product; your synthesis is scaffolding.
5. Do not include a list of clips at the end — that's rendered separately by the UI."""
