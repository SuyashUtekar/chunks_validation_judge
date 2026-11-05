from __future__ import annotations

import json
import logging
import socket
import time
from ipaddress import ip_address, ip_network
from pathlib import Path
from urllib.parse import urlparse
from typing import Any

import httpx

from .config import settings

logger = logging.getLogger(__name__)


class LLMUnavailableError(RuntimeError):
    """No approved local model endpoint is configured or reachable."""


def _parse_criteria_content(content: str) -> tuple[dict[str, Any], int]:
    """Find a criteria JSON object in plain JSON, a code fence, or surrounding model text."""
    decoder = json.JSONDecoder()
    for offset, char in enumerate(content):
        if char != "{":
            continue
        try:
            result, _end = decoder.raw_decode(content, offset)
        except json.JSONDecodeError:
            continue
        if isinstance(result, dict) and isinstance(result.get("criteria"), list):
            return result, offset
    raise ValueError("message.content contains no JSON object with a criteria array")


def _approved_host(host: str) -> bool:
    normalized = host.casefold().rstrip(".")
    if normalized in {"localhost", "localhost.localdomain"} or normalized.endswith((".local", ".internal", ".lan")):
        return True
    try:
        address = ip_address(normalized)
    except ValueError:
        try:
            resolved = {ip_address(result[4][0]) for result in socket.getaddrinfo(normalized, None)}
        except OSError:
            return False
        networks = (ip_network("10.0.0.0/8"), ip_network("172.16.0.0/12"),
                    ip_network("192.168.0.0/16"), ip_network("127.0.0.0/8"),
                    ip_network("fc00::/7"), ip_network("::1/128"))
        return bool(resolved) and all(any(address in network for network in networks)
                                      for address in resolved)
    networks = (ip_network("10.0.0.0/8"), ip_network("172.16.0.0/12"),
                ip_network("192.168.0.0/16"), ip_network("127.0.0.0/8"),
                ip_network("fc00::/7"), ip_network("::1/128"))
    return any(address in network for network in networks)


