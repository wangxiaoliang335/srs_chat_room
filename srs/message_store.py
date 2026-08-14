#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
消息持久化存储（2026-08-13 文档 §5）。

设计要点：
- 内存索引：room_id -> [消息列表]，按 seq 升序
- 持久化：<room_id>.json，原子写入
- 写盘：异步批量（仿 notification_store）
- 重要：每房间 seq 计数器独立持久化（messages/<room_id>.seq）

幂等：
- (user_id, client_msg_id) 唯一去重
- 已存在则返回原消息（包含原 seq）

并发：
- 单进程用 RLock 兜底；多进程需要外层锁（文档 §5.2 已说明）
"""
import json
import os
import queue
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT_DIR = Path(__file__).parent / "messages_data"
MESSAGES_FILE = Path(__file__).parent / "messages.json"  # 合并版（用户少时够用）
SEQ_FILE = Path(__file__).parent / "messages_seq.json"

FLUSH_INTERVAL = 0.2


class MessageStore:
    """聊天消息存储。

    使用单文件 messages.json（适合当前单实例部署）：
      {
        "<room_id>": [
          {"id": "m_...", "room_id": ..., "seq": 1, "user_id": ..., ...},
          ...
        ]
      }

    seq 单独存 messages_seq.json：
      {"<room_id>": 100}
    """

    def __init__(self, messages_file: Path = MESSAGES_FILE, seq_file: Path = SEQ_FILE):
        self._messages_file = messages_file
        self._seq_file = seq_file
        self._lock = threading.RLock()
        # room_id -> [msg, ...]  按 seq 升序
        self._messages: Dict[str, List[dict]] = {}
        # room_id -> int 当前最大 seq
        self._seq_counters: Dict[str, int] = {}
        # 幂等索引：f"{user_id}|{client_msg_id}" -> msg_id
        self._idempotency: Dict[str, str] = {}
        self._dirty = False
        self._write_queue: "queue.Queue" = queue.Queue()
        self._stop_event = threading.Event()
        self._flush_thread = threading.Thread(target=self._flush_loop, daemon=True, name="MsgFlush")
        self._flush_thread.start()
        self._load()

    # ---------------------------------------------------------------------
    # 加载
    # ---------------------------------------------------------------------
    def _load(self) -> None:
        if self._messages_file.exists():
            try:
                with self._messages_file.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    for rid, items in data.items():
                        if isinstance(items, list):
                            self._messages[rid] = items
                            for item in items:
                                if isinstance(item, dict):
                                    key = self._idem_key(item.get("user_id", ""), item.get("client_msg_id", ""))
                                    if key and "id" in item:
                                        self._idempotency[key] = item["id"]
            except (json.JSONDecodeError, OSError) as e:
                print(f"[MessageStore] messages.json 读取失败: {e}，忽略")
        if self._seq_file.exists():
            try:
                with self._seq_file.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self._seq_counters = {k: int(v) for k, v in data.items() if isinstance(v, (int, float))}
            except (json.JSONDecodeError, OSError) as e:
                print(f"[MessageStore] seq 文件读取失败: {e}，忽略")
        print(f"[MessageStore] 加载完成：{sum(len(v) for v in self._messages.values())} 条消息，{len(self._seq_counters)} 个房间")

    # ---------------------------------------------------------------------
    # 写入
    # ---------------------------------------------------------------------
    @staticmethod
    def _idem_key(user_id: str, client_msg_id: str) -> str:
        return f"{user_id}|{client_msg_id}"

    def send(self, room_id: str, user_id: str, client_msg_id: str,
             msg_type: str, content: str,
             file_name: str = "", file_size: int = 0, mime_type: str = "",
             width: int = 0, height: int = 0,
             timestamp: Optional[float] = None) -> dict:
        """发送消息（含幂等）。

        幂等命中：返回已存在的消息（不分配新 seq）。
        """
        if not room_id or not user_id or not client_msg_id:
            raise ValueError("room_id, user_id, client_msg_id 必填")
        if msg_type not in ("text", "image", "file"):
            raise ValueError(f"unsupported msg_type: {msg_type}")
        key = self._idem_key(user_id, client_msg_id)
        with self._lock:
            # 幂等命中
            existing_id = self._idempotency.get(key)
            if existing_id:
                for item in self._messages.get(room_id, []):
                    if item.get("id") == existing_id:
                        return {**item, "_idempotent": True}
            # 分配新 seq
            next_seq = self._seq_counters.get(room_id, 0) + 1
            self._seq_counters[room_id] = next_seq
            msg_id = f"m_{int(time.time() * 1000)}_{os.urandom(3).hex()}"
            item = {
                "id": msg_id,
                "room_id": room_id,
                "user_id": user_id,
                "client_msg_id": client_msg_id,
                "seq": next_seq,
                "type": msg_type,
                "content": content,
                "file_name": file_name,
                "file_size": file_size,
                "mime_type": mime_type,
                "width": width,
                "height": height,
                "timestamp": timestamp if timestamp is not None else time.time(),
            }
            self._messages.setdefault(room_id, []).append(item)
            self._idempotency[key] = msg_id
            self._dirty = True
        self._write_queue.put(("upsert", item))
        return item

    # ---------------------------------------------------------------------
    # 查询
    # ---------------------------------------------------------------------
    def history(self, room_id: str, after_seq: int = 0, limit: int = 50) -> List[dict]:
        """拉取历史。

        after_seq: 增量补拉时传客户端最后一条 seq；返回 seq > after_seq 的消息
        limit: 单次最多返回条数
        """
        if not room_id:
            return []
        with self._lock:
            items = list(self._messages.get(room_id, []))
        items.sort(key=lambda x: x.get("seq", 0))
        if after_seq > 0:
            items = [it for it in items if it.get("seq", 0) > after_seq]
        if limit > 0:
            items = items[:limit]
        return items

    def latest_seq(self, room_id: str) -> int:
        """获取房间当前最大 seq（用于离线补拉基准）。"""
        with self._lock:
            return self._seq_counters.get(room_id, 0)

    # ---------------------------------------------------------------------
    # 异步落盘
    # ---------------------------------------------------------------------
    def _flush_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self._write_queue.get(timeout=FLUSH_INTERVAL)
            except queue.Empty:
                continue
            try:
                self._flush()
            except Exception as e:
                print(f"[MessageStore] flush error: {e}")

    def _flush(self) -> None:
        with self._lock:
            if not self._dirty:
                return
            try:
                self._atomic_write(self._messages_file, self._messages)
                self._atomic_write(self._seq_file, self._seq_counters)
                self._dirty = False
            except Exception as e:
                print(f"[MessageStore] persist error: {e}")

    def _atomic_write(self, fp: Path, data) -> None:
        tmp = fp.with_suffix(fp.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, fp)

    def flush_and_stop(self) -> None:
        self._stop_event.set()
        while not self._write_queue.empty():
            try:
                self._write_queue.get_nowait()
            except queue.Empty:
                break
        self._flush()
        if self._flush_thread.is_alive():
            self._flush_thread.join(timeout=2)


# 全局单例
message_store = MessageStore()
