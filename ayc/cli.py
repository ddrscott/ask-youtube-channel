"""Typer CLI entry point. Talks to the cloud API at ayc.ljs.app."""

from __future__ import annotations

import typer
from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

from .config import Config
from .db import ApiClient
from .embed import embed_pending_chunks
from .enumerate import enumerate_channel
from .queue import (
    COMPLETED_DIR,
    FAILED_DIR,
    PENDING_DIR,
    augment_pending_for_gapfill,
    merge_completed,
    prepare_pending,
    queue_counts,
)
from .transcripts import transcribe_video
from .verify import quarantine_invalid, verify_completed_dir

app = typer.Typer(
    name="ayc",
    help="Ask a YouTube channel — local pipeline driving the cloud at ayc.ljs.app.",
    no_args_is_help=True,
)
console = Console()


def _cfg() -> Config:
    return Config.load()


FORM_CHOICES = ("all", "long", "short")


@app.command()
def init(channel_url: str) -> None:
    """Resolve a YouTube channel and enumerate every video to D1."""
    cfg = _cfg()
    with ApiClient(cfg) as client:
        console.print(f"[cyan]Resolving channel and enumerating videos:[/cyan] {channel_url}")
        info, counts = enumerate_channel(client, channel_url)
        console.print(
            f"[green]Channel:[/green] {info.display_name or info.handle} "
            f"([dim]{info.channel_id}[/dim])"
        )
        console.print(
            f"[green]Long-form:[/green] {counts['long']}  "
            f"[green]Streams:[/green] {counts['long_streams']}  "
            f"[green]Shorts:[/green] {counts['short']}"
        )


@app.command()
def transcripts(
    limit: int = typer.Option(0, help="Only fetch up to this many videos (0 = all)"),
    form: str = typer.Option("all", help="Restrict to long-form, shorts, or all"),
    redo_failed: bool = typer.Option(
        False, help="Retry videos previously marked skipped_no_captions"
    ),
) -> None:
    """Fetch transcripts for every video that doesn't have one yet (uploaded to R2)."""
    if form not in FORM_CHOICES:
        raise typer.BadParameter(f"--form must be one of {FORM_CHOICES}, got {form!r}")
    cfg = _cfg()
    statuses = ["pending"]
    if redo_failed:
        statuses.append("skipped_no_captions")

    with ApiClient(cfg) as client:
        # Gather all targets upfront for accurate progress bar.
        rows: list[dict[str, str]] = []
        for status in statuses:
            for v in client.list_videos(
                status=status,
                form=None if form == "all" else form,
                limit=200,
            ):
                rows.append({"id": v["id"], "title": v["title"]})
                if limit and len(rows) >= limit:
                    break
            if limit and len(rows) >= limit:
                break

        if not rows:
            console.print("[yellow]No videos pending transcription.[/yellow]")
            return

        with Progress(
            SpinnerColumn(),
            TextColumn("[bold]Transcribing[/bold]"),
            BarColumn(),
            TaskProgressColumn(),
            TimeElapsedColumn(),
            TextColumn("{task.fields[title]}"),
            console=console,
        ) as progress:
            task = progress.add_task("transcribing", total=len(rows), title="")
            ok = fail = 0
            for row in rows:
                progress.update(task, title=row["title"][:60])
                try:
                    t = transcribe_video(client, row["id"])
                    if t is None:
                        fail += 1
                    else:
                        ok += 1
                except Exception as e:  # noqa: BLE001
                    fail += 1
                    try:
                        client.mark_video(row["id"], "failed", error=f"transcribe: {e}")
                    except Exception:
                        pass
                progress.advance(task)
        console.print(
            f"[green]Transcribed:[/green] {ok}  [yellow]Skipped/failed:[/yellow] {fail}"
        )


@app.command()
def embed(batch_size: int = typer.Option(100, help="Embedding batch size")) -> None:
    """Embed every chunk that doesn't have an embedding yet (Vectorize)."""
    cfg = _cfg()
    with ApiClient(cfg) as client:
        n = embed_pending_chunks(cfg, client, batch_size=batch_size)
    console.print(f"[green]Embedded:[/green] {n} chunks")


@app.command()
def status() -> None:
    """Show ingest progress (queries the cloud)."""
    cfg = _cfg()
    with ApiClient(cfg) as client:
        s = client.stats()

    table = Table(title="Videos by form × status")
    table.add_column("Form")
    table.add_column("Status")
    table.add_column("Count", justify="right")
    for row in s.get("videos_by_status", []):
        table.add_row(row["form"], row["ingest_status"], str(row["n"]))
    console.print(table)

    console.print(
        f"\n[bold]Chunks:[/bold] {s['chunks']} ([green]{s['embedded']}[/green] embedded)"
    )
    by_kind = s.get("chunks_by_kind", [])
    if by_kind:
        kind_str = "  ".join(f"{r['kind']}={r['n']}" for r in by_kind)
        console.print(f"[dim]By kind:[/dim] {kind_str}")
    if s.get("submissions_pending"):
        console.print(f"[dim]Pending submissions:[/dim] {s['submissions_pending']}")


queue_app = typer.Typer(
    name="queue",
    help="Filesystem queue for Claude Code agent-based chunking.",
    no_args_is_help=True,
)
app.add_typer(queue_app)


