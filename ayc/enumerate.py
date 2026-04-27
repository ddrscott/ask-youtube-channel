"""Channel enumeration via yt-dlp.

We do separate passes against /videos and /shorts so each row lands with the
correct `form`. UPSERT handles dedupe.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass

from .db import ApiClient


@dataclass
class VideoStub:
    id: str
    title: str
    duration_seconds: int | None
    thumbnail_url: str | None


@dataclass
class ChannelInfo:
    channel_id: str
    handle: str
    display_name: str | None


def _normalize_channel_url(url: str) -> str:
    """Strip any trailing /videos /shorts /streams path so we can append our own."""
    return re.sub(r"/(?:videos|shorts|streams|featured|community|playlists)/?$", "", url.rstrip("/"))


def resolve_channel(channel_url: str) -> ChannelInfo:
    """Resolve a channel URL/handle to a stable channel ID via a single video probe."""
    proc = subprocess.run(
        [
            "yt-dlp",
            "--skip-download",
            "--print",
            "%(channel_id)s\t%(channel)s\t%(uploader_id)s",
            "--playlist-items",
            "1",
            channel_url,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"yt-dlp failed to resolve channel: {proc.stderr}")
    lines = [l for l in proc.stdout.strip().splitlines() if "\t" in l]
    if not lines:
        raise RuntimeError(
            f"yt-dlp produced no metadata line for {channel_url}\nstdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
        )
    channel_id, channel_name, uploader_id = lines[-1].split("\t", 2)
    if channel_id in ("NA", ""):
        raise RuntimeError(f"yt-dlp returned no channel_id for {channel_url} (got {lines[-1]!r})")
    return ChannelInfo(
        channel_id=channel_id,
        handle=uploader_id if uploader_id != "NA" else channel_url,
        display_name=channel_name if channel_name != "NA" else None,
    )


def list_videos(channel_url: str) -> list[VideoStub]:
    """List videos at the given URL via yt-dlp flat-playlist mode (id + title only)."""
    proc = subprocess.run(
        [
            "yt-dlp",
            "--flat-playlist",
            "--skip-download",
            "--dump-json",
            channel_url,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        # yt-dlp returns non-zero for empty tabs (e.g. channel without /shorts).
        # Treat empty stdout as "no videos at this URL" rather than a hard error.
        if not proc.stdout.strip():
            return []
        raise RuntimeError(f"yt-dlp enumeration failed for {channel_url}: {proc.stderr}")

    videos: list[VideoStub] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        vid_id = row.get("id")
        title = row.get("title")
        if not vid_id or not title:
            continue
        duration = row.get("duration")
        thumb_url = None
        thumbs = row.get("thumbnails")
        if isinstance(thumbs, list) and thumbs:
            thumb_url = thumbs[-1].get("url")
        elif row.get("thumbnail"):
            thumb_url = row["thumbnail"]
        videos.append(
            VideoStub(
                id=vid_id,
                title=title,
                duration_seconds=int(duration) if isinstance(duration, (int, float)) else None,
                thumbnail_url=thumb_url,
            )
        )
    return videos


def enumerate_channel(client: ApiClient, channel_url: str) -> tuple[ChannelInfo, dict[str, int]]:
    """Resolve channel and enumerate /videos + /streams + /shorts.

    Pushes the channel and every video to the cloud API. Returns the channel
    info and per-form counts.
    """
    info = resolve_channel(channel_url)
    client.upsert_channel(info.channel_id, info.handle, info.display_name)

    base = _normalize_channel_url(channel_url)
    counts = {"long": 0, "short": 0, "long_streams": 0}

    def _to_payload(v: VideoStub, form: str) -> dict[str, object]:
        return {
            "id": v.id,
            "channel_id": info.channel_id,
            "title": v.title,
            "duration_seconds": v.duration_seconds,
            "thumbnail_url": v.thumbnail_url,
            "published_at": None,
            "form": form,
            "ingest_status": "pending",
        }

    long_videos = list_videos(f"{base}/videos")
    counts["long"] = len(long_videos)
    if long_videos:
        client.upsert_videos_bulk([_to_payload(v, "long") for v in long_videos])

    streams = list_videos(f"{base}/streams")
    counts["long_streams"] = len(streams)
    if streams:
        client.upsert_videos_bulk([_to_payload(v, "long") for v in streams])

    shorts = list_videos(f"{base}/shorts")
    counts["short"] = len(shorts)
    if shorts:
        client.upsert_videos_bulk([_to_payload(v, "short") for v in shorts])

    return info, counts
