"""
sync_client.py — 与主业务服务器的 WebSocket 同步客户端

协议：
    - 连接：ws://SYNC_SERVER_URL/SYNC_WS_PATH
    - 鉴权：连接建立后发送 auth 消息，携带 HMAC token
    - 双向 JSON 消息
    - 心跳：双方 15s 发一次 ping/pong，30s 无响应则断开重连
    - 断线自动重连（1s → 2s → 4s → ... → 300s，最长 5 分钟）

消息格式（JSON）：
    {
        "type": "room_created",
        "server_id": "server_chat_001",
        "timestamp": 1752576800,
        "room_id": "room_abc",
        "data": {...}
    }

客户端→服务端（我们发送）：
    auth / room_created / room_deleted / member_joined / member_left /
    member_kicked / member_role_changed / room_mute_changed /
    rooms_sync / pong

服务端→客户端（我们接收）：
    auth_ack / ping / command
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import random
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

import websockets
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import (
    ConnectionClosed,
    ConnectionClosedOK,
)

_main_logger = logging.getLogger("__main__")

# =============================================================================
# 配置
# =============================================================================

SYNC_ENABLED    = os.getenv("SYNC_ENABLED", "true").lower() in ("true", "1", "yes")
SYNC_SERVER_URL = os.getenv("SYNC_SERVER_URL", "http://8.138.45.176:9005").rstrip("/")
SYNC_WS_PATH    = os.getenv("SYNC_WS_PATH", "/sync")      # WebSocket 路径
SYNC_APP_ID     = os.getenv("SYNC_APP_ID", "server_chat_001")
SYNC_APP_SECRET = os.getenv("SYNC_APP_SECRET", "")
SYNC_WS_TIMEOUT = float(os.getenv("SYNC_WS_TIMEOUT", "10.0"))   # 连接超时
SYNC_HB_INTERVAL = int(os.getenv("SYNC_HB_INTERVAL", "15"))    # 心跳间隔（秒）
SYNC_HB_TIMEOUT  = int(os.getenv("SYNC_HB_TIMEOUT", "30"))      # 无心跳超时（秒）
SYNC_DEBOUNCE_MS = int(os.getenv("SYNC_DEBOUNCE_MS", "100"))
SYNC_FULL_INTERVAL = int(os.getenv("SYNC_FULL_INTERVAL", "30"))  # 全量同步间隔（秒）
SYNC_MAX_RETRIES = int(os.getenv("SYNC_MAX_RETRIES", "3"))
# 重连最大间隔（秒）
SYNC_RECONNECT_MAX = int(os.getenv("SYNC_RECONNECT_MAX", "300"))


# =============================================================================
# 签名
# =============================================================================

def _sign_payload(secret: str, ts: int, data: dict) -> str:
    """
    HMAC-SHA256(secret, ts + "." + json.dumps(data, sort_keys=True))
    """
    raw = f"{ts}.{json.dumps(data, separators=(',', ':'), sort_keys=True)}"
    return hmac.new(
        secret.encode("utf-8"),
        raw.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _make_ws_url() -> str:
    """根据 HTTP URL 构造 WS URL"""
    base = SYNC_SERVER_URL
    if base.startswith("https://"):
        scheme, host = "wss://", base[8:]
    elif base.startswith("http://"):
        scheme, host = "ws://", base[7:]
    else:
        scheme, host = "ws://", base
    return f"{scheme}{host}{SYNC_WS_PATH}"


# =============================================================================
# SyncPayload
# =============================================================================

@dataclass
class SyncPayload:
    event: str
    server_id: str
    timestamp: int
    room_id: str
    data: Dict[str, Any]

    def to_dict(self) -> dict:
        return {
            "event": self.event,
            "server_id": self.server_id,
            "timestamp": self.timestamp,
            "room_id": self.room_id,
            "data": self.data,
        }


# =============================================================================
# 防抖调度器（同步版，用于发送端）
# =============================================================================

class Debouncer:
    """
    同房间同事件在 SYNC_DEBOUNCE_MS 内合并为一次发送。
    不再发 HTTP，直接投递 (room_id, event, body) 到 _pending，
    由 _flush() 在事件循环中统一发 WS。
    """

    def __init__(self, debounce_ms: int = SYNC_DEBOUNCE_MS):
        self._deb = debounce_ms / 1000.0
        self._pending: Dict[str, Dict] = {}   # key → (body, fire_at)
        self._full_pending: Dict[str, float] = {}  # room_id → fire_at
        self._lock = asyncio.Lock()

    def _key(self, room_id: str, event: str) -> str:
        return f"{room_id}::{event}"

    async def emit(self, room_id: str, event: str, body: dict, loop: asyncio.AbstractEventLoop):
        """
        接收事件：记录到防抖队列，100ms 后 flush。
        同一 key 的事件只保留最新 body。
        """
        key = self._key(room_id, event)
        now = time.time()
        fire_at = now + self._deb

        async with self._lock:
            self._pending[key] = {
                "body": body,
                "fire_at": fire_at,
                "room_id": room_id,
                "event": event,
            }

        # 注册延迟 flush
        loop.call_later(self._deb, lambda: asyncio.create_task(self._flush(key)))

    async def emit_full_sync(self, room_id: str, loop: asyncio.AbstractEventLoop, callback: Callable):
        """同房间 300ms 锁，注册延迟 flush"""
        now = time.time()
        async with self._lock:
            prev = self._full_pending.get(room_id, 0)
            fire_at = max(prev, now + 0.3)
            self._full_pending[room_id] = fire_at
            delay = fire_at - now

        loop.call_later(delay, lambda: asyncio.create_task(self._flush_full(room_id, callback)))

    async def _flush_full(self, room_id: str, callback: Callable):
        async with self._lock:
            self._full_pending.pop(room_id, None)
        try:
            await callback(room_id)
        except Exception as e:
            _main_logger.warning(f"[Debouncer] flush_full error: {e}")

    def set_flush_callback(self, fn: Callable):
        """注入回调：async fn(room_id, event, body)"""
        self._on_flush = fn

    async def _flush(self, key: str) -> None:
        """防抖到期后调用：取 pending[key] 并触发回调。"""
        async with self._lock:
            item = self._pending.pop(key, None)
        if not item:
            return
        if self._on_flush is None:
            return
        try:
            await self._on_flush(item["room_id"], item["event"], item["body"])
        except Exception as e:
            _main_logger.warning(f"[Debouncer] _on_flush error: {e}")

    async def cancel_all(self):
        async with self._lock:
            self._pending.clear()
            self._full_pending.clear()


# =============================================================================
# WebSocket Sync Client
# =============================================================================

class SyncClient:

    def __init__(
        self,
        server_url: str = SYNC_SERVER_URL,
        ws_path: str = SYNC_WS_PATH,
        app_id: str = SYNC_APP_ID,
        app_secret: str = SYNC_APP_SECRET,
        ws_timeout: float = SYNC_WS_TIMEOUT,
        hb_interval: int = SYNC_HB_INTERVAL,
        hb_timeout: int = SYNC_HB_TIMEOUT,
        max_retries: int = SYNC_MAX_RETRIES,
    ):
        self.server_url   = server_url
        self.ws_path      = ws_path
        self.app_id       = app_id
        self.app_secret   = app_secret
        self.ws_timeout   = ws_timeout
        self.hb_interval  = hb_interval
        self.hb_timeout   = hb_timeout
        self.max_retries  = max_retries
        self._enabled     = bool(app_secret) and SYNC_ENABLED

        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._debouncer = Debouncer()
        self._debouncer.set_flush_callback(self._on_flush_event)

        # 重连
        self._reconnect_delay = 1.0   # 秒
        self._should_run = False
        self._connected = asyncio.Event()

        # 心跳
        self._last_recv: float = 0
        self._hb_task: Optional[asyncio.Task] = None

        # 定时全量同步
        self._full_task: Optional[asyncio.Task] = None

        # 统计
        self._stats = {"sent": 0, "failed": 0, "skipped": 0, "reconnects": 0}

        _main_logger.info(
            f"[SyncClient] init: enabled={self._enabled}, "
            f"ws={_make_ws_url()}, app_id={self.app_id}"
        )

    # -------------------------------------------------------------------------
    # 生命周期
    # -------------------------------------------------------------------------

    async def start(self):
        """启动（FastAPI startup 时调用）"""
        if not self._enabled:
            _main_logger.warning("[SyncClient] disabled (no app_secret)")
            return

        self._loop = asyncio.get_running_loop()
        self._should_run = True

        # 立即尝试连接
        asyncio.create_task(self._run())

        # 心跳 + 定时全量同步
        self._hb_task = asyncio.create_task(self._heartbeat_loop())
        self._full_task = asyncio.create_task(self._full_sync_loop())

    async def stop(self):
        """停止（FastAPI shutdown 时调用）"""
        self._should_run = False
        self._connected.clear()
        if self._hb_task:
            self._hb_task.cancel()
        if self._full_task:
            self._full_task.cancel()
        await self._close_ws()
        await self._debouncer.cancel_all()
        _main_logger.info("[SyncClient] stopped")

    # -------------------------------------------------------------------------
    # 核心运行循环
    # -------------------------------------------------------------------------

    async def _run(self):
        """连接 → 认证 → 收发消息 → 断开 → 重连"""
        while self._should_run:
            # 每次重试前退避（而非连接失败后）
            if self._stats["reconnects"] > 0:
                self._reconnect_delay = min(
                    self._reconnect_delay * 2 + random.uniform(0, 1),
                    SYNC_RECONNECT_MAX,
                )
                _main_logger.info(f"[SyncClient] reconnecting in {self._reconnect_delay:.1f}s ...")
                await asyncio.sleep(self._reconnect_delay)

            try:
                self._reconnect_delay = 1.0   # 首次/成功后重置
                await self._connect()
                self._connected.set()
                self._stats["reconnects"] = 0
                await self._recv_loop()
            except ConnectionClosedOK:
                _main_logger.info("[SyncClient] server closed connection gracefully")
            except ConnectionClosed as e:
                _main_logger.warning(f"[SyncClient] connection closed: {e}")
            except Exception as e:
                _main_logger.warning(f"[SyncClient] connection error: {e}")

            self._stats["reconnects"] += 1
            self._connected.clear()
            await self._close_ws()

    async def _connect(self):
        url = _make_ws_url()
        _main_logger.info(f"[SyncClient] connecting to {url}")
        try:
            self._ws = await asyncio.wait_for(
                ws_connect(url, open_timeout=self.ws_timeout, close_timeout=5.0),
                timeout=self.ws_timeout + 2.0,
            )
        except asyncio.TimeoutError:
            raise ConnectionRefusedError(f"connect timeout after {self.ws_timeout}s")
        _main_logger.info("[SyncClient] connected, sending auth")
        await self._send_auth()

    async def _close_ws(self):
        try:
            if self._ws:
                await self._ws.close()
        except Exception:
            pass
        self._ws = None

    # -------------------------------------------------------------------------
    # 认证
    # -------------------------------------------------------------------------

    async def _send_auth(self):
        ts = int(time.time())
        token_data = {"server_id": self.app_id, "ts": ts}
        sig = _sign_payload(self.app_secret, ts, token_data)
        await self._send_json({
            "type": "auth",
            "server_id": self.app_id,
            "timestamp": ts,
            "data": {
                "server_id": self.app_id,
                "ts": ts,
                "signature": sig,
            },
        })

    # -------------------------------------------------------------------------
    # 收发循环
    # -------------------------------------------------------------------------

    async def _recv_loop(self):
        """接收消息直到断开"""
        self._last_recv = time.time()
        while self._should_run and self._ws:
            try:
                msg = await asyncio.wait_for(
                    self._ws.recv(),
                    timeout=self.hb_timeout,
                )
                self._last_recv = time.time()
                await self._handle_msg(msg)
            except asyncio.TimeoutError:
                _main_logger.warning(f"[SyncClient] no message for {self.hb_timeout}s, closing")
                break

    async def _handle_msg(self, raw: str | bytes):
        """处理接收到的消息"""
        try:
            msg = json.loads(raw) if isinstance(raw, str) else raw.decode("utf-8")
            msg = json.loads(msg) if isinstance(msg, str) else msg
        except Exception as e:
            _main_logger.warning(f"[SyncClient] invalid JSON from server: {e}")
            return

        msg_type = msg.get("type", "")

        if msg_type == "ping":
            # 心跳探测，回复 pong
            await self._send_json({"type": "pong", "timestamp": int(time.time())})

        elif msg_type == "pong":
            # 我们发的 pong 收到，不需要处理
            pass

        elif msg_type == "auth_ack":
            if msg.get("data", {}).get("ok"):
                _main_logger.info("[SyncClient] auth succeeded")
            else:
                _main_logger.error(f"[SyncClient] auth failed: {msg}")

        elif msg_type == "command":
            await self._handle_command(msg)

        else:
            _main_logger.debug(f"[SyncClient] unknown msg type: {msg_type}")

    async def _handle_command(self, msg: dict):
        """处理主服务器发来的命令"""
        data = msg.get("data", {})
        cmd = data.get("command", "")
        room_id = data.get("room_id", "")
        target_user = data.get("user_id", "")
        operator = data.get("operator_id", "")

        _main_logger.info(f"[SyncClient] command: {cmd} room={room_id} user={target_user}")

        if cmd == "kick_user":
            await self._cmd_kick_user(room_id, target_user, operator)
        elif cmd == "close_room":
            await self._cmd_close_room(room_id, operator)
        elif cmd == "mute_user":
            await self._cmd_mute_user(room_id, target_user, operator)
        elif cmd == "unmute_user":
            await self._cmd_unmute_user(room_id, target_user, operator)
        else:
            _main_logger.warning(f"[SyncClient] unknown command: {cmd}")

    # -------------------------------------------------------------------------
    # 主服务器命令处理（我们被动响应）
    # -------------------------------------------------------------------------

    async def _cmd_kick_user(self, room_id: str, user_id: str, operator: str):
        """主服务器命令：踢出用户"""
        try:
            from server_fastapi import user_manager, manager as ws_manager
            user_manager.leave_room(room_id, user_id)
            await ws_manager.broadcast_to_room(room_id, {
                "type": "member_kicked",
                "room_id": room_id,
                "user_id": user_id,
                "operator_id": operator,
            })
            _main_logger.info(f"[SyncClient] kicked {user_id} from {room_id} by command")
        except Exception as e:
            _main_logger.error(f"[SyncClient] kick_user error: {e}")

    async def _cmd_close_room(self, room_id: str, operator: str):
        """主服务器命令：关闭房间"""
        try:
            from server_fastapi import user_manager, manager as ws_manager
            # 广播
            await ws_manager.broadcast_to_room(room_id, {
                "type": "room_deleted",
                "room_id": room_id,
                "deleted_by": operator,
                "message": "房间已被管理员关闭",
            })
            user_manager.delete_room(room_id)
            _main_logger.info(f"[SyncClient] closed room {room_id} by command")
        except Exception as e:
            _main_logger.error(f"[SyncClient] close_room error: {e}")

    async def _cmd_mute_user(self, room_id: str, user_id: str, operator: str):
        try:
            from server_fastapi import user_manager, manager as ws_manager
            user_manager.mute_user(room_id, user_id)
            await ws_manager.broadcast_to_room(room_id, {
                "type": "member_muted",
                "room_id": room_id,
                "user_id": user_id,
                "operator_id": operator,
            })
        except Exception as e:
            _main_logger.error(f"[SyncClient] mute_user error: {e}")

    async def _cmd_unmute_user(self, room_id: str, user_id: str, operator: str):
        try:
            from server_fastapi import user_manager, manager as ws_manager
            user_manager.unmute_user(room_id, user_id)
            await ws_manager.broadcast_to_room(room_id, {
                "type": "member_unmuted",
                "room_id": room_id,
                "user_id": user_id,
                "operator_id": operator,
            })
        except Exception as e:
            _main_logger.error(f"[SyncClient] unmute_user error: {e}")

    # -------------------------------------------------------------------------
    # 发送
    # -------------------------------------------------------------------------

    async def _send_json(self, data: dict):
        """发送 JSON（已在事件循环中）"""
        if not self._ws or not self._connected.is_set():
            self._stats["failed"] += 1
            return
        try:
            await self._ws.send(json.dumps(data, separators=(",", ":")))
            self._stats["sent"] += 1
        except Exception as e:
            self._stats["failed"] += 1
            _main_logger.warning(f"[SyncClient] send error: {e}")

    async def _on_flush_event(self, room_id: str, event: str, body: dict):
        """防抖到期后调用，实际发送"""
        payload = SyncPayload(event, self.app_id, int(time.time()), room_id, body)
        await self._send_json(payload.to_dict())
        _main_logger.debug(f"[SyncClient] sent (debounced): {event} {room_id}")

    # -------------------------------------------------------------------------
    # 心跳
    # -------------------------------------------------------------------------

    async def _heartbeat_loop(self):
        while self._should_run:
            await asyncio.sleep(self.hb_interval)
            if not self._connected.is_set():
                continue
            try:
                await self._send_json({
                    "type": "ping",
                    "timestamp": int(time.time()),
                })
            except Exception as e:
                _main_logger.warning(f"[SyncClient] ping error: {e}")

    # -------------------------------------------------------------------------
    # 全量同步
    # -------------------------------------------------------------------------

    async def _full_sync_loop(self):
        while self._should_run:
            await asyncio.sleep(SYNC_FULL_INTERVAL)
            if not self._connected.is_set():
                continue
            try:
                states = await self._collect_all_rooms()
                if states:
                    await self._send_json({
                        "type": "rooms_sync",
                        "server_id": self.app_id,
                        "timestamp": int(time.time()),
                        "room_id": "",
                        "data": {"rooms": states},
                    })
                    _main_logger.info(f"[SyncClient] rooms_sync → {len(states)} rooms")
            except Exception as e:
                _main_logger.error(f"[SyncClient] rooms_sync error: {e}")

    async def _collect_all_rooms(self) -> List[dict]:
        try:
            loop = asyncio.get_running_loop()
            return await loop.run_in_executor(None, self._do_collect)
        except Exception as e:
            _main_logger.error(f"[SyncClient] _collect_all_rooms: {e}")
            return []

    def _do_collect(self) -> List[dict]:
        try:
            import user_manager as _um
            um = _um.user_manager
            result = []
            with um._lock:
                for room_id, room in um._rooms.items():
                    members = [
                        {
                            "user_id": uid,
                            "role": getattr(u.role, "value", str(u.role)),
                            "status": getattr(u.status, "value", "normal"),
                            "publish_allowed": getattr(u, "publish_allowed", True),
                            "joined_at": getattr(u, "joined_at", ""),
                        }
                        for uid, u in room.members.items()
                    ]
                    result.append({
                        "room_id": room_id,
                        "owner_id": room.owner_id,
                        "name": getattr(room, "name", ""),
                        "member_count": len(members),
                        "members": members,
                        "allow_speak": getattr(room, "allow_speak", True),
                    })
            return result
        except Exception as e:
            _main_logger.error(f"[SyncClient] _do_collect: {e}")
            return []

    # -------------------------------------------------------------------------
    # 公共事件接口（server_fastapi.py 调用）
    # -------------------------------------------------------------------------

    async def room_created(self, room_id: str, owner_id: str, name: str = "", max_members: int = 100):
        if not self._enabled:
            return
        body = {"room_id": room_id, "owner_id": owner_id, "name": name, "max_members": max_members}
        await self._debouncer.emit(room_id, "room_created", body, self._loop)
        await self._debouncer.emit_full_sync(room_id, self._loop, self._do_full_sync)

    async def _do_full_sync(self, room_id: str):
        if not self._connected.is_set():
            return
        states = await self._collect_all_rooms()
        if states:
            await self._send_json({
                "type": "rooms_sync",
                "server_id": self.app_id,
                "timestamp": int(time.time()),
                "room_id": "",
                "data": {"rooms": states},
            })

    async def room_deleted(self, room_id: str, deleted_by: str = ""):
        if not self._enabled:
            return
        body = {"room_id": room_id, "deleted_by": deleted_by}
        await self._debouncer.emit(room_id, "room_deleted", body, self._loop)

    async def member_joined(self, room_id: str, user_id: str, role: str = "member",
                            joined_at: str = "", last_active: str = ""):
        if not self._enabled:
            return
        body = {"user_id": user_id, "role": role, "joined_at": joined_at, "last_active": last_active}
        await self._debouncer.emit(room_id, "member_joined", body, self._loop)
        await self._debouncer.emit_full_sync(room_id, self._loop, self._do_full_sync)

    async def member_left(self, room_id: str, user_id: str, reason: str = "left"):
        if not self._enabled:
            return
        body = {"user_id": user_id, "reason": reason}
        await self._debouncer.emit(room_id, "member_left", body, self._loop)
        await self._debouncer.emit_full_sync(room_id, self._loop, self._do_full_sync)

    async def member_kicked(self, room_id: str, user_id: str, operator_id: str):
        if not self._enabled:
            return
        body = {"user_id": user_id, "operator_id": operator_id, "reason": "kicked"}
        await self._debouncer.emit(room_id, "member_kicked", body, self._loop)
        await self._debouncer.emit_full_sync(room_id, self._loop, self._do_full_sync)

    async def member_role_changed(self, room_id: str, user_id: str, old_role: str,
                                   new_role: str, operator_id: str = ""):
        if not self._enabled:
            return
        body = {"user_id": user_id, "old_role": old_role, "new_role": new_role, "operator_id": operator_id}
        await self._debouncer.emit(room_id, "member_role_changed", body, self._loop)
        await self._debouncer.emit_full_sync(room_id, self._loop, self._do_full_sync)

    async def room_mute_changed(self, room_id: str, allow_speak: bool, operator_id: str = ""):
        if not self._enabled:
            return
        body = {"room_id": room_id, "allow_speak": allow_speak, "operator_id": operator_id}
        await self._debouncer.emit(room_id, "room_mute_changed", body, self._loop)
        await self._debouncer.emit_full_sync(room_id, self._loop, self._do_full_sync)

    # -------------------------------------------------------------------------
    # 统计
    # -------------------------------------------------------------------------

    def stats(self) -> dict:
        return dict(self._stats)


# =============================================================================
# 全局单例
# =============================================================================

_client: Optional[SyncClient] = None


def get_client() -> SyncClient:
    global _client
    if _client is None:
        _client = SyncClient()
    return _client


sync = get_client()
