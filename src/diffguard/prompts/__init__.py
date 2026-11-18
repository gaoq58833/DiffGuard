"""Prompt 资产管理。

Prompt 与评测用例分目录隔离，CI 中禁止把用例内容拼进系统提示。
"""

from __future__ import annotations

from importlib.resources import files


def load_prompt(name: str) -> str:
    return files(__name__).joinpath(name).read_text(encoding="utf-8")
