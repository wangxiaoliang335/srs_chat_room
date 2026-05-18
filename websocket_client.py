#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WebSocket 客户端库
用于连接 WebSocket 服务器，支持订阅房间、接收消息等功能
"""

import os
import sys
import json
import asyncio
import logging
import threading
import time
import queue
from typing import Callable, Optional, Dict, Any, List
from enum import Enum

try:
    import websockets
    WEBSOCKETS_AVAILABLE = True
except ImportError:
    WEBSOCKETS_AVAILABLE = False
    logging.warning("websockets 库未安装")

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class MessageType(Enum):
    """消息类型"""
    PING = "ping"
    PONG = "pong"
    SUBSCRIBE_ROOM = "subscribe_room"
    UNSUBSCRIBE_ROOM = "unsubscribe_room"
    SUBSCRIBE_PRIVATE = "subscribe_private"
    ACK = "ack"
    ERROR = "error"
    CONNECTED = "connected"
    SUBSCRIBED = "subscribed"
    UNSUBSCRIBED = "unsubscribed"
    
    # 事件类型
    USER_JOINED = "user_joined"
    USER_LEFT = "user_left"
    KNOCK = "knock"
    SPEAKING = "speaking"
    TRANSLATION_TEXT = "translation_text"
    TRANSLATION_STARTED = "translation_started"
    TRANSLATION_STOPPED = "translation_stopped"


class WebSocketClient:
    """WebSocket 客户端"""
    
    def __init__(
        self,
        url: str,
        user_id: str = None,
        room_id: str = None,
        auto_reconnect: bool = True,
        reconnect_interval: int = 5,
        reconnect_max_attempts: int = 10
    ):
        """
        初始化 WebSocket 客户端
        
        Args:
            url: WebSocket 服务器地址，如 ws://localhost:8086
            user_id: 用户ID（可选）
            room_id: 房间ID（可选）
            auto_reconnect: 是否自动重连
            reconnect_interval: 重连间隔（秒）
            reconnect_max_attempts: 最大重连次数
        """
        self.url = url
        self.user_id = user_id
        self.room_id = room_id
        self.auto_reconnect = auto_reconnect
        self.reconnect_interval = reconnect_interval
        self.reconnect_max_attempts = reconnect_max_attempts
        
        self.websocket = None
        self.connected = False
        self.client_id = None
        
        # 消息处理
        self.handlers: Dict[str, List[Callable]] = {}
        self.message_queue: queue.Queue = queue.Queue()
        
        # 锁
        self.lock = threading.Lock()
        
        # 运行状态
        self.running = False
        self.receive_thread: threading.Thread = None
        self.process_thread: threading.Thread = None
        
        # 消息计数器
        self.msg_id_counter = 0
        
        logger.info(f"WebSocket Client initialized: {url}, user_id={user_id}")
    
    def _generate_msg_id(self) -> str:
        """生成消息ID"""
        self.msg_id_counter += 1
        return f"{int(time.time() * 1000)}_{self.msg_id_counter}"
    
    def on(self, event_type: str, handler: Callable):
        """
        注册消息处理器
        
        Args:
            event_type: 事件类型
            handler: 处理函数，接收 (client_id, message_data) 参数
        """
        if event_type not in self.handlers:
            self.handlers[event_type] = []
        self.handlers[event_type].append(handler)
        logger.debug(f"Registered handler for: {event_type}")
    
    def off(self, event_type: str, handler: Callable = None):
        """
        移除消息处理器
        
        Args:
            event_type: 事件类型
            handler: 处理函数，不指定则移除该类型所有处理器
        """
        if event_type in self.handlers:
            if handler:
                self.handlers[event_type].remove(handler)
            else:
                self.handlers[event_type].clear()
    
    def _process_message(self, msg_type: str, msg_data: Dict):
        """处理接收到的消息"""
        # 调用注册的处理器
        if msg_type in self.handlers:
            for handler in self.handlers[msg_type]:
                try:
                    handler(self.client_id, msg_data)
                except Exception as e:
                    logger.error(f"Error in handler for {msg_type}: {e}")
        
        # 调用通用处理器
        if '*' in self.handlers:
            for handler in self.handlers['*']:
                try:
                    handler(msg_type, msg_data)
                except Exception as e:
                    logger.error(f"Error in wildcard handler: {e}")
    
    def connect(self, timeout: int = 10) -> bool:
        """
        连接到服务器
        
        Args:
            timeout: 超时时间（秒）
        
        Returns:
            连接是否成功
        """
        if not WEBSOCKETS_AVAILABLE:
            logger.error("websockets 库不可用")
            return False
        
        try:
            # 构建 URL（添加查询参数）
            url = self.url
            params = []
            if self.user_id:
                params.append(f"user_id={self.user_id}")
            if params:
                url = f"{url}?{'&'.join(params)}"
            
            logger.info(f"Connecting to {url}...")
            
            # 同步方式连接（使用 event loop）
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            
            try:
                self.websocket = loop.run_until_complete(
                    websockets.connect(url, ping_interval=None)
                )
            finally:
                loop.close()
            
            self.connected = True
            self.running = True
            
            # 启动接收线程
            self.receive_thread = threading.Thread(target=self._receive_loop, daemon=True)
            self.receive_thread.start()
            
            # 启动消息处理线程
            self.process_thread = threading.Thread(target=self._process_loop, daemon=True)
            self.process_thread.start()
            
            # 自动订阅房间
            if self.room_id:
                self.subscribe_room(self.room_id)
                self.subscribe_private(self.user_id)
            
            logger.info(f"Connected successfully!")
            return True
            
        except Exception as e:
            logger.error(f"Connection failed: {e}")
            self.connected = False
            return False
    
    def _receive_loop(self):
        """接收消息循环"""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        try:
            while self.running and self.websocket:
                try:
                    message = loop.run_until_complete(
                        asyncio.wait_for(self.websocket.recv(), timeout=1)
                    )
                    self._handle_message(message)
                except asyncio.TimeoutError:
                    continue
                except Exception as e:
                    if self.running:
                        logger.error(f"Receive error: {e}")
                    break
        finally:
            loop.close()
        
        self.connected = False
        logger.info("Receive loop ended")
    
    def _handle_message(self, message: str):
        """处理接收到的消息"""
        try:
            data = json.loads(message)
            msg_type = data.get('type', '')
            msg_data = data.get('data', {})
            
            logger.debug(f"Received: {msg_type}")
            
            # 记录客户端ID
            if msg_type == 'connected':
                self.client_id = msg_data.get('client_id')
                logger.info(f"Server assigned client_id: {self.client_id}")
            
            # 放入队列
            self.message_queue.put((msg_type, msg_data))
            
        except json.JSONDecodeError:
            logger.warning(f"Invalid JSON: {message}")
    
    def _process_loop(self):
        """消息处理循环"""
        while self.running:
            try:
                msg_type, msg_data = self.message_queue.get(timeout=1)
                self._process_message(msg_type, msg_data)
            except queue.Empty:
                continue
            except Exception as e:
                logger.error(f"Process error: {e}")
    
    def send(self, msg_type: str, data: Dict = None, wait_ack: bool = False) -> bool:
        """
        发送消息
        
        Args:
            msg_type: 消息类型
            data: 消息数据
            wait_ack: 是否等待 ACK
        
        Returns:
            发送是否成功
        """
        if not self.connected or not self.websocket:
            logger.warning("Not connected")
            return False
        
        msg_id = self._generate_msg_id() if wait_ack else None
        
        message = {
            "type": msg_type,
            "data": data or {},
            "id": msg_id,
            "timestamp": time.time()
        }
        
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(self.websocket.send(json.dumps(message)))
            finally:
                loop.close()
            
            logger.debug(f"Sent: {msg_type}")
            return True
            
        except Exception as e:
            logger.error(f"Send error: {e}")
            return False
    
    def subscribe_room(self, room_id: str) -> bool:
        """订阅房间"""
        return self.send(MessageType.SUBSCRIBE_ROOM.value, {"room_id": room_id})
    
    def unsubscribe_room(self, room_id: str) -> bool:
        """取消订阅房间"""
        return self.send(MessageType.UNSUBSCRIBE_ROOM.value, {"room_id": room_id})
    
    def subscribe_private(self, user_id: str = None) -> bool:
        """订阅私人通知"""
        uid = user_id or self.user_id
        if not uid:
            logger.warning("No user_id for private subscription")
            return False
        return self.send(MessageType.SUBSCRIBE_PRIVATE.value, {"user_id": uid})
    
    def send_translation_request(self, room_id: str, source_user: str, target_user: str, 
                                  target_lang: str) -> bool:
        """发送翻译请求"""
        return self.send("translation_request", {
            "room_id": room_id,
            "source_user": source_user,
            "target_user": target_user,
            "target_lang": target_lang
        })
    
    def disconnect(self):
        """断开连接"""
        logger.info("Disconnecting...")
        self.running = False
        
        if self.websocket:
            try:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    loop.run_until_complete(self.websocket.close())
                finally:
                    loop.close()
            except Exception as e:
                logger.error(f"Error closing websocket: {e}")
        
        self.connected = False
        logger.info("Disconnected")
    
    def is_connected(self) -> bool:
        """检查是否已连接"""
        return self.connected


# ========== 同步 WebSocket 客户端 ==========

class SyncWebSocketClient(WebSocketClient):
    """同步 WebSocket 客户端（简化版）"""
    
    def __init__(self, url: str, user_id: str = None, room_id: str = None):
        super().__init__(url, user_id, room_id)
        self.callbacks: Dict[str, Callable] = {}
    
    def set_callback(self, event_type: str, callback: Callable):
        """设置回调函数"""
        self.callbacks[event_type] = callback
    
    def _process_message(self, msg_type: str, msg_data: Dict):
        """处理消息"""
        if msg_type in self.callbacks:
            try:
                self.callbacks[msg_type](msg_data)
            except Exception as e:
                logger.error(f"Callback error for {msg_type}: {e}")
        
        # 也调用父类处理器
        super()._process_message(msg_type, msg_data)


# ========== 测试代码 ==========

def test_client():
    """测试客户端"""
    import argparse
    
    parser = argparse.ArgumentParser(description='WebSocket Client Test')
    parser.add_argument('--url', default='ws://localhost:8086', help='WebSocket URL')
    parser.add_argument('--user-id', default='test_user', help='User ID')
    parser.add_argument('--room-id', default='test_room', help='Room ID')
    args = parser.parse_args()
    
    # 创建客户端
    client = WebSocketClient(args.url, user_id=args.user_id, room_id=args.room_id)
    
    # 注册事件处理器
    client.on(MessageType.CONNECTED.value, 
              lambda cid, data: print(f"Connected: {data}"))
    client.on(MessageType.SUBSCRIBED.value,
              lambda cid, data: print(f"Subscribed: {data}"))
    client.on(MessageType.USER_JOINED.value,
              lambda cid, data: print(f"User joined: {data}"))
    client.on(MessageType.TRANSLATION_TEXT.value,
              lambda cid, data: print(f"Translation: {data}"))
    client.on(MessageType.ERROR.value,
              lambda cid, data: print(f"Error: {data}"))
    
    # 连接
    if client.connect():
        print("连接成功，按 Ctrl+C 退出...")
        try:
            while client.is_connected():
                time.sleep(1)
        except KeyboardInterrupt:
            pass
    else:
        print("连接失败")
    
    client.disconnect()


if __name__ == '__main__':
    test_client()
