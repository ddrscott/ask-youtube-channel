"""Typer CLI entry point."""

from __future__ import annotations

import json

import typer
from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

from .chunker import chunk_video, persist_chunks
from .config import Config
from .db import connect, mark_video
from .embed import embed_pending_chunks
from .enumerate import enumerate_channel
from .queue import (
    COMPLETED_DIR,
    FAILED_DIR,
    PENDING_DIR,
    merge_completed,
    prepare_pending,
    queue_counts,
)
from .search import retrieve, synthesize_answer
from .transcripts import load_transcript, transcribe_video
from .verify import quarantine_invalid, verify_completed_dir

app = typer.Typer(
    name="ayc",
    help="Ask a YouTube channel — local prototype CLI.",
    no_args_is_help=True,
)
console = Console()


def _cfg() -> Config:
    return Config.load()


@app.command()
def init(channel_url: str) -> None:
    """Initialize the DB and enumerate every video on the channel.

    Enumerates /videos, /streams (live recordings), and /shorts as separate passes
    so each row is tagged with its form. Re-running upserts and refines the form.
    """
    cfg = _cfg()
    conn = connect(cfg.db_path)
    console.print(f"[cyan]Resolving channel and enumerating videos:[/cyan] {channel_url}")
    info, counts = enumerate_channel(conn, channel_url)
    console.print(
        f"[green]Channel:[/green] {info.display_name or info.handle} "
        f"([dim]{info.channel_id}[/dim])"
    )
    console.print(
        f"[green]Long-form:[/green] {counts['long']}  "
        f"[green]Streams:[/green] {counts['long_streams']}  "
        f"[green]Shorts:[/green] {counts['short']}  "
        f"[dim]({counts['new']} newly added)[/dim]"
    )


FORM_CHOICES = ("all", "long", "short")


def _form_clause(form: str) -> tuple[str, list[str]]:
    if form == "all":
        return "", []
    if form not in FORM_CHOICES:
        raise typer.BadParameter(f"--form must be one of {FORM_CHOICES}, got {form!r}")
    return " AND form = ?", [form]


@app.command()
def transcripts(
    limit: int = typer.Option(0, help="Only fetch up to this many videos (0 = all)"),
    form: str = typer.Option("all", help="Restrict to long-form, shorts, or all"),
    redo_failed: bool = typer.Option(False, help="Retry videos previously marked skipped_no_captions"),
) -> None:
    """Fetch transcripts for every video that doesn't have one yet."""
    cfg = _cfg()
    conn = connect(cfg.db_path)
    statuses = ["pending"]
    if redo_failed:
        statuses.append("skipped_no_captions")
    placeholders = ",".join("?" * len(statuses))
    form_sql, form_params = _form_clause(form)
    rows = conn.execute(
        f"SELECT id, title FROM videos WHERE ingest_status IN ({placeholders}){form_sql} ORDER BY id",
        statuses + form_params,
    ).fetchall()
    if limit:
        rows = rows[:limit]
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
                t = transcribe_video(conn, row["id"], cfg.transcripts_dir)
                if t is None:
                    fail += 1
                else:
                    ok += 1
            except Exception as e:  # noqa: BLE001
                fail += 1
                mark_video(conn, row["id"], "failed", error=f"transcribe: {e}")
                conn.commit()
            progress.advance(task)
    console.print(f"[green]Transcribed:[/green] {ok}  [yellow]Skipped/failed:[/yellow] {fail}")


