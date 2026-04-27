"""Runtime configuration loaded from environment + .env."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Config:
    # Cloud API (replaces local SQLite)
    api_base_url: str
    api_token: str

    # External AI APIs (still client-side)
    openai_api_key: str

    # Models
    embed_model: str

    # Local working directories (filesystem queue lives on)
    transcripts_dir: Path  # local cache of pulled transcripts before R2 upload
    queue_dir: Path

    @classmethod
    def load(cls) -> "Config":
        api_base = os.environ.get("AYC_API_BASE_URL", "").strip()
        api_token = os.environ.get("AYC_API_TOKEN", "").strip()
        openai_key = os.environ.get("OPENAI_API_KEY", "").strip()

        if not api_base:
            raise RuntimeError(
                "AYC_API_BASE_URL is required (set in env or .env, e.g. https://ayc.ljs.app)"
            )
        if not api_token:
            raise RuntimeError("AYC_API_TOKEN is required (set in env or .env)")
        if not openai_key:
            raise RuntimeError("OPENAI_API_KEY is required (set in env or .env)")

        transcripts_dir = Path(
            os.environ.get("AYC_TRANSCRIPTS_DIR", REPO_ROOT / "data" / "transcripts")
        ).resolve()
        queue_dir = Path(
            os.environ.get("AYC_QUEUE_DIR", REPO_ROOT / "queue")
        ).resolve()
        transcripts_dir.mkdir(parents=True, exist_ok=True)
        queue_dir.mkdir(parents=True, exist_ok=True)

        return cls(
            api_base_url=api_base,
            api_token=api_token,
            openai_api_key=openai_key,
            embed_model=os.environ.get("AYC_EMBED_MODEL", "text-embedding-3-small"),
            transcripts_dir=transcripts_dir,
            queue_dir=queue_dir,
        )
