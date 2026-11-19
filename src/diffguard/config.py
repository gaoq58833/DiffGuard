"""运行参数集中管理。

优先级：环境变量 > config/models.yaml > 代码默认值。
环境变量命名规则：DIFFGUARD__<SECTION>__<FIELD>，例如
  DIFFGUARD__MODELS__STYLE__NAME=qwen3.8-flash
  DIFFGUARD__BUDGET__PER_PR_TOKEN_BUDGET=30000
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "models.yaml"


class ModelConfig(BaseModel):
    name: str
    temperature: float = 0.0
    max_tokens: int = 2048
    timeout_seconds: int = 60


class ProviderConfig(BaseModel):
    base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    api_key_env: str = "DASHSCOPE_API_KEY"

    @property
    def api_key(self) -> str | None:
        return os.getenv(self.api_key_env) or None


class BudgetConfig(BaseModel):
    per_agent_context_tokens: int = 8000
    per_pr_token_budget: int = 50000
    task_timeout_seconds: int = 300


class ServiceConfig(BaseModel):
    max_concurrent_reviews: int = 4


class LimitsConfig(BaseModel):
    max_reflection_iterations: int = 1
    max_tool_calls_per_task: int = 10
    max_same_tool_retries: int = 2


class RuleConfig(BaseModel):
    hunk_context_lines: int = 20
    complexity_threshold: int = 10
    target_line_only: bool = True


class VerdictConfig(BaseModel):
    p1_block_threshold: int = 1  # P1 达到该数量才阻断；P0 始终一条即阻断


class Settings(BaseModel):
    provider: ProviderConfig = Field(default_factory=ProviderConfig)
    models: dict[str, ModelConfig]
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    service: ServiceConfig = Field(default_factory=ServiceConfig)
    limits: LimitsConfig = Field(default_factory=LimitsConfig)
    rules: RuleConfig = Field(default_factory=RuleConfig)
    verdict: VerdictConfig = Field(default_factory=VerdictConfig)

    def model(self, agent: str) -> ModelConfig:
        return self.models[agent]


def _env_overrides() -> dict[str, Any]:
    """把 DIFFGUARD__A__B=value 形式的变量折叠成嵌套 dict。"""
    out: dict[str, Any] = {}
    prefix = "DIFFGUARD__"
    for key, value in os.environ.items():
        if not key.startswith(prefix):
            continue
        path = [p.lower() for p in key[len(prefix) :].split("__") if p]
        if len(path) < 2:
            continue
        cursor = out
        for part in path[:-1]:
            cursor = cursor.setdefault(part, {})
            if not isinstance(cursor, dict):
                break
        else:
            cursor[path[-1]] = yaml.safe_load(value)
    return out


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def load_settings(path: str | Path | None = None) -> Settings:
    load_dotenv()
    config_path = Path(path or os.getenv("DIFFGUARD_CONFIG") or DEFAULT_CONFIG_PATH)
    raw: dict[str, Any] = {}
    if config_path.exists():
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    raw = _deep_merge(raw, _env_overrides())
    return Settings.model_validate(raw)
