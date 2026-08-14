#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MySQL 连接 + chat_user 表（2026-08-13 文档 §7.1 / §8.1）。

行为：
- 启动时建立连接池（懒加载）
- 失败时进入"下线模式"：所有操作返回 None，由调用方降级到 JSON 文件
- 不引入 ORM（pymysql 走 SQL，schema 由本文件维护）
"""
import json
import os
import threading
import time
from typing import Optional, List, Dict, Any


MYSQL_HOST = os.getenv("MYSQL_HOST", "127.0.0.1")
MYSQL_PORT = int(os.getenv("MYSQL_PORT", "3306"))
MYSQL_USER = os.getenv("MYSQL_USER", "root")
MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "")
MYSQL_DATABASE = os.getenv("MYSQL_DATABASE", "chat_room")
MYSQL_DISABLE = os.getenv("MYSQL_DISABLE", "0") == "1"
MYSQL_OP_TIMEOUT = float(os.getenv("MYSQL_OP_TIMEOUT", "2.0"))


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS chat_user (
    user_id       VARCHAR(64)  NOT NULL,
    username      VARCHAR(64)  NOT NULL,
    password_hash VARCHAR(128) DEFAULT NULL,
    salt          VARCHAR(64)  DEFAULT NULL,
    room_id       VARCHAR(64)  DEFAULT NULL,
    role          VARCHAR(32)  DEFAULT 'member',
    app_id        VARCHAR(64)  DEFAULT 'default',
    bus_id        VARCHAR(64)  DEFAULT NULL,
    ext_id        VARCHAR(64)  DEFAULT NULL,
    ext_data      LONGTEXT     DEFAULT NULL,
    created_at    BIGINT       NOT NULL,
    updated_at    BIGINT       DEFAULT NULL,
    PRIMARY KEY (user_id),
    KEY idx_username (username),
    KEY idx_app_bus (app_id, bus_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""


class MySQLClient:
    """轻量 MySQL 客户端（懒连接 + 失败下线 + 定时重试）。"""

    def __init__(self):
        self._lock = threading.RLock()
        self._conn = None
        self._disabled = MYSQL_DISABLE
        self._retry_at = 0.0
        self._last_error = ""

    # ---------------------------------------------------------------------
    # 连接管理
    # ---------------------------------------------------------------------
    def _get_conn(self):
        """拿到一个连接（失败返回 None）。"""
        if self._disabled:
            return None
        now = time.time()
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.ping(reconnect=True)
                    return self._conn
                except Exception:
                    self._conn = None
            if now < self._retry_at:
                return None
            try:
                import pymysql
                self._conn = pymysql.connect(
                    host=MYSQL_HOST,
                    port=MYSQL_PORT,
                    user=MYSQL_USER,
                    password=MYSQL_PASSWORD,
                    database=MYSQL_DATABASE,
                    charset="utf8mb4",
                    autocommit=True,
                    connect_timeout=MYSQL_OP_TIMEOUT,
                    read_timeout=MYSQL_OP_TIMEOUT,
                    write_timeout=MYSQL_OP_TIMEOUT,
                )
                self._last_error = ""
                return self._conn
            except Exception as e:
                self._last_error = str(e)
                self._conn = None
                # 30 秒内不再尝试
                self._retry_at = now + 30
                return None

    def _mark_offline(self, e: str = "") -> None:
        """本次失败：标记下线 30s。"""
        with self._lock:
            self._conn = None
            self._last_error = e
            self._retry_at = time.time() + 30

    def is_ok(self) -> bool:
        """健康检查：能在 1s 内 ping 通。"""
        c = self._get_conn()
        if c is None:
            return False
        try:
            c.ping(reconnect=True)
            return True
        except Exception:
            return False

    def init_schema(self) -> bool:
        """启动时建表（不存在则建）。"""
        c = self._get_conn()
        if c is None:
            return False
        try:
            with c.cursor() as cur:
                cur.execute(SCHEMA_SQL)
            return True
        except Exception as e:
            self._mark_offline(str(e))
            return False

    # ---------------------------------------------------------------------
    # CRUD：同步走 SQL 操作 users
    # ---------------------------------------------------------------------
    def insert_user(self, rec: Dict[str, Any]) -> bool:
        c = self._get_conn()
        if c is None:
            return False
        try:
            with c.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO chat_user
                      (user_id, username, password_hash, salt, room_id, role,
                       app_id, bus_id, ext_id, ext_data, created_at, updated_at)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    ON DUPLICATE KEY UPDATE
                      username=VALUES(username),
                      password_hash=VALUES(password_hash),
                      salt=VALUES(salt),
                      room_id=VALUES(room_id),
                      role=VALUES(role),
                      app_id=VALUES(app_id),
                      bus_id=VALUES(bus_id),
                      ext_id=VALUES(ext_id),
                      ext_data=VALUES(ext_data),
                      updated_at=VALUES(updated_at)
                    """,
                    (
                        rec.get("user_id"),
                        rec.get("username"),
                        rec.get("password_hash") or None,
                        rec.get("salt") or None,
                        rec.get("room_id"),
                        rec.get("role") or "member",
                        rec.get("app_id") or "default",
                        rec.get("bus_id"),
                        rec.get("ext_id"),
                        json.dumps(rec.get("ext_data") or {}, ensure_ascii=False),
                        rec.get("created_at") or int(time.time()),
                        int(time.time()),
                    ),
                )
            return True
        except Exception as e:
            self._mark_offline(str(e))
            return False

    def update_user(self, user_id: str, fields: Dict[str, Any]) -> bool:
        c = self._get_conn()
        if c is None:
            return False
        if not fields:
            return True
        # 允许的字段白名单
        allowed = {"username", "password_hash", "salt", "room_id", "role",
                   "app_id", "bus_id", "ext_id", "ext_data"}
        sets = []
        vals = []
        for k, v in fields.items():
            if k not in allowed:
                continue
            if k == "ext_data" and v is not None and not isinstance(v, str):
                v = json.dumps(v, ensure_ascii=False)
            sets.append(f"{k}=%s")
            vals.append(v)
        if not sets:
            return True
        sets.append("updated_at=%s")
        vals.append(int(time.time()))
        vals.append(user_id)
        try:
            with c.cursor() as cur:
                cur.execute(
                    f"UPDATE chat_user SET {', '.join(sets)} WHERE user_id=%s",
                    tuple(vals),
                )
            return cur.rowcount >= 0
        except Exception as e:
            self._mark_offline(str(e))
            return False

    def get_by_username(self, username: str) -> Optional[Dict[str, Any]]:
        c = self._get_conn()
        if c is None:
            return None
        try:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT * FROM chat_user WHERE username=%s LIMIT 1",
                    (username,),
                )
                row = cur.fetchone()
            return self._row_to_rec(row) if row else None
        except Exception as e:
            self._mark_offline(str(e))
            return None

    def get_by_user_id(self, user_id: str) -> Optional[Dict[str, Any]]:
        c = self._get_conn()
        if c is None:
            return None
        try:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT * FROM chat_user WHERE user_id=%s LIMIT 1",
                    (user_id,),
                )
                row = cur.fetchone()
            return self._row_to_rec(row) if row else None
        except Exception as e:
            self._mark_offline(str(e))
            return None

    def get_by_app_bus(self, app_id: str, bus_id: str) -> Optional[Dict[str, Any]]:
        c = self._get_conn()
        if c is None:
            return None
        try:
            with c.cursor() as cur:
                cur.execute(
                    "SELECT * FROM chat_user WHERE app_id=%s AND bus_id=%s LIMIT 1",
                    (app_id, bus_id),
                )
                row = cur.fetchone()
            return self._row_to_rec(row) if row else None
        except Exception as e:
            self._mark_offline(str(e))
            return None

    def count(self) -> int:
        c = self._get_conn()
        if c is None:
            return -1
        try:
            with c.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM chat_user")
                return int(cur.fetchone()[0])
        except Exception as e:
            self._mark_offline(str(e))
            return -1

    # ---------------------------------------------------------------------
    # 迁移：从 JSON 导入
    # ---------------------------------------------------------------------
    def bulk_insert(self, records: List[Dict[str, Any]]) -> int:
        """批量插入（迁移用），返回成功条数。"""
        c = self._get_conn()
        if c is None:
            return 0
        ok = 0
        for rec in records:
            if self.insert_user(rec):
                ok += 1
        return ok

    def _row_to_rec(self, row) -> Dict[str, Any]:
        """SQL 行 → 业务 record dict（与 JSON 格式一致）。"""
        cols = ("user_id", "username", "password_hash", "salt", "room_id",
                "role", "app_id", "bus_id", "ext_id", "ext_data",
                "created_at", "updated_at")
        rec = dict(zip(cols, row))
        if rec.get("ext_data"):
            try:
                rec["ext_data"] = json.loads(rec["ext_data"])
            except Exception:
                rec["ext_data"] = {}
        else:
            rec["ext_data"] = None
        return rec


# 全局单例
mysql_db = MySQLClient()
