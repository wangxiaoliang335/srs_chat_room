#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
跨房间通知 Socket 连接管理器（2026-08-13 文档 §1.1 / §8.2）。

与 room_socket 的区别：
- notice_socket 没有 room_id 概念（处理跨房间事件）
- 每个 user_id 最多一个活跃连接
- 推送失败进入失败队列，指数退避重试（1s/5s/30s）
- 离线时落库（notification_store），重连后按未读补推
"""
import asyncio
import json
import logging
import threading
import time
from typing import Dict, Optional, Set

logger = logging.getLogger(__name__)


class NoticeManager:
    """notice_socket 连接管理（仅记录 user_id -> WebSocket，不分房间）。

    线程安全：所有 dict 访问在 self._lock 内。
    """

    def __init__(self):
        self._lock = threading.RLock()
        # user_id -> WebSocket
        self._connections: Dict[str, object] = {}
        # 失败重试队列：[(target_user_id, payload, attempts, next_retry_ts)]
        self._retry_queue: list = []
        # 离线重投基准：user_id -> last_sync_ts（用户最近一次成功同步的时间戳）
        self._last_sync_ts: Dict[str, int] = {}

    # ---------------------------------------------------------------------
    # 连接生命周期
    # ---------------------------------------------------------------------
    async def connect(self, websocket, user_id: str):
        with self._lock:
            self._connections[user_id] = websocket
        logger.info(f"[NoticeSocket] Connected: user={user_id}")
        # 通知客户端已连接
        try:
            await websocket.send_json({
                "type": "notice_connected",
                "user_id": user_id,
                "timestamp": int(time.time()),
            })
        except Exception:
            pass

    def disconnect(self, websocket, user_id: str = ""):
        with self._lock:
            if user_id and self._connections.get(user_id) == websocket:
                del self._connections[user_id]
                logger.info(f"[NoticeSocket] Disconnected: user={user_id}")
            else:
                # 反向匹配
                for uid, ws in list(self._connections.items()):
                    if ws == websocket:
                        del self._connections[uid]
                        logger.info(f"[NoticeSocket] Disconnected: user={uid}")
                        break

    def is_online(self, user_id: str) -> bool:
        with self._lock:
            return user_id in self._connections

    # ---------------------------------------------------------------------
    # 推送（文档 §6.2：失败重试 + 离线重投）
    # ---------------------------------------------------------------------
    async def push_notification(self, user_id: str, payload: dict) -> bool:
        """推送通知到指定用户。

        在线 + 推送成功 → 记录 last_sync_ts，返回 True
        在线 + 推送失败 → 进入失败队列，返回 False
        离线 → 仅记录（由 notification_store 持久化），返回 False
        """
        with self._lock:
            ws = self._connections.get(user_id)
        if not ws:
            return False
        try:
            await ws.send_json(payload)
            # 成功：记录同步时间
            with self._lock:
                self._last_sync_ts[user_id] = int(time.time())
            return True
        except Exception as e:
            logger.warning(f"[NoticeSocket] push to {user_id} failed: {e}")
            self._enqueue_retry(user_id, payload)
            return False

    def _enqueue_retry(self, user_id: str, payload: dict) -> None:
        """进入失败队列，1s/5s/30s 指数退避（最多 3 次）"""
        with self._lock:
            # 限制单用户队列长度
            existing = [r for r in self._retry_queue if r[0] == user_id]
            if len(existing) >= 10:
                # 满了先丢最早的
                self._retry_queue = [r for r in self._retry_queue if r[0] != user_id]
            self._retry_queue.append((user_id, payload, 0, int(time.time()) + 1))

    async def retry_loop(self, stop_event: threading.Event):
        """后台协程：消费失败队列 + 指数退避（1s/5s/30s）"""
        backoffs = [1, 5, 30]
        while not stop_event.is_set():
            try:
                now = int(time.time())
                to_retry = []
                with self._lock:
                    ready = [r for r in self._retry_queue if r[3] <= now]
                    for r in ready:
                        self._retry_queue.remove(r)
                for user_id, payload, attempts, _ in ready:
                    with self._lock:
                        ws = self._connections.get(user_id)
                    if not ws:
                        # 重连时再投（重投逻辑见下）
                        continue
                    try:
                        await ws.send_json(payload)
                        with self._lock:
                            self._last_sync_ts[user_id] = int(time.time())
                    except Exception:
                        next_attempts = attempts + 1
                        if next_attempts >= len(backoffs):
                            # 超过最大重试次数，丢弃
                            logger.warning(f"[NoticeSocket] giving up push to {user_id} after {next_attempts} retries")
                            continue
                        delay = backoffs[next_attempts]
                        to_retry.append((user_id, payload, next_attempts, int(time.time()) + delay))
                if to_retry:
                    with self._lock:
                        self._retry_queue.extend(to_retry)
            except Exception as e:
                logger.error(f"[NoticeSocket] retry loop error: {e}", exc_info=True)
            await asyncio.sleep(1)

    # ---------------------------------------------------------------------
    # 离线重投（重连时调用）
    # ---------------------------------------------------------------------
    def get_last_sync_ts(self, user_id: str) -> int:
        with self._lock:
            return self._last_sync_ts.get(user_id, 0)

    def reset_last_sync_ts(self, user_id: str) -> None:
        with self._lock:
            self._last_sync_ts.pop(user_id, None)


# 全局单例
notice_manager = NoticeManager()
