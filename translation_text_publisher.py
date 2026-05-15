#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
翻译文本推送服务（纯 WebSocket 版本）
通过 WebSocket 实时推送翻译文本给客户端
支持 SQLite 数据库持久化存储
"""

import os
import sys
import json
import logging
import queue
import threading
import time
import asyncio
from datetime import datetime
from typing import Dict, Set, Optional

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# 尝试导入 websockets
try:
    import websockets
    WEBSOCKETS_AVAILABLE = True
except ImportError:
    WEBSOCKETS_AVAILABLE = False
    logger.warning("websockets 库未安装")

# 尝试导入数据库
try:
    from sqlalchemy import create_engine, Column, Integer, String, Float, Text, DateTime
    from sqlalchemy.ext.declarative import declarative_base
    from sqlalchemy.orm import sessionmaker
    SQLALCHEMY_AVAILABLE = True
except ImportError:
    SQLALCHEMY_AVAILABLE = False
    logger.warning("sqlalchemy 未安装，数据库功能将不可用")

# Flask（仅用于 HTTP API）
from flask import Flask, request, jsonify


# ========== 数据结构 ==========

class MessageType:
    """消息类型常量"""
    CONNECTED = "connected"
    SUBSCRIBED = "subscribed"
    UNSUBSCRIBED = "unsubscribed"
    TRANSLATION_TEXT = "translation_text"
    ORIGINAL_SPEECH_TEXT = "original_speech_text"
    CACHED_TEXTS = "cached_texts"
    ERROR = "error"
    ACK = "ack"
    PING = "ping"
    PONG = "pong"


# ========== 数据库配置 ==========

if SQLALCHEMY_AVAILABLE:
    Base = declarative_base()
    
    class TranslationRecord(Base):
        """翻译记录表"""
        __tablename__ = 'translation_records'
        
        id = Column(Integer, primary_key=True, autoincrement=True)
        request_id = Column(String(64), index=True)
        room_id = Column(String(64), index=True)
        source_user = Column(String(64), index=True)
        target_user = Column(String(64), index=True)
        original_text = Column(Text)
        translated_text = Column(Text)
        source_lang = Column(String(10))
        target_lang = Column(String(10))
        timestamp = Column(Float, index=True)
        created_at = Column(DateTime, default=datetime.utcnow)

# ========== WebSocket 服务器 ==========

class TranslationWebSocketServer:
    """翻译文本 WebSocket 服务器"""
    
    def __init__(self, host: str = "0.0.0.0", port: int = 8086):
        self.host = host
        self.port = port
        
        # 连接管理
        self.clients: Dict[str, any] = {}  # client_id -> websocket
        self.user_connections: Dict[str, Set[str]] = {}  # user_id -> set of client_ids
        self.client_info: Dict[str, dict] = {}  # client_id -> info
        self.room_connections: Dict[str, Set[str]] = {}  # room_id -> set of client_ids
        
        # 离线消息队列
        self.user_queues: Dict[str, queue.Queue] = {}
        
        # 数据库
        self.db_session = None
        self._init_db()
        
        # 锁
        self.lock = threading.Lock()
        
        logger.info(f"TranslationWebSocketServer initialized on {host}:{port}")
    
    def _init_db(self):
        """初始化数据库"""
        if not SQLALCHEMY_AVAILABLE:
            return
        
        DB_PATH = os.getenv('TEXT_DB_PATH', '/tmp/translation_records.db')
        os.makedirs(os.path.dirname(DB_PATH) if os.path.dirname(DB_PATH) else '/tmp', exist_ok=True)
        
        engine = create_engine(f'sqlite:///{DB_PATH}', echo=False, pool_pre_ping=True)
        Base.metadata.create_all(engine)
        self.db_session = sessionmaker(bind=engine)()
        logger.info(f"Database initialized: {DB_PATH}")
    
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
        
        user_id = query_params.get('user_id', f'anon_{client_id[:8]}')
        room_id = query_params.get('room_id', '')
        
        # 注册客户端
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
        
        logger.info(f"[WS] Client connected: {client_id}, user_id={user_id}")
        
        # 发送连接确认
        await self._send(websocket, {
            'type': MessageType.CONNECTED,
            'data': {
                'client_id': client_id,
                'user_id': user_id,
                'message': 'Connected to translation text service'
            }
        })
        
        try:
            async for message in websocket:
                try:
                    await self._process_message(client_id, message)
                except Exception as e:
                    logger.error(f"[WS] Error processing message: {e}")
                    
        except Exception as e:
            logger.info(f"[WS] Client disconnected: {client_id}, {e}")
        finally:
            await self._disconnect_client(client_id)
    
    async def _process_message(self, client_id: str, message_str: str):
        """处理消息"""
        try:
            data = json.loads(message_str)
            msg_type = data.get('type', '')
            msg_data = data.get('data', {})
            
            logger.debug(f"[WS] {client_id} sent: {msg_type}")
            
            if msg_type == 'subscribe':
                await self._handle_subscribe(client_id, msg_data)
                
            elif msg_type == 'unsubscribe':
                await self._handle_unsubscribe(client_id, msg_data)
                
            elif msg_type == MessageType.PING:
                await self._send(self.clients[client_id], {'type': MessageType.PONG})
                
            else:
                logger.debug(f"[WS] Unknown message type: {msg_type}")
                
        except json.JSONDecodeError:
            await self._send(self.clients[client_id], {
                'type': MessageType.ERROR,
                'data': {'message': 'Invalid JSON'}
            })
    
    async def _handle_subscribe(self, client_id: str, data: dict):
        """处理订阅"""
        user_id = data.get('user_id', '')
        room_id = data.get('room_id', '')

        if not user_id and not room_id:
            await self._send(self.clients[client_id], {
                'type': MessageType.ERROR,
                'data': {'message': 'Missing user_id or room_id'}
            })
            return

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

        # 发送离线缓存消息
        if user_id:
            cached = []
            if user_id in self.user_queues:
                q = self.user_queues[user_id]
                while not q.empty():
                    try:
                        cached.append(q.get_nowait())
                    except queue.Empty:
                        break

            if cached:
                await self._send(self.clients[client_id], {
                    'type': MessageType.CACHED_TEXTS,
                    'data': {
                        'count': len(cached),
                        'texts': cached
                    }
                })

        await self._send(self.clients[client_id], {
            'type': MessageType.SUBSCRIBED,
            'data': {
                'user_id': user_id,
                'room_id': room_id,
                'message': f'Subscribed to translation texts'
            }
        })

        logger.info(f"[WS] {client_id} subscribed to user={user_id}, room={room_id}")
    
    async def _handle_unsubscribe(self, client_id: str, data: dict):
        """处理取消订阅"""
        user_id = data.get('user_id', '')
        
        with self.lock:
            if user_id in self.user_connections:
                self.user_connections[user_id].discard(client_id)
                if not self.user_connections[user_id]:
                    del self.user_connections[user_id]
        
        await self._send(self.clients[client_id], {
            'type': MessageType.UNSUBSCRIBED,
            'data': {'user_id': user_id}
        })
        
        logger.info(f"[WS] {client_id} unsubscribed from user {user_id}")
    
    async def _disconnect_client(self, client_id: str):
        """断开客户端"""
        with self.lock:
            if client_id not in self.clients:
                return

            user_id = self.client_info.get(client_id, {}).get('user_id')
            room_id = self.client_info.get(client_id, {}).get('room_id')

            if user_id and user_id in self.user_connections:
                self.user_connections[user_id].discard(client_id)
                if not self.user_connections[user_id]:
                    del self.user_connections[user_id]

            if room_id and room_id in self.room_connections:
                self.room_connections[room_id].discard(client_id)
                if not self.room_connections[room_id]:
                    del self.room_connections[room_id]

            del self.clients[client_id]
            if client_id in self.client_info:
                del self.client_info[client_id]

        logger.info(f"[WS] Client {client_id} cleaned up")
    
    async def _send(self, websocket, message: dict):
        """发送消息"""
        try:
            await websocket.send(json.dumps(message))
        except Exception as e:
            logger.error(f"[WS] Send error: {e}")
    
    def push_translation_text(self, user_id: str, text_data: dict):
        """推送翻译文本给指定用户"""
        # 保存到数据库
        self._save_to_db(text_data)
        
        # 放入离线队列
        if user_id not in self.user_queues:
            self.user_queues[user_id] = queue.Queue(maxsize=100)
        try:
            self.user_queues[user_id].put_nowait(text_data)
        except queue.Full:
            logger.warning(f"[WS] Queue full for user {user_id}")
        
        # 通过 WebSocket 推送
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        try:
            client_ids = list(self.user_connections.get(user_id, set()))
            
            for client_id in client_ids:
                if client_id in self.clients:
                    message = {
                        'type': MessageType.TRANSLATION_TEXT,
                        'data': text_data
                    }
                    asyncio.run(self._send(self.clients[client_id], message))
                    logger.info(f"[WS] Pushed text to {user_id}: {text_data.get('original_text', '')[:20]}...")
        finally:
            loop.close()

    def broadcast_to_room(self, room_id: str, event_type: str, data: dict):
        """广播消息给房间所有连接的用户"""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            client_ids = list(self.room_connections.get(room_id, set()))
            message = {
                'type': event_type,
                'data': data
            }

            for client_id in client_ids:
                if client_id in self.clients:
                    asyncio.run(self._send(self.clients[client_id], message))
            logger.info(f"[WS] Broadcast {event_type} to room {room_id}, {len(client_ids)} clients")
        finally:
            loop.close()

    def push_original_speech_text_to_room(self, text_data: dict):
        """推送原语音识别文字给房间所有用户"""
        room_id = text_data.get('room_id', '')
        if not room_id:
            logger.warning("[WS] No room_id for original speech text push")
            return

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            client_ids = list(self.room_connections.get(room_id, set()))
            message = {
                'type': MessageType.ORIGINAL_SPEECH_TEXT,
                'data': text_data
            }

            for client_id in client_ids:
                if client_id in self.clients:
                    asyncio.run(self._send(self.clients[client_id], message))
            logger.info(f"[WS] Pushed original speech text to room {room_id}: "
                      f"'{text_data.get('original_text', '')[:20]}...', {len(client_ids)} clients")
        finally:
            loop.close()

    def _save_to_db(self, text_data: dict):
        """保存到数据库"""
        if not SQLALCHEMY_AVAILABLE or not self.db_session:
            return
        
        try:
            record = TranslationRecord(
                request_id=text_data.get('request_id', ''),
                room_id=text_data.get('room_id', ''),
                source_user=text_data.get('source_user', ''),
                target_user=text_data.get('target_user', ''),
                original_text=text_data.get('original_text', ''),
                translated_text=text_data.get('translated_text', ''),
                source_lang=text_data.get('source_lang', ''),
                target_lang=text_data.get('target_lang', ''),
                timestamp=text_data.get('timestamp', time.time())
            )
            self.db_session.add(record)
            self.db_session.commit()
        except Exception as e:
            logger.error(f"[DB] Error saving record: {e}")
            self.db_session.rollback()
    
    async def start_server(self):
        """启动服务器"""
        if not WEBSOCKETS_AVAILABLE:
            logger.error("websockets 库不可用")
            return False
        
        logger.info(f"Starting Translation WebSocket server on ws://{self.host}:{self.port}")
        
        # 设置 SO_REUSEADDR
        import socket
        import asyncio
        
        # 获取或创建事件循环
        loop = asyncio.get_event_loop()
        
        # 创建服务器并设置 socket 选项
        async with websockets.serve(self.handle_client, self.host, self.port, reuse_address=True):
            await asyncio.Future()
    
    def run_in_thread(self):
        """在新线程中运行"""
        def run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(self.start_server())
            finally:
                loop.close()
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread


# ========== Flask HTTP 服务器 ==========

class HTTPServer:
    """HTTP API 服务器"""
    
    def __init__(self, ws_server: TranslationWebSocketServer, host: str = "0.0.0.0", port: int = 8086):
        self.app = Flask(__name__)
        self.app.config['JSON_AS_ASCII'] = False
        self.ws_server = ws_server
        self.host = host
        self.port = port
        self._setup_routes()
    
    def _setup_routes(self):
        """设置路由"""
        
        @self.app.route('/health', methods=['GET'])
        def health():
            return jsonify({
                'status': 'ok',
                'service': 'translation-text-server',
                'ws_clients': len(self.ws_server.clients),
                'ws_users': len(self.ws_server.user_connections),
                'ws_rooms': len(self.ws_server.room_connections)
            })
        
        @self.app.route('/api/v1/translation/text/push', methods=['POST'])
        def push_text():
            """推送翻译文本"""
            data = request.json or {}
            target_user = data.get('target_user', '')

            if not target_user:
                return jsonify({'code': 400, 'message': 'Missing target_user'}), 400

            self.ws_server.push_translation_text(target_user, data)
            return jsonify({'code': 0, 'message': 'success'})

        @self.app.route('/api/v1/original/speech/text/push', methods=['POST'])
        def push_original_speech_text():
            """推送原语音识别文字给房间所有用户"""
            data = request.json or {}
            room_id = data.get('room_id', '')

            if not room_id:
                return jsonify({'code': 400, 'message': 'Missing room_id'}), 400

            self.ws_server.push_original_speech_text_to_room(data)
            return jsonify({'code': 0, 'message': 'success'})
        
        @self.app.route('/api/v1/translation/text/connections', methods=['GET'])
        def get_connections():
            """获取连接状态"""
            with self.ws_server.lock:
                return jsonify({
                    'total_users': len(self.ws_server.user_connections),
                    'users': {uid: len(sessions) for uid, sessions in self.ws_server.user_connections.items()}
                })
        
        @self.app.route('/api/v1/translation/text/history', methods=['GET'])
        def get_history():
            """查询翻译历史"""
            if not SQLALCHEMY_AVAILABLE:
                return jsonify({'code': 500, 'message': 'Database not available'}), 500
            
            user_id = request.args.get('user_id', '')
            limit = int(request.args.get('limit', 100))
            offset = int(request.args.get('offset', 0))
            
            if not user_id:
                return jsonify({'code': 400, 'message': 'Missing user_id'}), 400
            
            try:
                from sqlalchemy import desc
                query = self.ws_server.db_session.query(TranslationRecord).filter(
                    TranslationRecord.target_user == user_id
                ).order_by(desc(TranslationRecord.timestamp)).offset(offset).limit(limit)
                
                records = query.all()
                texts = [{
                    'original_text': r.original_text,
                    'translated_text': r.translated_text,
                    'source_lang': r.source_lang,
                    'target_lang': r.target_lang,
                    'timestamp': r.timestamp
                } for r in records]
                
                return jsonify({
                    'code': 0,
                    'data': {'texts': texts, 'limit': limit, 'offset': offset}
                })
            except Exception as e:
                return jsonify({'code': 500, 'message': str(e)}), 500
    
    def run(self, threaded: bool = True):
        """运行服务器"""
        logger.info(f"Starting HTTP server on http://{self.host}:{self.port}")
        
        # 设置全局 SO_REUSEADDR
        import socketserver
        socketserver.TCPServer.allow_reuse_address = True
        
        # 使用 WSGIServer 替代 Flask 内置服务器
        try:
            from gevent.pywsgi import WSGIServer
            http_server = WSGIServer((self.host, self.port), self.app)
            http_server.serve_forever()
        except ImportError:
            logger.warning("gevent 不可用，使用 Flask 内置服务器")
            self.app.run(host=self.host, port=self.port, debug=False, threaded=threaded)


# ========== 主程序 ==========

def main():
    """主入口"""
    host = os.getenv('TEXT_SERVER_HOST', '0.0.0.0')
    port = int(os.getenv('TEXT_SERVER_PORT', 8086))
    
    logger.info(f"=" * 60)
    logger.info(f"翻译文本推送服务 (纯 WebSocket)")
    logger.info(f"WebSocket: ws://{host}:{port}")
    logger.info(f"HTTP API:  http://{host}:{port}")
    logger.info(f"=" * 60)
    
    if not WEBSOCKETS_AVAILABLE:
        logger.error("请安装 websockets 库: pip install websockets>=10.0.0")
        sys.exit(1)
    
    # 创建 WebSocket 服务器
    ws_server = TranslationWebSocketServer(host=host, port=port)
    
    # 创建 HTTP 服务器
    http_server = HTTPServer(ws_server, host=host, port=port)
    
    # 设置全局 SO_REUSEADDR
    import socketserver
    socketserver.TCPServer.allow_reuse_address = True
    
    # 在后台线程启动 WebSocket 服务器
    ws_server.run_in_thread()
    
    # 等待一下让 WebSocket 服务器先绑定
    import time
    time.sleep(1)
    
    # 主线程运行 HTTP 服务器
    http_server.run()


if __name__ == '__main__':
    main()
