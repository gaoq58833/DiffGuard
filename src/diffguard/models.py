"""模型工厂。

模型 ID 一律来自配置，不在代码里硬编码 —— 方案里 "Qwen3-max" / "Qwen3.8-flash5"
都不是有效 ID，写死在代码里会变成静默失败。
"""

from __future__ import annotations

from langchain_openai import ChatOpenAI

from .config import Settings


class ModelFactory:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._cache: dict[str, ChatOpenAI] = {}

    @property
    def enabled(self) -> bool:
        return bool(self._settings.provider.api_key)

    def get(self, agent: str) -> ChatOpenAI | None:
        if not self.enabled:
            return None
        if agent not in self._cache:
            cfg = self._settings.model(agent)
            self._cache[agent] = ChatOpenAI(
                model=cfg.name,
                temperature=cfg.temperature,
                max_tokens=cfg.max_tokens,
                base_url=self._settings.provider.base_url,
                api_key=self._settings.provider.api_key,
                timeout=cfg.timeout_seconds,
            )
        return self._cache[agent]