class OpenAICompatibleJudge:
    """Small adapter for an explicitly configured on-prem OpenAI-compatible endpoint."""

    def __init__(self) -> None:
        if not settings.llm_base_url or not settings.llm_model:
            raise LLMUnavailableError("JUDGE_LLM_BASE_URL and JUDGE_LLM_MODEL are not configured.")
        parsed = urlparse(settings.llm_base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise LLMUnavailableError("Configured LLM URL must use HTTP or HTTPS.")
        if not settings.llm_allow_remote and not _approved_host(parsed.hostname):
            raise LLMUnavailableError(
                "Configured LLM host is not a recognized private/local address; set "
                "JUDGE_LLM_ALLOW_REMOTE=true only for an approved internal endpoint."
            )
        self.endpoint = settings.llm_base_url.rstrip("/")
        if self.endpoint.endswith("/chat/completions"):
            pass
        elif parsed.path.rstrip("/").endswith("/v1"):
            self.endpoint += "/chat/completions"
        else:
            self.endpoint += "/v1/chat/completions"
        self.model = settings.llm_model

    def judge(self, payload: dict[str, Any]) -> dict[str, Any]:
        evaluation_id = str(payload.get("evaluation_id") or "unknown")
        chunk_id = str(payload.get("chunk", {}).get("chunk_id") or "unknown")
        criterion_ids = [item.get("criterion_id") for item in payload.get("criteria", [])]
        started = time.perf_counter()
        finish_reason = None
        logger.info("llm_call_started evaluation_id=%s chunk_id=%s model=%s criteria=%s",
                    evaluation_id, chunk_id, self.model, ",".join(criterion_ids))
        prompt_path = Path(__file__).resolve().parents[1] / "prompts" / "chunk_judge_v5.txt"
        system_prompt = prompt_path.read_text(encoding="utf-8")
        headers = {"Content-Type": "application/json"}
        if settings.llm_api_key:
            headers["Authorization"] = f"Bearer {settings.llm_api_key}"
        body: dict[str, Any] = {
            "model": self.model,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
        }
        if settings.llm_json_mode:
            body["response_format"] = {"type": "json_object"}
        try:
            with httpx.Client(timeout=settings.llm_timeout_seconds, follow_redirects=False) as client:
                response = client.post(self.endpoint, headers=headers, json=body)
                response.raise_for_status()
            envelope = response.json()
            choice = envelope["choices"][0]
            finish_reason = choice.get("finish_reason")
            content = choice["message"]["content"]
            if not isinstance(content, str):
                raise TypeError("LLM message.content was not text")
            result, json_offset = _parse_criteria_content(content)
            if json_offset > len(content) - len(content.lstrip()):
                logger.warning(
                    "llm_response_json_wrapped evaluation_id=%s chunk_id=%s model=%s "
                    "leading_chars=%s finish_reason=%s",
                    evaluation_id, chunk_id, self.model, json_offset, finish_reason,
                )
            logger.info(
                "llm_call_succeeded evaluation_id=%s chunk_id=%s model=%s http_status=%s "
                "elapsed_seconds=%.2f response_criteria=%s finish_reason=%s",
                evaluation_id, chunk_id, self.model, response.status_code,
                time.perf_counter() - started, len(result["criteria"]), finish_reason,
            )
            return result
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if status in {401, 403}:
                message = f"LiteLLM returned HTTP {status}: check JUDGE_LLM_API_KEY and proxy access."
            elif status == 404:
                message = f"LiteLLM returned HTTP 404: check the proxy URL and model alias ({self.model})."
            elif status in {400, 415, 422}:
                message = (f"LiteLLM returned HTTP {status}: check the model alias and JSON response mode; "
                           "set JUDGE_LLM_JSON_MODE=false if the model rejects response_format.")
            elif status == 429:
                message = "LiteLLM returned HTTP 429: the proxy rate limit or model capacity was reached."
            else:
                message = f"LiteLLM returned HTTP {status}; check the proxy/model server logs."
            logger.error("llm_call_http_error evaluation_id=%s chunk_id=%s model=%s status=%s elapsed_seconds=%.2f",
                         evaluation_id, chunk_id, self.model, status, time.perf_counter() - started)
            raise LLMUnavailableError(message) from exc
        except httpx.TimeoutException as exc:
            logger.error("llm_call_timeout evaluation_id=%s chunk_id=%s model=%s timeout_seconds=%s",
                         evaluation_id, chunk_id, self.model, settings.llm_timeout_seconds)
            raise LLMUnavailableError(
                f"LiteLLM request timed out after {settings.llm_timeout_seconds}s; increase JUDGE_LLM_TIMEOUT_SECONDS or check the proxy."
            ) from exc
        except httpx.ConnectError as exc:
            logger.error("llm_call_connect_error evaluation_id=%s chunk_id=%s model=%s error_type=%s elapsed_seconds=%.2f",
                         evaluation_id, chunk_id, self.model, type(exc).__name__, time.perf_counter() - started)
            raise LLMUnavailableError(
                "Could not connect to the LiteLLM host; check the URL, VPN, DNS, and firewall."
            ) from exc
        except httpx.HTTPError as exc:
            logger.error("llm_call_network_error evaluation_id=%s chunk_id=%s model=%s error_type=%s elapsed_seconds=%.2f",
                         evaluation_id, chunk_id, self.model, type(exc).__name__, time.perf_counter() - started)
            raise LLMUnavailableError(f"LiteLLM network request failed ({type(exc).__name__}).") from exc
        except json.JSONDecodeError as exc:
            logger.error(
                "llm_call_response_envelope_invalid evaluation_id=%s chunk_id=%s model=%s "
                "line=%s column=%s elapsed_seconds=%.2f",
                evaluation_id, chunk_id, self.model, exc.lineno, exc.colno,
                time.perf_counter() - started,
            )
            raise LLMUnavailableError(
                "LiteLLM returned HTTP 200, but the chat-completions response envelope was not valid JSON."
            ) from exc
        except ValueError as exc:
            logger.error(
                "llm_call_content_invalid evaluation_id=%s chunk_id=%s model=%s "
                "finish_reason=%s content_error=%s elapsed_seconds=%.2f",
                evaluation_id, chunk_id, self.model, finish_reason, str(exc),
                time.perf_counter() - started,
            )
            if finish_reason == "length":
                detail = "The model output was truncated (finish_reason=length); raise the LiteLLM output-token limit."
            else:
                detail = "The model returned HTTP 200, but message.content contained no valid criteria JSON object."
            raise LLMUnavailableError(detail) from exc
        except (KeyError, IndexError, TypeError) as exc:
            logger.error("llm_call_response_error evaluation_id=%s chunk_id=%s model=%s error_type=%s elapsed_seconds=%.2f",
                         evaluation_id, chunk_id, self.model, type(exc).__name__, time.perf_counter() - started)
            raise LLMUnavailableError(
                f"LiteLLM returned an incomplete chat-completions response ({type(exc).__name__})."
            ) from exc
