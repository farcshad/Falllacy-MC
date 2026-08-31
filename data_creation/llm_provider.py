"""Small reusable LLM provider layer for OpenRouter-compatible chat models."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ProviderError(RuntimeError):
    """Raised when an LLM provider request or response fails."""


@dataclass(frozen=True)
class LLMResponse:
    """Provider-neutral completion result."""

    content: str
    response_id: str | None
    model: str | None
    provider: str | None
    usage: dict[str, Any]


def load_dotenv(path: Path, *, override: bool = False) -> None:
    """Load simple KEY=VALUE entries without adding a third-party dependency."""

    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        if key and (override or key not in os.environ):
            os.environ[key] = value


class OpenRouterProvider:
    """Minimal OpenRouter Chat Completions client using the standard library."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        timeout_seconds: float = 60.0,
        app_title: str = "Fallacy Misconception Research Pipeline",
    ) -> None:
        if not api_key:
            raise ValueError("An OpenRouter API key is required.")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._app_title = app_title

    def generate_json(
        self,
        *,
        prompt: str,
        model: str,
        temperature: float,
        seed: int,
        max_tokens: int,
        json_schema: dict[str, Any],
        reasoning_effort: str = "none",
    ) -> LLMResponse:
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "seed": seed,
            "max_tokens": max_tokens,
            "reasoning": {"effort": reasoning_effort, "exclude": True},
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "fallacy_schema",
                    "strict": True,
                    "schema": json_schema,
                },
            },
        }
        request = urllib.request.Request(
            f"{self._base_url}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/",
                "X-Title": self._app_title,
                "X-OpenRouter-Metadata": "enabled",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
                response_body = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise ProviderError(f"OpenRouter HTTP {exc.code}: {body[:1000]}") from exc
        except urllib.error.URLError as exc:
            raise ProviderError(f"OpenRouter connection failed: {exc.reason}") from exc

        try:
            data = json.loads(response_body)
            message = data["choices"][0]["message"]
            content = message["content"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
            raise ProviderError(
                f"Unexpected OpenRouter response shape: {response_body[:1000]}"
            ) from exc
        if not isinstance(content, str):
            raise ProviderError("OpenRouter returned non-text message content.")

        metadata = data.get("openrouter_metadata") or {}
        return LLMResponse(
            content=content,
            response_id=data.get("id"),
            model=data.get("model"),
            provider=metadata.get("provider_name") or data.get("provider"),
            usage=data.get("usage") if isinstance(data.get("usage"), dict) else {},
        )
