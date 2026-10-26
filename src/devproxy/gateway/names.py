"""路由名校验与域名拼装。"""

from __future__ import annotations

import re

# 用 \A / \Z 而非 ^ / $ —— $ 也会匹配末尾换行之前，"codeup\n" 会被误放行。
# 首尾都必须是字母/数字：与 HOSTNAME_RE / DNS_LABEL_RE 对齐，否则 "foo-"
# 会一路通过入口校验，直到渲染时才因 "foo-.test" 非法而失败。
NAME_RE = re.compile(r"\A[a-z0-9]([a-z0-9-]*[a-z0-9])?\Z")
MAX_NAME_LEN = 63


class InvalidNameError(ValueError):
    def __init__(self, name: str, reason: str):
        super().__init__(f"非法路由名 {name!r}: {reason}")
        self.name = name
        self.reason = reason


def validate_name(name: str) -> str:
    """合法则原样返回；非法抛 InvalidNameError。"""
    if not name:
        raise InvalidNameError(name, "不得为空")
    if len(name) > MAX_NAME_LEN:
        raise InvalidNameError(name, f"长度不得超过 {MAX_NAME_LEN}")
    if not NAME_RE.match(name):
        raise InvalidNameError(name, "限单标签 [a-z0-9-]，首尾为字母或数字，不得含点")
    return name


def domain_for(name: str, suffix: str) -> str:
    """<name>.<suffix>；suffix 前导点会被去掉。"""
    validate_name(name)
    return f"{name}.{suffix.lstrip('.')}"
