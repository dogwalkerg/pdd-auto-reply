# -*- coding: utf-8 -*-
"""
backend.app.core.token_blacklist —— 令牌失效登记（进程内简单失效记录）
=====================================================================
本文件用途：为 backend 服务提供「令牌主动失效」的简单失效记录机制，支撑
需求 1.5（用户主动登出使当前令牌失效）。实现采用进程内登记被失效令牌的
``jti``（令牌唯一标识）+ 过期时间戳的方式：

- 登出时将当前令牌的 ``jti`` 记入失效集合（携带其原 ``exp`` 过期时间）；
- 鉴权时若令牌 ``jti`` 命中失效集合，则视为「未登录或登录已过期」；
- 已自然过期的 ``jti`` 会被惰性清理，避免集合无限膨胀。

设计说明：
- 这是「简单失效记录」实现（任务 4.2 范围所述「可借 token 版本/简单失效
  记录」），满足单进程下登出即失效的语义；多实例 / 重启持久化的全局失效可
  在后续接入 Redis / 数据库令牌版本号统一托管（common.utils.security 已在
  载荷预留 ``jti`` 与 ``ver`` 字段作为扩展基础）。
- 线程安全：使用 ``threading.Lock`` 保护内部状态，适配多线程 ASGI 运行。
- 时间口径：统一使用北京时间（开发规范 17 / 需求 24.8）。
"""
from __future__ import annotations

import threading
from datetime import datetime
from typing import Dict

from common.utils.time_utils import now_beijing_naive


class TokenBlacklist:
    """进程内令牌失效登记表（jti -> 过期时间）。

    仅登记「已被主动失效」的令牌 ``jti``，并随其原过期时间一并记录，便于在
    令牌自然过期后清理该条记录。
    """

    def __init__(self) -> None:
        # jti -> 该令牌的过期时间（北京时间，naive）。
        self._revoked: Dict[str, datetime] = {}
        # 保护内部字典的互斥锁，适配多线程访问。
        self._lock = threading.Lock()

    def revoke(self, jti: str, expire_at: datetime | None = None) -> None:
        """登记一个被主动失效的令牌 ``jti``。

        Args:
            jti: 令牌唯一标识；为空则忽略。
            expire_at: 令牌原过期时间（北京时间）；缺省按当前时间登记，
                仅用于后续惰性清理，不影响「已失效」判定。
        """
        if not jti:
            return
        with self._lock:
            self._revoked[jti] = expire_at or now_beijing_naive()
            self._purge_expired_locked()

    def is_revoked(self, jti: str) -> bool:
        """判断给定 ``jti`` 是否已被主动失效。

        Args:
            jti: 令牌唯一标识。

        Returns:
            已登记失效返回 True，否则返回 False。
        """
        if not jti:
            return False
        with self._lock:
            return jti in self._revoked

    def _purge_expired_locked(self) -> None:
        """清理已自然过期的失效记录（调用方须已持有锁）。

        令牌一旦自然过期，鉴权时本就会判定为过期，无需再保留其失效记录，
        故在此惰性清理以控制集合规模。
        """
        now = now_beijing_naive()
        expired = [jti for jti, exp in self._revoked.items() if exp < now]
        for jti in expired:
            self._revoked.pop(jti, None)

    def clear(self) -> None:
        """清空全部失效记录（主要供测试使用）。"""
        with self._lock:
            self._revoked.clear()


# 进程内单例：全后端共享同一份令牌失效登记表。
_token_blacklist = TokenBlacklist()


def get_token_blacklist() -> TokenBlacklist:
    """返回进程内令牌失效登记表单例。"""
    return _token_blacklist


__all__ = [
    "TokenBlacklist",
    "get_token_blacklist",
]
