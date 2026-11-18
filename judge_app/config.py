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


def _float_setting(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except ValueError as exc:
        raise RuntimeError(f"{name} must be a number") from exc
    if value < 0:
        raise RuntimeError(f"{name} must be non-negative")
    return value


def _positive_float(name: str, default: float) -> float:
    value = _float_setting(name, default)
    if value <= 0:
        raise RuntimeError(f"{name} must be greater than zero")
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
    llm_max_workers: int = min(8, _integer("JUDGE_LLM_MAX_WORKERS", 4))
    llm_allow_remote: bool = os.getenv("JUDGE_LLM_ALLOW_REMOTE", "false").casefold() == "true"
    llm_json_mode: bool = os.getenv("JUDGE_LLM_JSON_MODE", "true").casefold() == "true"
    llm_visual_review_enabled: bool = os.getenv("JUDGE_LLM_VISUAL_REVIEW_ENABLED", "true").casefold() == "true"
    llm_visual_max_pages_per_chunk: int = min(3, _integer("JUDGE_LLM_VISUAL_MAX_PAGES_PER_CHUNK", 2))
    llm_visual_render_scale: float = _positive_float("JUDGE_LLM_VISUAL_RENDER_SCALE", 1.5)
    llm_max_retries: int = _integer("JUDGE_LLM_MAX_RETRIES", 4)
    llm_retry_base_delay_seconds: float = _float_setting("JUDGE_LLM_RETRY_BASE_DELAY_SECONDS", 1.0)
    llm_retry_max_delay_seconds: float = _float_setting("JUDGE_LLM_RETRY_MAX_DELAY_SECONDS", 30.0)



settings = Settings()
logger.info(
    "judge_settings_loaded llm_url_configured=%s llm_model_configured=%s "
    "llm_api_key_configured=%s llm_json_mode=%s llm_timeout_seconds=%s llm_max_workers=%s "
    "llm_max_retries=%s remote_llm_allowed=%s visual_review_enabled=%s "
    "visual_max_pages=%s visual_render_scale=%s",
    bool(settings.llm_base_url), bool(settings.llm_model), bool(settings.llm_api_key),
    settings.llm_json_mode, settings.llm_timeout_seconds, settings.llm_max_workers,
    settings.llm_max_retries, settings.llm_allow_remote,
    settings.llm_visual_review_enabled, settings.llm_visual_max_pages_per_chunk,
    settings.llm_visual_render_scale,
)
