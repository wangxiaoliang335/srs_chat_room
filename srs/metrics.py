#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
进程内 metrics 收集（2026-08-13 文档 §12.7）。

指标项：
- room_socket 连接数 / 在线用户数
- message_send 总数 / 重复数（命中幂等）
- notification_push 总数 / 失败数 / 平均延迟
- ws_messages 各类事件计数
- room_create / room_close / kick / mute 等操作

暴露方式：
- GET /api/v1/metrics 返回 JSON（轻量）
- 不引入 Prometheus 依赖（保持单进程）
"""
import os
import threading
import time
from collections import defaultdict, deque
from typing import Dict, Deque


class Metrics:
    """线程安全的指标收集器。"""

    def __init__(self, max_recent_samples: int = 1000):
        self._lock = threading.RLock()
        self._started_at = time.time()
        # 计数器
        self._counters: Dict[str, int] = defaultdict(int)
        # 累计延迟（毫秒）
        self._latency_sum_ms: Dict[str, float] = defaultdict(float)
        self._latency_count: Dict[str, int] = defaultdict(int)
        # 最近 N 次延迟（用于 p50/p95）
        self._latency_recent: Dict[str, Deque[float]] = defaultdict(
            lambda: deque(maxlen=max_recent_samples)
        )
        # 当前快照
        self._gauges: Dict[str, float] = defaultdict(float)

    # ---------------------------------------------------------------------
    # 操作 API
    # ---------------------------------------------------------------------
    def inc(self, name: str, value: int = 1) -> None:
        with self._lock:
            self._counters[name] += value

    def gauge(self, name: str, value: float) -> None:
        with self._lock:
            self._gauges[name] = value

    def observe(self, name: str, latency_ms: float) -> None:
        with self._lock:
            self._latency_sum_ms[name] += latency_ms
            self._latency_count[name] += 1
            self._latency_recent[name].append(latency_ms)

    # ---------------------------------------------------------------------
    # 导出
    # ---------------------------------------------------------------------
    def snapshot(self) -> dict:
        with self._lock:
            uptime = time.time() - self._started_at
            # 计算 p50 / p95
            latencies = {}
            for name, samples in self._latency_recent.items():
                if not samples:
                    continue
                sorted_samples = sorted(samples)
                n = len(sorted_samples)
                p50 = sorted_samples[n // 2]
                p95 = sorted_samples[int(n * 0.95)] if n > 1 else sorted_samples[-1]
                avg = self._latency_sum_ms[name] / self._latency_count[name]
                latencies[name] = {
                    "count": self._latency_count[name],
                    "avg_ms": round(avg, 2),
                    "p50_ms": round(p50, 2),
                    "p95_ms": round(p95, 2),
                    "recent_samples": min(n, 1000),
                }
            return {
                "uptime_seconds": round(uptime, 1),
                "counters": dict(self._counters),
                "gauges": dict(self._gauges),
                "latencies": latencies,
            }

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._latency_sum_ms.clear()
            self._latency_count.clear()
            for dq in self._latency_recent.values():
                dq.clear()
            self._gauges.clear()


# 全局单例
metrics = Metrics()
