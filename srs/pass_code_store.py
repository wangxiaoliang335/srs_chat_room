#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
通行码存储（2026-08-15 文档 R3）。

通行码（pass_code）：
- 邀请码兑换后生成（绑定用户 + 房间）
- 服务端保存（持久化到 pass_codes.json，可走 MySQL）
- active 后长期有效
- 状态：active / revoked / expired
- (user_id, room_id) 唯一（一人一房间一条 active）

接口：register_pass_code、validate_active_for_user、revoke_for_user、
      revoke_all_for_room、has_active、list_active_for_user
"""

import json
import logging
import secrets
import threading
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

PASS_CODES_FILE = Path(__file__).parent / "pass_codes.json"

# 过期时间：默认 30 天（可调）
DEFAULT_TTL_SECONDS = 30 * 24 * 3600


class PassCodeStore:
    """通行码存储。线程安全。"""

    def __init__(self, path: Path = PASS_CODES_FILE):
        self._path = path
        self._lock = threading.RLock()
        # 主索引：(user_id, room_id) -> record
        # 同时维护 user_id -> [record] 反向索引（快速拉用户房间列表）
        self._by_key: dict = {}        # key="user_id|room_id" -> record
        self._by_user: dict = {}       # user_id -> set of key
        self._by_room: dict = {}       # room_id -> set of user_id
        self._load()

    # ---------------------------------------------------------------------
    # 持久化
    # ---------------------------------------------------------------------
    def _load(self):
        if self._path.exists():
            try:
                with self._path.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    for key, rec in data.items():
                        self._by_key[key] = rec
                        uid = rec.get("user_id")
                        rid = rec.get("room_id")
                        if uid:
                            self._by_user.setdefault(uid, set()).add(key)
                        if rid:
                            self._by_room.setdefault(rid, set()).add(uid)
            except Exception as e:
                logger.warning(f"[PassCodeStore] load failed: {e}")

    def _flush(self):
        tmp = self._path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(self._by_key, f, ensure_ascii=False, indent=2)
        tmp.replace(self._path)

    # ---------------------------------------------------------------------
    # CRUD
    # ---------------------------------------------------------------------
    def issue(self, user_id: str, room_id: str, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> Optional[dict]:
        """签发一条 active 通行码（绑定用户 + 房间）。

        - (user_id, room_id) 已存在 active：返回已有
        - 否则新建
        """
        if not user_id or not room_id:
            return None
        with self._lock:
            key = self._k(user_id, room_id)
            existing = self._by_key.get(key)
            now = int(time.time())
            if existing and existing.get("status") == "active":
                # 续期
                existing["expires_at"] = now + ttl_seconds
                self._flush()
                return dict(existing)
            # 新建
            code = secrets.token_hex(8).upper()  # 16 位
            rec = {
                "user_id": user_id,
                "room_id": room_id,
                "code": code,
                "status": "active",
                "created_at": now,
                "expires_at": now + ttl_seconds,
            }
            self._by_key[key] = rec
            self._by_user.setdefault(user_id, set()).add(key)
            self._by_room.setdefault(room_id, set()).add(user_id)
            self._flush()
            return dict(rec)

    def has_active(self, user_id: str, room_id: str) -> bool:
        """用户在该房间是否存在 active 通行码（join 时校验用）。"""
        if not user_id or not room_id:
            return False
        with self._lock:
            key = self._k(user_id, room_id)
            rec = self._by_key.get(key)
            if not rec:
                return False
            if rec.get("status") != "active":
                return False
            if rec.get("expires_at", 0) and int(time.time()) > rec["expires_at"]:
                rec["status"] = "expired"
                self._flush()
                return False
            return True

    def get_active(self, user_id: str, room_id: str) -> Optional[dict]:
        """返回 active 通行码（用于查询/审计）；过期返回 None。"""
        if not self.has_active(user_id, room_id):
            return None
        with self._lock:
            return dict(self._by_key[self._k(user_id, room_id)])

    def revoke(self, user_id: str, room_id: str, operator_id: str = "") -> bool:
        """撤销该用户的本房间通行码（踢人/拒绝邀请时调用）。"""
        if not user_id or not room_id:
            return False
        with self._lock:
            key = self._k(user_id, room_id)
            rec = self._by_key.get(key)
            if not rec:
                return False
            if rec.get("status") != "active":
                return False
            rec["status"] = "revoked"
            rec["revoked_at"] = int(time.time())
            rec["revoked_by"] = operator_id
            self._flush()
            return True

    def revoke_all_for_room(self, room_id: str, operator_id: str = "") -> int:
        """撤销某房间的全部 active 通行码（房间关闭时调用）。"""
        if not room_id:
            return 0
        n = 0
        with self._lock:
            uids = list(self._by_room.get(room_id, set()))
            now = int(time.time())
            for uid in uids:
                key = self._k(uid, room_id)
                rec = self._by_key.get(key)
                if rec and rec.get("status") == "active":
                    rec["status"] = "revoked"
                    rec["revoked_at"] = now
                    rec["revoked_by"] = operator_id
                    n += 1
            if n > 0:
                self._flush()
        return n

    def list_active_for_user(self, user_id: str) -> list:
        """返回某用户全部 active 通行码（用于 me/rooms 等）。"""
        if not user_id:
            return []
        out = []
        now = int(time.time())
        with self._lock:
            keys = list(self._by_user.get(user_id, set()))
            for key in keys:
                rec = self._by_key.get(key)
                if not rec:
                    continue
                if rec.get("status") != "active":
                    continue
                if rec.get("expires_at", 0) and now > rec["expires_at"]:
                    rec["status"] = "expired"
                    continue
                out.append(dict(rec))
            if any(r.get("status") == "expired" for r in self._by_key.values()):
                self._flush()
        return out

    def cleanup_expired(self) -> int:
        """扫描过期通行码 → expired。可由定时任务调用。"""
        n = 0
        now = int(time.time())
        with self._lock:
            for key, rec in list(self._by_key.items()):
                if rec.get("status") == "active" and rec.get("expires_at", 0) and now > rec["expires_at"]:
                    rec["status"] = "expired"
                    n += 1
            if n > 0:
                self._flush()
        return n

    @staticmethod
    def _k(user_id: str, room_id: str) -> str:
        return f"{user_id}|{room_id}"


# 单例
pass_code_store = PassCodeStore()