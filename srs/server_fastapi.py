#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SRS HTTP回调服务器 - FastAPI版本
同时支持原生 WebSocket 和 HTTP API

所有接口响应格式统一为：
  成功: {"code": 0, "message": "success", "data": {...}}
  错误: {"code": <4xx/5xx>, "message": "错误信息"}

参考文档：客户端接口综合文档 v1.0（2026-06-19）
"""

import os
import re
import sys
import json
import time
import uuid
import logging
import asyncio
import threading
from typing import Dict, Set, Optional, List, Tuple
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware
import requests

# 加载 .env 环境变量文件
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))

# user_id 格式正则见下方 §用户查询接口（§1087 _USER_ID_RE）

from translation_manager import (
    TranslationManager, TranslationRequest, TranslationStatus, translation_manager
)
from user_manager import (
    UserManager, UserRole, UserStatus, User, Room, user_manager
)
from notification_service import notification_service
from notification_store import notification_store
from message_store import message_store
from invite_code_store import invite_code_store
from auth import user_store, issue_token, verify_token, AuthError, JWT_TTL_SECONDS, revocation, USERNAME_RE
from sync_client import sync
from invitation_store import invitation_store
from share_manager import share_manager, SHARE_DOMAIN
from user_name_resolver import (
    validate_user_name,
    resolve_display_name,
    invalidate_user_name_cache,
    UserNameInvalidError as UserNameError,
)

# 配置日志
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

log_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'callback_server.log')
file_handler = logging.FileHandler(log_file, encoding='utf-8')
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))

console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))

logger.addHandler(file_handler)
logger.addHandler(console_handler)

# API 鉴权：所有 /api/v1/* 业务接口（除白名单）都需要带 JWT
# - Authorization: Bearer <jwt>   → 校验通过，注入 request.state.user_id
# - 无 token 或校验失败 → 401
logger.info("[Auth] JWT-based auth enabled for /api/v1/*")

# =============================================================================
# 业务后端三方验证配置
# =============================================================================
BUSINESS_BACKEND_URL = os.getenv("BUSINESS_BACKEND_URL", "http://8.138.45.176:8080")
# 外部服务密钥，对应业务后端请求头 X-External-Service-Token
# 安全要求：必须由环境变量注入，不在源码中保留任何字面量兜底（避免 commit 进仓库后泄漏）
BUSINESS_APP_KEY = os.getenv("BUSINESS_APP_KEY")
if not BUSINESS_APP_KEY:
    raise RuntimeError(
        "BUSINESS_APP_KEY 未设置。请在 srs/.env 中配置后重启服务。"
    )
# 业务后端获取当前登录用户信息的接口
BUSINESS_PROFILE_PATH = os.getenv("BUSINESS_PROFILE_PATH", "/api/frontend/app/external/users/me")

# 完整 URL，留作调试日志与热感知
BUSINESS_PROFILE_URL = f"{BUSINESS_BACKEND_URL}{BUSINESS_PROFILE_PATH}"

# =============================================================================
# Redis 缓存（用户显示名缓存，见 user_name_resolver.py）
# 配置缺失或不连通 → 自动降级到进程内 dict + DB，接口契约不变
REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_DB = int(os.getenv("REDIS_DB", "0"))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD") or None

# 2026-08-13 文档 §2.2：room_socket 心跳参数（可配置）
HEARTBEAT_INTERVAL = int(os.getenv("HEARTBEAT_INTERVAL", "30"))
HEARTBEAT_FAIL_THRESHOLD = int(os.getenv("HEARTBEAT_FAIL_THRESHOLD", "3"))
# =============================================================================


def verify_external_token(external_token: str) -> dict:
    """拿着客户端的 external_token 去业务后端验证，返回业务用户信息。

    业务后端接口：
      GET {BUSINESS_BACKEND_URL}{BUSINESS_PROFILE_PATH}
      Header: Authorization: Bearer <external_token>
              X-External-Service-Token: {BUSINESS_APP_KEY}

    业务后端返回的用户信息（存到 data 里）：
      {
        "code": 0,
        "data": {
          "id": 123,
          "username": "alice",
          "nickname": "Alice",
          "avatar": "https://...",
          ...
        }
      }

    验证成功返回用户信息 dict，失败抛 AuthError。
    """
    url = BUSINESS_PROFILE_URL
    headers = {
        "Authorization": f"Bearer {external_token[:8]}...",  # 日志中只截前8位
        "X-External-Service-Token": BUSINESS_APP_KEY,
    }
    logger.info(
        f"[Auth] 业务后端验证开始: url={url} "
        f"external_token_len={len(external_token)} external_token_prefix={external_token[:8]}..."
    )
    t0 = time.time()
    try:
        resp = requests.get(
            url,
            headers={
                "Authorization": f"Bearer {external_token}",  # 实际请求用完整 token
                "X-External-Service-Token": BUSINESS_APP_KEY,
            },
            timeout=8,
        )
    except requests.exceptions.Timeout:
        elapsed = int((time.time() - t0) * 1000)
        logger.error(f"[Auth] 业务后端连接超时: url={url} elapsed_ms={elapsed}")
        raise AuthError(503, "业务后端连接超时，请稍后重试")
    except requests.RequestException as e:
        elapsed = int((time.time() - t0) * 1000)
        logger.error(f"[Auth] 业务后端请求失败: url={url} elapsed_ms={elapsed} err={e!r}")
        raise AuthError(503, "业务后端不可用，请稍后重试")

    elapsed = int((time.time() - t0) * 1000)
    logger.info(
        f"[Auth] 业务后端响应: status={resp.status_code} elapsed_ms={elapsed} "
        f"body_len={len(resp.text)} body_preview={resp.text[:300]!r}"
    )

    if resp.status_code == 401 or resp.status_code == 403:
        # 业务后端明确拒绝（token 无效/过期/权限不足）
        logger.warning(f"[Auth] 业务后端拒绝: HTTP {resp.status_code} -> resp={resp.text[:300]}")
        raise AuthError(401, "token 无效或已过期")
    if resp.status_code == 404:
        # 404 说明接口路径不对，或者业务后端服务停了/换了
        logger.error(f"[Auth] 业务后端 404（接口可能变更）: path={resp.text[:200]}")
        raise AuthError(503, "认证服务暂时不可用，请联系管理员")
    if resp.status_code >= 500:
        logger.error(f"[Auth] 业务后端服务端错误: HTTP {resp.status_code}")
        raise AuthError(503, "认证服务暂时不可用，请稍后重试")
    if resp.status_code != 200:
        logger.warning(f"[Auth] 业务后端返回 {resp.status_code}: {resp.text[:200]}")
        raise AuthError(401, "token 无效或已过期")

    try:
        body = resp.json()
    except ValueError as e:
        logger.error(f"[Auth] 业务后端返回非 JSON: err={e!r} text={resp.text[:200]!r}")
        raise AuthError(503, "认证服务返回格式异常")

    # 业务后端用 code 表示成功码：常见为 0 或 200
    # 区分业务成功 vs 业务失败：成功必须有 data 字段且为非空 dict
    backend_code = body.get("code")
    data = body.get("data")
    if not isinstance(data, dict) or not data:
        # data 缺失或为空 → 真正的失败
        msg = body.get("message", "token 无效或已过期")
        logger.warning(
            f"[Auth] 业务后端业务失败: backend_code={backend_code} message={msg} body={body!r}"
        )
        raise AuthError(401, msg)

    # 取 username（业务后端可能用 nickname/username/userId/userNo/中的某一个）
    username = (
        data.get("username")
        or data.get("nickname")
        or data.get("userNo")
        or str(data.get("userId", data.get("id", "")))
    ).strip()
    if not username:
        logger.error(f"[Auth] 业务后端用户信息缺身份字段: data_keys={list(data.keys())}")
        raise AuthError(500, "业务后端返回的用户信息缺少身份字段")

    logger.info(
        f"[Auth] 业务后端验证成功: username={username} "
        f"data_keys={list(data.keys())}"
    )
    return data


# 不需要鉴权的路径（公开接口、SRS 内部回调）
PUBLIC_PATHS = {
    "/api/v1/health",
    "/api/v1/auth/register",
    "/api/v1/auth/login",
    "/api/v1/auth/test_login",  # 仅本地/测试环境使用，生产部署建议网关鉴权
    # WS 元接口：返回 ws URL / 房间连接数，无需业务鉴权
    "/api/v1/ws/subscribe",
    "/api/v1/ws/status",
    # ID 解析：允许任意客户端查询 bus_id → chat_user_id 的映射
    "/api/v1/users/resolve",
}

# SRS 内部回调（来自 localhost，无法带 token）
SRS_CALLBACK_PATHS = {
    "/api/v1/streams/on_publish",
    "/api/v1/streams/on_unpublish",
    "/api/v1/streams/on_play",
    "/api/v1/streams/on_stop",
    "/api/v1/hooks/on_publish",
    "/api/v1/hooks/on_unpublish",
    "/api/v1/hooks/on_play",
    "/api/v1/hooks/on_stop",
}


class ApiAuthMiddleware(BaseHTTPMiddleware):
    """对 /api/v1/* 业务接口进行 JWT 校验，注入 request.state.user_id"""
    async def dispatch(self, request, call_next):
        path = request.url.path
        # 不在 /api/v1/ 下，放行（包括 /ws、/health、SRS 推流等）
        if not path.startswith("/api/v1/"):
            return await call_next(request)
        # 公开接口放行
        if path in PUBLIC_PATHS:
            return await call_next(request)
        # SRS 回调放行
        if path in SRS_CALLBACK_PATHS:
            return await call_next(request)
        # 提取 Bearer token
        auth_header = request.headers.get("Authorization", "")
        token = ""
        if auth_header.lower().startswith("bearer "):
            token = auth_header[7:].strip()
        if not token:
            token = request.headers.get("X-Auth-Token", "").strip()
        try:
            payload = verify_token(token)
        except AuthError as e:
            return JSONResponse(
                status_code=e.code,
                content={"code": e.code, "message": e.message},
            )
        # 注入业务可用的当前用户身份（实时同步自 user_store / user_manager）
        username = payload.get("sub", "")
        user_id = payload.get("uid", "")
        # JWT 的 name claim（客户端登录时传入的 user_name），WebSocket 事件直接读取
        name = payload.get("name", "") or username
        # role 优先以 user_store 中记录的为准（owner 可随时改 admin/member，无需重新登录）
        account = user_store.get_by_username(username) if username else None
        live_role = (account.get("role") if account else None) or payload.get("role", "member")
        # 优先使用 user_store 中实时的 username（避免 JWT 中 name claim 与库不一致时显示错）
        if account and (account.get("username") or ""):
            name = account["username"]
        # room_id 优先以 user_manager 中的实时房间为准（覆盖 JWT 中过期的 room 字段）
        live_room_id = user_manager.find_room_for_user(user_id)
        # 如果实时查不到（用户已离线/未进房），保留 JWT 中的 room 字段；否则以实时为准
        if live_room_id is not None:
            room_id_final = live_room_id
        else:
            # 还尝试用 user_store.room_id 作为兜底
            room_id_final = (account.get("room_id") if account else None) or payload.get("room")

        request.state.username = username
        request.state.name = name
        request.state.user_id = user_id
        request.state.room_id = room_id_final
        request.state.role = live_role
        request.state.jti = payload.get("jti", "")
        request.state.iat = int(payload.get("iat", 0))
        request.state.exp = int(payload.get("exp", 0))
        request.state.payload = payload
        return await call_next(request)


# 全局变量
port = int(os.getenv('CALLBACK_PORT', 8085))
SRS_URL = os.getenv("SRS_URL", "rtmp://localhost:1935")
_srs_host = SRS_URL.split("//", 1)[-1].split(":")[0]
SRS_HTTP_API = os.getenv("SRS_HTTP_API", f"http://{_srs_host}:1985")

# 客户端可访问的公网 host（用于返回 ws URL 等）
# 默认从环境变量读，否则用 0.0.0.0（本机测试）；生产应该设成 ECS 公网 IP 或域名
PUBLIC_WS_HOST = os.getenv("PUBLIC_WS_HOST", os.getenv("HOST", "localhost"))
PUBLIC_WS_PORT = int(os.getenv("PUBLIC_WS_PORT", port))

# WebSocket 连接管理
native_ws_connections: Dict[str, Set] = {}
native_ws_lock = threading.Lock()

# SRS 拉流播放器追踪：room_id -> {client_id_set, count}
room_players: Dict[str, Dict[str, object]] = {}
room_players_lock = threading.Lock()

# 敲门请求记录：knocker_id -> {room_id, owner_id, message, timestamp}
knock_requests: Dict[str, dict] = {}

# 房间邀请记录：使用持久化存储 invitation_store（invitations.json）
# 见 docs/房间邀请功能_服务端需求.md §2.1
# 注意：原内存 Dict[str, dict] 已被 invitation_store 取代。
INVITATION_TTL_SECONDS = 24 * 3600        # 邀请 24 小时过期
INVITATION_CLEANUP_INTERVAL = 3600        # 每小时清理一次


def _new_invitation_id() -> str:
    """生成邀请 ID：inv_<12位随机字符>"""
    import secrets
    import string
    chars = string.ascii_lowercase + string.digits
    return "inv_" + "".join(secrets.choice(chars) for _ in range(12))


def _invite_view(inv: dict, *, include_invitee: bool = False, include_status: bool = False) -> dict:
    """构造邀请展示视图（不同接口返回字段不同）。"""
    out = {
        "id": inv["id"],
        "room_id": inv["room_id"],
        "room_name": inv.get("room_name", ""),
        "inviter_id": inv["inviter_id"],
        "inviter_name": inv.get("inviter_name", ""),
        "created_at": inv["created_at"],
        "message": inv.get("message", ""),
    }
    if include_invitee:
        out["invitee_id"] = inv["invitee_id"]
    if include_status:
        out["status"] = inv["status"]
        out["expires_at"] = inv["expires_at"]
    return out


def _invitation_cleanup_worker():
    """后台线程：把过期的 pending 邀请标记为 expired（每小时一次）。"""
    global invitation_cleanup_running
    logger.info("[InviteCleanup] Starting worker (interval=%ss)", INVITATION_CLEANUP_INTERVAL)
    while invitation_cleanup_running:
        try:
            n = invitation_store.sweep_expired(int(time.time()))
            if n:
                logger.info("[InviteCleanup] Expired %d invitation(s)", n)
        except Exception as e:
            logger.error("[InviteCleanup] Error: %s", e, exc_info=True)
        # 用短切片 sleep，便于快速停止
        for _ in range(INVITATION_CLEANUP_INTERVAL):
            if not invitation_cleanup_running:
                break
            time.sleep(1)
    logger.info("[InviteCleanup] Worker stopped")


SHARE_CLEANUP_INTERVAL = 3600  # 每小时清理一次


def _share_cleanup_worker():
    """后台线程：把过期的分享链接标记为 expired（每小时一次）。"""
    global share_cleanup_running
    logger.info("[ShareCleanup] Starting worker (interval=%ss)", SHARE_CLEANUP_INTERVAL)
    while share_cleanup_running:
        try:
            n = share_manager.cleanup_expired_links()
            if n:
                logger.info("[ShareCleanup] Expired %d share link(s)", n)
        except Exception as e:
            logger.error("[ShareCleanup] Error: %s", e, exc_info=True)
        for _ in range(SHARE_CLEANUP_INTERVAL):
            if not share_cleanup_running:
                break
            time.sleep(1)
    logger.info("[ShareCleanup] Worker stopped")


share_cleanup_running = False
share_cleanup_thread = None


# ==============================================================================
# 统一响应格式工具
# ==============================================================================

def api_ok(data: dict = None) -> dict:
    """成功响应：code=0, message=success, data=...（data=None 时返回空对象）"""
    return {
        "code": 0,
        "message": "success",
        "data": data if data is not None else {}
    }


def api_err(code: int, message: str) -> JSONResponse:
    """错误响应：统一格式"""
    return JSONResponse(status_code=code if code < 600 else 500, content={
        "code": code,
        "message": message
    })


def _get_timestamp() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


# ==============================================================================
# ConnectionManager（WebSocket）
# ==============================================================================

class ConnectionManager:
    """WebSocket 连接管理器

    2026-08-13 文档 §2：
    - 在线状态由本管理器 + 心跳驱动（不再由 /join 接口设置）
    - 3 次心跳无响应 → 标记 offline（offline_at）
    - 断开连接 → 标记 offline
    """

    def __init__(self):
        self.active_connections: Dict[str, list] = {}
        self.user_connections: Dict[str, WebSocket] = {}
        # 2026-08-13：心跳 + 在线状态跟踪
        # _ws_state: ws -> {user_id, room_id, last_pong_at, fail_count}
        self._ws_state: Dict[WebSocket, dict] = {}
        self._ws_lock = threading.RLock()

    async def connect(self, websocket: WebSocket, room_id: str, user_id: str = ""):
        with native_ws_lock:
            if room_id not in self.active_connections:
                self.active_connections[room_id] = []
            self.active_connections[room_id].append(websocket)
        if user_id:
            self.user_connections[user_id] = websocket
            with self._ws_lock:
                self._ws_state[websocket] = {
                    "user_id": user_id,
                    "room_id": room_id,
                    "last_pong_at": time.time(),
                    "fail_count": 0,
                }
            # 2026-08-13 §2.2：连接建立 → 标记在线（覆盖之前的 offline）
            try:
                room = user_manager.get_room(room_id)
                if room and user_id in room.members:
                    user_manager._mark_member_online(room_id, user_id)
            except Exception as e:
                logger.warning(f"[WS] mark_online failed for {user_id}/{room_id}: {e}")
        logger.info(f"[WS] Connected: room={room_id}, user={user_id}")
        await websocket.send_json({
            "type": "connected",
            "room_id": room_id,
            "user_id": user_id
        })
        # 2026-08-13 §2.2：状态变更广播
        if user_id:
            await self._broadcast_online_status(room_id, user_id, "online")

    def disconnect(self, websocket: WebSocket, room_id: str):
        with native_ws_lock:
            if room_id in self.active_connections:
                if websocket in self.active_connections[room_id]:
                    self.active_connections[room_id].remove(websocket)
                if not self.active_connections[room_id]:
                    del self.active_connections[room_id]
        user_id = None
        for uid, ws in list(self.user_connections.items()):
            if ws == websocket:
                user_id = uid
                del self.user_connections[uid]
        # 2026-08-13 §2.2：断开 → 标记 offline（保留房间关联）
        offline_at = None
        if user_id:
            with self._ws_lock:
                self._ws_state.pop(websocket, None)
            try:
                room = user_manager.get_room(room_id)
                if room and user_id in room.members:
                    offline_at = user_manager._mark_member_offline(room_id, user_id)
            except Exception as e:
                logger.warning(f"[WS] mark_offline failed for {user_id}/{room_id}: {e}")
        logger.info(f"[WS] Disconnected: room={room_id}, user={user_id}")
        # 2026-08-13 §2.2：状态变更广播（异步触发）
        if user_id and offline_at is not None:
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    asyncio.create_task(self._broadcast_online_status(
                        room_id, user_id, "offline", offline_at=offline_at))
            except RuntimeError:
                pass

    async def _broadcast_online_status(self, room_id: str, user_id: str,
                                        status: str, offline_at: int = 0):
        """广播成员在线/离线状态变更（2026-08-13 §2.2 / 8.1 事件）"""
        msg = {
            "type": "member_online_status_changed",
            "room_id": room_id,
            "user_id": user_id,
            "data": {
                "online_status": status,
                "offline_at": offline_at,
            },
        }
        await self.broadcast_to_room_with_timestamp(room_id, msg)
        # 同步：触发通知服务（如果用户已不在任何房间，不需要 notification）
        if status == "offline":
            try:
                notification_service.notify_member_offline(
                    room_id=room_id, user_id=user_id, offline_at=offline_at)
            except Exception as e:
                logger.warning(f"[WS] notify_member_offline failed: {e}")

    def record_pong(self, websocket: WebSocket):
        """客户端响应 pong 时重置失败计数（2026-08-13 §2.2）"""
        with self._ws_lock:
            state = self._ws_state.get(websocket)
            if state:
                state["last_pong_at"] = time.time()
                state["fail_count"] = 0

    async def check_heartbeats(self, ping_interval: int = 30, fail_threshold: int = 3):
        """扫描所有 ws：对超过 ping_interval*fail_threshold 秒未 pong 的 → 强制关闭。

        文档 §2.2：3 次 ping 无响应 → 判离线。
        """
        now = time.time()
        stale: List[Tuple[WebSocket, str, str]] = []  # (ws, room_id, user_id)
        with self._ws_lock:
            for ws, state in list(self._ws_state.items()):
                elapsed = now - state["last_pong_at"]
                # 超过 ping_interval 秒未收到 pong → 计数 +1
                if elapsed > ping_interval:
                    state["fail_count"] += 1
                    if state["fail_count"] >= fail_threshold:
                        stale.append((ws, state["room_id"], state["user_id"]))
        # 警告但未到阈值：发一次 ping（让客户端有最后一次响应机会）
        for ws, state in list(self._ws_state.items()):
            with self._ws_lock:
                elapsed = now - state["last_pong_at"]
                if ping_interval < elapsed < ping_interval * fail_threshold:
                    try:
                        await ws.send_json({"type": "ping"})
                    except Exception:
                        stale.append((ws, state["room_id"], state["user_id"]))
        # 强制断开
        for ws, room_id, user_id in stale:
            logger.info(f"[WS] Heartbeat failure threshold: closing {user_id}/{room_id}")
            try:
                await ws.close(code=1011, reason="heartbeat_timeout")
            except Exception:
                pass
            self.disconnect(ws, room_id)

    async def broadcast_to_room(self, room_id: str, message: dict):
        with native_ws_lock:
            connections = list(self.active_connections.get(room_id, []))
        disconnected = []
        for ws in connections:
            try:
                await ws.send_json(message)
            except:
                disconnected.append(ws)
        for ws in disconnected:
            self.disconnect(ws, room_id)

    async def send_to_user(self, user_id: str, message: dict):
        if user_id in self.user_connections:
            try:
                await self.user_connections[user_id].send_json(message)
            except:
                del self.user_connections[user_id]

    async def broadcast_to_room_with_timestamp(self, room_id: str, message: dict):
        """广播时附上 timestamp"""
        msg = dict(message)
        msg["timestamp"] = _get_timestamp()
        await self.broadcast_to_room(room_id, msg)

    async def send_to_user_with_timestamp(self, user_id: str, message: dict):
        msg = dict(message)
        msg["timestamp"] = _get_timestamp()
        await self.send_to_user(user_id, msg)

    async def broadcast_to_room_exclude(self, room_id: str, message: dict, exclude_user_ids: Set[str]):
        """广播给房间内除指定用户外的所有在线成员"""
        msg = dict(message)
        msg["timestamp"] = _get_timestamp()
        with native_ws_lock:
            # 从 user_connections 找在线用户（排除 owner）
            for uid, ws in list(self.user_connections.items()):
                if uid in exclude_user_ids:
                    continue
                # 检查该用户是否在当前房间
                room = user_manager.get_room(room_id)
                if room and uid in room.members:
                    try:
                        await ws.send_json(msg)
                    except:
                        del self.user_connections[uid]


manager = ConnectionManager()

# ==============================================================================
# FastAPI 应用
# ==============================================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("[FastAPI] Starting up...")
    start_heartbeat_checker()
    notification_service._native_ws_manager = manager

    # 启动邀请过期清理后台线程
    global invitation_cleanup_running, invitation_cleanup_thread
    invitation_cleanup_running = True
    invitation_cleanup_thread = threading.Thread(
        target=_invitation_cleanup_worker, daemon=True, name="invitation-cleanup"
    )
    invitation_cleanup_thread.start()

    # 启动分享链接过期清理后台线程
    global share_cleanup_running, share_cleanup_thread
    share_cleanup_running = True
    share_cleanup_thread = threading.Thread(
        target=_share_cleanup_worker, daemon=True, name="share-cleanup"
    )
    share_cleanup_thread.start()

    await sync.start()
    yield

    # 停止邀请过期清理
    invitation_cleanup_running = False
    if invitation_cleanup_thread:
        invitation_cleanup_thread.join(timeout=2)

    # 停止分享链接过期清理
    share_cleanup_running = False
    if share_cleanup_thread:
        share_cleanup_thread.join(timeout=2)

    # 停止邀请异步写盘线程
    invitation_store.stop(timeout=3.0)

    await sync.stop()
    logger.info("[FastAPI] Shutting down...")

app = FastAPI(title="SRS Callback Server", lifespan=lifespan)


@app.exception_handler(HTTPException)
async def _http_exception_handler(request: Request, exc: HTTPException):
    """统一 HTTPException 响应为项目标准格式 {code, message, data}"""
    # B) 客户端把 /api/v1/users/names 写成 GET 时会被 Starlette 拒为 405，
    # 这里加一行 INFO 便于确认客户端是否真的改成 POST 了。
    if exc.status_code == 405 and request.url.path == "/api/v1/users/names":
        logger.info(
            f"[API] /users/names wrong method: method={request.method} "
            f"remote={request.client.host if request.client else '-'}"
        )
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "code": exc.status_code,
            "message": str(exc.detail) if exc.detail is not None else "",
            "data": None,
        },
    )


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# API Token 鉴权（在内层，避免 OPTIONS 预检被 401 拦截）
app.add_middleware(ApiAuthMiddleware)


# ==============================================================================
# WebSocket 端点
# ==============================================================================

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    room_id = websocket.query_params.get("room", "")
    user_id = websocket.query_params.get("user", "")

    if not room_id:
        await websocket.close(code=1008, reason="Missing room parameter")
        return

    await websocket.accept()
    await manager.connect(websocket, room_id, user_id)

    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get('type', '')

            if msg_type == 'ping':
                # 客户端 → 服务端 ping（兼容旧客户端）
                await websocket.send_json({"type": "pong"})
            elif msg_type == 'pong':
                # 服务端 ping → 客户端 pong（2026-08-13 §2.2）
                manager.record_pong(websocket)
            elif msg_type == 'subscribe':
                new_room = data.get('room_id', room_id)
                manager.disconnect(websocket, room_id)
                await manager.connect(websocket, new_room, user_id)
                room_id = new_room
            elif msg_type == 'chat_message':
                # 2026-08-13 §5.2：WS 发送消息（持久化 + 广播）
                try:
                    client_msg_id = data.get('client_msg_id', '')
                    if not client_msg_id:
                        await websocket.send_json({"type": "error", "message": "client_msg_id required"})
                        continue
                    item = message_store.send(
                        room_id=room_id,
                        user_id=user_id,
                        client_msg_id=client_msg_id,
                        msg_type=data.get('msg_type', data.get('type2', 'text')),
                        content=data.get('content', ''),
                        file_name=data.get('file_name', ''),
                        file_size=data.get('file_size', 0),
                        mime_type=data.get('mime_type', ''),
                        width=data.get('width', 0),
                        height=data.get('height', 0),
                        timestamp=data.get('timestamp'),
                    )
                    if not item.get("_idempotent"):
                        await _broadcast_chat_message(room_id, item)
                    await websocket.send_json({
                        "type": "chat_message_ack",
                        "id": item["id"],
                        "seq": item["seq"],
                        "client_msg_id": client_msg_id,
                        "_idempotent": bool(item.get("_idempotent")),
                    })
                except Exception as e:
                    logger.warning(f"[WS] chat_message error: {e}")
                    try:
                        await websocket.send_json({"type": "error", "message": str(e)})
                    except Exception:
                        pass
            elif msg_type == 'history_sync':
                # 2026-08-13 §5.2：离线补拉（按 seq 增量）
                try:
                    after_seq = int(data.get('after_seq', 0))
                    items = message_store.history(room_id, after_seq=after_seq, limit=200)
                    await websocket.send_json({
                        "type": "history_sync",
                        "room_id": room_id,
                        "items": items,
                        "latest_seq": message_store.latest_seq(room_id),
                    })
                except Exception as e:
                    logger.warning(f"[WS] history_sync error: {e}")

    except WebSocketDisconnect:
        manager.disconnect(websocket, room_id)
    except Exception as e:
        logger.warning(f"[WS] Error: {e}")
        manager.disconnect(websocket, room_id)


# ==============================================================================
# 统一异常处理
# ==============================================================================

@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    # 将 FastAPI HTTPException 转换为统一格式
    code = exc.status_code
    # 提取 detail 中的 message
    detail = exc.detail
    if isinstance(detail, dict):
        message = detail.get("message", str(detail))
    else:
        message = str(detail)
    return JSONResponse(status_code=code, content={
        "code": code,
        "message": message
    })


@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception):
    logger.error(f"[Unhandled Exception] {exc}", exc_info=True)
    return JSONResponse(status_code=500, content={
        "code": 500,
        "message": str(exc)
    })


# ==============================================================================
# Pydantic 请求模型（与文档保持一致）
# ==============================================================================

class RoomCreateRequest(BaseModel):
    room_id: str
    owner_id: str = ""                  # 现从 JWT 取，此字段保留兼容
    name: str = ""                     # 文档 §3.1：可选，默认等于 room_id

class RoomJoinRequest(BaseModel):
    user_id: str = ""
    room_id: str = None                # 可选，URL 路径中有则优先用路径的
    role: str = "member"               # 文档 §4.3：可选，默认 member
    invite_code: str = ""              # 2026-08-13 文档 §4.2.1：加入房间需校验邀请码（owner/admin 除外）

# 文档 §2.3 变更说明：移除 RoomLeaveRequest（/leave 接口已删除）
# class RoomLeaveRequest(BaseModel):
#     user_id: str = ""

class MemberOperatorRequest(BaseModel):
    """禁言/禁麦/踢人等接口的统一 operator_id 参数（现已从 JWT 取，此字段保留兼容）"""
    operator_id: str = ""

class KnockRequest(BaseModel):
    user_id: str = ""
    message: str = ""

class KnockAcceptRequest(BaseModel):
    operator_id: str = ""
    knocker_id: str
    role: str = "member"

class KnockRejectRequest(BaseModel):
    operator_id: str = ""
    knocker_id: str
    reason: str = ""

class TranslationStartRequest(BaseModel):
    room_id: str
    source_user: str
    target_user: str
    source_lang: str = None            # 不传则为 "auto"
    target_lang: str = None
    to_lang: str = None                # 兼容客户端

class TranslationTextRequest(BaseModel):
    room_id: str
    source_user: str
    target_user: str
    original_text: str
    translated_text: str
    source_lang: str
    target_lang: str

class TranslationHeartbeatRequest(BaseModel):
    room_id: str
    source_user: str
    client_id: str
    to_lang: str = None
    request_id: str = None


class MessageSendRequest(BaseModel):
    """2026-08-13 文档 §5.2：消息发送请求（含 client_msg_id 幂等）"""
    client_msg_id: str
    type: str = "text"  # text / image / file
    content: str = ""
    file_name: str = ""
    file_size: int = 0
    mime_type: str = ""
    width: int = 0
    height: int = 0
    timestamp: float = None


class MessageHistoryRequest(BaseModel):
    """文档 §5.2：历史查询的房间 + 游标"""
    room_id: str
    after_seq: int = 0
    limit: int = 50


# ==============================================================================
# §8 健康检查
# ==============================================================================

@app.get("/health")
async def health_check():
    return {"status": "ok"}

@app.get("/api/v1/health")
async def health_check_v1():
    return {"status": "ok"}


# ==============================================================================
# 账号 / JWT 鉴权接口（白名单：无需 token 也可访问）
# ==============================================================================

class RegisterRequest(BaseModel):
    username: str
    password: str


class LoginRequest(BaseModel):
    # 三方验证模式：客户端带业务后端 token 来登录
    external_token: Optional[str] = None
    # 兼容旧模式（内部账号，直接验用户名密码）
    username: Optional[str] = None
    password: Optional[str] = None
    # 客户端传入的"显示名"（与 username 解耦，存到 users.username，
    # 写入 JWT name claim，用于房间内成员名/敲门/说话事件显示）
    # 可选；不带时沿用业务后端返回的 nickname 或 user_id 兜底（向后兼容）
    user_name: Optional[str] = None
    # 业务应用标识，用于 (app_id, bus_id) 联合映射。
    # 若不传，resolve 接口会按 JWT 的 app_id 或 X-App-Id 请求头补缺。
    app_id: Optional[str] = None


@app.post("/api/v1/auth/register")
async def auth_register(req: RegisterRequest):
    """注册新用户。成功直接返回 JWT。"""
    try:
        record = user_store.register(req.username, req.password)
    except AuthError as e:
        return api_err(e.code, e.message)
    token, _jti, exp = issue_token(
        username=record["username"],
        user_id=record["user_id"],
        room_id=record.get("room_id"),
        role=record.get("role", "member"),
    )
    return {
        "code": 0,
        "message": "success",
        "data": {
            "username": record["username"],
            "user_id": record["user_id"],
            "room_id": record.get("room_id"),
            "role": record.get("role", "member"),
            "token": token,
            "expires_at": exp,
            "expires_in": JWT_TTL_SECONDS,
        },
    }


class TestLoginRequest(BaseModel):
    """临时测试登录：免业务后端校验，签发 1 小时短期 JWT。

    ⚠️ 仅用于本地/测试环境调试，禁止在生产使用。
    会真正往 users.json 写一条记录（或复用同名记录），保证与正常登录共用同一套接口。
    """
    user_name: str
    user_id: Optional[str] = None  # 不传则按 user_<uuid> 自动生成（也可传固定的 chat_user_id 复用）
    app_id: Optional[str] = None   # 可选，默认 "default"
    bus_id: Optional[str] = None    # 可选，模拟业务后端用户 id，resolve 时用
    role: Optional[str] = None     # 可选，默认 "member"


@app.post("/api/v1/auth/test_login")
async def auth_test_login(req: TestLoginRequest):
    """临时测试登录：传入 user_name/user_id 直接签发 1 小时 JWT，不调业务后端。

    鉴权：本接口本身**不要求** Authorization，生产部署建议通过反向代理白名单 / 网关鉴权。
    说明：
      - 若 user_id 已存在且 user_name 与之关联 → 复用旧记录（行为同正常登录）
      - 若 user_id 已存在但被其他 user_name 占用 → 仍以 user_name 为准，覆盖 username
        （业务方应保证自己的 user_id 唯一）
      - 若都不存在 → 创建新记录
      - 不动业务后端、不写审计日志
    """
    user_name = (req.user_name or "").strip()
    if not user_name:
        return api_err(400, "user_name 不能为空")

    app_id = (req.app_id or "").strip() or "default"
    bus_id = (req.bus_id or "").strip() or None  # 可为 None，表示非业务账号
    role = (req.role or "").strip() or "member"

    with user_store._lock:
        # 1) 按 user_id 找已有记录
        rec = None
        if req.user_id:
            rec = user_store.get_by_user_id(req.user_id)

        # 2) 找到记录 → 同步 username（user_name 为准）
        if rec:
            old_username_key = None
            for k, r in user_store._users.items():
                if r.get("user_id") == rec.get("user_id"):
                    old_username_key = k
                    break
            target_username = user_name
            if old_username_key != target_username:
                # 删除旧 key，重命名
                if target_username in user_store._users:
                    # 目标名已被占用：以已有记录为准，丢弃旧 key
                    user_store._users.pop(old_username_key, None)
                else:
                    user_store._users[target_username] = rec
                    user_store._users.pop(old_username_key, None)
                user_store._user_id_to_name[rec["user_id"]] = target_username
            rec["username"] = target_username
            rec["app_id"] = app_id
            rec["bus_id"] = bus_id
            rec["role"] = role
            if bus_id:
                user_store._app_bus_to_uid[(app_id, bus_id)] = rec["user_id"]

        # 3) 没找到 → 创建新记录
        else:
            uid = req.user_id or ("user_" + uuid.uuid4().hex[:12])
            if user_name in user_store._users:
                # user_name 已存在，复用其记录并改 user_id
                rec = user_store._users[user_name]
                old_uid = rec.get("user_id")
                if old_uid:
                    user_store._user_id_to_name.pop(old_uid, None)
                rec["user_id"] = uid
                rec["app_id"] = app_id
                rec["bus_id"] = bus_id
                rec["role"] = role
            else:
                rec = {
                    "username": user_name,
                    "user_id": uid,
                    "room_id": None,
                    "role": role,
                    "app_id": app_id,
                    "bus_id": bus_id,
                    "ext_id": bus_id,
                    "ext_data": None,
                    "created_at": int(time.time()),
                    "salt": "",
                    "password_hash": "",
                }
                user_store._users[user_name] = rec
            user_store._user_id_to_name[uid] = user_name
            if bus_id:
                user_store._app_bus_to_uid[(app_id, bus_id)] = uid
            user_store._flush()

    user_id = rec["user_id"]

    # 短期 token（1 小时）。复用 issue_token，但临时修改 TTL 后再恢复，避免影响其他接口
    global JWT_TTL_SECONDS
    saved_ttl = JWT_TTL_SECONDS
    JWT_TTL_SECONDS = 60 * 60
    try:
        token, _jti, exp = issue_token(
            username=user_name,
            user_id=user_id,
            room_id=rec.get("room_id"),
            role=role,
            name=user_name,
            app_id=app_id,
        )
    finally:
        JWT_TTL_SECONDS = saved_ttl

    logger.warning(
        f"[Auth] test_login 临时 token 已签发: user_name={user_name} "
        f"user_id={user_id} bus_id={bus_id} app_id={app_id} role={role} ttl=3600s"
    )

    return {
        "code": 0,
        "message": "success",
        "data": {
            "username": user_name,
            "name": user_name,
        "user_id": user_id,
        "app_id": app_id,
        "bus_id": bus_id,
        "role": role,
        "token": token,
        "expires_at": exp,
        "expires_in": 3600,
    },
}


@app.post("/api/v1/auth/login")
async def auth_login(req: LoginRequest):
    """登录，支持两种模式：

    1. 三方验证（external_token 优先）：
       客户端带业务后端 token → 聊天服务器去业务后端验 → 验过发自己的 JWT

    2. 内部账号（兼容旧模式）：
       客户端直接带 username+password → 聊天服务器本地验 → 发 JWT
    """
    # ---------- 模式1：业务后端三方验证 ----------
    logger.info(
        f"[Auth] /auth/login 入口: mode=external "
        f"external_token_len={len(req.external_token) if req.external_token else 0} "
        f"external_token_prefix={req.external_token[:8] if req.external_token else None} "
        f"user_name={req.user_name!r}"
    )
    if req.external_token:
        try:
            ext_data = verify_external_token(req.external_token)
        except AuthError as e:
            return api_err(e.code, e.message)

        # 从业务后端拿到的用户身份作为 username
        # 优先用 username/nickname/userNo/userId 中的一个
        username = (
            ext_data.get("username")
            or ext_data.get("nickname")
            or ext_data.get("userNo")
            or str(ext_data.get("userId", ext_data.get("id", "")))
        ).strip()
        if not username:
            logger.error(f"[Auth] 业务后端用户信息缺身份字段: data_keys={list(ext_data.keys())}")
            return api_err(500, "业务后端返回的用户信息缺少身份字段")

        # app_id 优先级：1) 请求体  2) 业务后端 ext_data.app_id  3) "default"
        app_id = (
            (req.app_id or "").strip()
            or (ext_data.get("app_id") or "").strip()
            or "default"
        )

        # 校验 client 传入的 user_name（可选）。
        try:
            client_user_name = validate_user_name(req.user_name or "")
        except UserNameError as e:
            return api_err(e.code, e.message)

        # 用 user_store 做"注册或取记录"：按 (app_id, bus_id) 定位，
        # 同一业务用户在同 app 下始终映射到同一个 chat_user_id
        record = user_store.get_or_create_from_external(username, ext_data, app_id=app_id)

        # 需求 §2.3 优先级：1) 请求体 user_name  2) 业务后端 nickname  3) user_id 兜底
        biz_user_name = (
            ext_data.get("nickname")
            or ext_data.get("username")
            or ""
        ).strip()
        final_user_name = client_user_name or biz_user_name or record["user_id"]

        # 需求 §2.7：若 client 带 user_name 且与库中不同 → 更新库
        if final_user_name and (record.get("username") or "") != final_user_name:
            ok = user_store.update_username(record["user_id"], final_user_name)
            if ok:
                record["username"] = final_user_name
                invalidate_user_name_cache(record["user_id"])
                logger.info(
                    f"[Auth] 更新 username: user_id={record['user_id']} "
                    f"old={record.get('username')!r} new={final_user_name!r}"
                )

        # 发 token 时把 name 写入 JWT，便于 WebSocket 事件直接读
        record_app_id = record.get("app_id", "default")
        token, _jti, exp = issue_token(
            username=record["username"],
            user_id=record["user_id"],
            room_id=record.get("room_id"),
            role=record.get("role", "member"),
            name=final_user_name,
            app_id=record_app_id,
        )
        logger.info(
            f"[Auth] 三方登录成功: app_id={record_app_id} username={record['username']} "
            f"user_id={record['user_id']} name={final_user_name!r}"
        )
        return {
            "code": 0,
            "message": "success",
            "data": {
                "username": record["username"],  # 与库中一致（含更新后的显示名）
                "name": final_user_name,         # 实际生效的"显示名"，便于客户端核对
                "user_id": record["user_id"],
                "app_id": record.get("app_id", "default"),
                "bus_id": record.get("bus_id"),
                "room_id": record.get("room_id"),
                "role": record.get("role", "member"),
                "token": token,
                "expires_at": exp,
                "expires_in": JWT_TTL_SECONDS,
            },
        }

    # ---------- 模式2：内部账号（兼容旧客户端） ----------
    logger.info(
        f"[Auth] /auth/login 入口: mode=internal username={req.username!r} "
        f"password_len={len(req.password) if req.password else 0} "
        f"user_name={req.user_name!r}"
    )
    if not req.username or not req.password:
        return api_err(400, "需要提供 external_token 或 username+password")
    try:
        record = user_store.verify(req.username, req.password)
    except AuthError as e:
        logger.warning(f"[Auth] 内部账号校验失败: username={req.username!r} code={e.code} message={e.message}")
        return api_err(e.code, e.message)

    # 校验 client 传入的 user_name（可选）
    try:
        client_user_name = validate_user_name(req.user_name or "")
    except UserNameError as e:
        return api_err(e.code, e.message)

    # 内部账号模式下，user_name 是显示名；若 client 传了就更新库 + 缓存
    if client_user_name and (record.get("username") or "") != client_user_name:
        # 注意：内部账号模式下 _users 的 key 就是 username，不能改 key
        # 这里只更新 record["username"] 字段语义为"显示名"，但保持 key 不变
        # → 需求 §2.7：重复登录带 user_name → 更新库中 username（按 record.username 字段）
        old = record.get("username", "")
        record["username"] = client_user_name
        with user_store._lock:
            user_store._flush()
        invalidate_user_name_cache(record["user_id"])
        logger.info(f"[Auth] 内部账号更新 username: user_id={record['user_id']} old={old!r} new={client_user_name!r}")

    final_user_name = client_user_name or record["username"] or record["user_id"]

    token, _jti, exp = issue_token(
        username=record["username"],
        user_id=record["user_id"],
        room_id=record.get("room_id"),
        role=record.get("role", "member"),
        name=final_user_name,
    )
    return {
        "code": 0,
        "message": "success",
        "data": {
            "username": record["username"],
            "name": final_user_name,
            "user_id": record["user_id"],
            "room_id": record.get("room_id"),
            "role": record.get("role", "member"),
            "token": token,
            "expires_at": exp,
            "expires_in": JWT_TTL_SECONDS,
        },
    }


@app.get("/api/v1/auth/me")
async def auth_me(request: Request):
    """查询当前登录用户（依赖 middleware 注入的 request.state.*）"""
    username = getattr(request.state, "username", None)
    if not username:
        raise HTTPException(status_code=401, detail="not authenticated")
    return {
        "code": 0,
        "message": "success",
        "data": {
            "username": username,
            "user_id": getattr(request.state, "user_id", ""),
            "room_id": getattr(request.state, "room_id", None),
            "role": getattr(request.state, "role", "member"),
        },
    }


@app.post("/api/v1/auth/logout")
async def auth_logout(request: Request):
    """撤销当前 token（立即失效）。后续用此 token 调任何业务接口都会 401。"""
    jti = getattr(request.state, "jti", "")
    exp = getattr(request.state, "exp", 0)
    username = getattr(request.state, "username", "")
    if not jti:
        raise HTTPException(status_code=401, detail="not authenticated")
    revocation.revoke_jti(jti, exp)
    logger.info(f"[Auth] logout: user={username} jti={jti[:8]}...")
    return {"code": 0, "message": "logged out", "data": {}}


class LogoutAllRequest(BaseModel):
    username: str = ""            # 可选；不填则撤销当前用户


@app.post("/api/v1/auth/logout-all")
async def auth_logout_all(request: Request, req: LogoutAllRequest):
    """撤销某个用户所有未过期 token。
    - body 里不传 username：撤销当前登录用户的所有 token
    - body 里传 username：必须等于当前登录用户（不允许撤销别人）
    """
    cur_username = getattr(request.state, "username", "")
    if not cur_username:
        raise HTTPException(status_code=401, detail="not authenticated")
    target = req.username.strip() or cur_username
    if target != cur_username:
        raise HTTPException(status_code=403, detail="只能撤销自己的 token")
    count = revocation.revoke_user_all(target)
    logger.info(f"[Auth] logout-all: user={target} revoked={count}")
    return {"code": 0, "message": "all tokens revoked", "data": {"username": target, "revoked_count": count}}


@app.get("/api/v1/auth/revocation-status")
async def auth_revocation_status():
    """查看撤销列表的规模（白名单，调试用）"""
    return {"code": 0, "message": "success", "data": revocation.status()}




# ==============================================================================
# §3 房间管理接口
# ==============================================================================

@app.get("/api/v1/rooms")
async def get_rooms():
    """§3.3 查询所有房间"""
    try:
        rooms = user_manager.get_all_rooms()
        room_list = [
            {
                "room_id": r.room_id,
                "owner_id": r.owner_id,
                "member_count": len(r.members)
            }
            for r in rooms
        ]
        return api_ok({"rooms": room_list})
    except Exception as e:
        logger.error(f"[API] get_rooms error: {e}")
        return api_err(500, str(e))

@app.post("/api/v1/room")
async def create_room(request: Request, req: RoomCreateRequest):
    """§3.1 创建房间（owner_id 从 JWT 取，请求体里若带值需一致）

    2026-08-13 文档 §7.1：
    - 用户已是某房间房主 → 不创建新房间，走"覆盖更新"现有房间（room_id 不变）
    - 用户非任何房主 → 正常创建，用户成为 owner
    """
    try:
        jwt_user_id = request.state.user_id
        if not jwt_user_id:
            return api_err(401, "not authenticated")

        # 若请求体里指定了 owner_id，必须等于当前登录用户（防越权）
        if req.owner_id and req.owner_id != jwt_user_id:
            return api_err(403, "owner_id must match current user")

        # 2026-08-13 文档 §7.1：房主唯一性 + 覆盖更新语义
        existing_owned = user_manager.find_owned_room(jwt_user_id)
        if existing_owned:
            # 已是某房间房主 → 不创建新房间，覆盖更新现有房间
            # 若请求中的 room_id 与现有不同，记一条警告（不阻断，覆盖优先）
            if req.room_id and req.room_id != existing_owned:
                logger.warning(
                    f"[API] create_room: user {jwt_user_id} already owns room {existing_owned}, "
                    f"ignoring requested room_id {req.room_id}, applying overwrite"
                )
            room = user_manager.update_room(
                existing_owned,
                name=req.name if req.name else None,
            )
            logger.info(f"[API] Owner {jwt_user_id} re-create: overwrite room {existing_owned}")
        else:
            # 非任何房间房主 → 正常创建
            room = user_manager.create_room(req.room_id, jwt_user_id, name=req.name)

        asyncio.create_task(sync.room_created(
            room_id=room.room_id,
            owner_id=room.owner_id,
            name=room.name or req.name or "",
            max_members=room.max_members,
        ))
        return api_ok({
            "room_id": room.room_id,
            "name": room.name,
            "owner_id": room.owner_id,
            "created_at": room.created_at,
            "max_members": room.max_members,
            "allow_speak": room.allow_speak,
            "member_count": len(room.members),
            # 2026-08-13 文档：标记是否走"覆盖更新"语义（客户端可据此选择不重置房间状态）
            "overwritten": bool(existing_owned),
        })
    except Exception as e:
        logger.error(f"[API] create_room error: {e}")
        return api_err(400, str(e))

@app.get("/api/v1/room/{room_id}")
async def get_room_info(room_id: str):
    """§3.2 获取房间信息"""
    try:
        room = user_manager.get_room(room_id)
        if not room:
            return api_err(404, f"Room not found: {room_id}")
        return api_ok({
            "room_id": room.room_id,
            "name": room.name,
            "owner_id": room.owner_id,
            "created_at": room.created_at,
            "max_members": room.max_members,
            "allow_speak": room.allow_speak,
            "member_count": len(room.members)
        })
    except Exception as e:
        logger.error(f"[API] get_room error: {e}")
        return api_err(500, str(e))

@app.get("/api/v1/rooms/{room_id}/health")
async def room_health(room_id: str):
    """§3.2 检测房间是否存活"""
    try:
        room = user_manager.get_room(room_id)
        if not room:
            return JSONResponse(status_code=404, content={
                "code": 404,
                "message": "room not found or closed",
                "data": None
            })
        if room.status.value == "closed":
            return JSONResponse(status_code=404, content={
                "code": 404,
                "message": "room not found or closed",
                "data": None
            })
        return api_ok({
            "room_id": room.room_id,
            "status": room.status.value,
            "owner_id": room.owner_id,
            # 2026-08-13：移除 owner_status，改用 owner_online_status（取自 Room.members[owner]）
            "owner_online_status": (
                room.members[room.owner_id].online_status
                if room.owner_id and room.owner_id in room.members else "offline"
            ),
            "member_count": len(room.members)
        })
    except Exception as e:
        logger.error(f"[API] room_health error: {e}")
        return api_err(500, str(e))


# ==============================================================================
# § 用户名查询接口（按 user_id 取 username / 批量）
# ==============================================================================
_USER_ID_RE = re.compile(r"^user_[A-Za-z0-9]{12}$")


@app.get("/api/v1/users/{user_id}/name")
async def get_user_name(request: Request, user_id: str):
    """按 user_id 查询用户名。

    鉴权：需 Authorization: Bearer <chat_token>
    响应：data = {user_id, username, avatar=null}
    """
    try:
        # 鉴权
        jwt_user_id = getattr(request.state, "user_id", None)
        if not jwt_user_id:
            return api_err(401, "token 无效或已过期")

        # 格式校验
        if not _USER_ID_RE.match(user_id or ""):
            return JSONResponse(
                status_code=400,
                content={"code": 400, "message": "user_id 格式非法"},
            )

        # 查记录
        rec = user_store.get_by_user_id(user_id)
        if not rec:
            logger.info(
                f"[API] get_user_name 404: user_id={user_id} "
                f"jwt_user_id={jwt_user_id} remote={request.client.host if request.client else '-'}"
            )
            return JSONResponse(
                status_code=404,
                content={"code": 404, "message": "user not found", "data": None},
            )

        username = resolve_display_name(user_id, user_store=user_store, fallback=user_id)
        return api_ok({
            "user_id": user_id,
            "username": username,
            "avatar": None,
        })
    except Exception as e:
        logger.error(f"[API] get_user_name error: {e}")
        return api_err(500, str(e))


class BatchUserNamesRequest(BaseModel):
    user_ids: List[str]


@app.post("/api/v1/users/names")
async def batch_get_user_names(request: Request, req: BatchUserNamesRequest):
    """批量按 user_id 查询用户名（房间成员列表渲染用）。

    鉴权：需 Authorization: Bearer <chat_token>
    请求：data.user_ids: [user_id, ...]  ≤100
    响应：data.users: [{user_id, username, avatar}, ...]  不存在的 user_id 省略
    """
    try:
        jwt_user_id = getattr(request.state, "user_id", None)
        if not jwt_user_id:
            return api_err(401, "token 无效或已过期")

        ids = req.user_ids or []
        if len(ids) > 100:
            return api_err(400, "user_ids 长度不能超过 100")

        # 去重 + 过滤非法
        seen = []
        for uid in ids:
            if isinstance(uid, str) and _USER_ID_RE.match(uid) and uid not in seen:
                seen.append(uid)

        users_out = []
        missing_ids = []  # A) 记录查不到的 user_id，便于回溯
        for uid in seen:
            rec = user_store.get_by_user_id(uid)
            if not rec:
                missing_ids.append(uid)
                continue
            username = resolve_display_name(uid, user_store=user_store, fallback=uid)
            users_out.append({
                "user_id": uid,
                "username": username,
                "avatar": None,
            })

        if missing_ids:
            sample = ",".join(missing_ids[:5])
            more = len(missing_ids) - min(len(missing_ids), 5)
            logger.info(
                f"[API] batch_get_user_names skip: requested={len(seen)} "
                f"missing={len(missing_ids)} sample={sample}{f' +{more}more' if more > 0 else ''} "
                f"jwt_user_id={jwt_user_id} remote={request.client.host if request.client else '-'}"
            )

        return api_ok({"users": users_out})
    except Exception as e:
        logger.error(f"[API] batch_get_user_names error: {e}")
        return api_err(500, str(e))


class UserResolveRequest(BaseModel):
    """统一 ID 解析接口：支持 bus: / chat: / user_ 三种前缀格式。

    用途：客户端只知道业务后端 bus_id（如 123），先调本接口转成 chat_user_id，
    再用 chat_user_id 调其他接口。业务方可在登录时缓存 chat_user_id，
    后续请求直接使用，无需每次 resolve。

    app_id 来源（bus: 前缀省略 app_id 时）：
      1) JWT claim.app
      2) 请求头 X-App-Id
      3) 约定常量 "default"
    """
    ids: List[str]


def _parse_user_id(value: str, default_app_id: str) -> Tuple[str, Optional[str]]:
    """按 §3.1 解析单个 id 值。

    Returns:
        (resolved_chat_user_id, error_reason)
        - resolved_chat_user_id 非空 → 成功
        - error_reason 非空 → 失败
        两者互斥。
    """
    if not value or not isinstance(value, str):
        return "", "user_id 格式非法"

    value = value.strip()
    if not value:
        return "", "user_id 格式非法"

    # chat: 前缀 → 直接使用
    if value.startswith("chat:"):
        chat_uid = value[5:]
        if not re.match(r"^user_[A-Za-z0-9]{12}$", chat_uid):
            return "", "user_id 格式非法"
        return chat_uid, None

    # bus: 前缀 → 转换为 chat_user_id
    if value.startswith("bus:"):
        rest = value[4:]
        parts = rest.split(":", 1)
        if len(parts) == 1:
            app_id, biz_id = default_app_id, parts[0]
        else:
            app_id, biz_id = parts[0], parts[1]

        # 校验 app_id / biz_id 非空、字符集合法
        if not app_id or not biz_id:
            return "", "user_id 格式非法"
        if not re.match(r"^[A-Za-z0-9_-]+$", app_id) or not re.match(r"^[A-Za-z0-9_-]+$", biz_id):
            return "", "user_id 格式非法"

        rec = user_store.get_by_app_bus(app_id, biz_id)
        if not rec:
            return "", "user not found"
        return rec["user_id"], None

    # 存量 user_xxx 格式 → 直接使用
    if re.match(r"^user_[A-Za-z0-9]{12}$", value):
        return value, None

    return "", "user_id 格式非法"


@app.post("/api/v1/users/resolve")
async def resolve_users(request: Request, req: UserResolveRequest):
    """统一 ID 解析：bus: / chat: / user_ -> chat_user_id。

    鉴权：需 Authorization: Bearer <chat_token>
    请求：data.ids: [id_string, ...]  ≤100，元素可为：
            - bus:<app_id>:<bus_id>   多 app 显式形式
            - bus:<bus_id>             单 app，省略 app_id
            - chat:<chat_user_id>      聊天室 id，直接透传
            - <chat_user_id>           存量格式，等价 chat:
    响应：data.resolved: [{input, chat_user_id, username}, ...]
          data.unresolved: [{input, reason}, ...]
    """
    try:
        jwt_payload = getattr(request.state, "payload", {})
        jwt_app_id = (jwt_payload or {}).get("app", "") or "default"
        header_app_id = (request.headers.get("X-App-Id") or "").strip()
        default_app_id = header_app_id or jwt_app_id or "default"

        ids = req.ids or []
        if len(ids) > 100:
            return api_err(400, "ids 长度不能超过 100")

        resolved = []
        unresolved = []
        seen = []

        for raw in ids:
            if not isinstance(raw, str):
                raw = str(raw)
            raw = raw.strip()
            if not raw or raw in seen:
                continue
            seen.append(raw)

            chat_uid, err = _parse_user_id(raw, default_app_id)
            if err:
                unresolved.append({"input": raw, "reason": err})
            else:
                rec = user_store.get_by_user_id(chat_uid)
                username = (rec["username"] if rec else "") or chat_uid
                resolved.append({
                    "input": raw,
                    "chat_user_id": chat_uid,
                    "username": username,
                })

        return api_ok({"resolved": resolved, "unresolved": unresolved})
    except Exception as e:
        logger.error(f"[API] resolve_users error: {e}")
        return api_err(500, str(e))


@app.get("/api/v1/users/{user_id}/room")
async def get_user_room(request: Request, user_id: str):
    """§3.1 获取指定用户的房间（查找好友房间）"""
    try:
        jwt_user_id = request.state.user_id
        if not jwt_user_id:
            return api_err(401, "not authenticated")

        # 检查用户是否在系统中存在
        user_exists = any(
            rec.get("user_id") == user_id or name == user_id
            for name, rec in user_store._users.items()
        )
        if not user_exists:
            return JSONResponse(status_code=404, content={
                "code": 404,
                "message": "user not found",
                "data": None
            })

        info = user_manager.get_user_room_info(user_id)
        if info is None:
            return api_ok(None)

        return api_ok(info)
    except Exception as e:
        logger.error(f"[API] get_user_room error: {e}")
        return api_err(500, str(e))


@app.delete("/api/v1/room/{room_id}")
async def delete_room(request: Request, room_id: str):
    """§3.4 关闭房间（仅群主可操作，从 JWT 取身份；保留房主占位）"""
    try:
        operator_id = request.state.user_id
        if not operator_id:
            return api_err(401, "not authenticated")

        room = user_manager.get_room(room_id)
        if not room:
            return api_err(404, f"Room not found: {room_id}")

        # 只有群主可以关闭房间
        if room.owner_id != operator_id:
            return api_err(403, "只有群主可以关闭房间")

        # 检查活跃资源
        check = await check_room_can_delete(room_id)
        if not check["can_delete"]:
            return JSONResponse(status_code=409, content={
                "code": 409,
                "message": "房间内存在活跃资源，无法删除",
                "data": {
                    "reasons": check["reasons"],
                    "active": check["detail"]
                }
            })

        member_ids = [m.user_id for m in user_manager.get_room_members(room_id) if m.user_id != room.owner_id]

        # 广播 room_closed 给其他成员
        await manager.broadcast_to_room_with_timestamp(room_id, {
            "type": "room_closed",
            "room_id": room_id,
            "data": {
                "closed_by": operator_id,
                "reason": "owner_entered_another_room",
                "timestamp": int(time.time())
            }
        })

        # 关闭非房主成员的 WS 连接
        conns = list(manager.active_connections.get(room_id, []))
        for ws in conns:
            try:
                await ws.send_json({
                    "type": "room_closed",
                    "room_id": room_id,
                    "data": {
                        "closed_by": operator_id,
                        "reason": "owner_entered_another_room",
                        "timestamp": int(time.time())
                    },
                    "timestamp": _get_timestamp(),
                })
                await ws.close(code=1000, reason=f"room {room_id} closed")
            except:
                pass
        manager.active_connections.pop(room_id, None)

        # 清理拉流追踪
        with room_players_lock:
            room_players.pop(room_id, None)

        # 使用 close_room：保留房主占位
        user_manager.close_room(room_id, closed_by=operator_id, reason="owner_entered_another_room")
        asyncio.create_task(sync.room_deleted(room_id=room_id, deleted_by=operator_id))
        logger.info(f"[API] Room {room_id} closed (operator={operator_id})")

        return api_ok({
            "room_id": room_id,
            "owner_id": room.owner_id,
            "closed_members": member_ids
        })
    except JSONResponse:
        raise
    except Exception as e:
        logger.error(f"[API] delete_room error: {e}")
        return api_err(500, str(e))


# ==============================================================================
# §3' /api/v1/rooms/* 复数别名（兼容文档 URL）
# ==============================================================================

@app.get("/api/v1/rooms")
async def list_rooms_alias(request: Request):
    """GET /api/v1/rooms（复数别名）"""
    return await list_rooms(request)


@app.post("/api/v1/rooms")
async def create_room_alias(request: Request, req: RoomCreateRequest):
    """POST /api/v1/rooms（复数别名）"""
    return await create_room(request, req)


@app.get("/api/v1/rooms/{room_id}")
async def get_room_alias(request: Request, room_id: str):
    """GET /api/v1/rooms/{room_id}（复数别名）"""
    return await get_room(request, room_id)


@app.delete("/api/v1/rooms/{room_id}")
async def close_room_alias(request: Request, room_id: str):
    """DELETE /api/v1/rooms/{room_id}（复数别名，关闭房间）"""
    return await delete_room(request, room_id)


@app.get("/api/v1/rooms/{room_id}/members")
async def get_room_members_alias(request: Request, room_id: str):
    """GET /api/v1/rooms/{room_id}/members（复数别名）"""
    return await get_room_members(request, room_id)


@app.get("/api/v1/rooms/{room_id}/member/{user_id}")
async def get_room_member_alias(request: Request, room_id: str, user_id: str):
    """GET /api/v1/rooms/{room_id}/member/{user_id}（复数别名）"""
    return await get_room_member(request, room_id, user_id)


@app.post("/api/v1/rooms/{room_id}/join")
async def join_room_alias(request: Request, room_id: str, req: RoomJoinRequest):
    """POST /api/v1/rooms/{room_id}/join（复数别名）"""
    return await join_room(request, room_id, req)


# 文档 §2.3 变更说明：移除 /api/v1/rooms/{room_id}/leave 接口
# 离线由 room_socket 心跳判定，客户端断开连接即可
# @app.post("/api/v1/rooms/{room_id}/leave")  # 已移除（2026-08-13 文档变更）


@app.delete("/api/v1/rooms/{room_id}/member/{user_id}/kick")
async def kick_member_alias(request: Request, room_id: str, user_id: str, operator_id: str = ""):
    """DELETE /api/v1/rooms/{room_id}/member/{user_id}/kick（复数别名）"""
    return await kick_member(request, room_id, user_id, operator_id)


# ==============================================================================
# §4 成员管理接口
# ==============================================================================

@app.get("/api/v1/room/{room_id}/members")
async def get_room_members(room_id: str):
    """§4.1 获取成员列表"""
    try:
        members = user_manager.get_room_members(room_id)
        room = user_manager.get_room(room_id)
        member_list = [
            {
                "user_id": m.user_id,
                "role": m.role.value if hasattr(m.role, 'value') else m.role,
                "status": m.status.value if hasattr(m.status, 'value') else m.status,
                "publish_allowed": m.publish_allowed,
                "joined_at": m.joined_at,
                "last_active": m.last_active,
            }
            for m in members
        ]
        return api_ok({
            "room_id": room_id,
            "member_count": len(member_list),
            "allow_speak": room.allow_speak if room else True,
            "members": member_list
        })
    except Exception as e:
        logger.error(f"[API] get_room_members error: {e}")
        return api_err(500, str(e))

@app.get("/api/v1/room/{room_id}/member/{user_id}")
async def get_member_detail(room_id: str, user_id: str):
    """§4.2 获取成员详情"""
    try:
        member = user_manager.get_member(room_id, user_id)
        if not member:
            return api_err(404, f"Member {user_id} not found in room {room_id}")
        return api_ok({
            "user_id": member.user_id,
            "room_id": member.room_id,
            "role": member.role.value if hasattr(member.role, 'value') else member.role,
            "status": member.status.value if hasattr(member.status, 'value') else member.status,
            "publish_allowed": member.publish_allowed,
            "joined_at": member.joined_at,
            "last_active": member.last_active,
        })
    except Exception as e:
        logger.error(f"[API] get_member_detail error: {e}")
        return api_err(500, str(e))

@app.post("/api/v1/room/{room_id}/join")
async def join_room(request: Request, room_id: str, req: RoomJoinRequest):
    """§4.3 加入房间（2026-08-13 文档）：
    - 拒绝加入已关闭房间
    - 普通成员必须携带有效邀请码（CAS 消费）
    - 不再设置在线状态（由心跳判定）
    """
    try:
        jwt_user_id = request.state.user_id
        if not jwt_user_id:
            return api_err(401, "not authenticated")
        if req.user_id and req.user_id != jwt_user_id:
            return api_err(403, "user_id must match current user")

        actual_room_id = req.room_id if req.room_id else room_id

        # 先检查房间状态（拒绝加入已关闭房间）
        existing_room = user_manager.get_room(actual_room_id)
        if existing_room and existing_room.status.value == "closed":
            return api_err(400, "room is closed")

        role_map = {"owner": UserRole.OWNER, "admin": UserRole.ADMIN, "member": UserRole.MEMBER, "guest": UserRole.GUEST}
        role = role_map.get(req.role, UserRole.MEMBER)

        # 2026-08-13 文档 §4.2.1：邀请码校验
        # owner/admin 无需邀请码；普通成员/guest 必须
        if role in (UserRole.MEMBER, UserRole.GUEST):
            if not req.invite_code:
                return api_err(403, "invalid or missing invite code")
            ok, reason, _ = invite_code_store.validate(req.invite_code, room_id=actual_room_id)
            if not ok:
                return api_err(403, f"invalid or missing invite code: {reason}")
            # CAS 原子消费（文档 §3.3）
            ok, reason, _ = invite_code_store.consume(req.invite_code, used_by=jwt_user_id)
            if not ok:
                return api_err(403, f"invalid or missing invite code: {reason}")

        user = user_manager.join_room(actual_room_id, jwt_user_id, role=role)

        # 回填 room_id 到账号表（便于下次登录时 JWT 带上 room_id）
        username_for_account = None
        for name, rec in list(user_store._users.items()):
            if rec.get("user_id") == jwt_user_id or name == jwt_user_id:
                username_for_account = name
                break
        if username_for_account:
            user_store.set_room(username_for_account, actual_room_id)

        asyncio.create_task(sync.member_joined(
            room_id=actual_room_id,
            user_id=user.user_id,
            role=user.role.value,
            joined_at=user.joined_at or "",
            last_active="",
        ))

        # 2026-08-13 文档：join 不再设在线状态、也不再广播 owner_online
        # 在线/离线一律由 room_socket 心跳判定（详见 §2）
        await manager.broadcast_to_room_with_timestamp(actual_room_id, {
            "type": "member_joined",
            "room_id": actual_room_id,
            "user_id": jwt_user_id,
            "data": {
                "name": getattr(request.state, "name", "") or jwt_user_id,
                "role": role.value,
            }
        })

        logger.info(f"[API] User {jwt_user_id} joined room {actual_room_id}")
        return api_ok({
            "user_id": user.user_id,
            "room_id": user.room_id,
            "role": user.role.value,
            "status": user.status.value,
            "publish_allowed": user.publish_allowed,
            "joined_at": user.joined_at,
            "online_status": user.online_status,
            "offline_at": user.offline_at,
        })
    except Exception as e:
        logger.error(f"[API] join_room error: {e}")
        return api_err(400, str(e))

# 文档 §2.3 变更说明：移除 /api/v1/room/{room_id}/leave 接口
# 离线由 room_socket 心跳判定，客户端断开连接即可
# @app.post("/api/v1/room/{room_id}/leave")  # 已移除（2026-08-13 文档变更）
# async def leave_room(request: Request, room_id: str, req: RoomLeaveRequest):
#     ...

@app.delete("/api/v1/room/{room_id}/member/{user_id}/kick")
async def kick_member(request: Request, room_id: str, user_id: str, operator_id: str = ""):
    """§4.5 踢出用户（operator_id 从 JWT 取，URL 上若带需一致）"""
    try:
        jwt_user_id = request.state.user_id
        if not jwt_user_id:
            return api_err(401, "not authenticated")
        if operator_id and operator_id != jwt_user_id:
            return api_err(403, "operator_id mismatch")
        operator_id = jwt_user_id

        room = user_manager.get_room(room_id)
        if not room:
            return api_err(404, f"Room not found: {room_id}")

        member = user_manager.get_member(room_id, user_id)
        if not member:
            return api_err(404, f"Member {user_id} not found in room {room_id}")

        # 权限检查：群主可踢任何人；admin 可踢 member
        if not user_manager.can_kick(room_id, operator_id, user_id):
            return api_err(403, "权限不足：群主可踢任何人，admin 可踢普通成员")

        user_manager.leave_room(room_id, user_id)

        asyncio.create_task(sync.member_kicked(room_id=room_id, user_id=user_id, operator_id=operator_id))

        # 2026-08-13 文档 §6.2：通知持久化（被踢人是 recipient）+ notice_socket 实时推送
        await _notify_user(
            user_id=user_id,
            type_="member_kicked",
            title="你被踢出房间",
            content=f"你已被踢出房间 {room_id}",
            room_id=room_id,
            related_user_id=operator_id,
            data={},
        )

        await manager.broadcast_to_room_with_timestamp(room_id, {
            "type": "member_kicked",
            "room_id": room_id,
            "user_id": user_id,
            "operator_id": operator_id
        })

        logger.info(f"[API] User {user_id} kicked from room {room_id} by {operator_id}")
        return api_ok({})
    except JSONResponse:
        raise
    except Exception as e:
        logger.error(f"[API] kick_member error: {e}")
        return api_err(500, str(e))


# ==============================================================================
# §5 敲门接口
# ==============================================================================

@app.post("/api/v1/room/{room_id}/knock")
async def knock_door(request: Request, room_id: str, req: KnockRequest):
    """§5.1 敲门请求（knocker 身份从 JWT 取，请求体里若带需一致）"""
    try:
        jwt_user_id = request.state.user_id
        if not jwt_user_id:
            return api_err(401, "not authenticated")
        if req.user_id and req.user_id != jwt_user_id:
            return api_err(403, "user_id mismatch")
        knocker_id = jwt_user_id

        room = user_manager.get_room(room_id)
        if not room:
            return api_err(404, f"Room not found: {room_id}")

        knock_requests[knocker_id] = {
            "room_id": room_id,
            "owner_id": room.owner_id,
            "knocker_id": knocker_id,
            "message": req.message,
            "timestamp": _get_timestamp()
        }

        # 取敲门者的"显示名"：优先 JWT 中的 name claim，兜底 user_id
        knocker_name = getattr(request.state, "name", "") or knocker_id

        # 通知房主
        await manager.send_to_user_with_timestamp(room.owner_id, {
            "type": "room_knock",
            "room_id": room_id,
            "knocker_id": knocker_id,
            "data": {
                "message": req.message,
                "name": knocker_name,
            }
        })

        logger.info(f"[API] Knock from {knocker_id} (name={knocker_name!r}) on room {room_id}")
        return api_ok({
            "room_id": room_id,
            "owner_id": room.owner_id,
            "knocker_id": knocker_id
        })
    except Exception as e:
        logger.error(f"[API] knock_door error: {e}")
        return api_err(500, str(e))

@app.post("/api/v1/room/{room_id}/knock/accept")
async def knock_accept(request: Request, room_id: str, req: KnockAcceptRequest):
    """§5.2 接受敲门（operator_id 从 JWT 取，请求体里若带需一致）"""
    try:
        jwt_user_id = request.state.user_id
        if not jwt_user_id:
            return api_err(401, "not authenticated")
        if req.operator_id and req.operator_id != jwt_user_id:
            return api_err(403, "operator_id mismatch")
        operator_id = jwt_user_id

        room = user_manager.get_room(room_id)
        if not room:
            return api_err(404, f"Room not found: {room_id}")

        # 权限检查：仅 owner 可接受敲门（admin 也不允许）
        if operator_id != room.owner_id:
            return api_err(403, "权限不足，仅房主可操作")

        knock_info = knock_requests.pop(req.knocker_id, None)

        # 将敲门者加入房间（使用请求中指定的角色）
        role_map = {"owner": UserRole.OWNER, "admin": UserRole.ADMIN, "member": UserRole.MEMBER, "guest": UserRole.GUEST}
        role = role_map.get(req.role, UserRole.MEMBER)
        user = user_manager.join_room(room_id, req.knocker_id, role=role)

        asyncio.create_task(sync.member_joined(
            room_id=room_id,
            user_id=user.user_id,
            role=user.role.value,
            joined_at=user.joined_at or "",
            last_active="",
        ))

        # 通知敲门者
        await manager.send_to_user_with_timestamp(req.knocker_id, {
            "type": "room_knock_accepted",
            "room_id": room_id
        })

        # 通知房间内其他成员
        await manager.broadcast_to_room_with_timestamp(room_id, {
            "type": "member_joined",
            "room_id": room_id,
            "user_id": req.knocker_id
        })

        logger.info(f"[API] Knock accepted: {req.knocker_id} joined room {room_id}")
        return api_ok({})
    except JSONResponse:
        raise
    except Exception as e:
        logger.error(f"[API] knock_accept error: {e}")
        return api_err(500, str(e))

@app.post("/api/v1/room/{room_id}/knock/reject")
async def knock_reject(request: Request, room_id: str, req: KnockRejectRequest):
    """§5.3 拒绝敲门（operator_id 从 JWT 取，请求体里若带需一致）"""
    try:
        jwt_user_id = request.state.user_id
        if not jwt_user_id:
            return api_err(401, "not authenticated")
        if req.operator_id and req.operator_id != jwt_user_id:
            return api_err(403, "operator_id mismatch")
        operator_id = jwt_user_id

        room = user_manager.get_room(room_id)
        if not room:
            return api_err(404, f"Room not found: {room_id}")

        if operator_id != room.owner_id:
            return api_err(403, "权限不足，仅房主可操作")

        knock_requests.pop(req.knocker_id, None)

        await manager.send_to_user_with_timestamp(req.knocker_id, {
            "type": "room_knock_rejected",
            "room_id": room_id,
            "data": {"reason": req.reason}
        })

        logger.info(f"[API] Knock rejected: {req.knocker_id} by {operator_id}")
        return api_ok({})
    except JSONResponse:
        raise
    except Exception as e:
        logger.error(f"[API] knock_reject error: {e}")
        return api_err(500, str(e))


class SetRoleRequest(BaseModel):
    """设置成员角色（owner / admin / member / guest）"""
    role: str = "member"
    operator_id: str = ""


@app.post("/api/v1/room/{room_id}/member/{user_id}/role")
async def set_member_role(request: Request, room_id: str, user_id: str, req: SetRoleRequest):
    """设置成员角色。仅群主可操作。"""
    try:
        jwt_user_id = request.state.user_id
        if not jwt_user_id:
            return api_err(401, "not authenticated")
        if req.operator_id and req.operator_id != jwt_user_id:
            return api_err(403, "operator_id mismatch")
        operator_id = jwt_user_id

        role_map = {"owner": UserRole.OWNER, "admin": UserRole.ADMIN, "member": UserRole.MEMBER, "guest": UserRole.GUEST}
        new_role = role_map.get(req.role)
        if not new_role:
            return api_err(400, f"无效的角色: {req.role}（可选 owner/admin/member/guest）")

        room = user_manager.get_room(room_id)
        if not room:
            return api_err(404, f"Room not found: {room_id}")
        if operator_id != room.owner_id:
            return api_err(403, "仅群主可设置成员角色")

        # 拿旧角色（用于同步事件）
        member_before = user_manager.get_member(room_id, user_id)
        old_role = member_before.role.value if member_before else ""

        ok = user_manager.update_user_role(room_id, user_id, new_role)
        if not ok:
            return api_err(400, "设置失败（不能修改群主角色或用户不存在）")

        asyncio.create_task(sync.member_role_changed(
            room_id=room_id,
            user_id=user_id,
            old_role=old_role,
            new_role=new_role.value,
            operator_id=operator_id,
        ))

        # 同步账号表中的 role（若是当前登录用户则一并写回）
        target_username = None
        for name, rec in list(user_store._users.items()):
            if rec.get("user_id") == user_id or name == user_id:
                target_username = name
                break
        if target_username and new_role == UserRole.OWNER:
            user_store.set_role(target_username, "owner")
        elif target_username and new_role == UserRole.ADMIN:
            user_store.set_role(target_username, "admin")
        elif target_username and new_role == UserRole.MEMBER:
            user_store.set_role(target_username, "member")

        return api_ok({"user_id": user_id, "room_id": room_id, "role": new_role.value})
    except JSONResponse:
        raise
    except Exception as e:
        logger.error(f"[API] set_member_role error: {e}")
        return api_err(500, str(e))


# ==============================================================================
# §6 禁言/禁麦接口
# ==============================================================================

@app.post("/api/v1/room/{room_id}/member/{user_id}/mute")
async def mute_member(request: Request, room_id: str, user_id: str, req: MemberOperatorRequest):
    """§6.1 禁言用户（operator_id 从 JWT 取，请求体里若带需一致）"""
    try:
        jwt_user_id = request.state.user_id
        if not jwt_user_id:
            return api_err(401, "not authenticated")
        if req.operator_id and req.operator_id != jwt_user_id:
            return api_err(403, "operator_id mismatch")
        operator_id = jwt_user_id

        room = user_manager.get_room(room_id)
        if not room:
            return api_err(404, f"Room not found: {room_id}")

        if not user_manager.can_manage_members(room_id, operator_id):
            return api_err(403, "权限不足，仅群主/管理员可操作")

        ok = user_manager.mute_user(room_id, user_id)
        if not ok:
            return api_err(400, "禁言失败，用户不存在或不能禁言群主")

        member = user_manager.get_member(room_id, user_id)

        await manager.broadcast_to_room_with_timestamp(room_id, {
            "type": "member_muted",
            "room_id": room_id,
            "user_id": user_id,
            "operator_id": req.operator_id
        })

        return api_ok({
            "user_id": user_id,
            "status": member.status.value,
            "publish_allowed": member.publish_allowed
        })
    except JSONResponse:
        raise
    except Exception as e:
        logger.error(f"[API] mute_member error: {e}")
        return api_err(500, str(e))

@app.post("/api/v1/room/{room_id}/member/{user_id}/unmute")
async def unmute_member(request: Request, room_id: str, user_id: str, req: MemberOperatorRequest):
    """§6.2 解除禁言（operator_id 从 JWT 取，请求体里若带需一致）"""
    try:
        jwt_user_id = request.state.user_id
        if not jwt_user_id:
            return api_err(401, "not authenticated")
        if req.operator_id and req.operator_id != jwt_user_id:
            return api_err(403, "operator_id mismatch")
        operator_id = jwt_user_id

        room = user_manager.get_room(room_id)
        if not room:
            return api_err(404, f"Room not found: {room_id}")

        if not user_manager.can_manage_members(room_id, operator_id):
            return api_err(403, "权限不足，仅群主/管理员可操作")

        ok = user_manager.unmute_user(room_id, user_id)
        if not ok:
            return api_err(400, "解除禁言失败，用户不存在")

        member = user_manager.get_member(room_id, user_id)

        await manager.broadcast_to_room_with_timestamp(room_id, {
            "type": "member_unmuted",
            "room_id": room_id,
            "user_id": user_id,
            "operator_id": req.operator_id
        })

        return api_ok({
            "user_id": user_id,
            "status": member.status.value,
            "publish_allowed": member.publish_allowed
        })
    except JSONResponse:
        raise
    except Exception as e:
        logger.error(f"[API] unmute_member error: {e}")
        return api_err(500, str(e))

@app.post("/api/v1/room/{room_id}/member/{user_id}/mic/disable")
async def disable_mic(request: Request, room_id: str, user_id: str, req: MemberOperatorRequest):
    """§6.3 禁麦（operator_id 从 JWT 取，请求体里若带需一致）"""
    try:
        jwt_user_id = request.state.user_id
        if not jwt_user_id:
            return api_err(401, "not authenticated")
        if req.operator_id and req.operator_id != jwt_user_id:
            return api_err(403, "operator_id mismatch")
        operator_id = jwt_user_id

        room = user_manager.get_room(room_id)
        if not room:
            return api_err(404, f"Room not found: {room_id}")

        if not user_manager.can_manage_members(room_id, operator_id):
            return api_err(403, "权限不足，仅群主/管理员可操作")

        ok = user_manager.disable_mic(room_id, user_id)
        if not ok:
            return api_err(400, "禁麦失败，用户不存在或不能禁麦群主")

        member = user_manager.get_member(room_id, user_id)

        await manager.broadcast_to_room_with_timestamp(room_id, {
            "type": "member_mic_disabled",
            "room_id": room_id,
            "user_id": user_id,
            "operator_id": req.operator_id
        })

        return api_ok({
            "user_id": user_id,
            "status": member.status.value,
            "publish_allowed": member.publish_allowed
        })
    except JSONResponse:
        raise
    except Exception as e:
        logger.error(f"[API] disable_mic error: {e}")
        return api_err(500, str(e))

@app.post("/api/v1/room/{room_id}/member/{user_id}/mic/enable")
async def enable_mic(request: Request, room_id: str, user_id: str, req: MemberOperatorRequest):
    """§6.4 解除禁麦（operator_id 从 JWT 取，请求体里若带需一致）"""
    try:
        jwt_user_id = request.state.user_id
        if not jwt_user_id:
            return api_err(401, "not authenticated")
        if req.operator_id and req.operator_id != jwt_user_id:
            return api_err(403, "operator_id mismatch")
        operator_id = jwt_user_id

        room = user_manager.get_room(room_id)
        if not room:
            return api_err(404, f"Room not found: {room_id}")

        if not user_manager.can_manage_members(room_id, operator_id):
            return api_err(403, "权限不足，仅群主/管理员可操作")

        ok = user_manager.enable_mic(room_id, user_id)
        if not ok:
            return api_err(400, "解除禁麦失败，用户不存在")

        member = user_manager.get_member(room_id, user_id)

        await manager.broadcast_to_room_with_timestamp(room_id, {
            "type": "member_mic_enabled",
            "room_id": room_id,
            "user_id": user_id,
            "operator_id": req.operator_id
        })

        return api_ok({
            "user_id": user_id,
            "status": member.status.value,
            "publish_allowed": member.publish_allowed
        })
    except JSONResponse:
        raise
    except Exception as e:
        logger.error(f"[API] enable_mic error: {e}")
        return api_err(500, str(e))

@app.post("/api/v1/room/{room_id}/mute-all")
async def mute_all(request: Request, room_id: str, req: MemberOperatorRequest):
    """§6.5 全体禁言（operator_id 从 JWT 取，请求体里若带需一致）"""
    try:
        jwt_user_id = request.state.user_id
        if not jwt_user_id:
            return api_err(401, "not authenticated")
        if req.operator_id and req.operator_id != jwt_user_id:
            return api_err(403, "operator_id mismatch")
        operator_id = jwt_user_id

        room = user_manager.get_room(room_id)
        if not room:
            return api_err(404, f"Room not found: {room_id}")

        if not user_manager.can_manage_members(room_id, operator_id):
            return api_err(403, "权限不足，仅群主/管理员可操作")

        count = user_manager.mute_all(room_id)

        asyncio.create_task(sync.room_mute_changed(room_id=room_id, allow_speak=False, operator_id=jwt_user_id))

        await manager.broadcast_to_room_with_timestamp(room_id, {
            "type": "room_muted_all",
            "room_id": room_id,
            "operator_id": req.operator_id,
            "data": {"muted_count": count}
        })

        return api_ok({
            "room_id": room_id,
            "allow_speak": False,
            "muted_count": count
        })
    except JSONResponse:
        raise
    except Exception as e:
        logger.error(f"[API] mute_all error: {e}")
        return api_err(500, str(e))

@app.post("/api/v1/room/{room_id}/unmute-all")
async def unmute_all(request: Request, room_id: str, req: MemberOperatorRequest):
    """§6.6 解除全体禁言（operator_id 从 JWT 取，请求体里若带需一致）"""
    try:
        jwt_user_id = request.state.user_id
        if not jwt_user_id:
            return api_err(401, "not authenticated")
        if req.operator_id and req.operator_id != jwt_user_id:
            return api_err(403, "operator_id mismatch")
        operator_id = jwt_user_id

        room = user_manager.get_room(room_id)
        if not room:
            return api_err(404, f"Room not found: {room_id}")

        if not user_manager.can_manage_members(room_id, operator_id):
            return api_err(403, "权限不足，仅群主/管理员可操作")

        count = user_manager.unmute_all(room_id)

        asyncio.create_task(sync.room_mute_changed(room_id=room_id, allow_speak=True, operator_id=jwt_user_id))

        await manager.broadcast_to_room_with_timestamp(room_id, {
            "type": "room_unmuted_all",
            "room_id": room_id,
            "operator_id": req.operator_id,
            "data": {"unmuted_count": count}
        })

        return api_ok({
            "room_id": room_id,
            "allow_speak": True,
            "unmuted_count": count
        })
    except JSONResponse:
        raise
    except Exception as e:
        logger.error(f"[API] unmute_all error: {e}")
        return api_err(500, str(e))

@app.get("/api/v1/room/{room_id}/check-publish")
async def check_publish(room_id: str, user_id: str):
    """§6.7 检查发言权限"""
    try:
        can_publish = user_manager.can_publish(room_id, user_id)
        member = user_manager.get_member(room_id, user_id)
        status = member.status.value if member else "normal"
        return api_ok({
            "user_id": user_id,
            "can_publish": can_publish,
            "status": status
        })
    except Exception as e:
        logger.error(f"[API] check_publish error: {e}")
        return api_err(500, str(e))


# ==============================================================================
# 房间邀请接口（实现房间邀请功能_服务端需求.md §3、§4、§5）
# ==============================================================================

class InviteCreateRequest(BaseModel):
    """§3.1 发送邀请请求体"""
    room_id: str = ""
    invitee_id: str = ""
    message: str = ""


class InviteRejectRequest(BaseModel):
    """§3.4 拒绝邀请请求体"""
    reason: str = ""


class InviteBatchRequest(BaseModel):
    """§3.5 批量查询请求体"""
    ids: List[str] = []


# ==============================================================================
# §10 分享链接接口
# ==============================================================================

class ShareCreateRequest(BaseModel):
    """§10.1 生成分享链接请求体"""
    room_id: str
    message: str = ""


@app.post("/api/v1/share")
async def create_share_link(request: Request, req: ShareCreateRequest):
    """§10.1 生成分享链接"""
    try:
        jwt_user_id = request.state.user_id
        if not jwt_user_id:
            return api_err(401, "not authenticated")

        if len(req.message) > 200:
            return api_err(400, "message too long")

        room = user_manager.get_room(req.room_id)
        if not room:
            return api_err(404, "room not found")

        if room.owner_id != jwt_user_id:
            return api_err(403, "not room owner")

        member = user_manager.get_member(req.room_id, jwt_user_id)
        if not member:
            return api_err(400, "sharer not in room")

        link = share_manager.create_share_link(
            room_id=req.room_id,
            room_name=room.name,
            sharer_id=jwt_user_id,
            sharer_name=jwt_user_id,
            message=req.message,
        )

        share_url = f"{SHARE_DOMAIN}/room/{link.share_id}"

        asyncio.create_task(sync.member_joined(
            room_id=req.room_id,
            user_id=jwt_user_id,
            role="owner",
            joined_at="",
            last_active="",
        ))
        await manager.broadcast_to_room_exclude(req.room_id, {
            "type": "room_shared",
            "room_id": req.room_id,
            "data": {
                "share_id": link.share_id,
                "sharer_id": jwt_user_id,
                "sharer_name": jwt_user_id,
                "share_url": share_url,
                "timestamp": int(time.time())
            }
        }, exclude_user_ids={jwt_user_id})

        logger.info(f"[Share] Created share link {link.share_id} for room {req.room_id}")
        return api_ok({
            "share_id": link.share_id,
            "share_url": share_url,
            "room_id": link.room_id,
            "room_name": link.room_name,
            "expires_at": link.expires_at,
        })
    except Exception as e:
        logger.error(f"[API] create_share_link error: {e}")
        return api_err(500, str(e))


@app.get("/api/v1/share/{share_id}")
async def resolve_share_link_public(share_id: str):
    """§10.3 公共解析（无需 JWT）：在微信/QQ等外部应用打开"""
    try:
        link = share_manager.get_share_link(share_id)
        if not link or link.is_expired():
            return JSONResponse(status_code=410, content={
                "code": 410,
                "message": "share link expired",
                "data": None
            })

        room = user_manager.get_room(link.room_id)
        if not room or room.status.value == "closed":
            return JSONResponse(status_code=404, content={
                "code": 404,
                "message": "room not found or closed",
                "data": None
            })

        return api_ok({
            "share_id": link.share_id,
            "room_id": link.room_id,
            "room_name": link.room_name,
            "sharer_name": link.sharer_name,
            "message": link.message,
            "room_status": room.status.value,
            "member_count": len(room.members),
            "expires_at": link.expires_at,
        })
    except Exception as e:
        logger.error(f"[API] resolve_share_link_public error: {e}")
        return api_err(500, str(e))


@app.get("/api/v1/share/{share_id}/resolve")
async def resolve_share_link_authenticated(request: Request, share_id: str):
    """§10.2 普通解析（需 JWT）：在 App 内打开"""
    try:
        jwt_user_id = request.state.user_id
        if not jwt_user_id:
            return api_err(401, "not authenticated")

        link = share_manager.get_share_link(share_id)
        if not link or link.is_expired():
            return JSONResponse(status_code=410, content={
                "code": 410,
                "message": "share link expired",
                "data": None
            })

        room = user_manager.get_room(link.room_id)
        if not room or room.status.value == "closed":
            return JSONResponse(status_code=404, content={
                "code": 404,
                "message": "room not found or closed",
                "data": None
            })

        member = user_manager.get_member(link.room_id, jwt_user_id)
        your_role = "owner" if jwt_user_id == room.owner_id else (member.role.value if member else "none")

        return api_ok({
            "share_id": link.share_id,
            "room_id": link.room_id,
            "room_name": link.room_name,
            "sharer_id": link.sharer_id,
            "sharer_name": link.sharer_name,
            "message": link.message,
            "room_status": room.status.value,
            "member_count": len(room.members),
            "owner_id": room.owner_id,
            "your_role": your_role,
            "expires_at": link.expires_at,
        })
    except Exception as e:
        logger.error(f"[API] resolve_share_link_authenticated error: {e}")
        return api_err(500, str(e))


@app.post("/api/v1/share/{share_id}/join")
async def join_via_share_link(request: Request, share_id: str):
    """§10.4 通过分享链接加入房间"""
    try:
        jwt_user_id = request.state.user_id
        if not jwt_user_id:
            return api_err(401, "not authenticated")

        link = share_manager.get_share_link(share_id)
        if not link or link.is_expired():
            return JSONResponse(status_code=410, content={
                "code": 410,
                "message": "share link expired",
                "data": None
            })

        room = user_manager.get_room(link.room_id)
        if not room or room.status.value == "closed":
            return JSONResponse(status_code=404, content={
                "code": 404,
                "message": "room not found or closed",
                "data": None
            })

        existing = user_manager.get_member(link.room_id, jwt_user_id)
        if existing:
            return api_ok({
                "room_id": link.room_id,
                "room_name": link.room_name,
                "share_id": link.share_id,
            })

        user = user_manager.join_room(link.room_id, jwt_user_id, role=UserRole.MEMBER)

        username_for_account = None
        for name, rec in list(user_store._users.items()):
            if rec.get("user_id") == jwt_user_id or name == jwt_user_id:
                username_for_account = name
                break
        if username_for_account:
            user_store.set_room(username_for_account, link.room_id)

        asyncio.create_task(sync.member_joined(
            room_id=link.room_id,
            user_id=jwt_user_id,
            role="member",
            joined_at=user.joined_at or "",
            last_active="",
        ))
        await manager.broadcast_to_room_with_timestamp(link.room_id, {
            "type": "member_joined",
            "room_id": link.room_id,
            "user_id": jwt_user_id
        })

        logger.info(f"[Share] User {jwt_user_id} joined room {link.room_id} via share {share_id}")
        return api_ok({
            "room_id": link.room_id,
            "room_name": link.room_name,
            "share_id": link.share_id,
        })
    except Exception as e:
        logger.error(f"[API] join_via_share_link error: {e}")
        return api_err(500, str(e))


@app.post("/api/v1/invite")
async def create_invitation(request: Request, req: InviteCreateRequest):
    """§3.1 发送邀请：邀请者从 JWT 取，被邀请者走 invitee_id。"""
    try:
        jwt_user_id = request.state.user_id
        jwt_username = request.state.username
        if not jwt_user_id:
            return api_err(401, "not authenticated")
        if not req.room_id or not req.invitee_id:
            return api_err(400, "room_id and invitee_id are required")
        # invitee_id 必须是本系统分配给被邀请者的 user_id（格式 user_<12hex>，共 17 字符），
        # 不能传业务后端的 userId（64-bit 整数）或 username，否则 WS 推送静默丢失、
        # 后续 accept 会被 403 拒绝，邀请永久 pending 死锁。
        if not _USER_ID_RE.fullmatch(req.invitee_id):
            return api_err(
                400,
                "invitee_id 必须是聊天服务器分配的 user_id（格式 user_<12hex>），"
                "请先让被邀请者通过 /api/v1/auth/login 拿 user_id",
            )
        if req.invitee_id == jwt_user_id:
            return api_err(400, "不能邀请自己")

        room = user_manager.get_room(req.room_id)
        if not room:
            return api_err(404, "room not found")

        # 校验 1：邀请者必须在房间中
        inviter = user_manager.get_member(req.room_id, jwt_user_id)
        if not inviter:
            return api_err(400, "inviter not in room")
        # 校验 1b：邀请者不能是被禁言状态（被禁麦不影响，因为这是异步通知）
        if inviter.status == UserStatus.MUTED:
            return api_err(400, "inviter is muted, cannot send invitation")

        # 校验 2：被邀请者未在房间中
        if user_manager.get_member(req.room_id, req.invitee_id):
            return api_err(400, "invitee already in room")

        # 校验 3：被邀请者没有 pending 的该房间邀请（按被邀请者维度唯一）
        now = int(time.time())
        if invitation_store.find_pending_for_invitee_room(req.invitee_id, req.room_id, now):
            return api_err(400, "duplicate invitation")

        inv_id = _new_invitation_id()
        inv_record = {
            "id": inv_id,
            "room_id": req.room_id,
            "room_name": room.name or "",
            "inviter_id": jwt_user_id,
            "inviter_name": jwt_username or "",
            "invitee_id": req.invitee_id,
            "status": "pending",
            "message": req.message or "",
            "created_at": now,
            "expires_at": now + INVITATION_TTL_SECONDS,
        }
        invitation_store.put(inv_record)

        # §4.1 WS 事件：推送给被邀请者
        await manager.send_to_user_with_timestamp(req.invitee_id, {
            "type": "room_invite",
            "room_id": req.room_id,
            "data": {
                "invitation_id": inv_id,
                "inviter_id": jwt_user_id,
                "inviter_name": jwt_username or "",
                "room_name": room.name or "",
                "message": req.message or "",
                "created_at": now,
            },
        })

        logger.info(f"[Invite] {jwt_user_id} -> {req.invitee_id} (room={req.room_id}, id={inv_id})")
        return api_ok({"id": inv_id})
    except Exception as e:
        logger.error(f"[API] create_invitation error: {e}", exc_info=True)
        return api_err(500, str(e))


@app.get("/api/v1/invites/pending")
async def list_pending_invitations(request: Request):
    """§3.2 获取当前用户所有 pending 邀请。"""
    try:
        jwt_user_id = request.state.user_id
        if not jwt_user_id:
            return api_err(401, "not authenticated")
        now = int(time.time())
        records = invitation_store.list_pending_for_invitee(jwt_user_id, now)
        result = [_invite_view(r) for r in records]
        return api_ok(result)
    except Exception as e:
        logger.error(f"[API] list_pending_invitations error: {e}", exc_info=True)
        return api_err(500, str(e))


@app.post("/api/v1/invite/{invitation_id}/accept")
async def accept_invitation(request: Request, invitation_id: str):
    """§3.3 接受邀请：被邀请者从 JWT 取，邀请状态置 accepted 并加入房间。"""
    try:
        jwt_user_id = request.state.user_id
        jwt_username = request.state.username
        if not jwt_user_id:
            return api_err(401, "not authenticated")

        inv = invitation_store.get(invitation_id)
        if not inv:
            return api_err(404, "invitation not found")
        if inv.get("invitee_id") != jwt_user_id:
            return api_err(403, "not your invitation")
        if inv.get("status") != "pending":
            return api_err(400, f"invitation status is {inv.get('status')}, not pending")
        if int(inv.get("expires_at", 0)) <= int(time.time()):
            # 顺手标 expired（与 §5 worker 等价）
            invitation_store.update_status(invitation_id, "expired")
            return api_err(400, "invitation expired")

        # 校验：房间还存在
        room = user_manager.get_room(inv["room_id"])
        if not room:
            invitation_store.update_status(invitation_id, "expired")
            return api_err(404, "room not found")

        # 校验：被邀请者不在房间中（可能已经通过其它方式加入了）
        if user_manager.get_member(inv["room_id"], jwt_user_id):
            return api_err(400, "invitee already in room")

        # 原子标记 accepted（store 内部加锁）
        if not invitation_store.update_status(invitation_id, "accepted", accepted_at=int(time.time())):
            # 状态已被并发改动
            return api_err(409, "invitation status changed, please retry")

        inviter_id = inv["inviter_id"]
        room_id = inv["room_id"]

        # 复用已有 join_room 逻辑（不会重复发送 WS 通知外的内容，
        # 因为 join_room 内部还会广播 member_joined，这正是我们想要的）
        user = user_manager.join_room(room_id, jwt_user_id, role=UserRole.MEMBER)
        # 回填 room_id 到账号表
        for name, rec in list(user_store._users.items()):
            if rec.get("user_id") == jwt_user_id or name == jwt_user_id:
                user_store.set_room(name, room_id)
                break

        # 同步事件
        asyncio.create_task(sync.member_joined(
            room_id=room_id, user_id=jwt_user_id,
            role=UserRole.MEMBER.value,
            joined_at=user.joined_at or "",
            last_active="",
        ))
        await manager.broadcast_to_room_with_timestamp(room_id, {
            "type": "member_joined",
            "room_id": room_id,
            "user_id": jwt_user_id,
        })

        # §4.2 WS 事件：推送给邀请者
        await manager.send_to_user_with_timestamp(inviter_id, {
            "type": "room_invite_accepted",
            "room_id": room_id,
            "data": {
                "invitation_id": invitation_id,
                "invitee_id": jwt_user_id,
                "invitee_name": jwt_username or "",
            },
        })

        logger.info(f"[Invite] {jwt_user_id} accepted {invitation_id} (room={room_id})")
        return api_ok({"room_id": room_id, "user_id": jwt_user_id})
    except Exception as e:
        logger.error(f"[API] accept_invitation error: {e}", exc_info=True)
        return api_err(500, str(e))


@app.post("/api/v1/invite/{invitation_id}/reject")
async def reject_invitation(request: Request, invitation_id: str, req: InviteRejectRequest):
    """§3.4 拒绝邀请。"""
    try:
        jwt_user_id = request.state.user_id
        if not jwt_user_id:
            return api_err(401, "not authenticated")

        inv = invitation_store.get(invitation_id)
        if not inv:
            return api_err(404, "invitation not found")
        if inv.get("invitee_id") != jwt_user_id:
            return api_err(403, "not your invitation")
        if inv.get("status") != "pending":
            return api_err(400, f"invitation status is {inv.get('status')}, not pending")

        if not invitation_store.update_status(invitation_id, "rejected", rejected_at=int(time.time())):
            return api_err(409, "invitation status changed, please retry")

        inviter_id = inv["inviter_id"]
        room_id = inv["room_id"]
        invitee_name = request.state.username or ""

        # §4.3 WS 事件：推送给邀请者
        await manager.send_to_user_with_timestamp(inviter_id, {
            "type": "room_invite_rejected",
            "room_id": room_id,
            "data": {
                "invitation_id": invitation_id,
                "invitee_id": jwt_user_id,
                "invitee_name": invitee_name,
                "reason": req.reason or "",
            },
        })

        logger.info(f"[Invite] {jwt_user_id} rejected {invitation_id} (room={room_id})")
        return api_ok({})
    except Exception as e:
        logger.error(f"[API] reject_invitation error: {e}", exc_info=True)
        return api_err(500, str(e))


@app.post("/api/v1/invites/batch")
async def batch_invitations(request: Request, req: InviteBatchRequest):
    """§3.5 批量获取邀请信息（含 status）。"""
    try:
        jwt_user_id = request.state.user_id
        if not jwt_user_id:
            return api_err(401, "not authenticated")
        if not req.ids:
            return api_ok([])
        records = invitation_store.get_batch(req.ids)
        result = []
        for inv in records:
            # 仅返回与自己相关的邀请（避免越权查看他人邀请）
            if inv.get("invitee_id") != jwt_user_id and inv.get("inviter_id") != jwt_user_id:
                continue
            result.append(_invite_view(inv, include_status=True))
        return api_ok(result)
    except Exception as e:
        logger.error(f"[API] batch_invitations error: {e}", exc_info=True)
        return api_err(500, str(e))


# 2026-08-13 文档 §3.2.3：邀请码管理接口
# - POST /api/v1/invite/code/generate  房主手动生成邀请码
# - POST /api/v1/invite/code/revoke    房主撤销邀请码
# - GET  /api/v1/invite/code/list      房主查看本房间的邀请码列表

@app.post("/api/v1/invite/code/generate")
async def generate_invite_code(request: Request, room_id: str = "",
                                target_user_id: str = "",
                                expire_seconds: int = 0):
    """§3.2.3 房主生成邀请码。

    Query/Body:
      room_id: 必填（房间路径）
      target_user_id: 限定使用的用户（空 = 通用）
      expire_seconds: 有效期秒数（0 = 默认 600s）
    """
    try:
        jwt_user_id = request.state.user_id
        if not jwt_user_id:
            return api_err(401, "not authenticated")
        if not room_id:
            return api_err(400, "room_id required")
        room = user_manager.get_room(room_id)
        if not room:
            return api_err(404, "room not found")
        if room.owner_id != jwt_user_id:
            return api_err(403, "only owner can generate invite code")
        item = invite_code_store.generate(
            room_id=room_id,
            created_by=jwt_user_id,
            target_user_id=target_user_id,
            expire_seconds=expire_seconds if expire_seconds > 0 else 600,
        )
        return api_ok(item)
    except Exception as e:
        logger.error(f"[API] generate_invite_code error: {e}", exc_info=True)
        return api_err(500, str(e))


@app.post("/api/v1/invite/code/revoke")
async def revoke_invite_code(request: Request, code: str):
    """§3.2.3 房主撤销未用的邀请码。"""
    try:
        jwt_user_id = request.state.user_id
        if not jwt_user_id:
            return api_err(401, "not authenticated")
        ok = invite_code_store.revoke(code, operator_id=jwt_user_id)
        if not ok:
            return api_err(404, "invite code not found or not unused")
        return api_ok({"code": code, "status": "revoked"})
    except Exception as e:
        logger.error(f"[API] revoke_invite_code error: {e}", exc_info=True)
        return api_err(500, str(e))


@app.get("/api/v1/invite/code/list")
async def list_invite_codes(request: Request, room_id: str, status: str = ""):
    """§3.2.3 房主查看本房间的邀请码列表。"""
    try:
        jwt_user_id = request.state.user_id
        if not jwt_user_id:
            return api_err(401, "not authenticated")
        room = user_manager.get_room(room_id)
        if not room:
            return api_err(404, "room not found")
        if room.owner_id != jwt_user_id:
            return api_err(403, "only owner can view invite codes")
        items = invite_code_store.list_for_room(room_id, status=status)
        return api_ok({"items": items, "count": len(items)})
    except Exception as e:
        logger.error(f"[API] list_invite_codes error: {e}", exc_info=True)
        return api_err(500, str(e))


# ==============================================================================
# 通知管理接口（2026-08-13 文档 §6.2）
# ==============================================================================
# - GET    /api/v1/notifications              分页列表（时间倒序）
# - GET    /api/v1/notifications/unread-count 未读数
# - POST   /api/v1/notifications/{id}/read   单条已读
# - POST   /api/v1/notifications/read-all    全部已读
# - DELETE /api/v1/notifications/{id}        删除通知

@app.get("/api/v1/notifications")
async def list_notifications(request: Request, limit: int = 50, before_ts: int = 0):
    """§6.2 通知列表（分页，时间倒序）。

    Query:
      limit:     每页条数（默认 50，最大 200）
      before_ts: 游标分页，返回 created_at < before_ts 的条目（0 = 最新页）
    """
    try:
        jwt_user_id = request.state.user_id
        if not jwt_user_id:
            return api_err(401, "not authenticated")
        if limit < 1 or limit > 200:
            limit = 50
        items = notification_store.list(
            jwt_user_id,
            limit=limit,
            before_ts=before_ts if before_ts > 0 else None,
        )
        next_before_ts = items[-1]["created_at"] if len(items) == limit else 0
        return api_ok({
            "items": items,
            "limit": limit,
            "next_before_ts": next_before_ts,
            "has_more": next_before_ts > 0,
        })
    except Exception as e:
        logger.error(f"[API] list_notifications error: {e}", exc_info=True)
        return api_err(500, str(e))


@app.get("/api/v1/notifications/unread-count")
async def get_unread_count(request: Request):
    """§6.2 未读通知数。"""
    try:
        jwt_user_id = request.state.user_id
        if not jwt_user_id:
            return api_err(401, "not authenticated")
        count = notification_store.unread_count(jwt_user_id)
        return api_ok({"unread_count": count})
    except Exception as e:
        logger.error(f"[API] get_unread_count error: {e}", exc_info=True)
        return api_err(500, str(e))


@app.post("/api/v1/notifications/{notification_id}/read")
async def mark_notification_read(request: Request, notification_id: str):
    """§6.2 单条已读。"""
    try:
        jwt_user_id = request.state.user_id
        if not jwt_user_id:
            return api_err(401, "not authenticated")
        ok = notification_store.mark_read(jwt_user_id, notification_id)
        if not ok:
            return api_err(404, "notification not found")
        return api_ok({"id": notification_id, "is_read": True})
    except Exception as e:
        logger.error(f"[API] mark_notification_read error: {e}", exc_info=True)
        return api_err(500, str(e))


@app.post("/api/v1/notifications/read-all")
async def mark_all_notifications_read(request: Request):
    """§6.2 全部已读。"""
    try:
        jwt_user_id = request.state.user_id
        if not jwt_user_id:
            return api_err(401, "not authenticated")
        count = notification_store.mark_all_read(jwt_user_id)
        return api_ok({"marked_count": count})
    except Exception as e:
        logger.error(f"[API] mark_all_notifications_read error: {e}", exc_info=True)
        return api_err(500, str(e))


@app.delete("/api/v1/notifications/{notification_id}")
async def delete_notification(request: Request, notification_id: str):
    """§6.2 删除通知。"""
    try:
        jwt_user_id = request.state.user_id
        if not jwt_user_id:
            return api_err(401, "not authenticated")
        ok = notification_store.delete(jwt_user_id, notification_id)
        if not ok:
            return api_err(404, "notification not found")
        return api_ok({"id": notification_id, "deleted": True})
    except Exception as e:
        logger.error(f"[API] delete_notification error: {e}", exc_info=True)
        return api_err(500, str(e))


# ==============================================================================
# 消息接口（2026-08-13 文档 §5.2）
# ==============================================================================
# - POST /api/v1/messages/send    发送消息（client_msg_id 幂等 + 房间 seq）
# - GET  /api/v1/messages/history 增量历史（按 seq 游标）

@app.post("/api/v1/messages/send")
async def send_message(request: Request, req: MessageSendRequest):
    """§5.2 发送消息（含 client_msg_id 幂等 + 房间内 seq 单调递增）。

    幂等命中：返回原消息 + _idempotent=true（不分配新 seq）。
    """
    try:
        jwt_user_id = request.state.user_id
        if not jwt_user_id:
            return api_err(401, "not authenticated")
        if not req.client_msg_id:
            return api_err(400, "client_msg_id required")
        # room_id 从 query 或 path 取（这里用 body 形式简洁）
        room_id = request.query_params.get("room_id", "")
        if not room_id:
            return api_err(400, "room_id required (query param)")
        # 鉴权：必须在房间内
        room = user_manager.get_room(room_id)
        if not room or jwt_user_id not in room.members:
            return api_err(403, "not in room")
        item = message_store.send(
            room_id=room_id,
            user_id=jwt_user_id,
            client_msg_id=req.client_msg_id,
            msg_type=req.type,
            content=req.content,
            file_name=req.file_name,
            file_size=req.file_size,
            mime_type=req.mime_type,
            width=req.width,
            height=req.height,
            timestamp=req.timestamp,
        )
        # 异步广播给房间（不通过 WS 端 send_message 重复发送，而是直接 broadcast）
        if not item.get("_idempotent"):
            asyncio.create_task(_broadcast_chat_message(room_id, item))
        return api_ok({k: v for k, v in item.items() if k != "_idempotent"})
    except Exception as e:
        logger.error(f"[API] send_message error: {e}", exc_info=True)
        return api_err(500, str(e))


async def _broadcast_chat_message(room_id: str, item: dict):
    """广播聊天消息给房间内所有在线成员（2026-08-13 §5.2 / 8.1）"""
    msg = {
        "type": "chat_message",
        "room_id": room_id,
        "user_id": item["user_id"],
        "data": {
            "id": item["id"],
            "seq": item["seq"],
            "msg_type": item["type"],
            "content": item["content"],
            "file_name": item.get("file_name", ""),
            "file_size": item.get("file_size", 0),
            "mime_type": item.get("mime_type", ""),
            "width": item.get("width", 0),
            "height": item.get("height", 0),
            "timestamp": item.get("timestamp"),
        },
    }
    await manager.broadcast_to_room_with_timestamp(room_id, msg)


# 2026-08-13 文档 §6.2 + §1.2：通知 helper（持久化 + notice_socket 推送）
# 通过 HTTP 调用 8090 notice_server 的 /internal/push
_NOTICE_SERVER_URL = os.getenv("NOTICE_SERVER_URL", "http://127.0.0.1:8090")


async def _notify_user(user_id: str, type_: str, title: str, content: str,
                       room_id: str = "", related_user_id: str = "",
                       data: Optional[dict] = None) -> dict:
    """持久化通知 + 实时推送（跨房间 -> notice_socket）。"""
    item = notification_store.add(
        user_id=user_id,
        type_=type_,
        title=title,
        content=content,
        room_id=room_id,
        related_user_id=related_user_id,
        data=data or {},
    )

    async def _push():
        try:
            import requests as _req
            await asyncio.to_thread(
                _req.post,
                f"{_NOTICE_SERVER_URL}/internal/push",
                json={"user_id": user_id, "item": item},
                timeout=2,
            )
        except Exception as e:
            logger.debug(f"[Notify] push to notice_server failed: {e}")

    # 不阻塞调用方
    asyncio.create_task(_push())
    return item


@app.get("/api/v1/messages/history")
async def get_message_history(request: Request, room_id: str, after_seq: int = 0, limit: int = 50):
    """§5.2 历史查询（按 seq 游标分页，after_seq=0 = 最新一页）。

    鉴权：必须是房间成员。
    """
    try:
        jwt_user_id = request.state.user_id
        if not jwt_user_id:
            return api_err(401, "not authenticated")
        if not room_id:
            return api_err(400, "room_id required")
        room = user_manager.get_room(room_id)
        if not room or jwt_user_id not in room.members:
            return api_err(403, "not in room")
        if limit < 1 or limit > 200:
            limit = 50
        items = message_store.history(room_id, after_seq=after_seq, limit=limit)
        latest = message_store.latest_seq(room_id)
        return api_ok({
            "items": items,
            "room_id": room_id,
            "latest_seq": latest,
            "count": len(items),
        })
    except Exception as e:
        logger.error(f"[API] get_message_history error: {e}", exc_info=True)
        return api_err(500, str(e))


# ==============================================================================
# §7 说话状态接口
# ==============================================================================

@app.get("/api/v1/room/{room_id}/speaking")
async def get_speaking(room_id: str):
    """§7.1 获取正在说话的用户（基于 SRS 推流判断）"""
    try:
        streams = await _srs_list_streams()
        room_prefix = f"{room_id}_"
        speaking = []
        for s in streams:
            name = s.get("name", "")
            if name.startswith(room_prefix):
                # 从 stream_name 提取 user_id：{room_id}_{user_id}
                user_id = name[len(room_prefix):]
                if user_id:
                    speaking.append(user_id)
        return api_ok({
            "room_id": room_id,
            "speaking_users": speaking
        })
    except Exception as e:
        logger.error(f"[API] get_speaking error: {e}")
        return api_err(500, str(e))


# ==============================================================================
# §7.2 说话状态 WS 事件广播（内部调用）
# ==============================================================================

@app.post("/api/v1/room/{room_id}/speaking/broadcast")
async def broadcast_speaking_event(room_id: str, request: Request):
    """
    广播 user_speaking_start / user_speaking_stop 事件给房间所有用户。

    请求体（JSON）:
      type: "start" 或 "stop"
      user_id: 说话的用户ID
      stream_url: 可选，start 时携带
    """
    try:
        data = await request.json()
        event_type = data.get("type", "")
        user_id = data.get("user_id", "")
        stream_url = data.get("stream_url", "")

        if not user_id:
            return api_err(400, "user_id is required")

        # 取说话者的显示名（user_id 是说话者；这里没鉴权，从 user_store 取）
        speaker_name = resolve_display_name(user_id, user_store=user_store, fallback=user_id)

        if event_type == "start":
            msg = {
                "type": "user_speaking_start",
                "room_id": room_id,
                "user_id": user_id,
                "data": {
                    "stream_url": stream_url,
                    "user_name": speaker_name,
                }
            }
        elif event_type == "stop":
            msg = {
                "type": "user_speaking_stop",
                "room_id": room_id,
                "user_id": user_id,
                "data": {
                    "user_name": speaker_name,
                }
            }
        else:
            return api_err(400, "type must be 'start' or 'stop'")

        await manager.broadcast_to_room_with_timestamp(room_id, msg)
        logger.info(f"[API] Broadcast speaking {event_type}: user={user_id}, room={room_id}")
        return api_ok({})
    except Exception as e:
        logger.error(f"[API] broadcast_speaking_event error: {e}")
        return api_err(500, str(e))


# ==============================================================================
# §9 翻译接口
# ==============================================================================

@app.post("/api/v1/translation/start")
async def start_translation(req: TranslationStartRequest):
    """§9.2 启动翻译"""
    try:
        actual_source_lang = req.source_lang or "auto"
        actual_target_lang = req.target_lang or req.to_lang

        # 杀掉旧翻译
        old_req = translation_manager.get_request_by_source(
            req.room_id, req.source_user, to_lang=actual_target_lang
        )
        if old_req:
            stop_translation_service(old_req.request_id)
            translation_manager.stop_translation_by_request(old_req.request_id)
            await asyncio.sleep(1)

        request_id = translation_manager.start_translation(
            room_id=req.room_id,
            source_user=req.source_user,
            target_user=req.target_user,
            to_lang=actual_target_lang,
            source_lang=actual_source_lang
        )

        start_translation_service(request_id, SRS_URL)

        await manager.broadcast_to_room(req.room_id, {
            "type": "translation_started",
            "room_id": req.room_id,
            "user_id": req.source_user,
            "target_user": req.target_user,
            "data": {"to_lang": actual_target_lang}
        })

        logger.info(f"[API] Started translation: {request_id}")
        return api_ok({"request_id": request_id})
    except Exception as e:
        logger.error(f"[API] start_translation error: {e}")
        return api_err(400, str(e))

@app.post("/api/v1/translation/text")
async def translation_text(request: Request):
    """
    发送翻译文本。

    **关键**：翻译文本只推送给申请翻译的目标用户（target_user），
    不是广播给房间所有人。

    请求体（JSON）:
      room_id: 房间ID
      user_id: 说话用户ID（source_user）
      target_user: 目标用户ID（申请翻译的人）
      original_text: 原始文本
      translated_text: 翻译后文本
      source_lang: 源语言
      target_lang: 目标语言
    """
    try:
        data = await request.json()
        room_id = data.get("room_id", "")
        source_user = data.get("source_user", "")
        target_user = data.get("target_user", "")
        original_text = data.get("original_text", "")
        translated_text = data.get("translated_text", "")
        source_lang = data.get("source_lang", "auto")
        target_lang = data.get("target_lang", "")

        if not room_id or not source_user or not target_user:
            return api_err(400, "room_id, source_user and target_user are required")

        msg = {
            "type": "translation_text",
            "room_id": room_id,
            "user_id": source_user,
            "target_user": target_user,
            "data": {
                "original_text": original_text,
                "translated_text": translated_text,
                "source_lang": source_lang,
                "target_lang": target_lang
            }
        }
        await manager.send_to_user_with_timestamp(target_user, msg)
        logger.info(f"[API] Sent translation_text to {target_user}: user={source_user}, room={room_id}")
        return api_ok({})
    except Exception as e:
        logger.error(f"[API] translation_text error: {e}")
        return api_err(500, str(e))

@app.post("/api/v1/translation/stop")
async def stop_translation(request_id: str = None, room_id: str = None, source_user: str = None, to_lang: str = None):
    """§9.4 停止翻译（所有参数走 query string）"""
    try:
        # 支持 request_id 精确定位，也支持 (room_id, source_user, to_lang) 模糊匹配
        if request_id:
            req = translation_manager.get_request(request_id)
        else:
            req = translation_manager.get_request_by_source(room_id, source_user, to_lang)
            request_id = req.request_id if req else None

        if not request_id:
            return api_err(404, "翻译任务不存在")

        stop_translation_service(request_id)
        translation_manager.stop_translation_by_request(request_id)

        if req:
            await manager.broadcast_to_room(req.room_id, {
                "type": "translation_stopped",
                "room_id": req.room_id,
                "user_id": req.source_user,
                "target_user": req.target_user,
                "data": {}
            })

        logger.info(f"[API] Stopped translation: {request_id}")
        return api_ok({})
    except Exception as e:
        logger.error(f"[API] stop_translation error: {e}")
        return api_err(400, str(e))

@app.post("/api/v1/translation/heartbeat")
async def translation_heartbeat(req: TranslationHeartbeatRequest):
    """§9.3 翻译心跳"""
    try:
        updated = False
        if req.request_id:
            updated = translation_manager.update_requester_heartbeat(
                req.request_id, req.client_id
            )
        else:
            updated = translation_manager.update_requester_heartbeat_by_source(
                req.room_id, req.source_user, req.to_lang, req.client_id
            )
        return api_ok({"updated": updated})
    except Exception as e:
        logger.error(f"[API] translation_heartbeat error: {e}")
        return api_err(500, str(e))

@app.get("/api/v1/translation/active")
async def get_active_translations():
    """§9.5 查询活跃翻译"""
    try:
        translations = translation_manager.get_active_translations()
        return api_ok({"translations": translations})
    except Exception as e:
        logger.error(f"[API] get_active_translations error: {e}")
        return api_err(500, str(e))


@app.post("/api/v1/original-speech")
async def broadcast_original_speech(request: Request):
    """
    广播 original_speech_text 事件给房间所有用户（原文推送）。

    请求体（JSON）:
      room_id: 房间ID
      user_id: 说话用户ID
      original_text: 原始文本
      source_lang: 源语言

    **关键**：无论是否有人申请翻译，只要翻译服务在运行，
    原文都会广播给房间所有在线用户。
    """
    try:
        data = await request.json()
        room_id = data.get("room_id", "")
        user_id = data.get("user_id", "")
        original_text = data.get("original_text", "")
        source_lang = data.get("source_lang", "auto")

        if not room_id or not user_id:
            return api_err(400, "room_id and user_id are required")

        msg = {
            "type": "original_speech_text",
            "room_id": room_id,
            "user_id": user_id,
            "data": {
                "original_text": original_text,
                "source_lang": source_lang
            }
        }
        await manager.broadcast_to_room_with_timestamp(room_id, msg)
        logger.info(f"[API] Broadcast original_speech_text: user={user_id}, room={room_id}")
        return api_ok({})
    except Exception as e:
        logger.error(f"[API] broadcast_original_speech error: {e}")
        return api_err(500, str(e))


# ==============================================================================
# WS 辅助接口（兼容旧客户端）
# ==============================================================================

@app.post("/api/v1/ws/subscribe")
async def ws_subscribe(request: Request):
    """WebSocket 订阅（兼容旧接口，公开）"""
    try:
        data = await request.json()
        room_id = data.get("room_id", "")
        user_id = data.get("user_id", "")
        return api_ok({
            "ws_url": f"ws://{PUBLIC_WS_HOST}:{PUBLIC_WS_PORT}/ws?room={room_id}&user={user_id}"
        })
    except Exception as e:
        logger.error(f"[API] ws_subscribe error: {e}")
        return api_err(400, str(e))

@app.get("/api/v1/ws/status")
async def ws_status():
    """WebSocket 状态"""
    with native_ws_lock:
        active_count = sum(len(conns) for conns in native_ws_connections.values())
    return api_ok({"active_connections": active_count})


# ==============================================================================
# SRS HTTP Hooks 回调端点
# ==============================================================================

@app.post("/api/v1/streams/on_publish")
async def streams_on_publish(request: Request):
    try:
        data = await request.json()
        logger.info(f"[SRS Hook] on_publish: {data}")
        return {"code": 0, "server": SRS_URL}
    except Exception as e:
        logger.error(f"[SRS Hook] on_publish error: {e}")
        return {"code": 1, "msg": str(e)}

@app.post("/api/v1/streams/on_unpublish")
async def streams_on_unpublish(request: Request):
    try:
        data = await request.json()
        logger.info(f"[SRS Hook] on_unpublish: {data}")
        stream = data.get("stream", "")
        client_id = str(data.get("client_id", ""))
        if client_id and stream:
            rid = _extract_room_id_from_stream(stream)
            if rid:
                with room_players_lock:
                    entry = room_players.get(rid)
                    if entry and client_id in entry["clients"]:
                        entry["clients"].discard(client_id)
                        if not entry["clients"]:
                            del room_players[rid]
                        else:
                            entry["count"] = len(entry["clients"])
        return {"code": 0}
    except Exception as e:
        logger.error(f"[SRS Hook] on_unpublish error: {e}")
        return {"code": 1, "msg": str(e)}

@app.post("/api/v1/streams/on_play")
async def streams_on_play(request: Request):
    try:
        data = await request.json()
        logger.info(f"[SRS Hook] on_play: {data}")
        stream = data.get("stream", "")
        client_id = str(data.get("client_id", ""))
        if not client_id:
            client_id = f"unknown_{id(data)}"
        if stream:
            rid = _extract_room_id_from_stream(stream)
            if rid:
                with room_players_lock:
                    entry = room_players.setdefault(rid, {"clients": set(), "count": 0})
                    entry["clients"].add(client_id)
                    entry["count"] = len(entry["clients"])
        return {"code": 0}
    except Exception as e:
        logger.error(f"[SRS Hook] on_play error: {e}")
        return {"code": 1, "msg": str(e)}

@app.post("/api/v1/streams/on_stop")
async def streams_on_stop(request: Request):
    try:
        data = await request.json()
        logger.info(f"[SRS Hook] on_stop: {data}")
        stream = data.get("stream", "")
        client_id = str(data.get("client_id", ""))
        if client_id and stream:
            rid = _extract_room_id_from_stream(stream)
            if rid:
                with room_players_lock:
                    entry = room_players.get(rid)
                    if entry and client_id in entry["clients"]:
                        entry["clients"].discard(client_id)
                        if not entry["clients"]:
                            del room_players[rid]
                        else:
                            entry["count"] = len(entry["clients"])
        return {"code": 0}
    except Exception as e:
        logger.error(f"[SRS Hook] on_stop error: {e}")
        return {"code": 1, "msg": str(e)}

# 原始路径别名
@app.post("/api/v1/hooks/on_publish")
async def on_publish(request: Request):
    try:
        data = await request.json()
        logger.info(f"[SRS Hook] on_publish: {data}")
        return {"code": 0, "server": SRS_URL}
    except Exception as e:
        return {"code": 1, "msg": str(e)}

@app.post("/api/v1/hooks/on_unpublish")
async def on_unpublish(request: Request):
    try:
        data = await request.json()
        logger.info(f"[SRS Hook] on_unpublish: {data}")
        stream_key = data.get("stream_key", "")
        request_id = translation_processes_by_stream.get(stream_key)
        if request_id:
            stop_translation_service(request_id)
            translation_manager.stop_translation_by_request(request_id)
        return {"code": 0}
    except Exception as e:
        return {"code": 1, "msg": str(e)}

@app.post("/api/v1/hooks/on_play")
async def on_play(request: Request):
    try:
        data = await request.json()
        logger.info(f"[SRS Hook] on_play: {data}")
        return {"code": 0}
    except Exception as e:
        return {"code": 1, "msg": str(e)}

@app.post("/api/v1/hooks/on_stop")
async def on_stop(request: Request):
    try:
        data = await request.json()
        logger.info(f"[SRS Hook] on_stop: {data}")
        return {"code": 0}
    except Exception as e:
        return {"code": 1, "msg": str(e)}


# ==============================================================================
# 心跳检查线程
# ==============================================================================

heartbeat_check_thread = None
heartbeat_check_running = False

def heartbeat_check_worker():
    global heartbeat_check_running
    from translation_manager import translation_manager
    logger.info("[HeartbeatCheck] Starting heartbeat check worker")

    while heartbeat_check_running:
        try:
            results = translation_manager.check_and_cleanup()
            for result in results:
                if result["stopped"]:
                    logger.info(f"[HeartbeatCheck] Cleaned up translation: {result['request_id']}")
                    asyncio.run(manager.broadcast_to_room(result["room_id"], {
                        "type": "translation_stopped",
                        "room_id": result["room_id"],
                        "user_id": result["source_user"],
                        "data": {}
                    }))
        except Exception as e:
            logger.error(f"[HeartbeatCheck] Error: {e}", exc_info=True)

        # 2026-08-13 §2.2：扫描 room_socket 心跳（HEARTBEAT_INTERVAL/HOLD_THRESHOLD 来自 .env）
        try:
            asyncio.run(manager.check_heartbeats(
                ping_interval=HEARTBEAT_INTERVAL,
                fail_threshold=HEARTBEAT_FAIL_THRESHOLD,
            ))
        except Exception as e:
            logger.error(f"[HeartbeatCheck] WS heartbeat error: {e}", exc_info=True)

        for _ in range(10):
            if not heartbeat_check_running:
                break
            threading.Event().wait(1)

def start_heartbeat_checker():
    global heartbeat_check_thread, heartbeat_check_running
    if heartbeat_check_thread is not None and heartbeat_check_thread.is_alive():
        return
    heartbeat_check_running = True
    heartbeat_check_thread = threading.Thread(target=heartbeat_check_worker, daemon=True)
    heartbeat_check_thread.start()
    logger.info("[HeartbeatCheck] Heartbeat checker started")

def stop_heartbeat_checker():
    global heartbeat_check_running
    heartbeat_check_running = False
    if heartbeat_check_thread:
        heartbeat_check_thread.join(timeout=5)


# ==============================================================================
# SRS HTTP API 辅助函数
# ==============================================================================

def _extract_room_id_from_stream(stream: str) -> str:
    if not stream:
        return ""
    if stream.startswith("translation_"):
        return ""
    if stream.startswith("room"):
        idx = stream.rfind("_")
        if idx > 0:
            return stream[:idx]
    return ""

async def _srs_list_streams() -> list:
    try:
        import aiohttp
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
            async with session.get(f"{SRS_HTTP_API}/api/v1/streams/") as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                return data.get("streams", []) or []
    except ImportError:
        try:
            import requests
            r = requests.get(f"{SRS_HTTP_API}/api/v1/streams/", timeout=5)
            if r.status_code == 200:
                return (r.json().get("streams", []) or [])
        except:
            pass
        return []
    except Exception as e:
        logger.warning(f"[SRS API] list streams error: {e}")
        return []

async def _srs_kick_stream(stream_url: str, action: str = "publish") -> bool:
    try:
        import aiohttp
        path = stream_url
        if "://" in path:
            path = path.split("://", 1)[1]
        if "/" in path:
            path = "/" + path.split("/", 1)[1]
        path = path.split("?")[0].split(".")[0]
        url = f"{SRS_HTTP_API}/api/v1/streams/{path.lstrip('/')}"
        params = {"action": action}
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
            async with session.delete(url, params=params) as resp:
                return resp.status == 200
    except:
        return False

def get_room_active_streams(room_id: str) -> dict:
    result = {
        "has_publish": False,
        "has_play": False,
        "has_translation": False,
        "publish_streams": [],
        "play_count": 0,
        "play_clients": [],
        "translation_pids": [],
        "translation_request_ids": [],
    }
    room_prefix = f"{room_id}_"

    for sk, rid in translation_processes_by_stream.items():
        if sk.startswith(room_prefix):
            pid = translation_processes_stream_to_pid.get(sk)
            result["has_translation"] = True
            result["translation_request_ids"].append(rid)
            if pid:
                result["translation_pids"].append(pid)

    with room_players_lock:
        entry = room_players.get(room_id)
        if entry and entry["clients"]:
            result["has_play"] = True
            result["play_count"] = entry["count"]
            result["play_clients"] = list(entry["clients"])

    return result

async def check_room_can_delete(room_id: str) -> dict:
    detail = get_room_active_streams(room_id)
    reasons = []

    streams = await _srs_list_streams()
    translation_prefix = "translation_"
    room_prefix = f"{room_id}_"

    for s in streams:
        name = s.get("name", "")
        if name.startswith(room_prefix) or name.startswith(translation_prefix):
            detail["publish_streams"].append(name)
            detail["has_publish"] = True

    if detail["has_publish"]:
        reasons.append("房间内存在推流")
    if detail["has_play"]:
        reasons.append(f"房间内存在拉流（{detail['play_count']} 个播放器）")
    if detail["has_translation"]:
        reasons.append("房间内存在翻译进程")

    return {
        "can_delete": len(reasons) == 0,
        "reasons": reasons,
        "detail": detail,
    }


# ==============================================================================
# 翻译进程管理
# ==============================================================================

translation_processes: Dict[str, str] = {}
translation_processes_by_stream: Dict[str, str] = {}
translation_processes_stream_to_pid: Dict[str, int] = {}

def start_translation_service(request_id: str, srs_url: str):
    import subprocess

    req = translation_manager.get_request(request_id)
    if not req:
        logger.error(f"[Translation] Request not found: {request_id}")
        return None

    stream_name = f"translation_{req.source_user}_{req.to_lang}"

    cmd = [
        sys.executable,
        os.path.join(os.path.dirname(__file__), "audio_translation_service_websocket.py")
    ]

    env = os.environ.copy()
    env['REQUEST_ID'] = request_id
    env['ROOM_ID'] = req.room_id
    env['SOURCE_USER'] = req.source_user
    env['TO_LANG'] = req.to_lang
    env['FROM_LANG'] = req.source_lang
    env['TARGET_USER'] = req.target_user
    env['STREAM_NAME'] = stream_name
    env['SRS_URL'] = srs_url
    env['INPUT_SAVE_ENABLED'] = os.getenv('INPUT_SAVE_ENABLED', 'true')
    env['INPUT_SAVE_DIR'] = os.getenv('INPUT_SAVE_DIR', 'input_recordings')

    try:
        log_file = f"translation_{request_id}.log"
        with open(log_file, "w") as f:
            proc = subprocess.Popen(cmd, env=env, stdout=f, stderr=subprocess.STDOUT)

        translation_processes[proc.pid] = request_id
        source_stream_key = f"{req.room_id}_{req.source_user}"
        translation_processes_by_stream[source_stream_key] = request_id
        translation_processes_stream_to_pid[source_stream_key] = proc.pid
        logger.info(f"[Translation] Started service: {request_id}, PID: {proc.pid}")
        return proc.pid
    except Exception as e:
        logger.error(f"[Translation] Failed to start service: {e}")
        return None

def stop_translation_service(request_id: str):
    stopped = False
    for pid, rid in list(translation_processes.items()):
        if rid == request_id:
            try:
                import signal
                os.kill(pid, signal.SIGTERM)
                logger.info(f"[Translation] Sent SIGTERM to PID {pid} (request_id={request_id})")
                stopped = True
            except ProcessLookupError:
                logger.info(f"[Translation] PID {pid} already exited")
                stopped = True
            except Exception as e:
                logger.warning(f"[Translation] Failed to kill PID {pid}: {e}")
            finally:
                translation_processes.pop(pid, None)
    for sk, rid in list(translation_processes_by_stream.items()):
        if rid == request_id:
            translation_processes_by_stream.pop(sk, None)
            translation_processes_stream_to_pid.pop(sk, None)
    if not stopped:
        logger.warning(f"[Translation] No process found for request_id={request_id}")


# ==============================================================================
# 启动服务器
# ==============================================================================

if __name__ == '__main__':
    import uvicorn
    logger.info(f"Starting FastAPI server on 0.0.0.0:{port}")
    logger.info(f"SRS URL: {SRS_URL}")
    # 业务后端三方验证相关配置
    logger.info(
        f"[Boot] 业务后端配置: base={BUSINESS_BACKEND_URL} "
        f"path={BUSINESS_PROFILE_PATH} full_url={BUSINESS_PROFILE_URL}"
    )
    logger.info(
        f"[Boot] 业务后端配置: app_key_prefix={BUSINESS_APP_KEY[:8]}... app_key_len={len(BUSINESS_APP_KEY)}"
    )
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
