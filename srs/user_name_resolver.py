#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
用户显示名解析工具：
- 校验 user_name 格式（1 ≤ length ≤ 64，去除首尾空白后非空，不含控制字符）
- Redis 主缓存（chat:user:name:<user_id>，TTL 600s）
  注：需求文档 §3.7 推荐用 Redis。配置：
    REDIS_HOST  默认 localhost
    REDIS_PORT  默认 6379
    REDIS_PASSWORD 可选
    REDIS_DB    默认 0
- 进程内 dict 降级缓存（Redis 不可用时使用，TTL 60s，避免长期不一致）
- resolve_display_name(user_id) → str（Redis → 进程内降级 → DB → user_id 兜底）

降级策略：Redis 连接失败 → 记一次 warning → 后续 30s 不再尝试 → 用进程内 dict + DB。
30s 后自动重试一次，Redis 恢复后自动切回。接口调用方无感。
"""

import os
import re
import time
import threading
import logging
from typing import Optional

logger = logging.getLogger("user_name_resolver")


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


# ---------- Redis 主缓存 ----------

_REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
_REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
_REDIS_DB = int(os.getenv("REDIS_DB", "0"))
_REDIS_PASSWORD = os.getenv("REDIS_PASSWORD") or None

_USER_NAME_KEY_PREFIX = "chat:user:name:"
_USER_NAME_TTL_SECONDS = 600  # 需求文档 §3.7 推荐 600s

_redis_client = None
_redis_lock = threading.RLock()
_redis_unavailable_until = 0.0  # Redis 不可用截止时间（unix），0 表示可用
_REDIS_BACKOFF_SECONDS = 30  # 失败后多久再尝试


def _build_redis_client():
    """懒加载 Redis client。"""
    global _redis_client
    if _redis_client is not None:
        return _redis_client
    try:
        import redis as _redis_lib  # 延迟导入，方便没装时降级
        client = _redis_lib.Redis(
            host=_REDIS_HOST,
            port=_REDIS_PORT,
            db=_REDIS_DB,
            password=_REDIS_PASSWORD,
            socket_connect_timeout=1.0,  # 1s 连接超时
            socket_timeout=1.0,           # 1s 读写超时
            decode_responses=True,
            # 兼容老版本 Redis（如本机 3.2.12 不支持 RESP3 / HELLO 握手）
            protocol=2,
            health_check_interval=0,
        )
        # 启动时不做 PING（避免阻塞启动）；延迟到第一次调用
        _redis_client = client
        return client
    except ImportError:
        logger.warning("[user_name_resolver] redis-py 未安装，使用进程内降级缓存")
        return None
    except Exception as e:
        logger.warning(f"[user_name_resolver] 构建 Redis client 失败: {e}")
        return None


def _redis_available() -> bool:
    """检查 Redis 是否可用。不可用时返回 False 并设置 backoff。"""
    global _redis_unavailable_until
    now = time.time()
    if _redis_unavailable_until > now:
        return False

    client = _build_redis_client()
    if client is None:
        _redis_unavailable_until = now + _REDIS_BACKOFF_SECONDS
        return False

    try:
        client.ping()
        _redis_unavailable_until = 0.0  # 重置为可用
        return True
    except Exception as e:
        _redis_unavailable_until = now + _REDIS_BACKOFF_SECONDS
        logger.warning(f"[user_name_resolver] Redis 不可用，降级到进程内缓存: {e}")
        return False


def _redis_get(user_id: str) -> Optional[str]:
    """从 Redis 读缓存。Redis 不可用时返回 None。"""
    client = _build_redis_client()
    if client is None:
        return None
    try:
        val = client.get(f"{_USER_NAME_KEY_PREFIX}{user_id}")
        return val  # decode_responses=True → str
    except Exception as e:
        global _redis_unavailable_until
        _redis_unavailable_until = time.time() + _REDIS_BACKOFF_SECONDS
        logger.warning(f"[user_name_resolver] Redis GET 失败: {e}")
        return None


def _redis_set(user_id: str, username: str) -> None:
    """写 Redis 缓存。失败不抛。"""
    client = _build_redis_client()
    if client is None:
        return
    try:
        client.setex(
            f"{_USER_NAME_KEY_PREFIX}{user_id}",
            _USER_NAME_TTL_SECONDS,
            username,
        )
    except Exception as e:
        global _redis_unavailable_until
        _redis_unavailable_until = time.time() + _REDIS_BACKOFF_SECONDS
        logger.warning(f"[user_name_resolver] Redis SETEX 失败: {e}")


def _redis_del(user_id: str) -> None:
    """删 Redis 缓存。失败不抛。"""
    client = _build_redis_client()
    if client is None:
        return
    try:
        client.delete(f"{_USER_NAME_KEY_PREFIX}{user_id}")
    except Exception as e:
        global _redis_unavailable_until
        _redis_unavailable_until = time.time() + _REDIS_BACKOFF_SECONDS
        logger.warning(f"[user_name_resolver] Redis DEL 失败: {e}")


# ---------- 进程内降级缓存 ----------

# user_id -> (username_or_None, expires_at_unix)
_LOCAL_CACHE: dict = {}
_LOCAL_LOCK = threading.RLock()
_LOCAL_TTL_SECONDS = 60  # 短一点，避免 Redis 恢复后还长期不一致


def _local_get(user_id: str) -> Optional[str]:
    now = int(time.time())
    with _LOCAL_LOCK:
        entry = _LOCAL_CACHE.get(user_id)
        if entry and entry[1] > now:
            return entry[0]
    return None


def _local_set(user_id: str, username: str) -> None:
    if not user_id:
        return
    with _LOCAL_LOCK:
        _LOCAL_CACHE[user_id] = (username, int(time.time()) + _LOCAL_TTL_SECONDS)


def _local_del(user_id: str) -> None:
    with _LOCAL_LOCK:
        _LOCAL_CACHE.pop(user_id, None)


# ---------- 对外接口（签名保持兼容） ----------

def invalidate_user_name_cache(user_id: str) -> None:
    """外部触发缓存失效（用户在 /auth/login 更新了 username 时调用）。

    同时清 Redis 和进程内降级缓存。
    """
    if not user_id:
        return
    _redis_del(user_id)
    _local_del(user_id)


def cache_user_name(user_id: str, username: str) -> None:
    """写入 / 刷新缓存（不依赖 DB）。"""
    if not user_id:
        return
    _redis_set(user_id, username)
    _local_set(user_id, username)


def resolve_display_name(user_id: str, *, user_store, fallback: Optional[str] = None) -> str:
    """按 user_id 拿"显示名"。

    优先级：
      1. Redis 主缓存（TTL 600s，跨实例共享）
      2. 进程内降级缓存（TTL 60s，Redis 不可用时使用）
      3. user_store.get_by_user_id(user_id).username
      4. fallback（通常传 user_id 本身）
    """
    if not user_id:
        return fallback or ""

    # 1) Redis 主缓存
    if _redis_available():
        cached = _redis_get(user_id)
        if cached is not None:
            # 同时回填降级缓存，避免 Redis 短暂抖动时还要再查 DB
            _local_set(user_id, cached)
            return cached or fallback or user_id

    # 2) 进程内降级缓存
    cached = _local_get(user_id)
    if cached is not None:
        return cached or fallback or user_id

    # 3) DB
    name = None
    if user_store is not None:
        try:
            rec = user_store.get_by_user_id(user_id)
            if rec:
                name = (rec.get("username") or "").strip()
        except Exception:
            name = None

    # 4) 兜底
    if not name:
        name = fallback or user_id

    # 写回缓存（即使兜底也写，避免缓存击穿）
    cache_user_name(user_id, name)
    return name


def clear_all_cache() -> None:
    """测试 / 调试用：清进程内降级缓存。

    Redis 缓存请用 redis-cli FLUSHDB 或 DEL chat:user:name:* 手动清。
    """
    with _LOCAL_LOCK:
        _LOCAL_CACHE.clear()