@queue_app.command("prepare")
def queue_prepare(
    form: str = typer.Option("all", help="Restrict to long-form, shorts, or all"),
    limit: int = typer.Option(0, help="Limit number of files written (0 = all)"),
    rechunk: bool = typer.Option(
        False,
        "--rechunk",
        help="Include already-chunked and already-embedded videos. Used by the "
        "rechunk runbook to re-process under updated chunker rules.",
    ),
) -> None:
    """Pull transcripts and write queue/pending/<id>.json files for the
    ayc-chunker agent.

    Default: only videos at status='transcribed'.
    With --rechunk: also include 'chunked' and 'embedded' videos so the merge
    step replaces their old chunks (and Vectorize entries) atomically."""
    cfg = _cfg()
    with ApiClient(cfg) as client:
        written, skipped = prepare_pending(
            cfg, client, form=form, limit=limit, rechunk=rechunk
        )
    mode = " (rechunk mode)" if rechunk else ""
    console.print(
        f"[green]Wrote:[/green] {written} pending file(s){mode} to {PENDING_DIR}\n"
        f"[yellow]Skipped:[/yellow] {skipped} (transcript fetch failed)"
    )


@queue_app.command("augment-gapfill")
def queue_augment_gapfill() -> None:
    """Walk every queue/pending/<id>.json, fetch existing chunks from the API,
    compute covered + gap ranges, and rewrite the pending file with a
    ``gap_fill: true`` flag. The chunker treats those files as second-pass
    extractions: emit only chunks that fall in the gaps. The merge step then
    POSTs them with replace=False so existing chunks are preserved.

    Run AFTER ``ayc queue prepare --rechunk`` and BEFORE dispatching agents."""
    cfg = _cfg()
    with ApiClient(cfg) as client:
        augmented, skipped = augment_pending_for_gapfill(client)
    _ = cfg
    console.print(
        f"[green]Augmented:[/green] {augmented} pending file(s) with gap-fill metadata.\n"
        f"[yellow]Skipped:[/yellow] {skipped} (chunk fetch failed or malformed pending)."
    )


@queue_app.command("merge")
def queue_merge() -> None:
    """Verify every queue/completed/*.chunks.json against its source transcript,
    quarantine any with errors to queue/failed/, then POST the rest to the API."""
    cfg = _cfg()
    with ApiClient(cfg) as client:
        videos, chunks, errors, quarantined = merge_completed(client)
    _ = cfg
    console.print(
        f"[green]Merged:[/green] {videos} video(s), {chunks} chunk(s)  "
        f"[yellow]Errors:[/yellow] {errors}  "
        f"[red]Quarantined:[/red] {quarantined}"
    )
    if quarantined:
        console.print(
            f"[dim]→ See {FAILED_DIR}/*.verify-error.txt for details on the quarantined files.[/dim]"
        )


@queue_app.command("verify")
def queue_verify(
    quarantine: bool = typer.Option(
        False,
        "--quarantine",
        help="Move files with errors to queue/failed/ (otherwise just report).",
    ),
) -> None:
    """Verify every queue/completed/*.chunks.json against its source transcript
    (read from queue/pending/<id>.json — same shape the chunker saw)."""
    verifications = verify_completed_dir(COMPLETED_DIR, PENDING_DIR)
    total_files = len(verifications)
    valid = sum(1 for v in verifications if v.is_valid)
    invalid = total_files - valid
    total_warnings = sum(len(v.warnings) for v in verifications)
    total_chunks = sum(v.chunk_count for v in verifications)

    if total_files == 0:
        console.print("[yellow]No files in queue/completed/ to verify.[/yellow]")
        return

    table = Table(title="Verification results")
    table.add_column("Video")
    table.add_column("Chunks", justify="right")
    table.add_column("Errors", justify="right")
    table.add_column("Warnings", justify="right")
    table.add_column("Sample issue", overflow="fold")
    for v in verifications:
        first = v.errors[0] if v.errors else (v.warnings[0] if v.warnings else None)
        sample = ""
        if first is not None:
            loc = (
                f"chunk[{first.chunk_index}].{first.field}"
                if first.chunk_index >= 0
                else f"<file>.{first.field}"
            )
            sample = f"[{first.severity}] {loc}: {first.message}"
        table.add_row(
            v.video_id, str(v.chunk_count), str(len(v.errors)), str(len(v.warnings)), sample
        )
    console.print(table)
    console.print(
        f"\n[bold]{total_files}[/bold] file(s), [bold]{total_chunks}[/bold] chunk(s)  "
        f"[green]Valid:[/green] {valid}  [red]Invalid:[/red] {invalid}  "
        f"[yellow]Warnings:[/yellow] {total_warnings}"
    )

    if quarantine and invalid:
        moved = quarantine_invalid(verifications, FAILED_DIR)
        console.print(
            f"\n[red]Quarantined[/red] {moved} file(s) to {FAILED_DIR}. "
            f"See *.verify-error.txt for details."
        )
    elif invalid:
        console.print(
            f"\n[dim]Pass --quarantine to move the {invalid} invalid file(s) "
            f"to queue/failed/. (Otherwise `ayc queue merge` will quarantine them automatically.)[/dim]"
        )


@queue_app.command("status")
def queue_status() -> None:
    """Show queue file counts."""
    counts = queue_counts()
    table = Table(title="Queue files")
    table.add_column("State")
    table.add_column("Count", justify="right")
    table.add_column("Path", style="dim")
    table.add_row("pending", str(counts.pending), str(PENDING_DIR))
    table.add_row("completed", str(counts.completed), str(COMPLETED_DIR))
    table.add_row("failed", str(counts.failed), str(FAILED_DIR))
    console.print(table)


if __name__ == "__main__":
    app()
