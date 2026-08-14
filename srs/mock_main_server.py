"""
mock_main_server.py — 模拟主业务服务器 (Mock Server)

用于在没有真实主服务器的情况下测试 WebSocket 同步客户端。

功能:
- WebSocket 端点 /sync
- HMAC-SHA256 认证验证
- 接收并打印所有业务消息
- 定期发送 ping 心跳
- 发送测试命令 (kick_user, close_room, mute_user, unmute_user)

用法:
    python mock_main_server.py [--host HOST] [--port PORT] [--secret SECRET]
"""

import argparse
import asyncio
import hashlib
import hmac
import json
import logging
import random
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Set

import websockets
from websockets.asyncio.server import serve, ServerConnection

# =============================================================================
# 配置
# =============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("MockServer")

DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 9005
DEFAULT_SECRET = "changeme_change_this_secret_before_production"
DEFAULT_SERVER_ID = "server_chat_001"
ALLOWED_SERVER_IDS = {"server_chat_001"}
TIMESTAMP_TOLERANCE = 30
HB_INTERVAL = 15  # ping 间隔（秒）


# =============================================================================
# 签名验证
# =============================================================================

def verify_signature(secret: str, ts: int, data: dict, signature: str) -> bool:
    """验证 HMAC-SHA256 签名"""
    # 时间戳容差检查
    if abs(time.time() - ts) > TIMESTAMP_TOLERANCE:
        return False

    raw = f"{ts}.{json.dumps(data, separators=(',', ':'), sort_keys=True)}"
    expected = hmac.new(
        secret.encode("utf-8"),
        raw.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(expected, signature)


def sign_payload(secret: str, ts: int, data: dict) -> str:
    """生成签名（用于测试）"""
    raw = f"{ts}.{json.dumps(data, separators=(',', ':'), sort_keys=True)}"
    return hmac.new(
        secret.encode("utf-8"),
        raw.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


# =============================================================================
# 连接管理器
# =============================================================================

@dataclass
class ClientConnection:
    ws: ServerConnection
    server_id: str
    authenticated: bool
    connected_at: float


class ConnectionManager:
    def __init__(self):
        self.clients: List[ClientConnection] = []

    async def add(self, ws: ServerConnection, server_id: str = "") -> ClientConnection:
        client = ClientConnection(ws, server_id, False, time.time())
        self.clients.append(client)
        logger.info(f"[WS] Client connected, total={len(self.clients)}")
        return client

    def remove(self, client: ClientConnection):
        if client in self.clients:
            self.clients.remove(client)
        logger.info(f"[WS] Client disconnected, total={len(self.clients)}")

    async def broadcast(self, msg: dict):
        """广播消息到所有已认证客户端"""
        for client in list(self.clients):
            if client.authenticated:
                try:
                    await client.ws.send(json.dumps(msg, separators=(",", ":")))
                except Exception:
                    self.clients.discard(client)


manager = ConnectionManager()


# =============================================================================
# 业务消息处理
# =============================================================================

def handle_message(msg_type: str, data: Dict) -> str:
    """处理业务消息，返回描述"""
    handlers = {
        "room_created": lambda d: f"创建房间 {d.get('room_id')} (所有者: {d.get('owner_id')})",
        "room_deleted": lambda d: f"删除房间 {d.get('room_id')} (操作者: {d.get('deleted_by')})",
        "member_joined": lambda d: f"成员加入 {d.get('room_id')}: {d.get('user_id')} (角色: {d.get('role')})",
        "member_left": lambda d: f"成员离开 {d.get('room_id')}: {d.get('user_id')} (原因: {d.get('reason')})",
        "member_kicked": lambda d: f"成员被踢 {d.get('room_id')}: {d.get('user_id')} (操作者: {d.get('operator_id')})",
        "member_role_changed": lambda d: f"角色变更 {d.get('room_id')}: {d.get('user_id')} {d.get('old_role')}→{d.get('new_role')}",
        "room_mute_changed": lambda d: f"禁言状态变更 {d.get('room_id')}: allow_speak={d.get('allow_speak')}",
        "rooms_sync": lambda d: f"全量同步 {len(d.get('rooms', []))} 个房间",
        "pong": lambda d: "心跳响应",
    }

    handler = handlers.get(msg_type)
    if handler:
        return handler(data)
    return f"未知消息类型: {msg_type}"


# =============================================================================
# WebSocket 处理
# =============================================================================

async def handle_client(ws: ServerConnection, secret: str):
    """处理 WebSocket 客户端连接"""
    client = await manager.add(ws)
    last_recv = time.time()

    try:
        while True:
            # 带超时接收
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=35)
            except asyncio.TimeoutError:
                logger.warning("[WS] Client timeout, closing")
                break

            last_recv = time.time()

            # 解析消息
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning(f"[WS] Invalid JSON: {raw[:100]}")
                continue

            msg_type = msg.get("type", "")

            # ── auth ──
            if msg_type == "auth":
                sid = msg.get("server_id", "")
                ts = msg.get("timestamp", 0)
                data_body = msg.get("data", {})
                sig = data_body.get("signature", "")

                logger.info(f"[WS] Auth request: server_id={sid}, ts={ts}")

                # 检查 server_id
                if sid not in ALLOWED_SERVER_IDS:
                    await ws.send(json.dumps({
                        "type": "auth_ack",
                        "data": {"ok": False, "reason": "unknown server"}
                    }))
                    logger.warning(f"[WS] Auth failed: unknown server {sid}")
                    break

                # 验签：需要移除 signature 字段后再计算
                data_for_verify = {k: v for k, v in data_body.items() if k != "signature"}
                if verify_signature(secret, ts, data_for_verify, sig):
                    client.authenticated = True
                    client.server_id = sid
                    await ws.send(json.dumps({"type": "auth_ack", "data": {"ok": True}}))
                    logger.info(f"[WS] Auth succeeded: {sid}")
                else:
                    await ws.send(json.dumps({
                        "type": "auth_ack",
                        "data": {"ok": False, "reason": "signature mismatch"}
                    }))
                    logger.warning(f"[WS] Auth failed: signature mismatch for {sid}")
                    break

                continue

            # ── 未认证 ──
            if not client.authenticated:
                await ws.send(json.dumps({
                    "type": "error",
                    "data": {"reason": "not authenticated"}
                }))
                continue

            # ── 业务消息 ──
            room_id = msg.get("room_id", "")
            data = msg.get("data", {})
            desc = handle_message(msg_type, data)
            logger.info(f"[WS] {msg_type} room={room_id}: {desc}")

            # pong 心跳响应
            if msg_type == "pong":
                pass

    except websockets.exceptions.ConnectionClosed:
        logger.info("[WS] Client disconnected")
    except Exception as e:
        logger.error(f"[WS] Error: {e}")
    finally:
        manager.remove(client)


# =============================================================================
# 心跳任务
# =============================================================================

async def heartbeat_loop(ws_server, secret: str):
    """定期向所有已认证客户端发送 ping"""
    while True:
        await asyncio.sleep(HB_INTERVAL)

        for client in list(manager.clients):
            if not client.authenticated:
                continue

            try:
                ping_msg = {
                    "type": "ping",
                    "timestamp": int(time.time()),
                }
                await client.ws.send(json.dumps(ping_msg, separators=(",", ":")))
                logger.debug(f"[HB] Sent ping to {client.server_id}")
            except Exception as e:
                logger.warning(f"[HB] Failed to send ping to {client.server_id}: {e}")
                manager.remove(client)


async def command_sender(ws_server, secret: str):
    """定期向客户端发送测试命令"""
    await asyncio.sleep(10)  # 等待客户端连接

    commands = [
        {
            "type": "command",
            "server_id": "mock_server",
            "timestamp": 0,
            "room_id": "test_room",
            "data": {
                "command": "kick_user",
                "user_id": "test_user",
                "operator_id": "admin_mock",
                "reason": "测试踢人",
            },
        },
        {
            "type": "command",
            "server_id": "mock_server",
            "timestamp": 0,
            "room_id": "test_room",
            "data": {
                "command": "mute_user",
                "user_id": "test_user",
                "operator_id": "admin_mock",
            },
        },
    ]

    for i, cmd_template in enumerate(commands):
        await asyncio.sleep(30)  # 30 秒后发送

        for client in list(manager.clients):
            if not client.authenticated:
                continue

            try:
                cmd = cmd_template.copy()
                cmd["timestamp"] = int(time.time())
                await client.ws.send(json.dumps(cmd, separators=(",", ":")))
                logger.info(f"[CMD] Sent {cmd['data']['command']} to {client.server_id}")
            except Exception as e:
                logger.warning(f"[CMD] Failed to send command: {e}")


# =============================================================================
# 主函数
# =============================================================================

async def main(host: str, port: int, secret: str):
    """启动 Mock 服务器"""

    # 创建 partial 函数来传递 secret 参数
    async def ws_handler(ws: ServerConnection):
        await handle_client(ws, secret)

    logger.info("=" * 60)
    logger.info("Mock 主业务服务器")
    logger.info("=" * 60)
    logger.info(f"WebSocket 端点: ws://{host}:{port}/sync")
    logger.info(f"共享密钥: {secret[:8]}...")
    logger.info(f"心跳间隔: {HB_INTERVAL} 秒")
    logger.info("=" * 60)

    async with serve(ws_handler, host, port) as ws_server:
        # 启动心跳任务
        hb_task = asyncio.create_task(heartbeat_loop(ws_server, secret))
        # 启动命令发送任务（可选）
        cmd_task = asyncio.create_task(command_sender(ws_server, secret))

        logger.info("服务器已启动，按 Ctrl+C 停止")

        try:
            await asyncio.Future()  # 永久运行
        except KeyboardInterrupt:
            logger.info("正在停止服务器...")
            hb_task.cancel()
            cmd_task.cancel()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Mock 主业务服务器")
    parser.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help=f"监听地址 (默认: {DEFAULT_HOST})",
    )
    parser.add_argument(
        "--port",
        "-p",
        type=int,
        default=DEFAULT_PORT,
        help=f"监听端口 (默认: {DEFAULT_PORT})",
    )
    parser.add_argument(
        "--secret",
        default=DEFAULT_SECRET,
        help=f"共享密钥 (默认: {DEFAULT_SECRET})",
    )

    args = parser.parse_args()

    asyncio.run(main(args.host, args.port, args.secret))
