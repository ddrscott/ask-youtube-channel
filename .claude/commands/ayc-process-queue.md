---
description: Dispatch the ayc-chunker agent for every file in queue/pending/, then merge results into the DB.
argument-hint: "[--limit N] [--parallel K]"
---

You are processing the AYC chunker queue.

## Steps

1. **List pending files** with `Bash: ls queue/pending/*.json 2>/dev/null | head -<limit-or-30>`. If none, say "Queue is empty." and stop.

2. **Dispatch the `ayc-chunker` agent** for each pending file. Send agents in parallel batches (default 5 at a time, or whatever `--parallel K` was passed). Each agent's prompt should be exactly: `"Process the file <full-path-to-pending-file>"`.

   - Use a single message with multiple `Agent` tool calls to launch a parallel batch.
   - Wait for the batch to finish before dispatching the next batch.
   - If the user passed `--limit N`, only process the first N pending files.

3. **After all batches complete**, run `Bash: uv run ayc queue merge` to insert the chunks into the SQLite DB and archive the completed files.

4. **Run `Bash: uv run ayc queue status`** and report the final counts.

## Notes

- The `ayc-chunker` agent reads ONE pending file per invocation, writes ONE chunks file to `queue/completed/`, and reports a one-line summary.
- The agent does not modify the DB. Merging into the DB is what `uv run ayc queue merge` does.
- If an agent reports a failure, leave its output in `queue/failed/<id>.error.txt` for the user to inspect — don't retry automatically.

User args: $ARGUMENTS
