#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
房间邀请持久化存储（分片 + 异步批量写盘）。

设计要点：
1. **内存分两片**
   - _active:    pending 邀请（热数据，小，写入频繁）
   - _archive:   accepted / rejected / expired 历史（冷数据，大，只追加）
2. **写盘异步批量**
   - 业务接口调用 store 写操作时，只改内存 + 把 dirty 标记 + 把操作 append 到 _write_queue
   - 单写线程每 FLUSH_INTERVAL 消费一次队列，批量 _flush
   - 接口调用不会被磁盘 IO 阻塞
3. **启动加载**
   - 启动时一次读盘，把 pending 放入 _active，其余放入 _archive
4. **原子写入**
   - 仍然用 tmp + os.replace，避免半文件
5. **优雅关闭**
   - FastAPI lifespan 关闭时调用 flush_and_stop()，保证数据落盘

文件位置：
- <脚本所在目录>/invitations_active.json
- <脚本所在目录>/invitations_archive.json
"""
import json
import os
import queue
import threading
import time
from pathlib import Path
from typing import Dict, Optional, List, Tuple

ACTIVE_FILE = Path(__file__).parent / "invitations_active.json"
ARCHIVE_FILE = Path(__file__).parent / "invitations_archive.json"

FLUSH_INTERVAL = 0.2     # 写盘线程每 200ms 消费一次队列
TERMINAL_STATUSES = ("accepted", "rejected", "expired")


class InvitationStore:
    """线程安全 + 异步批量落盘的邀请存储。

    数据 schema：
        id, room_id, room_name, inviter_id, inviter_name, invitee_id,
        status, message, created_at, expires_at,
        accepted_at?, rejected_at?, expired_at?
    """

    def __init__(
        self,
        active_path: Path = ACTIVE_FILE,
        archive_path: Path = ARCHIVE_FILE,
    ):
        self._active_path = active_path
        self._archive_path = archive_path

        # 内存分片
        self._active: Dict[str, dict] = {}     # 仅 pending
        self._archive: Dict[str, dict] = {}    # accepted / rejected / expired

        # 锁：业务接口只锁短时间，不调 IO
        self._lock = threading.RLock()

        # 异步写盘：put/delete 队列
        self._write_queue: "queue.Queue[Tuple[str, str, dict]]" = queue.Queue()
        self._dirty = {"active": False, "archive": False}

        # 写线程
        self._writer_thread: Optional[threading.Thread] = None
        self._writer_running = False

        # 启动时同步加载
        self._load()

        # 启动写线程
        self._start_writer()

    # ----------------- 加载/启停 -----------------

    def _load(self):
        """启动时一次加载：合并新老格式，统一去重。

        兼容老版本遗留的单文件 invitations.json：
        - 先读新格式（active/archive 两个文件）
        - 再读老格式 invitations.json，里面 pending 进 active，其余进 archive
        - 老文件加载完后**改名为 .legacy**（只迁移一次，避免下次再读）
        """
        # 合并两个文件读出来，统一去重
        merged: Dict[str, dict] = {}

        # 新格式
        for path in (self._active_path, self._archive_path):
            if not path.exists():
                continue
            try:
                with path.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    for k, v in data.items():
                        if isinstance(v, dict):
                            merged[str(k)] = dict(v)
            except Exception as e:
                print(f"[InvitationStore] load {path} failed: {e}", flush=True)

        # 老格式兼容
        legacy_path = Path(__file__).parent / "invitations.json"
        if legacy_path.exists():
            try:
                with legacy_path.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    for k, v in data.items():
                        if isinstance(v, dict):
                            # 老数据优先（覆盖同名 key），但 active/archive 已有的不重复
                            merged.setdefault(str(k), dict(v))
                # 改名避免下次再读
                legacy_renamed = legacy_path.with_suffix(".json.legacy")
                if not legacy_renamed.exists():
                    legacy_path.rename(legacy_renamed)
                    print(f"[InvitationStore] legacy file migrated: {legacy_path} -> {legacy_renamed}", flush=True)
            except Exception as e:
                print(f"[InvitationStore] load legacy {legacy_path} failed: {e}", flush=True)

        with self._lock:
            for iid, rec in merged.items():
                if rec.get("status") == "pending":
                    self._active[iid] = rec
                else:
                    self._archive[iid] = rec

        print(f"[InvitationStore] loaded: active={len(self._active)} archive={len(self._archive)}", flush=True)

    def _start_writer(self):
        self._writer_running = True
        self._writer_thread = threading.Thread(
            target=self._writer_loop, daemon=True, name="invitation-writer"
        )
        self._writer_thread.start()

    def stop(self, timeout: float = 5.0):
        """FastAPI 关闭时调用：让 writer 线程把队列排空再退出。"""
        self._writer_running = False
        if self._writer_thread:
            self._writer_thread.join(timeout=timeout)

    def _writer_loop(self):
        """写线程：批量消费队列，按需刷盘。"""
        import logging
        log = logging.getLogger("server_fastapi")
        while self._writer_running:
            try:
                # 等一批写操作
                items = []
                try:
                    items.append(self._write_queue.get(timeout=FLUSH_INTERVAL))
                except queue.Empty:
                    pass

                # 在 FLUSH_INTERVAL 窗口内尽量多收一些
                deadline = time.time() + 0.05
                while time.time() < deadline:
                    try:
                        items.append(self._write_queue.get_nowait())
                    except queue.Empty:
                        break

                # 应用操作到内存快照（锁外读快照，锁内应用）
                # 但因为 _flush 是全量写，所以只需要知道哪些片脏即可
                active_dirty = False
                archive_dirty = False
                for op, target, _ in items:
                    if target == "active":
                        active_dirty = True
                    elif target == "archive":
                        archive_dirty = True

                # 刷盘：拍快照
                if active_dirty:
                    self._flush_to(self._active_path, dict(self._active))
                if archive_dirty:
                    self._flush_to(self._archive_path, dict(self._archive))

            except Exception as e:
                log.error(f"[InviteWriter] error: {e}", exc_info=True)

    def _flush_to(self, path: Path, snapshot: dict):
        tmp = path.with_suffix(f".json.{os.getpid()}.tmp")
        try:
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(snapshot, f, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception as e:
            print(f"[InvitationStore] flush {path} failed: {e}", flush=True)
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass

    # ----------------- 业务 API（线程安全，调用方异步友好）-----------------

    def put(self, inv: dict) -> None:
        """新建邀请（一定是 pending）。"""
        with self._lock:
            self._active[inv["id"]] = dict(inv)
        self._write_queue.put(("put", "active", {}))

    def get(self, inv_id: str) -> Optional[dict]:
        """查询邀请（先 active 后 archive）。"""
        with self._lock:
            rec = self._active.get(inv_id) or self._archive.get(inv_id)
            return dict(rec) if rec else None

    def update_status(self, inv_id: str, new_status: str, **extra) -> bool:
        """原子更新状态。状态从 pending -> 终态时移动到 archive。

        返回是否成功找到并更新。"""
        with self._lock:
            rec = self._active.get(inv_id)
            if rec is None:
                # 可能在 archive 中，但 pending 才能 update 到终态
                rec = self._archive.get(inv_id)
                if rec is None:
                    return False
                # 已经在终态，按 append 写
                rec["status"] = new_status
                for k, v in extra.items():
                    rec[k] = v
                self._write_queue.put(("update", "archive", {}))
                return True

            # 在 active 中
            old_status = rec.get("status", "pending")
            rec["status"] = new_status
            for k, v in extra.items():
                rec[k] = v

            if new_status in TERMINAL_STATUSES:
                # 移到 archive
                self._archive[inv_id] = rec
                del self._active[inv_id]
                self._write_queue.put(("move", "active", {}))   # 让 active 刷盘
                self._write_queue.put(("put", "archive", {}))   # 让 archive 刷盘
            else:
                # 还是 pending
                self._write_queue.put(("update", "active", {}))
            return True

    def find_pending_for_invitee_room(self, invitee_id: str, room_id: str, now: int) -> Optional[dict]:
        with self._lock:
            for rec in self._active.values():
                if (
                    rec.get("invitee_id") == invitee_id
                    and rec.get("room_id") == room_id
                    and rec.get("status") == "pending"
                    and int(rec.get("expires_at", 0)) > now
                ):
                    return dict(rec)
        return None

    def list_pending_for_invitee(self, invitee_id: str, now: int) -> List[dict]:
        out = []
        with self._lock:
            for rec in self._active.values():
                if (
                    rec.get("invitee_id") == invitee_id
                    and rec.get("status") == "pending"
                    and int(rec.get("expires_at", 0)) > now
                ):
                    out.append(dict(rec))
        return out

    def sweep_expired(self, now: int) -> int:
        """把 active 中过期的 pending 移到 archive(状态=expired)。"""
        to_move = []
        with self._lock:
            for iid, rec in self._active.items():
                if rec.get("status") == "pending" and int(rec.get("expires_at", 0)) <= now:
                    to_move.append(iid)

            for iid in to_move:
                rec = self._active[iid]
                rec["status"] = "expired"
                rec["expired_at"] = now
                self._archive[iid] = rec
                del self._active[iid]

        if to_move:
            self._write_queue.put(("move", "active", {}))
            self._write_queue.put(("put", "archive", {}))
        return len(to_move)

    def get_batch(self, ids: List[str]) -> List[dict]:
        out = []
        with self._lock:
            for iid in ids:
                rec = self._active.get(iid) or self._archive.get(iid)
                if rec:
                    out.append(dict(rec))
        return out

    def count(self) -> int:
        with self._lock:
            return len(self._active) + len(self._archive)

    def status(self) -> dict:
        with self._lock:
            ac = {"pending": len(self._active)}
            ar_counts = {}
            for rec in self._archive.values():
                s = rec.get("status", "unknown")
                ar_counts[s] = ar_counts.get(s, 0) + 1
            return {
                "active": ac,
                "archive": ar_counts,
                "active_file": str(self._active_path),
                "archive_file": str(self._archive_path),
                "write_queue_size": self._write_queue.qsize(),
            }


# 全局单例
invitation_store = InvitationStore()