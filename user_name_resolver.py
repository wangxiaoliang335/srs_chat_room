#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
用户显示名解析工具：
- 校验 user_name 格式（1 ≤ length ≤ 64，去除首尾空白后非空，不含控制字符）
- 进程内 TTL 缓存（chat:user:name:<user_id>，TTL 600s）
  注：需求文档 3.7 推荐用 Redis；当前实现用进程内 dict + 时间戳占位。
  后续可平滑替换为 Redis（接口相同）。
- resolve_display_name(user_id) → str（带缓存 + DB 兜底 + user_id 兜底）
"""

import re
import time
import threading
from typing import Optional


_USER_NAME_RE = re.compile(r"^[\S ]{1,64}$")  # 1-64 个非空白开始的可见字符
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


class UserNameInvalidError(ValueError):
    """user_name 字段非法（用于让接口返回 400）"""

    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


def validate_user_name(raw: str) -> str:
    """校验 user_name 字段。

    规则（需求文档 §2.5）：
      - 长度：1 ≤ len ≤ 64
      - 去除首尾空白后必须非空
      - 不允许控制字符

    校验通过返回"已 strip 后的" user_name（与库中值一致）；
    校验失败抛 UserNameInvalidError(code=400)。
    """
    if raw is None:
        return ""
    s = str(raw).strip()
    if not s:
        return ""
    if len(s) > 64:
        raise UserNameInvalidError(400, "user_name 长度超过 64")
    if _CONTROL_RE.search(s):
        raise UserNameInvalidError(400, "user_name 格式非法")
    return s


# 进程内 TTL 缓存：user_id -> (username_or_None, expires_at_unix)
_USER_NAME_CACHE: dict = {}
_USER_NAME_LOCK = threading.RLock()
_USER_NAME_TTL_SECONDS = 600  # 需求文档 3.7 推荐 600s


def invalidate_user_name_cache(user_id: str) -> None:
    """外部触发缓存失效（用户在 /auth/login 更新了 username 时调用）"""
    with _USER_NAME_LOCK:
        _USER_NAME_CACHE.pop(user_id, None)


def cache_user_name(user_id: str, username: str) -> None:
    """写入 / 刷新缓存（不依赖 DB）。"""
    if not user_id:
        return
    with _USER_NAME_LOCK:
        _USER_NAME_CACHE[user_id] = (username, int(time.time()) + _USER_NAME_TTL_SECONDS)


def resolve_display_name(user_id: str, *, user_store, fallback: Optional[str] = None) -> str:
    """按 user_id 拿"显示名"。

    优先级：
      1. 进程内缓存（TTL 600s）
      2. user_store.get_by_user_id(user_id).username
      3. fallback（通常传 user_id 本身）
    """
    if not user_id:
        return fallback or ""

    # 1) 缓存
    now = int(time.time())
    with _USER_NAME_LOCK:
        cached = _USER_NAME_CACHE.get(user_id)
        if cached and cached[1] > now:
            return cached[0] or fallback or user_id

    # 2) DB
    name = None
    if user_store is not None:
        try:
            rec = user_store.get_by_user_id(user_id)
            if rec:
                name = (rec.get("username") or "").strip()
        except Exception:
            name = None

    # 3) 兜底
    if not name:
        name = fallback or user_id

    # 写回缓存（即使兜底也写，避免缓存击穿）
    cache_user_name(user_id, name)
    return name


def clear_all_cache() -> None:
    """测试 / 调试用。"""
    with _USER_NAME_LOCK:
        _USER_NAME_CACHE.clear()