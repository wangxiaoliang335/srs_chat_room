#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
邀请码持久化存储（2026-08-13 文档 §3）。

与 invitation_store.py 的区别：
- invitation_store.py：存"邀请记录"（A 想邀请 B，A 推 B，B 接受/拒绝）
- invite_code_store.py：存"加入房间用的一次性码"（生成 → 校验 → 一次性消费）

状态机：
    unused --使用--> used
    unused --过期--> expired
    unused --撤销--> revoked

并发安全（文档 §3.3）：
- CAS 原子消费：mark_used 通过 file lock + 状态校验，仿 DB 的
  UPDATE WHERE status='unused' AND 影响行数 == 1
- 限流：失败计数 ≥ 5 次/码 → 锁定 5 分钟
- 有效期：默认 10 分钟（now + 600s）

存储位置：
- <脚本所在目录>/invite_codes.json
"""
import json
import os
import string
import random
import threading
import time
from pathlib import Path
from typing import Dict, Optional, Tuple

DATA_FILE = Path(__file__).parent / "invite_codes.json"

DEFAULT_EXPIRE_SECONDS = 600  # 10 分钟
CODE_LEN = 10  # 8~12 位随机
MAX_FAIL_PER_CODE = 5  # 失败 5 次 → 锁定
LOCK_SECONDS = 300  # 锁定 5 分钟


class InviteCodeStore:
    """邀请码存储（线程安全 + 文件持久化）。

    数据 schema：
        code:               邀请码字符串
        room_id:            所属房间
        created_by:         生成者（房主 user_id）
        target_user_id:     指定对象（null = 通用）
        status:             unused / used / expired / revoked
        used_by:            使用者
        created_at:         时间戳
        expires_at:         过期时间戳
        used_at:            使用时间戳
        fail_count:         失败次数（CAS 锁相关）
        locked_until:       锁定到期时间戳
    """

    def __init__(self, data_file: Path = DATA_FILE):
        self._file = data_file
        self._lock = threading.RLock()
        # code -> dict
        self._codes: Dict[str, dict] = {}
        self._load()

    # ---------------------------------------------------------------------
    # 工具
    # ---------------------------------------------------------------------
    @staticmethod
    def _gen_random_code(length: int = CODE_LEN) -> str:
        """生成 8~12 位随机字符串（去除易混淆字符 0/O/1/I/l）"""
        chars = "".join(c for c in string.ascii_uppercase + string.digits if c not in ("0", "O", "1", "I", "L"))
        return "".join(random.choices(chars, k=length))

    def _now(self) -> int:
        return int(time.time())

    # ---------------------------------------------------------------------
    # 加载 + 持久化
    # ---------------------------------------------------------------------
    def _load(self) -> None:
        if not self._file.exists():
            return
        try:
            with self._file.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self._codes = data
        except (json.JSONDecodeError, OSError) as e:
            print(f"[InviteCodeStore] 加载失败: {e}，忽略")

    def _flush(self) -> None:
        try:
            tmp = self._file.with_suffix(self._file.suffix + ".tmp")
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(self._codes, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._file)
        except Exception as e:
            print(f"[InviteCodeStore] flush error: {e}")

    # ---------------------------------------------------------------------
    # 生成
    # ---------------------------------------------------------------------
    def generate(self, room_id: str, created_by: str,
                 target_user_id: str = "",
                 expire_seconds: int = DEFAULT_EXPIRE_SECONDS) -> dict:
        """生成邀请码（仅 unused 状态）。

        失败重试：撞码（极端）则重新生成。"""
        with self._lock:
            for _ in range(10):
                code = self._gen_random_code()
                if code not in self._codes:
                    break
            else:
                raise RuntimeError("生成邀请码失败（撞码 10 次）")
            now = self._now()
            item = {
                "code": code,
                "room_id": room_id,
                "created_by": created_by,
                "target_user_id": target_user_id,
                "status": "unused",
                "used_by": None,
                "created_at": now,
                "expires_at": now + expire_seconds,
                "used_at": None,
                "fail_count": 0,
                "locked_until": 0,
            }
            self._codes[code] = item
            self._flush()
            return item

    # ---------------------------------------------------------------------
    # 校验 + 消费（CAS 模拟）
    # ---------------------------------------------------------------------
    def validate(self, code: str, room_id: str = "", target_user_id: str = "") -> Tuple[bool, str, dict]:
        """校验邀请码（不消费）。

        Returns: (ok, reason, item)
        """
        with self._lock:
            item = self._codes.get(code)
            if not item:
                return False, "邀请码不存在", {}
            now = self._now()
            # 锁定检查
            if item.get("locked_until", 0) > now:
                return False, "邀请码已锁定，请稍后重试", item
            # 状态检查
            if item["status"] != "unused":
                return False, f"邀请码状态为 {item['status']}", item
            # 过期检查 + 自动 expire
            if now > item["expires_at"]:
                item["status"] = "expired"
                self._flush()
                return False, "邀请码已过期", item
            # 房间归属
            if room_id and item["room_id"] != room_id:
                return False, "邀请码与房间不匹配", item
            # 目标用户
            if item.get("target_user_id") and target_user_id and item["target_user_id"] != target_user_id:
                return False, "邀请码不属于该用户", item
            return True, "ok", item

    def consume(self, code: str, used_by: str) -> Tuple[bool, str, dict]:
        """CAS 原子消费。

        行为：
        - 校验失败 → fail_count +1（≥ 5 锁 5 分钟）
        - 校验成功且状态 unused → 标记 used，记录 used_by/used_at
        - 返回 (ok, reason, item)
        """
        with self._lock:
            item = self._codes.get(code)
            if not item:
                return False, "邀请码不存在", {}
            now = self._now()
            # 锁定
            if item.get("locked_until", 0) > now:
                return False, "邀请码已锁定", item
            # 状态（仅 unused 可消费）
            if item["status"] != "unused":
                self._record_fail(code, item)
                return False, f"邀请码状态为 {item['status']}", item
            if now > item["expires_at"]:
                item["status"] = "expired"
                self._flush()
                self._record_fail(code, item)
                return False, "邀请码已过期", item
            # CAS：通过
            item["status"] = "used"
            item["used_by"] = used_by
            item["used_at"] = now
            self._flush()
            return True, "ok", item

    def _record_fail(self, code: str, item: dict) -> None:
        """记录失败次数，超阈值锁定。"""
        item["fail_count"] = item.get("fail_count", 0) + 1
        if item["fail_count"] >= MAX_FAIL_PER_CODE:
            item["locked_until"] = self._now() + LOCK_SECONDS
        self._flush()

    # ---------------------------------------------------------------------
    # 撤销 / 过期扫描
    # ---------------------------------------------------------------------
    def revoke(self, code: str, operator_id: str = "") -> bool:
        """房主撤销邀请码。"""
        with self._lock:
            item = self._codes.get(code)
            if not item:
                return False
            if item["status"] != "unused":
                return False
            if operator_id and item["created_by"] != operator_id:
                return False
            item["status"] = "revoked"
            self._flush()
            return True

    def sweep_expired(self) -> int:
        """扫描过期未用的码（unused → expired）。返回处理条数。"""
        now = self._now()
        count = 0
        with self._lock:
            for item in self._codes.values():
                if item["status"] == "unused" and now > item["expires_at"]:
                    item["status"] = "expired"
                    count += 1
            if count:
                self._flush()
        return count

    # ---------------------------------------------------------------------
    # 查询
    # ---------------------------------------------------------------------
    def get(self, code: str) -> Optional[dict]:
        return self._codes.get(code)

    def list_for_room(self, room_id: str, status: str = "") -> list:
        result = []
        for item in self._codes.values():
            if item["room_id"] != room_id:
                continue
            if status and item["status"] != status:
                continue
            result.append(item)
        result.sort(key=lambda x: x.get("created_at", 0), reverse=True)
        return result


# 全局单例
invite_code_store = InviteCodeStore()
