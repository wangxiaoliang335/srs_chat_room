#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
审计日志（2026-08-13 文档 §12.7）。

记录关键操作：
- 房间创建/删除/关闭
- 邀请码生成/使用/撤销
- 成员踢出/禁言/禁麦
- 通知发送

输出：JSON 行格式（易于 grep / 后续导入 ES）
"""
import json
import os
import threading
import time
from pathlib import Path

LOG_FILE = Path(__file__).parent / "audit.log"


class AuditLogger:
    """线程安全的审计日志记录器。"""

    def __init__(self, log_file: Path = LOG_FILE):
        self._file = log_file
        self._lock = threading.Lock()

    def log(self, action: str, actor_id: str = "", target_id: str = "",
            room_id: str = "", details: dict = None):
        """记录一条审计。

        action: 动作类型（room_created, room_closed, member_kicked, ...）
        actor_id: 操作者 user_id
        target_id: 目标 user_id（如被踢的人）
        room_id: 房间 context
        details: 其他字段
        """
        entry = {
            "ts": int(time.time()),
            "iso": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()),
            "action": action,
            "actor_id": actor_id,
            "target_id": target_id,
            "room_id": room_id,
            "details": details or {},
        }
        line = json.dumps(entry, ensure_ascii=False)
        with self._lock:
            try:
                with self._file.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except Exception:
                pass  # 审计失败不阻断业务


# 全局单例
audit_logger = AuditLogger()
