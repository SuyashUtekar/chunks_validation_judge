from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1]


def _integer(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if value < 1:
        raise RuntimeError(f"{name} must be positive")
    return value


@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path(os.getenv("JUDGE_DATA_DIR", str(APP_ROOT / "data" / "evaluations")))
    max_package_bytes: int = _integer("JUDGE_MAX_PACKAGE_BYTES", 100 * 1024 * 1024)
    max_chunks_bytes: int = _integer("JUDGE_MAX_CHUNKS_BYTES", 25 * 1024 * 1024)
    max_chunks: int = _integer("JUDGE_MAX_CHUNKS", 1000)
    max_zip_members: int = _integer("JUDGE_MAX_ZIP_MEMBERS", 2000)
    max_uncompressed_bytes: int = _integer("JUDGE_MAX_UNCOMPRESSED_BYTES", 500 * 1024 * 1024)
    llm_base_url: str | None = os.getenv("JUDGE_LLM_BASE_URL")
    llm_model: str | None = os.getenv("JUDGE_LLM_MODEL")
    llm_api_key: str | None = os.getenv("JUDGE_LLM_API_KEY")
    llm_timeout_seconds: int = _integer("JUDGE_LLM_TIMEOUT_SECONDS", 90)
    llm_allow_remote: bool = os.getenv("JUDGE_LLM_ALLOW_REMOTE", "false").casefold() == "true"
    llm_json_mode: bool = os.getenv("JUDGE_LLM_JSON_MODE", "true").casefold() == "true"


settings = Settings()
