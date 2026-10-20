"""LLM access for schema discovery. The model only ever returns JSON that is validated against a schema and then
checked by deterministic guards; it cannot execute anything, change workflows, or approve a profile."""
from __future__ import annotations

import json
import urllib.error
import urllib.request


class LLMDisabled(RuntimeError):
    pass


class LLMError(RuntimeError):
    pass


class NullLLM:
    calls = 0

    def complete_json(self, system: str, user: str) -> dict:
        raise LLMDisabled("no LLM provider configured (MIS_LLM_PROVIDER)")


class AnthropicLLM:
    """Minimal Messages-API client (stdlib only). Logs nothing about content."""

    def __init__(self, api_key: str, model: str, timeout: int = 60, url: str = "https://api.anthropic.com/v1/messages"):
        if not api_key:
            raise LLMDisabled("ANTHROPIC_API_KEY is not set")
        self.api_key, self.model, self.timeout, self.url = api_key, model, timeout, url
        self.calls = 0

    def complete_json(self, system: str, user: str) -> dict:
        body = json.dumps({"model": self.model, "max_tokens": 4000, "temperature": 0, "system": system,
                           "messages": [{"role": "user", "content": user}]}).encode()
        req = urllib.request.Request(self.url, data=body, method="POST", headers={
            "content-type": "application/json", "x-api-key": self.api_key, "anthropic-version": "2023-06-01"})
        self.calls += 1
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                payload = json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise LLMError(f"LLM HTTP {e.code}") from None
        except Exception as e:
            raise LLMError(type(e).__name__) from None
        text = "".join(b.get("text", "") for b in payload.get("content", []) if b.get("type") == "text").strip()
        if text.startswith("```"):
            text = text.strip("`")
            text = text[text.find("{"):]
        try:
            return json.loads(text)
        except ValueError:
            raise LLMError("LLM did not return JSON") from None


def make_llm(settings):
    if settings.llm_provider == "anthropic":
        return AnthropicLLM(settings.anthropic_api_key, settings.llm_model, settings.llm_timeout_seconds)
    return NullLLM()
