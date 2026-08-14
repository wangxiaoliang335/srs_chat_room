#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
进程内缓存（镜像 Redis 风格，2026-08-13 文档 §2.5 / §3.3）。

不引入 Redis 依赖，用 threading 锁 + TTL 兜底实现。

支持的 key：
- presence:{user_id}           在线状态（"online" / "offline" + offline_at）
- room_online:{room_id}        Set[user_id] 在线人数（聚合）
- invite:valid:{code}          邀请码有效缓存（冗余 invite_code_store，TTL=有效期）
- ws_user_conn:{user_id}       当前 WS 连接的 user_id（room_socket 视角）

特性：
- 自动过期（get 时检查）
- 写时自动刷新 TTL
- 按时清理（避免内存泄漏）
"""
import os
import threading
import time
from collections import defaultdict
from typing import Any, Dict, Optional, Set


class Cache:
    """进程内缓存（线程安全）。"""

    def __init__(self):
        self._lock = threading.RLock()
        # 字符串值：key -> (value, expire_at)
        self._items: Dict[str, tuple] = {}
        # 集合值：key -> (set, expire_at)
        self._sets: Dict[str, tuple] = {}
        # 启动清理线程
        self._stop = threading.Event()
        self._cleanup_thread = threading.Thread(
            target=self._cleanup_loop, daemon=True, name="CacheCleanup"
        )
        self._cleanup_thread.start()

    # ---------------------------------------------------------------------
    # 字符串
    # ---------------------------------------------------------------------
    def set(self, key: str, value: Any, ttl: int = 0) -> None:
        """设置 value（ttl=0 表示无过期）。"""
        with self._lock:
            exp = (time.time() + ttl) if ttl > 0 else 0
            self._items[key] = (value, exp)

    def get(self, key: str) -> Optional[Any]:
        """获取 value（过期返回 None）。"""
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
        with self._lock:
            self._items.pop(key, None)
            self._sets.pop(key, None)

    # ---------------------------------------------------------------------
    # 集合
    # ---------------------------------------------------------------------
    def sadd(self, key: str, value: Any, ttl: int = 0) -> None:
        with self._lock:
            if key in self._sets:
                s, exp = self._sets[key]
            else:
                s, exp = set(), (time.time() + ttl if ttl > 0 else 0)
            s.add(value)
            self._sets[key] = (s, exp)

    def srem(self, key: str, value: Any) -> None:
        with self._lock:
            if key in self._sets:
                s, exp = self._sets[key]
                s.discard(value)
                if not s:
                    del self._sets[key]

    def smembers(self, key: str) -> Set[Any]:
        with self._lock:
            if key not in self._sets:
                return set()
            s, exp = self._sets[key]
            if exp > 0 and time.time() > exp:
                del self._sets[key]
                return set()
            return set(s)

    def scard(self, key: str) -> int:
        return len(self.smembers(key))

    # ---------------------------------------------------------------------
    # 业务 helpers
    # ---------------------------------------------------------------------
    def mark_user_online(self, user_id: str, room_id: str = "") -> None:
        """标记用户在线（presence:user_id + room_online 集合）。"""
        self.set(f"presence:{user_id}", {"online": True, "room": room_id, "ts": int(time.time())},
                 ttl=300)  # 5 分钟兜底
        if room_id:
            self.sadd(f"room_online:{room_id}", user_id, ttl=300)

    def mark_user_offline(self, user_id: str, room_id: str = "") -> None:
        """标记用户离线。"""
        self.set(f"presence:{user_id}",
                 {"online": False, "room": room_id, "offline_at": int(time.time())},
                 ttl=86400)  # 24 小时（用于查询）
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
        """§3.3 邀请码有效缓存（Redis 风格），TTL = 有效期"""
        self.set(f"invite:valid:{code}", {"room_id": room_id, "ts": int(time.time())},
                 ttl=ttl)

    def invalidate_invite_code(self, code: str) -> None:
        """邀请码被消费/撤销/过期"""
        self.delete(f"invite:valid:{code}")

    def get_invite_code_meta(self, code: str) -> Optional[dict]:
        return self.get(f"invite:valid:{code}")

    # ---------------------------------------------------------------------
    # 清理
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
