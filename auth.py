#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
账号 / JWT 鉴权模块
- 用户表持久化到 users.json
- 密码用 PBKDF2-HMAC-SHA256 哈希存储（无需第三方依赖）
- JWT 用 PyJWT (HS256)
"""

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import threading
import time
import uuid
from pathlib import Path
from typing import Optional, Set, Tuple

import jwt

USERNAME_RE = re.compile(r"^[A-Za-z0-9_\-]{3,32}$")

logger = logging.getLogger(__name__)

# 用户表文件
USERS_FILE = Path(__file__).parent / "users.json"

# 被撤销 token 持久化文件
REVOKED_FILE = Path(__file__).parent / "revoked_tokens.json"

# 密码哈希参数
PBKDF2_ITERS = 200_000
PBKDF2_SALT_BYTES = 16
PBKDF2_HASH_BYTES = 32

# JWT 参数
JWT_ALG = "HS256"
JWT_TTL_SECONDS = 7 * 24 * 3600  # 7 天
JWT_ISSUER = "srs-project"
# 时钟偏移容差（秒）：客户端时间略快/慢时避免误判过期
JWT_LEEWAY_SECONDS = 30

# JWT 密钥：优先环境变量，否则用一个随机生成的并落到 users.json 同目录的 .jwt_secret
JWT_SECRET_FILE = Path(__file__).parent / ".jwt_secret"


class AuthError(Exception):
    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


class UserStore:
    """线程安全的用户表（持久化到 JSON）"""

    def __init__(self, path: Path = USERS_FILE):
        self._path = path
        self._lock = threading.RLock()
        self._users: dict = {}  # username -> {username, user_id, room_id?, password_hash, salt, role, created_at}
        # 快速索引：user_id -> username（便于反向查找）
        self._user_id_to_name: dict = {}
        self._load()

    def _load(self):
        if self._path.exists():
            try:
                with self._path.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self._users = data
                    self._user_id_to_name = {
                        rec.get("user_id"): name
                        for name, rec in data.items()
                        if rec.get("user_id")
                    }
            except Exception:
                self._users = {}

    def _flush(self):
        tmp = self._path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(self._users, f, ensure_ascii=False, indent=2)
        tmp.replace(self._path)

    @staticmethod
    def _hash_password(password: str, salt: bytes) -> bytes:
        return hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, PBKDF2_ITERS, dklen=PBKDF2_HASH_BYTES
        )

    def register(self, username: str, password: str) -> dict:
        username = (username or "").strip()
        password = password or ""
        if not USERNAME_RE.match(username):
            raise AuthError(400, "用户名必须为 3-32 位字母/数字/下划线/连字符")
        if len(password) < 6 or len(password) > 128:
            raise AuthError(400, "密码长度需在 6-128 之间")

        with self._lock:
            if username in self._users:
                raise AuthError(409, "用户名已存在")

            salt = secrets.token_bytes(PBKDF2_SALT_BYTES)
            pwd_hash = self._hash_password(password, salt)
            # 稳定的 user_id：user_<uuid>，作为 user_manager 中的身份标识
            user_id = "user_" + uuid.uuid4().hex[:12]
            self._users[username] = {
                "username": username,
                "user_id": user_id,
                "room_id": None,
                "role": "member",
                "salt": salt.hex(),
                "password_hash": pwd_hash.hex(),
                "created_at": int(time.time()),
            }
            self._user_id_to_name[user_id] = username
            self._flush()
            return self._users[username]

    def verify(self, username: str, password: str) -> dict:
        username = (username or "").strip()
        with self._lock:
            record = self._users.get(username)
            if not record:
                raise AuthError(401, "用户名或密码错误")
            # 外部账号（业务后端用户）不允许本地密码登录
            if not record.get("password_hash"):
                raise AuthError(401, "用户名或密码错误")
            salt = bytes.fromhex(record["salt"])
            expected = bytes.fromhex(record["password_hash"])
            actual = self._hash_password(password or "", salt)
            if not hmac.compare_digest(actual, expected):
                raise AuthError(401, "用户名或密码错误")
            # 兼容老数据：补字段并落盘
            changed = False
            if not record.get("user_id"):
                record["user_id"] = "user_" + uuid.uuid4().hex[:12]
                self._user_id_to_name[record["user_id"]] = username
                changed = True
            if "room_id" not in record:
                record["room_id"] = None
                changed = True
            if not record.get("role"):
                record["role"] = "member"
                changed = True
            if changed:
                self._flush()
            return record

    def get_by_username(self, username: str) -> Optional[dict]:
        with self._lock:
            return self._users.get(username)

    def get_by_user_id(self, user_id: str) -> Optional[dict]:
        """按 user_id 查记录（扫描 _user_id_to_name → username → record）"""
        with self._lock:
            name = self._user_id_to_name.get(user_id)
            if name:
                rec = self._users.get(name)
                if rec:
                    return rec
            # 兜底：兼容早期没有 _user_id_to_name 的情况（直接扫描）
            for name, rec in self._users.items():
                if rec.get("user_id") == user_id:
                    return rec
            return None

    def update_username(self, user_id: str, new_username: str) -> bool:
        """更新库中 username。user_id 决定唯一记录，new_username 是新的显示名。

        行为：
          - 若 user_id 不存在 → 返回 False（不抛错）。
          - 同步维护 _user_id_to_name 索引（删除旧的 username 映射）。
          - 落盘到 users.json。
        """
        new_username = (new_username or "").strip()
        with self._lock:
            rec = None
            old_username = ""
            for name, r in self._users.items():
                if r.get("user_id") == user_id:
                    rec = r
                    old_username = name
                    break
            if rec is None:
                return False
            if old_username == new_username:
                return True
            # 注意：_users 的 key 是 username，但 get_or_create_from_external
            # 用 username 当唯一键。改名时不应改 key，否则会破坏按 username 的索引。
            # 改的是 record["username"] 字段（外部读取时使用的"显示名"），
            # 而不是 _users 的 key。
            rec["username"] = new_username
            rec["updated_at"] = int(time.time())
            self._flush()
            return True

    def set_room(self, username: str, room_id: Optional[str]) -> None:
        """登录进房后回填 / 离开房间后清空"""
        with self._lock:
            rec = self._users.get(username)
            if not rec:
                return
            rec["room_id"] = room_id
            self._flush()

    def set_role(self, username: str, role: str) -> None:
        with self._lock:
            rec = self._users.get(username)
            if not rec:
                return
            rec["role"] = role
            self._flush()

    def get_or_create_from_external(self, username: str, ext_data: dict) -> dict:
        """业务后端三方登录时，根据业务用户身份在本地创建或更新记录。

        - 如果 username 已存在，直接返回（不重复创建）
        - 如果 username 不存在，自动注册一个新账号：
            * user_id: user_<uuid>
            * username: 业务后端返回的 username/nickname/id
            * ext_id: 业务后端的用户 id（如果有）
            * ext_data: 完整业务后端返回的用户信息（快照）
        """
        with self._lock:
            if username in self._users:
                return self._users[username]

            user_id = "user_" + uuid.uuid4().hex[:12]
            self._users[username] = {
                "username": username,
                "user_id": user_id,
                "room_id": None,
                "role": "member",
                "ext_id": str(ext_data.get("id", "")) or None,
                "ext_data": ext_data,
                "created_at": int(time.time()),
                # 内部账号密码相关字段设空（不允许本地密码登录）
                "salt": "",
                "password_hash": "",
            }
            self._user_id_to_name[user_id] = username
            self._flush()
            logger.info(f"[UserStore] 业务后端用户首次登录，自动创建账号: username={username} user_id={user_id}")
            return self._users[username]


# JWT 密钥加载（环境变量优先，否则从文件读取，否则随机生成并落盘）
def _load_jwt_secret() -> str:
    env_secret = os.getenv("JWT_SECRET", "").strip()
    if env_secret:
        return env_secret
    if JWT_SECRET_FILE.exists():
        return JWT_SECRET_FILE.read_text(encoding="utf-8").strip()
    secret = secrets.token_urlsafe(48)
    JWT_SECRET_FILE.write_text(secret, encoding="utf-8")
    try:
        os.chmod(JWT_SECRET_FILE, 0o600)
    except Exception:
        pass
    return secret


JWT_SECRET = _load_jwt_secret()


def issue_token(
    username: str,
    user_id: str = "",
    room_id: Optional[str] = None,
    role: str = "member",
    name: Optional[str] = None,
) -> Tuple[str, str, int]:
    """签发 JWT。返回 (token, jti, expires_at_unix)。
    同时把 jti 注册到 revocation.active_jtis，供后续 logout-all 使用。

    name: 用户在聊天室内显示的"显示名"（与 username 不同：username 是 user_store._users 的 key，
          通常是业务后端 identity；name 是客户端登录时通过 user_name 传入或兜底取的"显示名"）。
          写入 JWT 的 `name` claim，便于 WebSocket 事件直接读取，避免每次查库。
    """
    now = int(time.time())
    exp = now + JWT_TTL_SECONDS
    jti = uuid.uuid4().hex
    payload = {
        "sub": username,
        "uid": user_id,
        # name 默认等于 username（向后兼容）
        "name": (name or username or ""),
        "room": room_id,
        "role": role,
        "jti": jti,
        "iat": now,
        "exp": exp,
        "iss": JWT_ISSUER,
    }
    token = jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALG)
    revocation.register(username, jti, exp)
    return token, jti, exp


def verify_token(token: str) -> dict:
    """校验 JWT，返回 payload。失败抛 AuthError。
    同时检查 jti 是否在撤销列表里。
    """
    if not token:
        raise AuthError(401, "missing token")
    try:
        payload = jwt.decode(
            token,
            JWT_SECRET,
            algorithms=[JWT_ALG],
            issuer=JWT_ISSUER,
            options={"require": ["sub", "exp", "iat", "jti"]},
            leeway=JWT_LEEWAY_SECONDS,
        )
    except jwt.ExpiredSignatureError:
        raise AuthError(401, "token expired")
    except jwt.InvalidTokenError as e:
        raise AuthError(401, f"invalid token: {e}")
    if revocation.is_revoked(payload):
        raise AuthError(401, "token revoked")
    return payload


# 全局单例
user_store = UserStore()


class TokenRevocationStore:
    """撤销列表（持久化到 revoked_tokens.json）。
    支持两类撤销：
      1) 按 jti 撤销单个 token
      2) 按用户名撤销该用户所有活跃 token（需要先追踪 jti）
    """

    def __init__(self, path: Path = REVOKED_FILE):
        self._path = path
        self._lock = threading.RLock()
        # jti -> expires_at
        self._revoked_jtis: dict = {}
        # username -> {jti: expires_at}
        # 维护每个用户签发过的活跃 token，用于 logout-all
        self._active_jtis: dict = {}
        self._load()

    def _load(self):
        if self._path.exists():
            try:
                with self._path.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self._revoked_jtis = {
                        str(k): int(v) for k, v in (data.get("jtis") or {}).items()
                    }
                    active = data.get("active") or {}
                    self._active_jtis = {
                        str(u): {str(j): int(e) for j, e in (jtis or {}).items()}
                        for u, jtis in active.items()
                    }
                self._gc()
            except Exception:
                self._revoked_jtis = {}
                self._active_jtis = {}

    def _flush(self):
        tmp = self._path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(
                {
                    "jtis": self._revoked_jtis,
                    "active": self._active_jtis,
                },
                f, ensure_ascii=False, indent=2,
            )
        tmp.replace(self._path)

    def _gc(self):
        """清理已过期的记录"""
        now = int(time.time())
        self._revoked_jtis = {
            jti: exp for jti, exp in self._revoked_jtis.items() if exp > now
        }
        self._active_jtis = {
            u: {j: e for j, e in jtis.items() if e > now}
            for u, jtis in self._active_jtis.items()
        }

    def register(self, username: str, jti: str, exp: int):
        """注册一个新签发的活跃 token（用于后续 logout-all）"""
        with self._lock:
            self._active_jtis.setdefault(username, {})[jti] = exp
            self._flush()

    def revoke_jti(self, jti: str, exp: int):
        with self._lock:
            self._revoked_jtis[jti] = exp
            # 同时从活跃列表中移除
            for u, jtis in self._active_jtis.items():
                jtis.pop(jti, None)
            self._flush()

    def revoke_user_all(self, username: str) -> int:
        """撤销该用户所有活跃 token，返回撤销数量"""
        with self._lock:
            jtis = self._active_jtis.get(username, {})
            count = 0
            for jti, exp in list(jtis.items()):
                if exp > int(time.time()):
                    self._revoked_jtis[jti] = exp
                    count += 1
            self._active_jtis.pop(username, None)
            self._flush()
            return count

    def is_revoked(self, payload: dict) -> bool:
        with self._lock:
            jti = payload.get("jti", "")
            if jti and jti in self._revoked_jtis:
                return True
            return False

    def status(self) -> dict:
        with self._lock:
            return {
                "revoked_jtis": len(self._revoked_jtis),
                "active_users": len(self._active_jtis),
                "active_jtis_total": sum(len(j) for j in self._active_jtis.values()),
            }


# 全局单例
revocation = TokenRevocationStore()