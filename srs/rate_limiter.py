#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
进程内限流器（2026-08-13 文档 §3.4）。

设计要点：
- 应用场景：单实例部署下的限流（不引入 Redis）
- 算法：滑动窗口 + 令牌桶（按用户/房间/码分类）
- 接口：简单，并提供 429 错误码辅助

默认配额（可由 .env 覆盖）：
- 邀请：每用户 1 次 / 30s
- 敲门：每用户 1 次 / 30s；同房间 3 次 / 小时
"""
import os
import threading
import time
from collections import deque
from typing import Deque, Dict, Tuple


# 默认配额
INVITE_INTERVAL_SECONDS = int(os.getenv("RATELIMIT_INVITE_INTERVAL", "30"))
KNOCK_INTERVAL_SECONDS = int(os.getenv("RATELIMIT_KNOCK_INTERVAL", "30"))
KNOCK_ROOM_HOURLY = int(os.getenv("RATELIMIT_KNOCK_ROOM_HOURLY", "3"))


class RateLimiter:
    """进程内限流器（线程安全）。

    用 deque 存历史时间戳，命中 N 次/窗口 → 拒绝。
    """

    def __init__(self):
        self._lock = threading.RLock()
        # key -> deque[timestamps]
        self._buckets: Dict[str, Deque[float]] = {}

    def _check_and_record(self, key: str, max_count: int, window_seconds: int) -> Tuple[bool, int]:
        """核心检查：是否允许 + 剩余等待时间。

        Returns: (allowed, wait_seconds)
        """
        now = time.time()
        with self._lock:
            dq = self._buckets.setdefault(key, deque())
            # 清理过期
            while dq and (now - dq[0]) > window_seconds:
                dq.popleft()
            if len(dq) >= max_count:
                # 等待到最早一条过期
                wait = max(1, int(window_seconds - (now - dq[0])))
                return False, wait
            dq.append(now)
            return True, 0

    # ---------------------------------------------------------------------
    # 业务快捷方式
    # ---------------------------------------------------------------------
    def check_invite(self, user_id: str) -> Tuple[bool, int]:
        """§3.4 发送邀请：每用户 1 次/30s"""
        return self._check_and_record(
            f"invite:{user_id}",
            max_count=1,
            window_seconds=INVITE_INTERVAL_SECONDS,
        )

    def check_knock(self, user_id: str, room_id: str) -> Tuple[bool, int]:
        """§3.4 敲门：每用户 1 次/30s + 同房间 3 次/小时"""
        # 先查用户级
        ok_user, wait_user = self._check_and_record(
            f"knock:user:{user_id}",
            max_count=1,
            window_seconds=KNOCK_INTERVAL_SECONDS,
        )
        if not ok_user:
            return False, wait_user
        # 再查房间级（同一用户在指定房间的频率）
        ok_room, wait_room = self._check_and_record(
            f"knock:room:{room_id}:{user_id}",
            max_count=KNOCK_ROOM_HOURLY,
            window_seconds=3600,
        )
        if not ok_room:
            return False, wait_room
        return True, 0

    def check_invite_code(self, code: str) -> Tuple[bool, int]:
        """§3.4 邀请码校验：失败次数限制（结合 invite_code_store 自身 fail_count 更精细）"""
        # 这里只做粗粒度：同一码 1 次/秒级限流
        return self._check_and_record(
            f"invite_code:{code}",
            max_count=10,
            window_seconds=1,
        )

    # ---------------------------------------------------------------------
    # 内部辅助
    # ---------------------------------------------------------------------
    def reset(self, key: str = "") -> None:
        """重置（key 为空则全清）"""
        with self._lock:
            if key:
                self._buckets.pop(key, None)
            else:
                self._buckets.clear()


# 全局单例
rate_limiter = RateLimiter()
