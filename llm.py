#!/usr/bin/env python3
"""Configurable, resilient LLM client for TechPulse.

One client, three transports — selected by ``provider``:
  - "ollama"      → Ollama generate API   (POST {url} e.g. .../api/generate)
  - "ollama-chat" → Ollama chat API       (POST {url} e.g. .../api/chat)
  - "openai"      → OpenAI-compatible chat (POST {url} e.g. .../v1/chat/completions)

Every setting is overridable via environment variables, so the same code runs
against a local model or a hosted endpoint with no edits. Two properties fix
the truncated/unstable output of the old cloud cron setup:
  1. Output is no longer capped at a tiny token budget (default 512, not 200).
  2. Every call is retried with exponential backoff and validated as non-empty.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

DEFAULT_MODEL = "gemma3:27b"
DEFAULT_URL = "http://127.0.0.1:11434/api/generate"


def _env(*names: str, default=None):
    """Return the first non-empty environment variable among ``names``."""
    for name in names:
        val = os.environ.get(name)
        if val not in (None, ""):
            return val
    return default


@dataclass
class LLMConfig:
    provider: str = "ollama"          # ollama | ollama-chat | openai
    model: str = DEFAULT_MODEL
    url: str = DEFAULT_URL
    api_key: str = ""
    max_tokens: int = 512
    temperature: float = 0.3
    top_p: float = 0.9
    timeout: int = 90
    retries: int = 3

    @classmethod
    def from_settings(cls, settings: dict | None = None) -> "LLMConfig":
        """Build config from a settings dict, then let env vars override."""
        s = settings or {}
        cfg = cls(
            provider=str(s.get("llm_provider", cls.provider)),
            model=str(s.get("llm_model", s.get("model", cls.model))),
            url=str(s.get("llm_url", cls.url)),
            api_key=str(s.get("llm_api_key", "")),
            max_tokens=int(s.get("llm_max_tokens", cls.max_tokens)),
            temperature=float(s.get("llm_temperature", cls.temperature)),
            top_p=float(s.get("llm_top_p", cls.top_p)),
            timeout=int(s.get("llm_timeout_seconds", cls.timeout)),
            retries=int(s.get("llm_retries", cls.retries)),
        )
        cfg.provider = str(_env("TECHPULSE_LLM_PROVIDER", default=cfg.provider))
        cfg.model = str(_env("TECHPULSE_MODEL", "TECHPULSE_LLM_MODEL", default=cfg.model))
        cfg.url = str(_env("TECHPULSE_LLM_URL", "TECHPULSE_OLLAMA_URL", default=cfg.url))
        cfg.api_key = str(_env("TECHPULSE_LLM_API_KEY", "OPENAI_API_KEY", default=cfg.api_key))
        cfg.max_tokens = int(_env("TECHPULSE_LLM_MAX_TOKENS", default=cfg.max_tokens))
        cfg.timeout = int(_env("TECHPULSE_LLM_TIMEOUT", default=cfg.timeout))
        cfg.retries = int(_env("TECHPULSE_LLM_RETRIES", default=cfg.retries))
        return cfg

    def describe(self) -> str:
        return f"{self.model} via {self._transport()}"

    def _transport(self) -> str:
        prov = (self.provider or "ollama").lower()
        if prov == "openai" or "chat/completions" in self.url:
            return "openai"
        if prov == "ollama-chat" or self.url.rstrip("/").endswith("/api/chat"):
            return "ollama-chat"
        return "ollama"

    def complete(self, system: str, user: str) -> str:
        """Run a completion, retrying with exponential backoff.

        Returns the trimmed response text, or "" if every attempt failed so the
        caller can fall back to raw RSS summaries instead of crashing.
        """
        last_err = "unknown error"
        for attempt in range(1, self.retries + 1):
            try:
                text = self._request(system, user)
                if text and text.strip():
                    return text.strip()
                last_err = "empty response"
            except urllib.error.HTTPError as exc:
                last_err = f"HTTP {exc.code}"
            except Exception as exc:  # noqa: BLE001 — surface any transport error to the retry loop
                last_err = str(exc)
            if attempt < self.retries:
                time.sleep(2 ** (attempt - 1))
        sys.stderr.write(f"  [LLM] all {self.retries} attempt(s) failed: {last_err}\n")
        return ""

    def _request(self, system: str, user: str) -> str:
        transport = self._transport()
        if transport == "openai":
            return self._openai(system, user)
        if transport == "ollama-chat":
            return self._ollama_chat(system, user)
        return self._ollama_generate(system, user)

    def _ollama_generate(self, system: str, user: str) -> str:
        prompt = f"{system}\n\n---\n\n{user}" if system else user
        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": self.temperature,
                "num_predict": self.max_tokens,
                "top_p": self.top_p,
            },
        }
        return self._post(self.url, payload).get("response", "")

    def _ollama_chat(self, system: str, user: str) -> str:
        payload = {
            "model": self.model,
            "messages": self._messages(system, user),
            "stream": False,
            "options": {
                "temperature": self.temperature,
                "num_predict": self.max_tokens,
                "top_p": self.top_p,
            },
        }
        return (self._post(self.url, payload).get("message") or {}).get("content", "")

    def _openai(self, system: str, user: str) -> str:
        payload = {
            "model": self.model,
            "messages": self._messages(system, user),
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "top_p": self.top_p,
        }
        headers = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        choices = self._post(self.url, payload, headers).get("choices") or []
        if choices:
            return (choices[0].get("message") or {}).get("content", "")
        return ""

    @staticmethod
    def _messages(system: str, user: str) -> list:
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user})
        return messages

    def _post(self, url: str, payload: dict, headers: dict | None = None) -> dict:
        body = json.dumps(payload).encode("utf-8")
        hdrs = {"Content-Type": "application/json"}
        if headers:
            hdrs.update(headers)
        req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
