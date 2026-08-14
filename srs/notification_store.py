#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
通知持久化存储（分片 + 异步批量写盘）。

设计要点（仿 invitation_store.py）：
1. **内存分两片**
   - _active:    未读通知（热数据，写入频繁、读取多）
   - _archive:   已读 + 已删除历史（冷数据，只追加）
2. **写盘异步批量**
   - 业务接口调用 store 写操作时，只改内存 + dirty + append 到 _write_queue
   - 单写线程每 FLUSH_INTERVAL 消费一次队列，批量 _flush
   - 接口调用不会被磁盘 IO 阻塞
3. **启动加载**
   - 启动时一次读盘，把未读放入 _active，已读/已删除放入 _archive
4. **原子写入**
   - tmp + os.replace，避免半文件
5. **优雅关闭**
   - FastAPI lifespan 关闭时调用 flush_and_stop()

文件位置：
- <脚本所在目录>/notifications_active.json
- <脚本所在目录>/notifications_archive.json

schema：
    id, user_id, type, title, content, room_id, related_user_id,
    data (dict), is_read, created_at, read_at, deleted
"""
import json
import os
import queue
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ACTIVE_FILE = Path(__file__).parent / "notifications_active.json"
ARCHIVE_FILE = Path(__file__).parent / "notifications_archive.json"

FLUSH_INTERVAL = 0.2  # 写盘线程每 200ms 消费一次队列


class NotificationStore:
    """线程安全 + 异步批量落盘的通知存储。"""

    def __init__(self, active_file: Path = ACTIVE_FILE, archive_file: Path = ARCHIVE_FILE):
        self._active_file = active_file
        self._archive_file = archive_file
        self._lock = threading.RLock()
        # _active: user_id -> {notification_id: notification_dict}
        self._active: Dict[str, Dict[str, dict]] = {}
        # _archive: user_id -> {notification_id: notification_dict}（已读+已删除）
        self._archive: Dict[str, Dict[str, dict]] = {}
        self._write_queue: "queue.Queue[Tuple[str, dict]]" = queue.Queue()
        self._dirty = False
        self._stop_event = threading.Event()
        self._flush_thread = threading.Thread(target=self._flush_loop, daemon=True, name="NotifyFlush")
        self._flush_thread.start()
        self._load()
        logger_seed = "NotificationStore"
        print(f"[{logger_seed}] 持久化加载完成（active={self._count_active()} 条未读）")

    # ---------------------------------------------------------------------
    # 内部辅助
    # ---------------------------------------------------------------------
    def _count_active(self) -> int:
        return sum(len(v) for v in self._active.values())

    def _gen_id(self) -> str:
        return f"n_{int(time.time() * 1000)}_{os.urandom(3).hex()}"

    # ---------------------------------------------------------------------
    # 启动加载
    # ---------------------------------------------------------------------
    def _load(self) -> None:
        for fp, target in ((self._active_file, "active"), (self._archive_file, "archive")):
            if not fp.exists():
                continue
            try:
                with fp.open("r", encoding="utf-8") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                print(f"[NotificationStore] {fp} 读取失败: {e}，忽略")
                continue
            if not isinstance(data, dict):
                continue
            for user_id, items in data.items():
                if not isinstance(items, list):
                    continue
                bucket = self._active if target == "active" else self._archive
                bucket.setdefault(user_id, {})
                for item in items:
                    if not isinstance(item, dict) or "id" not in item:
                        continue
                    bucket[user_id][item["id"]] = item

    # ---------------------------------------------------------------------
    # 写入操作（业务接口调用）
    # ---------------------------------------------------------------------
    def add(self, user_id: str, type_: str, title: str, content: str,
            room_id: str = "", related_user_id: str = "",
            data: Optional[dict] = None) -> dict:
        """新增一条通知，返回通知 dict。

        写入分片：未读 → _active。
        """
        if not user_id:
            return {}
        ts = int(time.time())
        nid = self._gen_id()
        item = {
            "id": nid,
            "user_id": user_id,
            "type": type_,
            "title": title,
            "content": content,
            "room_id": room_id,
            "related_user_id": related_user_id,
            "data": data or {},
            "is_read": False,
            "created_at": ts,
            "read_at": None,
        }
        with self._lock:
            self._active.setdefault(user_id, {})[nid] = item
            self._dirty = True
        self._write_queue.put(("upsert", item))
        return item

    def mark_read(self, user_id: str, notification_id: str) -> bool:
        """单条已读。

        从 _active 移到 _archive（is_read=True）。
        """
        with self._lock:
            bucket = self._active.get(user_id, {})
            item = bucket.pop(notification_id, None)
            if item is None:
                # 可能已经读过
                return False
            item["is_read"] = True
            item["read_at"] = int(time.time())
            self._archive.setdefault(user_id, {})[notification_id] = item
            self._dirty = True
        self._write_queue.put(("delete", {"user_id": user_id, "id": notification_id}))
        self._write_queue.put(("archive_upsert", item))
        return True

    def mark_all_read(self, user_id: str) -> int:
        """全部已读。返回标记条数。"""
        ts = int(time.time())
        moved = 0
        with self._lock:
            bucket = self._active.get(user_id, {})
            archive_bucket = self._archive.setdefault(user_id, {})
            ids_to_delete = []
            for nid, item in list(bucket.items()):
                item["is_read"] = True
                item["read_at"] = ts
                archive_bucket[nid] = item
                ids_to_delete.append({"user_id": user_id, "id": nid})
                moved += 1
            bucket.clear()
            if moved:
                self._dirty = True
        for nid in ids_to_delete:
            self._write_queue.put(("delete", nid))
        # archive 整片重写（量小，调用不频繁）
        if moved:
            self._write_queue.put(("archive_user_bulk", {"user_id": user_id}))
        return moved

    def delete(self, user_id: str, notification_id: str) -> bool:
        """删除一条通知（从 _active 或 _archive 都尝试删除）。"""
        with self._lock:
            bucket = self._active.get(user_id, {})
            if notification_id in bucket:
                del bucket[notification_id]
                self._dirty = True
                self._write_queue.put(("delete", {"user_id": user_id, "id": notification_id}))
                return True
            archive_bucket = self._archive.get(user_id, {})
            if notification_id in archive_bucket:
                del archive_bucket[notification_id]
                self._dirty = True
                self._write_queue.put(("archive_delete", {"user_id": user_id, "id": notification_id}))
                return True
        return False

    # ---------------------------------------------------------------------
    # 读取操作
    # ---------------------------------------------------------------------
    def list(self, user_id: str, limit: int = 50, before_ts: Optional[int] = None) -> List[dict]:
        """列出通知（时间倒序，未读优先；支持 before_ts 游标分页）。

        合并 _active + _archive（不返回已删除）。
        """
        if not user_id:
            return []
        with self._lock:
            items = list(self._active.get(user_id, {}).values())
            items.extend(self._archive.get(user_id, {}).values())
        items.sort(key=lambda x: x.get("created_at", 0), reverse=True)
        if before_ts is not None:
            items = [it for it in items if it.get("created_at", 0) < before_ts]
        if limit > 0:
            items = items[:limit]
        return items

    def unread_count(self, user_id: str) -> int:
        if not user_id:
            return 0
        with self._lock:
            return len(self._active.get(user_id, {}))

    # ---------------------------------------------------------------------
    # 异步落盘
    # ---------------------------------------------------------------------
    def _flush_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                op, payload = self._write_queue.get(timeout=FLUSH_INTERVAL)
            except queue.Empty:
                continue
            try:
                self._flush()
            except Exception as e:
                print(f"[NotificationStore] flush error: {e}")

    def _flush(self) -> None:
        if not self._dirty:
            return
        with self._lock:
            try:
                self._atomic_write(self._active_file, self._active)
                self._atomic_write(self._archive_file, self._archive)
                self._dirty = False
            except Exception as e:
                print(f"[NotificationStore] persist error: {e}")

    def _atomic_write(self, fp: Path, data: dict) -> None:
        tmp = fp.with_suffix(fp.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, fp)

    def flush_and_stop(self) -> None:
        """关闭前调用：消费剩余队列 + 最终落盘。"""
        self._stop_event.set()
        # 消费剩余
        while not self._write_queue.empty():
            try:
                self._write_queue.get_nowait()
            except queue.Empty:
                break
        # 最终一次刷盘
        self._flush()
        if self._flush_thread.is_alive():
            self._flush_thread.join(timeout=2)


# 全局单例
notification_store = NotificationStore()
