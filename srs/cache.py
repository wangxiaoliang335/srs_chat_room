#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
混合缓存：Redis（主）+ 进程内（兜底）。

按 2026-08-13 文档 §2.5 / §3.3 实施：在线状态优先存 Redis，
邀请码有效缓存也走 Redis，多实例可共享。

行为：
- 写：先 Redis，成功即返回。Redis 失败 → 写入进程内兜底。
- 读：先 Redis，miss / 失败 → 读进程内兜底。
- 这样即使 Redis 挂了，仍可降级运行（仅单实例生效）。

连接：环境变量
- REDIS_URL（默认 redis://127.0.0.1:6379/0）
- REDIS_DISABLE=1 时强制走进程内（用于调试）

支持的 key：
- presence:{user_id}           在线状态（"online" / "offline" + offline_at）
- room_online:{room_id}        Set[user_id] 在线人数（聚合）
- invite:valid:{code}          邀请码有效缓存（冗余 invite_code_store，TTL=有效期）
"""
import json
import os
import threading
import time
from typing import Any, Dict, Optional, Set


REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
REDIS_DISABLE = os.getenv("REDIS_DISABLE", "0") == "1"
REDIS_OP_TIMEOUT = float(os.getenv("REDIS_OP_TIMEOUT", "0.5"))  # 500ms 超时


class Cache:
    """Redis 优先 + 进程内兜底的缓存。"""

    def __init__(self):
        self._lock = threading.RLock()
        # 进程内兜底
        self._items: Dict[str, tuple] = {}
        self._sets: Dict[str, tuple] = {}
        # Redis 客户端（懒加载）
        self._redis = None
        self._redis_disabled = REDIS_DISABLE
        # 启动清理线程
        self._stop = threading.Event()
        self._cleanup_thread = threading.Thread(
            target=self._cleanup_loop, daemon=True, name="CacheCleanup"
        )
        self._cleanup_thread.start()

    # ---------------------------------------------------------------------
    # Redis 客户端（懒加载、失败计数自动重试）
    # ---------------------------------------------------------------------
    def _get_redis(self):
        if self._redis_disabled:
            return None
        if self._redis is not None:
            try:
                self._redis.ping()
                return self._redis
            except Exception:
                # 连接已死，重建
                try:
                    self._redis.close()
                except Exception:
                    pass
                self._redis = None
        try:
            import redis
            client = redis.Redis.from_url(
                REDIS_URL,
                socket_timeout=REDIS_OP_TIMEOUT,
                socket_connect_timeout=REDIS_OP_TIMEOUT,
                decode_responses=True,
                # 兼容 Redis 3.x：不发 HELLO 命令（HELLO 是 Redis 6+ 引入）
                # redis-py 8.x 默认 protocol=3（需要 HELLO），强制走 RESP2
                protocol=2,
            )
            client.ping()
            self._redis = client
            return client
        except Exception as e:
            # 启动期失败：标记禁用一段时间（10 秒）
            self._redis_disabled = True
            threading.Timer(10.0, self._enable_redis_retry).start()
            return None

    def _enable_redis_retry(self):
        self._redis_disabled = False
        self._redis = None

    def is_redis_ok(self) -> bool:
        """健康检查：Redis 是否可用。"""
        return self._get_redis() is not None

    # ---------------------------------------------------------------------
    # 字符串
    # ---------------------------------------------------------------------
    def set(self, key: str, value: Any, ttl: int = 0) -> None:
        """设置 value（ttl=0 表示无过期）。"""
        # 写到 Redis
        r = self._get_redis()
        if r is not None:
            try:
                payload = json.dumps(value, ensure_ascii=False)
                if ttl > 0:
                    r.setex(key, ttl, payload)
                else:
                    r.set(key, payload)
                return  # 写成功即可
            except Exception:
                pass  # 走兜底
        # 兜底：进程内
        with self._lock:
            exp = (time.time() + ttl) if ttl > 0 else 0
            self._items[key] = (value, exp)

    def get(self, key: str) -> Optional[Any]:
        """获取 value（过期返回 None）。"""
        r = self._get_redis()
        if r is not None:
            try:
                raw = r.get(key)
                if raw is not None:
                    return json.loads(raw)
            except Exception:
                pass
        # 兜底：进程内
        with self._lock:
            item = self._items.get(key)
            if not item:
                return None
            value, exp = item
            if exp > 0 and time.time() > exp:
                del self._items[key]
                return None
            return value

    def delete(self, key: str) -> None:
        r = self._get_redis()
        if r is not None:
            try:
                r.delete(key)
            except Exception:
                pass
        with self._lock:
            self._items.pop(key, None)
            self._sets.pop(key, None)

    # ---------------------------------------------------------------------
    # 集合
    # ---------------------------------------------------------------------
    def sadd(self, key: str, value: Any, ttl: int = 0) -> None:
        r = self._get_redis()
        if r is not None:
            try:
                r.sadd(key, value)
                if ttl > 0:
                    r.expire(key, ttl)
                return
            except Exception:
                pass
        with self._lock:
            new_exp = (time.time() + ttl) if ttl > 0 else 0
            if key in self._sets:
                s, exp = self._sets[key]
                # 过期检查：过期则重置为空集 + 新 TTL
                if exp > 0 and time.time() > exp:
                    s = set()
            else:
                s = set()
            s.add(value)
            self._sets[key] = (s, new_exp)

    def srem(self, key: str, value: Any) -> None:
        r = self._get_redis()
        if r is not None:
            try:
                r.srem(key, value)
                return
            except Exception:
                pass
        with self._lock:
            if key in self._sets:
                s, exp = self._sets[key]
                s.discard(value)
                if not s:
                    del self._sets[key]

    def smembers(self, key: str) -> Set[Any]:
        r = self._get_redis()
        if r is not None:
            try:
                return set(r.smembers(key))
            except Exception:
                pass
        with self._lock:
            if key not in self._sets:
                return set()
            s, exp = self._sets[key]
            if exp > 0 and time.time() > exp:
                del self._sets[key]
                return set()
            return set(s)

    def scard(self, key: str) -> int:
        r = self._get_redis()
        if r is not None:
            try:
                return r.scard(key)
            except Exception:
                pass
        return len(self.smembers(key))

    # ---------------------------------------------------------------------
    # 业务 helpers（与原 cache.py 一致）
    # ---------------------------------------------------------------------
    def mark_user_online(self, user_id: str, room_id: str = "") -> None:
        self.set(f"presence:{user_id}", {"online": True, "room": room_id, "ts": int(time.time())},
                 ttl=300)
        if room_id:
            self.sadd(f"room_online:{room_id}", user_id, ttl=300)

    def mark_user_offline(self, user_id: str, room_id: str = "") -> None:
        self.set(f"presence:{user_id}",
                 {"online": False, "room": room_id, "offline_at": int(time.time())},
                 ttl=86400)
        if room_id:
            self.srem(f"room_online:{room_id}", user_id)

    def is_user_online(self, user_id: str) -> bool:
        v = self.get(f"presence:{user_id}")
        if not v:
            return False
        return bool(v.get("online"))

    def room_online_count(self, room_id: str) -> int:
        return self.scard(f"room_online:{room_id}")

    def cache_invite_code(self, code: str, room_id: str, ttl: int = 600) -> None:
        self.set(f"invite:valid:{code}", {"room_id": room_id, "ts": int(time.time())},
                 ttl=ttl)

    def invalidate_invite_code(self, code: str) -> None:
        self.delete(f"invite:valid:{code}")

    def get_invite_code_meta(self, code: str) -> Optional[dict]:
        return self.get(f"invite:valid:{code}")

    # ---------------------------------------------------------------------
    # 清理（仅进程内兜底用）
    # ---------------------------------------------------------------------
    def _cleanup_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._cleanup_expired()
            except Exception:
                pass
            self._stop.wait(60)

    def _cleanup_expired(self) -> None:
        now = time.time()
        with self._lock:
            for k in list(self._items.keys()):
                _, exp = self._items[k]
                if exp > 0 and now > exp:
                    del self._items[k]
            for k in list(self._sets.keys()):
                _, exp = self._sets[k]
                if exp > 0 and now > exp:
                    del self._sets[k]

    def shutdown(self) -> None:
        self._stop.set()


# 全局单例
cache = Cache()
