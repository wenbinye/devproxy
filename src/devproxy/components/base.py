"""组件协议。check 永不抛异常 —— doctor 依赖它一次列出全部问题。"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from devproxy.platform.base import Action, Status


@runtime_checkable
class Component(Protocol):
    name: str

    def check(self) -> Status:
        """检查现状。永不抛异常。"""

    def plan(self) -> list[Action]:
        """根据当前状态算出要执行的动作。空列表表示无需变更。"""

    def apply(self, actions: list[Action]) -> None:
        """执行动作。"""
