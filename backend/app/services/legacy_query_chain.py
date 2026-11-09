from __future__ import annotations


class LegacyQueryChainDisabled(RuntimeError):
    pass


def raise_disabled(feature: str) -> None:
    raise LegacyQueryChainDisabled(
        f"旧查询链路已停用（{feature}），请使用 QuerySpec 单轮编译器。"
    )