@app.command()
def chunk(
    limit: int = typer.Option(0, help="Only chunk up to this many videos (0 = all)"),
    form: str = typer.Option("all", help="Restrict to long-form, shorts, or all"),
    redo: bool = typer.Option(False, help="Re-chunk videos already chunked"),
) -> None:
    """Run the API-based LLM chunker on every transcribed video.

    For batch ingest at scale, prefer the queue + Claude Code agent flow
    (`ayc queue prepare` + dispatch the ayc-chunker agent). This command exists
    for one-off API-based chunking and as a fallback.
    """
    cfg = _cfg()
    conn = connect(cfg.db_path)
    statuses = ["transcribed"]
    if redo:
        statuses.extend(["chunked", "embedded"])
    placeholders = ",".join("?" * len(statuses))
    form_sql, form_params = _form_clause(form)
    rows = conn.execute(
        f"SELECT id, title FROM videos WHERE ingest_status IN ({placeholders}){form_sql} ORDER BY id",
        statuses + form_params,
    ).fetchall()
    if limit:
        rows = rows[:limit]
    if not rows:
        console.print("[yellow]No videos ready to chunk.[/yellow]")
        return

    total_chunks = 0
    total_in = 0
    total_out = 0
    total_cache_read = 0

    with Progress(
        SpinnerColumn(),
        TextColumn("[bold]Chunking[/bold]"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        TextColumn("{task.fields[title]}"),
        console=console,
    ) as progress:
        task = progress.add_task("chunking", total=len(rows), title="")
        for row in rows:
            progress.update(task, title=row["title"][:60])
            transcript = load_transcript(cfg.transcripts_dir, row["id"])
            if transcript is None:
                mark_video(conn, row["id"], "failed", error="chunker: transcript missing on disk")
                conn.commit()
                progress.advance(task)
                continue
            try:
                result = chunk_video(cfg, transcript)
                count = persist_chunks(conn, result)
                total_chunks += count
                total_in += result.input_tokens
                total_out += result.output_tokens
                total_cache_read += result.cache_read_tokens
            except Exception as e:  # noqa: BLE001
                mark_video(conn, row["id"], "failed", error=f"chunker: {e}")
                conn.commit()
            progress.advance(task)

    console.print(
        f"[green]Chunks added:[/green] {total_chunks}\n"
        f"[dim]Input tokens:[/dim] {total_in:,}  "
        f"[dim]Output tokens:[/dim] {total_out:,}  "
        f"[dim]Cache reads:[/dim] {total_cache_read:,}"
    )


@app.command()
def embed(batch_size: int = typer.Option(100, help="Embedding batch size")) -> None:
    """Embed every chunk that doesn't have an embedding yet."""
    cfg = _cfg()
    conn = connect(cfg.db_path)
    n = embed_pending_chunks(cfg, conn, batch_size=batch_size)
    console.print(f"[green]Embedded:[/green] {n} chunks")


@app.command()
def ask(
    query: str = typer.Argument(..., help="Your question, objection, or topic"),
    top_k: int = typer.Option(5, help="Number of clips to return"),
    confidence_floor: float = typer.Option(0.5, help="Drop clips below this chunker confidence"),
    form: str = typer.Option(
        "long",
        help="Source-video form: 'long' (default — for editors remixing into long-form), 'short', or 'all'",
    ),
    no_synthesis: bool = typer.Option(False, help="Skip the synthesized answer; show only clips"),
    json_out: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Ask the channel a question and get back the top clips + a cited answer."""
    cfg = _cfg()
    conn = connect(cfg.db_path)

    if form not in FORM_CHOICES:
        raise typer.BadParameter(f"--form must be one of {FORM_CHOICES}, got {form!r}")

    reforms, clips = retrieve(
        cfg,
        conn,
        query,
        top_k=top_k,
        confidence_floor=confidence_floor,
        form_filter=form,
    )

    if json_out:
        out = {
            "query": query,
            "form_filter": form,
            "reformulations": [r.model_dump() for r in reforms],
            "clips": [
                {
                    "chunk_id": c.chunk_id,
                    "video_id": c.video_id,
                    "video_title": c.video_title,
                    "video_form": c.video_form,
                    "kind": c.kind,
                    "start_seconds": c.start_seconds,
                    "end_seconds": c.end_seconds,
                    "question": c.question,
                    "answer": c.answer,
                    "speaker": c.speaker,
                    "topics": c.topics,
                    "confidence": c.confidence,
                    "score": c.score,
                    "matched_via": c.matched_via,
                    "youtube_url": c.youtube_url,
                }
                for c in clips
            ],
        }
        if not no_synthesis and clips:
            out["synthesized_answer"] = synthesize_answer(cfg, query, clips)
        typer.echo(json.dumps(out, indent=2, ensure_ascii=False))
        return

    console.print(Panel.fit(f"{query}\n[dim](form: {form})[/dim]", title="Query", border_style="cyan"))
    if reforms:
        console.print("[dim]Reformulations:[/dim]")
        for r in reforms:
            console.print(f"  [{r.kind}] {r.phrasing}")

    if not clips:
        console.print(
            f"\n[yellow]No matching clips above the confidence floor "
            f"(form filter: {form}).[/yellow]"
        )
        return

    if not no_synthesis:
        answer = synthesize_answer(cfg, query, clips)
        console.print(Panel(answer, title="Synthesized answer", border_style="green"))

    table = Table(title=f"Top {len(clips)} clips", show_lines=True)
    table.add_column("#", justify="right", style="dim")
    table.add_column("Kind")
    table.add_column("Form")
    table.add_column("Score", justify="right")
    table.add_column("Conf", justify="right")
    table.add_column("Q / Objection")
    table.add_column("Source")
    for i, c in enumerate(clips, start=1):
        table.add_row(
            str(i),
            c.kind,
            c.video_form,
            f"{c.score:.3f}",
            f"{c.confidence:.2f}",
            c.question[:90] + ("..." if len(c.question) > 90 else ""),
            f"[link={c.youtube_url}]{c.video_title[:50]}[/link]\n[dim]{c.youtube_url}[/dim]",
        )
    console.print(table)


@app.command()
def status() -> None:
    """Show ingest progress."""
    cfg = _cfg()
    conn = connect(cfg.db_path)
    grid = conn.execute(
        "SELECT form, ingest_status, COUNT(*) AS n FROM videos "
        "GROUP BY form, ingest_status ORDER BY form, ingest_status"
    ).fetchall()
    chunks_n = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    embedded_n = conn.execute("SELECT COUNT(*) FROM chunks WHERE embedding IS NOT NULL").fetchone()[
        0
    ]
    by_kind = conn.execute(
        "SELECT kind, COUNT(*) AS n FROM chunks GROUP BY kind"
    ).fetchall()

    table = Table(title="Videos by form × status")
    table.add_column("Form")
    table.add_column("Status")
    table.add_column("Count", justify="right")
    for row in grid:
        table.add_row(row["form"], row["ingest_status"], str(row["n"]))
    console.print(table)

    console.print(
        f"\n[bold]Chunks:[/bold] {chunks_n} ([green]{embedded_n}[/green] embedded)"
    )
    if by_kind:
        kind_str = "  ".join(f"{r['kind']}={r['n']}" for r in by_kind)
        console.print(f"[dim]By kind:[/dim] {kind_str}")


@app.command()
def ingest(limit: int = typer.Option(0, help="Limit transcripts/chunks to first N pending videos")) -> None:
    """Convenience: run transcripts → chunk → embed in one shot (API-based chunker)."""
    transcripts(limit=limit, form="all", redo_failed=False)
    chunk(limit=0, form="all", redo=False)
    embed(batch_size=100)


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
) -> None:
    """Write transcripts of videos that are transcribed-but-not-yet-chunked into queue/pending/.

    The ayc-chunker Claude Code agent picks them up from there.
    """
    cfg = _cfg()
    conn = connect(cfg.db_path)
    written, skipped = prepare_pending(conn, cfg.transcripts_dir, form=form, limit=limit)
    console.print(
        f"[green]Wrote:[/green] {written} pending file(s) to {PENDING_DIR}\n"
        f"[yellow]Skipped:[/yellow] {skipped} (transcript file missing)"
    )


@queue_app.command("merge")
def queue_merge() -> None:
    """Verify every queue/completed/*.chunks.json against its source transcript,
    quarantine any with errors to queue/failed/, then insert the rest into the DB.
    """
    cfg = _cfg()
    conn = connect(cfg.db_path)
    videos, chunks, errors, quarantined = merge_completed(conn, cfg.transcripts_dir)
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
    """Verify every queue/completed/*.chunks.json against its source transcript.

    Checks: timestamps within transcript range, schema validity, non-empty Q/A.
    By default, only reports — pass --quarantine to move bad files to queue/failed/.
    """
    cfg = _cfg()
    verifications = verify_completed_dir(COMPLETED_DIR, cfg.transcripts_dir)

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
        table.add_row(v.video_id, str(v.chunk_count), str(len(v.errors)), str(len(v.warnings)), sample)
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
