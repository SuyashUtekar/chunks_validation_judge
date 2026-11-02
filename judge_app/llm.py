from __future__ import annotations

import json
import re
import socket
from ipaddress import ip_address, ip_network
from pathlib import Path
from urllib.parse import urlparse
from typing import Any

import httpx

from .config import settings


class LLMUnavailableError(RuntimeError):
    """No approved local model endpoint is configured or reachable."""


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
        prompt_path = Path(__file__).resolve().parents[1] / "prompts" / "chunk_judge_v2.txt"
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
            content = response.json()["choices"][0]["message"]["content"]
            if not isinstance(content, str):
                raise ValueError("LLM response content was not text")
            content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.IGNORECASE)
            result = json.loads(content)
            if not isinstance(result, dict) or not isinstance(result.get("criteria"), list):
                raise ValueError("LLM response must contain a criteria array")
            return result
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise LLMUnavailableError(f"LLM call or response validation failed ({type(exc).__name__}).") from exc
