"""Model settings, read from environment variables only. Never hard-code keys.

Any OpenAI-compatible endpoint works (DeepSeek, Qwen via DashScope/OpenRouter, Gemini's
OpenAI-compatible endpoint, ...):

    REIMBURSE_AGENT_MODEL     model name, e.g. deepseek-chat            (required for real runs)
    REIMBURSE_AGENT_BASE_URL  e.g. https://api.deepseek.com/v1          (optional; OpenAI default)
    REIMBURSE_AGENT_API_KEY   the provider key                          (required for real runs)
    REIMBURSE_AGENT_PROVIDER  free-text label for logs/eval, e.g. deepseek (optional)
    REIMBURSE_AGENT_TEMPERATURE default 0
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class ModelConfig:
    model: str
    api_key: str
    base_url: str | None = None
    provider: str = "openai-compatible"
    temperature: float = 0.0

    def __repr__(self) -> str:  # never print the key
        return (f"ModelConfig(provider={self.provider!r}, model={self.model!r}, "
                f"base_url={self.base_url!r}, api_key=***)")


class MissingConfig(RuntimeError):
    pass


def load_config(env: dict[str, str] | None = None) -> ModelConfig:
    env = dict(os.environ if env is None else env)
    model = env.get("REIMBURSE_AGENT_MODEL", "").strip()
    key = env.get("REIMBURSE_AGENT_API_KEY", "").strip()
    missing = [n for n, v in (("REIMBURSE_AGENT_MODEL", model), ("REIMBURSE_AGENT_API_KEY", key)) if not v]
    if missing:
        raise MissingConfig("Set " + " and ".join(missing) + " (see agent/README.md).")
    return ModelConfig(
        model=model,
        api_key=key,
        base_url=env.get("REIMBURSE_AGENT_BASE_URL") or None,
        provider=env.get("REIMBURSE_AGENT_PROVIDER") or "openai-compatible",
        temperature=float(env.get("REIMBURSE_AGENT_TEMPERATURE", "0")),
    )


def build_chat_model(cfg: ModelConfig):
    from langchain_openai import ChatOpenAI

    # Bounded: one slow provider call can't hang a run (review finding 5).
    return ChatOpenAI(model=cfg.model, api_key=cfg.api_key, base_url=cfg.base_url,
                      temperature=cfg.temperature, timeout=60, max_retries=1)
