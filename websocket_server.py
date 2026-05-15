#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
统一 WebSocket 服务器
提供纯 WebSocket 协议支持，替代 Socket.IO
支持房间订阅、私人通知、翻译文本推送等功能
"""

import os
import sys
import json
import asyncio
import logging
import threading
import time
import queue
from datetime import datetime
from typing import Dict, Set, Optional, Any
from collections import defaultdict
from dataclasses import dataclass, asdict
from enum import Enum

# 尝试导入 websockets
try:
    import websockets
    WEBSOCKETS_AVAILABLE = True
except ImportError:
    WEBSOCKETS_AVAILABLE = False
    logging.warning("websockets 库未安装，将使用 flask 实现")

from flask import Flask, request, jsonify
from gevent.pywsgi import WSGIServer

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


# ========== 数据结构 ==========

class MessageType(Enum):
    """消息类型枚举"""
    # 订阅相关
    SUBSCRIBE_ROOM = "subscribe_room"
    UNSUBSCRIBE_ROOM = "unsubscribe_room"
    SUBSCRIBE_PRIVATE = "subscribe_private"
    
    # 连接相关
    CONNECT = "connect"
    DISCONNECT = "disconnect"
    PING = "ping"
    PONG = "pong"
    
    # 事件通知
    USER_JOINED = "user_joined"
    USER_LEFT = "user_left"
    KNOCK = "knock"
    KNOCK_ACCEPTED = "knock_accepted"
    KNOCK_REJECTED = "knock_rejected"
    SPEAKING = "speaking"
    MUTED = "muted"
    UNMUTED = "unmuted"
    KICKED = "kicked"
    
    # 翻译相关
    TRANSLATION_TEXT = "translation_text"
    TRANSLATION_STARTED = "translation_started"
    TRANSLATION_STOPPED = "translation_stopped"
    ASR_RESULT = "asr_result"
    
    # 响应
    ACK = "ack"
    ERROR = "error"
    SUBSCRIBED = "subscribed"
    UNSUBSCRIBED = "unsubscribed"
    CONNECTED = "connected"


@dataclass
class WebSocketMessage:
    """WebSocket 消息结构"""
    type: str
    data: Optional[Dict[str, Any]] = None
    id: Optional[str] = None
    timestamp: Optional[float] = None
    
    def to_json(self) -> str:
        return json.dumps({
            "type": self.type,
            "data": self.data or {},
            "id": self.id,
            "timestamp": self.timestamp or time.time()
        })
    
    @classmethod
    def from_json(cls, json_str: str) -> 'WebSocketMessage':
        data = json.loads(json_str)
        return cls(
            type=data.get("type", ""),
            data=data.get("data"),
            id=data.get("id"),
            timestamp=data.get("timestamp")
        )


@dataclass
class ClientInfo:
    """客户端信息"""
    client_id: str
    user_id: Optional[str] = None
    room_id: Optional[str] = None
    subscribed_rooms: Set[str] = None
    subscribed_private: bool = False
    connected_at: float = None
    
    def __post_init__(self):
        if self.subscribed_rooms is None:
            self.subscribed_rooms = set()
        if self.connected_at is None:
            self.connected_at = time.time()


# ========== WebSocket 服务器 ==========

class WebSocketServer:
    """纯 WebSocket 服务器"""
    
    def __init__(self, host: str = "0.0.0.0", port: int = 8085):
        self.host = host
        self.port = port
        self.clients: Dict[str, Any] = {}  # client_id -> websocket
        self.client_info: Dict[str, ClientInfo] = {}  # client_id -> ClientInfo
        self.rooms: Dict[str, Set[str]] = defaultdict(set)  # room_id -> set of client_ids
        self.private_rooms: Dict[str, Set[str]] = defaultdict(set)  # user_id -> set of client_ids
        self.lock = threading.Lock()
        
        # 消息队列，用于异步处理
        self.message_queue: queue.Queue = queue.Queue()
        
        # 回调函数
        self.on_message_callback = None
        self.on_room_event_callback = None
        self.on_translation_callback = None
        
        logger.info(f"WebSocket Server initialized on {host}:{port}")
    
    def set_message_callback(self, callback):
        """设置消息回调"""
        self.on_message_callback = callback
    
    def set_room_event_callback(self, callback):
        """设置房间事件回调"""
        self.on_room_event_callback = callback
    
    def set_translation_callback(self, callback):
        """设置翻译回调"""
        self.on_translation_callback = callback
    
    async def handle_client(self, websocket, path: str = "/"):
        """处理客户端连接"""
        client_id = str(id(websocket))
        
        # 解析查询参数
        query_params = {}
        if '?' in path:
            query_string = path.split('?')[1]
            for param in query_string.split('&'):
                if '=' in param:
                    key, value = param.split('=', 1)
                    query_params[key] = value
        
        user_id = query_params.get('user_id', f'anonymous_{client_id[:8]}')
        
        # 注册客户端
        with self.lock:
            self.clients[client_id] = websocket
            self.client_info[client_id] = ClientInfo(
                client_id=client_id,
                user_id=user_id
            )
        
        logger.info(f"[WS] Client connected: {client_id}, user_id={user_id}")
        
        # 发送连接确认
        await self.send_message(websocket, WebSocketMessage(
            type=MessageType.CONNECTED.value,
            data={"client_id": client_id, "user_id": user_id}
        ))
        
        try:
            async for message in websocket:
                try:
                    await self.process_message(client_id, message)
                except json.JSONDecodeError:
                    logger.warning(f"[WS] Invalid JSON from {client_id}")
                    await self.send_message(websocket, WebSocketMessage(
                        type=MessageType.ERROR.value,
                        data={"message": "Invalid JSON format"}
                    ))
                except Exception as e:
                    logger.error(f"[WS] Error processing message: {e}")
                    
        except websockets.exceptions.ConnectionClosed:
            logger.info(f"[WS] Client disconnected: {client_id}")
        finally:
            await self.disconnect_client(client_id)
    
    async def process_message(self, client_id: str, message_str: str):
        """处理接收到的消息"""
        msg = WebSocketMessage.from_json(message_str)
        msg_type = msg.type
        data = msg.data or {}
        
        logger.debug(f"[WS] {client_id} sent: {msg_type}")
        
        with self.lock:
            client_info = self.client_info.get(client_id)
        
        if msg_type == MessageType.PING.value:
            await self.send_message(self.clients[client_id], WebSocketMessage(type=MessageType.PONG.value))
            
        elif msg_type == MessageType.SUBSCRIBE_ROOM.value:
            room_id = data.get("room_id", "")
            if room_id:
                await self.subscribe_to_room(client_id, room_id)
                await self.send_ack(client_id, msg.id, {"room_id": room_id})
                
        elif msg_type == MessageType.UNSUBSCRIBE_ROOM.value:
            room_id = data.get("room_id", "")
            if room_id:
                await self.unsubscribe_from_room(client_id, room_id)
                await self.send_ack(client_id, msg.id, {"room_id": room_id})
                
        elif msg_type == MessageType.SUBSCRIBE_PRIVATE.value:
            user_id = data.get("user_id", "")
            if user_id:
                await self.subscribe_to_private(client_id, user_id)
                await self.send_ack(client_id, msg.id, {"user_id": user_id})
                
        elif msg_type == MessageType.DISCONNECT.value:
            await self.disconnect_client(client_id)
            
        else:
            # 传递给回调处理
            if self.on_message_callback:
                callback_result = self.on_message_callback(client_id, msg)
                if callback_result:
                    await self.send_ack(client_id, msg.id, callback_result)
    
    async def subscribe_to_room(self, client_id: str, room_id: str):
        """订阅房间"""
        with self.lock:
            if client_id in self.clients:
                self.rooms[room_id].add(client_id)
                self.client_info[client_id].subscribed_rooms.add(room_id)
                self.client_info[client_id].room_id = room_id
        
        logger.info(f"[WS] {client_id} subscribed to room {room_id}")
        
        await self.send_message(self.clients[client_id], WebSocketMessage(
            type=MessageType.SUBSCRIBED.value,
            data={"room_id": room_id, "type": "room"}
        ))
        
        # 触发回调
        if self.on_room_event_callback:
            self.on_room_event_callback("subscribe_room", room_id, client_id)
    
    async def unsubscribe_from_room(self, client_id: str, room_id: str):
        """取消订阅房间"""
        with self.lock:
            if client_id in self.clients:
                self.rooms[room_id].discard(client_id)
                self.client_info[client_id].subscribed_rooms.discard(room_id)
                if self.client_info[client_id].room_id == room_id:
                    self.client_info[client_id].room_id = None
        
        logger.info(f"[WS] {client_id} unsubscribed from room {room_id}")
        
        await self.send_message(self.clients[client_id], WebSocketMessage(
            type=MessageType.UNSUBSCRIBED.value,
            data={"room_id": room_id, "type": "room"}
        ))
    
    async def subscribe_to_private(self, client_id: str, user_id: str):
        """订阅私人通知"""
        with self.lock:
            if client_id in self.clients:
                self.private_rooms[user_id].add(client_id)
                self.client_info[client_id].subscribed_private = True
                self.client_info[client_id].user_id = user_id
        
        logger.info(f"[WS] {client_id} subscribed to private notifications for {user_id}")
        
        await self.send_message(self.clients[client_id], WebSocketMessage(
            type=MessageType.SUBSCRIBED.value,
            data={"user_id": user_id, "type": "private"}
        ))
    
    async def disconnect_client(self, client_id: str):
        """断开客户端连接"""
        with self.lock:
            if client_id not in self.clients:
                return
            
            # 从所有房间移除
            if client_id in self.client_info:
                client_info = self.client_info[client_id]
                for room_id in list(client_info.subscribed_rooms):
                    self.rooms[room_id].discard(client_id)
                for user_id in list(self.private_rooms.keys()):
                    self.private_rooms[user_id].discard(client_id)
            
            # 清理
            del self.clients[client_id]
            if client_id in self.client_info:
                del self.client_info[client_id]
        
        logger.info(f"[WS] Client {client_id} disconnected and cleaned up")
    
    async def send_message(self, websocket, message: WebSocketMessage):
        """发送消息到客户端"""
        try:
            await websocket.send(message.to_json())
        except Exception as e:
            logger.error(f"[WS] Error sending message: {e}")
    
    async def send_ack(self, client_id: str, msg_id: Optional[str], data: Dict = None):
        """发送 ACK 响应"""
        if client_id not in self.clients:
            return
        await self.send_message(self.clients[client_id], WebSocketMessage(
            type=MessageType.ACK.value,
            data=data or {},
            id=msg_id
        ))
    
    async def broadcast_to_room(self, room_id: str, message: WebSocketMessage, exclude: str = None):
        """向房间内所有客户端广播消息"""
        with self.lock:
            client_ids = list(self.rooms.get(room_id, set()))
        
        for client_id in client_ids:
            if client_id != exclude and client_id in self.clients:
                try:
                    await self.send_message(self.clients[client_id], message)
                except Exception as e:
                    logger.error(f"[WS] Error broadcasting to {client_id}: {e}")
    
    async def send_to_user(self, user_id: str, message: WebSocketMessage):
        """向指定用户的所有连接发送消息"""
        with self.lock:
            client_ids = list(self.private_rooms.get(user_id, set()))
        
        for client_id in client_ids:
            if client_id in self.clients:
                try:
                    await self.send_message(self.clients[client_id], message)
                except Exception as e:
                    logger.error(f"[WS] Error sending to user {user_id}: {e}")
    
    async def send_to_client(self, client_id: str, message: WebSocketMessage):
        """向指定客户端发送消息"""
        with self.lock:
            if client_id in self.clients:
                try:
                    await self.send_message(self.clients[client_id], message)
                except Exception as e:
                    logger.error(f"[WS] Error sending to {client_id}: {e}")
    
    async def start_server(self):
        """启动 WebSocket 服务器"""
        if not WEBSOCKETS_AVAILABLE:
            logger.error("websockets 库不可用")
            return False
        
        logger.info(f"Starting WebSocket server on ws://{self.host}:{self.port}")
        
        async with websockets.serve(self.handle_client, self.host, self.port):
            await asyncio.Future()  # 运行直到被终止
    
    def run_in_thread(self):
        """在新线程中运行服务器"""
        def run():
            asyncio.run(self.start_server())
        
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        logger.info("WebSocket server started in background thread")
        return thread


# ========== Flask HTTP 服务器 ==========

class HTTPServer:
    """HTTP API 服务器（与 WebSocket 配合使用）"""
    
    def __init__(self, ws_server: WebSocketServer, host: str = "0.0.0.0", port: int = 8085):
        self.app = Flask(__name__)
        self.app.config['JSON_AS_ASCII'] = False
        self.ws_server = ws_server
        self.host = host
        self.port = port
        self._setup_routes()
    
    def _setup_routes(self):
        """设置 HTTP 路由"""
        
        @self.app.route('/health', methods=['GET'])
        def health():
            return jsonify({
                'status': 'ok',
                'service': 'websocket-server',
                'ws_clients': len(self.ws_server.clients),
                'rooms': {rid: len(clients) for rid, clients in self.ws_server.rooms.items()}
            })
        
        @self.app.route('/api/v1/ws/status', methods=['GET'])
        def ws_status():
            """获取 WebSocket 连接状态"""
            with self.ws_server.lock:
                clients_info = []
                for cid, info in self.ws_server.client_info.items():
                    clients_info.append({
                        'client_id': cid,
                        'user_id': info.user_id,
                        'room_id': info.room_id,
                        'rooms': list(info.subscribed_rooms),
                        'private': info.subscribed_private
                    })
                return jsonify({
                    'total_clients': len(self.ws_server.clients),
                    'total_rooms': len(self.ws_server.rooms),
                    'clients': clients_info
                })
        
        @self.app.route('/api/v1/ws/broadcast', methods=['POST'])
        def ws_broadcast():
            """通过 WebSocket 广播消息"""
            data = request.json or {}
            room_id = data.get('room_id')
            message_type = data.get('type', 'notification')
            message_data = data.get('data', {})
            
            msg = WebSocketMessage(type=message_type, data=message_data)
            
            if room_id:
                # 发送到房间
                asyncio.run(self.ws_server.broadcast_to_room(room_id, msg))
            else:
                # 广播到所有
                for client_id in list(self.ws_server.clients.keys()):
                    asyncio.run(self.ws_server.send_to_client(client_id, msg))
            
            return jsonify({'code': 0, 'message': 'broadcast sent'})
        
        @self.app.route('/api/v1/ws/send', methods=['POST'])
        def ws_send():
            """向指定用户发送消息"""
            data = request.json or {}
            user_id = data.get('user_id')
            message_type = data.get('type', 'notification')
            message_data = data.get('data', {})
            
            if not user_id:
                return jsonify({'code': 400, 'message': 'Missing user_id'}), 400
            
            msg = WebSocketMessage(type=message_type, data=message_data)
            asyncio.run(self.ws_server.send_to_user(user_id, msg))
            
            return jsonify({'code': 0, 'message': 'message sent'})
    
    def run(self, threaded: bool = True):
        """运行 HTTP 服务器"""
        logger.info(f"Starting HTTP server on http://{self.host}:{self.port}")
        if threaded:
            self.app.run(host=self.host, port=self.port, debug=False, threaded=True)
        else:
            http_server = WSGIServer((self.host, self.port), self.app)
            http_server.serve_forever()


# ========== 主程序 ==========

def main():
    """主入口"""
    host = os.getenv('WS_HOST', '0.0.0.0')
    port = int(os.getenv('WS_PORT', 8085))
    
    logger.info(f"=" * 60)
    logger.info(f"统一 WebSocket 服务器")
    logger.info(f"协议: 纯 WebSocket (ws://{host}:{port})")
    logger.info(f"=" * 60)
    
    if not WEBSOCKETS_AVAILABLE:
        logger.error("请安装 websockets 库: pip install websockets")
        sys.exit(1)
    
    # 创建 WebSocket 服务器
    ws_server = WebSocketServer(host=host, port=port)
    
    # 创建 HTTP 服务器
    http_server = HTTPServer(ws_server, host=host, port=port)
    
    # 在后台线程启动 WebSocket 服务器
    ws_server.run_in_thread()
    
    # 主线程运行 HTTP 服务器
    http_server.run()


if __name__ == '__main__':
    main()
