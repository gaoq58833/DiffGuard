"""发送给模型前的密钥脱敏。

静态初筛已经能识别硬编码密钥，如果原样把 diff 送进第三方模型，
等于把泄露面从代码库扩大到模型服务商。所有送审文本必须先过这一层。
"""

from __future__ import annotations

import re

_REDACT_RE = re.compile(
    r"(?ix)"
    r"(?P<prefix>\b(?:api[_-]?key|secret|token|password|passwd|pwd|access[_-]?key|private[_-]?key|auth[_-]?token)\b"
    r"\s*[:=]\s*[\"'])"
    r"(?P<value>[^\"'\s]{8,})"
    r"(?P<suffix>[\"'])"
)


def redact_secrets(text: str) -> tuple[str, int]:
    """返回 (脱敏后文本, 替换次数)。保留变量名与引号，只抹掉值。"""
    return _REDACT_RE.subn(r"\g<prefix><REDACTED>\g<suffix>", text)
