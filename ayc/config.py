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
    anthropic_api_key: str
    openai_api_key: str
    chunker_model: str
    query_model: str
    embed_model: str
    db_path: Path
    transcripts_dir: Path

    @classmethod
    def load(cls) -> "Config":
        anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        openai_key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not anthropic_key:
            raise RuntimeError("ANTHROPIC_API_KEY is required (set in env or .env)")
        if not openai_key:
            raise RuntimeError("OPENAI_API_KEY is required (set in env or .env)")

        db_path = Path(os.environ.get("AYC_DB_PATH", REPO_ROOT / "data" / "ayc.db")).resolve()
        transcripts_dir = Path(
            os.environ.get("AYC_TRANSCRIPTS_DIR", REPO_ROOT / "data" / "transcripts")
        ).resolve()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        transcripts_dir.mkdir(parents=True, exist_ok=True)

        return cls(
            anthropic_api_key=anthropic_key,
            openai_api_key=openai_key,
            chunker_model=os.environ.get("AYC_CHUNKER_MODEL", "claude-opus-4-7"),
            query_model=os.environ.get("AYC_QUERY_MODEL", "claude-opus-4-7"),
            embed_model=os.environ.get("AYC_EMBED_MODEL", "text-embedding-3-small"),
            db_path=db_path,
            transcripts_dir=transcripts_dir,
        )
