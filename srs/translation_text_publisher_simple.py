#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
翻译文本推送服务 - 简化版本
HTTP API + 原生 WebSocket（非 Socket.IO）
"""

import os
import sys
import json
import logging
import threading
import asyncio
import time
from typing import Dict, Set

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

from flask import Flask, request, jsonify

# ========== 消息类型 ==========

class MessageType:
    CONNECTED = "connected"
    SUBSCRIBE = "subscribe"          # 客户端订阅（消息类型）
    SUBSCRIBED = "subscribed"        # 服务端订阅确认（消息类型）
    UNSUBSCRIBED = "unsubscribed"
    UNSUBSCRIBE = "unsubscribe"
    TRANSLATION_TEXT = "translation_text"
    ORIGINAL_SPEECH_TEXT = "original_speech_text"
    CACHED_TEXTS = "cached_texts"
    ERROR = "error"
    ACK = "ack"
    PING = "ping"
    PONG = "pong"
    MESSAGE = "message"


# ========== 原生 WebSocket 服务器 ==========

class NativeWebSocketServer:
    """原生 WebSocket 服务器（使用 Python websockets 库）"""

    def __init__(self, host: str = "0.0.0.0", port: int = 8086):
        self.host = host
        self.port = port
        self.clients: Dict[str, any] = {}
        self.user_connections: Dict[str, Set[str]] = {}
        self.room_connections: Dict[str, Set[str]] = {}
        self.client_info: Dict[str, dict] = {}
        self.lock = threading.Lock()
        self._loop = None
        self._executor = None

    async def handle_client(self, websocket, path: str = "/"):
        client_id = str(id(websocket))

        query_params = {}
        if '?' in path:
            for param in path.split('?')[1].split('&'):
                if '=' in param:
                    key, value = param.split('=', 1)
                    query_params[key] = value

        user_id = query_params.get('user_id', f'anon_{client_id[:8]}')
        room_id = query_params.get('room_id', '')

        with self.lock:
            self.clients[client_id] = websocket
            self.client_info[client_id] = {
                'user_id': user_id,
                'room_id': room_id,
                'connected_at': time.time()
            }
            if user_id not in self.user_connections:
                self.user_connections[user_id] = set()
            self.user_connections[user_id].add(client_id)
            if room_id:
                if room_id not in self.room_connections:
                    self.room_connections[room_id] = set()
                self.room_connections[room_id].add(client_id)

        logger.info(f"[WS] 客户端连接: {client_id}, user_id={user_id}, room_id={room_id}")

        await self._send(websocket, {
            'type': MessageType.CONNECTED,
            'data': {
                'client_id': client_id,
                'user_id': user_id,
                'message': 'Connected to translation text service'
            }
        })

        try:
            async for raw in websocket:
                try:
                    msg = json.loads(raw)
                    await self._process_message(client_id, msg)
                except json.JSONDecodeError:
                    await self._send(websocket, {
                        'type': MessageType.ERROR,
                        'data': {'message': 'Invalid JSON'}
                    })
        except Exception as e:
            logger.info(f"[WS] 客户端断开: {client_id}, reason={e}")

        with self.lock:
            info = self.client_info.pop(client_id, {})
            uid = info.get('user_id', '')
            rid = info.get('room_id', '')
            self.clients.pop(client_id, None)
            if uid and uid in self.user_connections:
                self.user_connections[uid].discard(client_id)
                if not self.user_connections[uid]:
                    del self.user_connections[uid]
            if rid and rid in self.room_connections:
                self.room_connections[rid].discard(client_id)
                if not self.room_connections[rid]:
                    del self.room_connections[rid]
        logger.info(f"[WS] 客户端清理: {client_id}")

    async def _process_message(self, client_id: str, msg: dict):
        msg_type = msg.get('type', '')
        msg_data = msg.get('data', {})

        if msg_type == MessageType.SUBSCRIBE:
            await self._handle_subscribe(client_id, msg_data)
        elif msg_type == MessageType.UNSUBSCRIBED:
            await self._handle_unsubscribe(client_id, msg_data)
        elif msg_type == MessageType.PING:
            await self._send(self.clients[client_id], {'type': MessageType.PONG})
        elif msg_type == MessageType.MESSAGE:
            await self._send(self.clients[client_id], {
                'type': MessageType.ACK,
                'data': {'message': 'Message received'}
            })
        else:
            logger.debug(f"[WS] 未知消息类型: {msg_type}")

    async def _handle_subscribe(self, client_id: str, data: dict):
        user_id = data.get('user_id', '')
        room_id = data.get('room_id', '')
        with self.lock:
            if user_id:
                if user_id not in self.user_connections:
                    self.user_connections[user_id] = set()
                self.user_connections[user_id].add(client_id)
                self.client_info[client_id]['user_id'] = user_id
            if room_id:
                if room_id not in self.room_connections:
                    self.room_connections[room_id] = set()
                self.room_connections[room_id].add(client_id)
                self.client_info[client_id]['room_id'] = room_id
        logger.info(f"[WS] 订阅: client={client_id}, room={room_id}, user={user_id}")
        await self._send(self.clients[client_id], {
            'type': MessageType.SUBSCRIBED,
            'data': {'room_id': room_id, 'user_id': user_id}
        })

    async def _handle_unsubscribe(self, client_id: str, data: dict):
        room_id = data.get('room_id', '')
        with self.lock:
            if room_id and room_id in self.room_connections:
                self.room_connections[room_id].discard(client_id)
                if not self.room_connections[room_id]:
                    del self.room_connections[room_id]
        await self._send(self.clients[client_id], {
            'type': MessageType.UNSUBSCRIBED,
            'data': {'room_id': room_id}
        })

    async def _send(self, websocket, message: dict):
        try:
            await websocket.send(json.dumps(message))
        except Exception as e:
            logger.error(f"[WS] 发送失败: {e}")

    async def start_server(self):
        import websockets
        logger.info(f"Starting native WebSocket server on ws://{self.host}:{self.port}")
        async with websockets.serve(self.handle_client, self.host, self.port, reuse_address=True):
            await asyncio.Future()

    def run_in_thread(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        def run():
            try:
                loop.run_until_complete(self.start_server())
            except Exception as e:
                logger.error(f"[WS] Server error: {e}")
                loop.close()
        t = threading.Thread(target=run, daemon=True)
        t.start()
        return t

    def _async_push(self, cid: str, msg: dict):
        """在 WebSocket 线程中执行发送"""
        if self._loop is None or cid not in self.clients:
            return
        asyncio.run_coroutine_threadsafe(
            self._send(self.clients[cid], msg),
            self._loop
        )

    def push_translation_text(self, user_id: str, text_data: dict):
        msg = {'type': MessageType.TRANSLATION_TEXT, 'data': text_data}
        with self.lock:
            client_ids = list(self.user_connections.get(user_id, set()))
        for cid in client_ids:
            self._async_push(cid, msg)
        logger.info(f"[WS] 推送翻译文本给 {len(client_ids)} 个连接: {text_data.get('translated_text', '')}")

    def broadcast_to_room(self, room_id: str, event_type: str, data: dict):
        msg = {'type': event_type, 'data': data}
        with self.lock:
            client_ids = list(self.room_connections.get(room_id, set()))
        for cid in client_ids:
            self._async_push(cid, msg)
        logger.info(f"[WS] 广播 {event_type} 到房间 {room_id}, {len(client_ids)} 客户端")

    def push_original_speech_text_to_room(self, text_data: dict):
        room_id = text_data.get('room_id', '')
        if room_id:
            self.broadcast_to_room(room_id, MessageType.ORIGINAL_SPEECH_TEXT, text_data)


# ========== Flask HTTP 服务器 ==========

class HTTPServer:
    def __init__(self, ws_server, host: str = "0.0.0.0", port: int = 8087):
        self.app = Flask(__name__)
        self.app.config['JSON_AS_ASCII'] = False
        self.ws_server = ws_server
        self.host = host
        self.port = port
        self._setup_routes()

    def _setup_routes(self):
        @self.app.route('/health', methods=['GET'])
        def health():
            return jsonify({
                'status': 'ok',
                'service': 'translation-text-server',
                'ws_protocol': 'native-websocket',
                'ws_clients': len(self.ws_server.clients),
                'ws_users': len(self.ws_server.user_connections),
                'ws_rooms': len(self.ws_server.room_connections)
            })

        @self.app.route('/api/v1/translation/text/push', methods=['POST'])
        def push_text():
            data = request.json or {}
            target_user = data.get('target_user', '')
            if not target_user:
                return jsonify({'code': 400, 'message': 'Missing target_user'}), 400
            self.ws_server.push_translation_text(target_user, data)
            return jsonify({'code': 0, 'message': 'success'})

        @self.app.route('/api/v1/original/speech/text/push', methods=['POST'])
        def push_original_speech_text():
            data = request.json or {}
            room_id = data.get('room_id', '')
            if not room_id:
                return jsonify({'code': 400, 'message': 'Missing room_id'}), 400
            self.ws_server.push_original_speech_text_to_room(data)
            return jsonify({'code': 0, 'message': 'success'})

        @self.app.route('/api/v1/translation/text/connections', methods=['GET'])
        def get_connections():
            with self.ws_server.lock:
                return jsonify({
                    'total_users': len(self.ws_server.user_connections),
                    'users': {uid: len(sids) for uid, sids in self.ws_server.user_connections.items()}
                })

    def run(self):
        import socketserver
        socketserver.TCPServer.allow_reuse_address = True
        logger.info(f"Starting HTTP server on http://{self.host}:{self.port}")
        self.app.run(host=self.host, port=self.port, debug=False, threaded=True)


def main():
    host = os.getenv('TEXT_SERVER_HOST', '0.0.0.0')
    ws_port = int(os.getenv('TEXT_SERVER_PORT', 8086))
    http_port = int(os.getenv('TEXT_SERVER_HTTP_PORT', 8087))

    logger.info(f"{'=' * 60}")
    logger.info(f"翻译文本推送服务（原生 WebSocket）")
    logger.info(f"HTTP API:  http://{host}:{http_port}")
    logger.info(f"WebSocket: ws://{host}:{ws_port}")
    logger.info(f"{'=' * 60}")

    ws_server = NativeWebSocketServer(host=host, port=ws_port)
    http_server = HTTPServer(ws_server, host=host, port=http_port)

    # WebSocket 在后台线程运行
    ws_server.run_in_thread()
    time.sleep(1)

    # HTTP 在主线程运行
    http_server.run()


if __name__ == '__main__':
    main()
