from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger(__name__)
APP_ROOT = Path(__file__).resolve().parents[1]
# Load project-local settings, while keeping shell-defined variables authoritative.
load_dotenv(APP_ROOT / ".env", override=False)


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
logger.info(
    "judge_settings_loaded llm_url_configured=%s llm_model_configured=%s "
    "llm_api_key_configured=%s llm_json_mode=%s llm_timeout_seconds=%s "
    "remote_llm_allowed=%s",
    bool(settings.llm_base_url), bool(settings.llm_model), bool(settings.llm_api_key),
    settings.llm_json_mode, settings.llm_timeout_seconds, settings.llm_allow_remote,
)
