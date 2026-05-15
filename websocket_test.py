#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WebSocket API 测试客户端
用于测试语音聊天室回调服务器接口
支持纯 WebSocket 协议

使用方法:
    pip install websockets requests
    python websocket_test.py
"""

import json
import time
import asyncio
import requests
import threading
from typing import Optional, Callable

# 尝试导入 websockets
try:
    import websockets
    WEBSOCKETS_AVAILABLE = True
except ImportError:
    WEBSOCKETS_AVAILABLE = False
    print("注意: websockets 未安装，WebSocket 功能不可用")
    print("安装命令: pip install websockets")


# ========== 配置 ==========
SERVER_HOST = "localhost"
SERVER_PORT = 8085
BASE_URL = f"http://{SERVER_HOST}:{SERVER_PORT}"
WS_URL = f"ws://{SERVER_HOST}:{SERVER_PORT}"


# ========== 颜色输出 ==========
class Colors:
    GREEN = '\033[92m'
    RED = '\033[91m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    RESET = '\033[0m'

def success(msg):
    print(f"{Colors.GREEN}✓ {msg}{Colors.RESET}")

def error(msg):
    print(f"{Colors.RED}✗ {msg}{Colors.RESET}")

def info(msg):
    print(f"{Colors.BLUE}ℹ {msg}{Colors.RESET}")

def warn(msg):
    print(f"{Colors.YELLOW}⚠ {msg}{Colors.RESET}")


# ========== HTTP API 测试 ==========
class HTTPClient:
    """HTTP API 客户端"""

    def __init__(self, base_url):
        self.base_url = base_url
        self.session = requests.Session()
        self.session.headers.update({
            'Content-Type': 'application/json',
            'User-Agent': 'WebSocketTestClient/1.0'
        })

    def get(self, path):
        """GET 请求"""
        url = f"{self.base_url}{path}"
        try:
            resp = self.session.get(url, timeout=10)
            try:
                return resp.status_code, resp.json()
            except:
                return resp.status_code, resp.text
        except requests.exceptions.RequestException as e:
            return None, str(e)

    def post(self, path, data=None):
        """POST 请求"""
        url = f"{self.base_url}{path}"
        try:
            resp = self.session.post(url, json=data, timeout=10)
            try:
                return resp.status_code, resp.json()
            except:
                return resp.status_code, resp.text
        except requests.exceptions.RequestException as e:
            return None, str(e)

    def put(self, path, data=None):
        """PUT 请求"""
        url = f"{self.base_url}{path}"
        try:
            resp = self.session.put(url, json=data, timeout=10)
            try:
                return resp.status_code, resp.json()
            except:
                return resp.status_code, resp.text
        except requests.exceptions.RequestException as e:
            return None, str(e)

    def delete(self, path):
        """DELETE 请求"""
        url = f"{self.base_url}{path}"
        try:
            resp = self.session.delete(url, timeout=10)
            try:
                return resp.status_code, resp.json()
            except:
                return resp.status_code, resp.text
        except requests.exceptions.RequestException as e:
            return None, str(e)


# ========== 纯 WebSocket 客户端 ==========
class WebSocketClient:
    """纯 WebSocket 客户端（替代 Socket.IO）"""

    def __init__(self, server_url: str):
        self.server_url = server_url
        self.websocket = None
        self.connected = False
        self.client_id = None
        self.received_messages = []
        self.handlers = {}
        self._receive_task = None
        self._running = False

    def on(self, event_type: str, handler: Callable):
        """注册消息处理器"""
        self.handlers[event_type] = handler

    async def _receive_loop(self):
        """接收消息循环"""
        try:
            async for message in self.websocket:
                try:
                    data = json.loads(message)
                    msg_type = data.get('type', '')
                    msg_data = data.get('data', {})
                    
                    self.received_messages.append({'type': msg_type, 'data': msg_data})
                    info(f"收到事件: {msg_type} -> {msg_data}")
                    
                    # 调用处理器
                    if msg_type in self.handlers:
                        self.handlers[msg_type](msg_data)
                    
                    # 记录 client_id
                    if msg_type == 'connected':
                        self.client_id = msg_data.get('client_id')
                        
                except json.JSONDecodeError:
                    warn(f"收到无效 JSON: {message}")
        except websockets.exceptions.ConnectionClosed:
            info("WebSocket 连接已关闭")
        finally:
            self.connected = False

    async def _connect_async(self, user_id: str = None, room_id: str = None) -> bool:
        """异步连接"""
        params = []
        if user_id:
            params.append(f"user_id={user_id}")
        if room_id:
            params.append(f"room_id={room_id}")
        
        url = self.server_url
        if params:
            url = f"{url}?{'&'.join(params)}"
        
        info(f"连接 WebSocket: {url}")
        
        try:
            self.websocket = await websockets.connect(url, ping_interval=None)
            self.connected = True
            self._running = True
            
            # 启动接收循环
            asyncio.create_task(self._receive_loop())
            
            success("WebSocket 连接成功")
            return True
        except Exception as e:
            error(f"WebSocket 连接失败: {e}")
            self.connected = False
            return False

    def connect(self, user_id: str = None, room_id: str = None) -> bool:
        """连接到 WebSocket 服务器"""
        if not WEBSOCKETS_AVAILABLE:
            error("websockets 未安装")
            return False
        
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        
        return loop.run_until_complete(self._connect_async(user_id, room_id))

    def send(self, msg_type: str, data: dict = None) -> bool:
        """发送消息"""
        if not self.connected or not self.websocket:
            warn("未连接")
            return False
        
        message = {
            'type': msg_type,
            'data': data or {},
            'timestamp': time.time()
        }
        
        try:
            loop = asyncio.get_event_loop()
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        
        try:
            loop.run_until_complete(self.websocket.send(json.dumps(message)))
            info(f"发送消息: {msg_type}")
            return True
        except Exception as e:
            error(f"发送失败: {e}")
            return False

    def subscribe_room(self, room_id: str) -> bool:
        """订阅房间"""
        return self.send('subscribe_room', {'room_id': room_id})

    def unsubscribe_room(self, room_id: str) -> bool:
        """取消订阅房间"""
        return self.send('unsubscribe_room', {'room_id': room_id})

    def subscribe_private(self, user_id: str) -> bool:
        """订阅私人通知"""
        return self.send('subscribe_private', {'user_id': user_id})

    def disconnect(self):
        """断开连接"""
        self._running = False
        if self.websocket:
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
            
            try:
                loop.run_until_complete(self.websocket.close())
            except:
                pass
        
        self.connected = False
        success("已断开 WebSocket 连接")


# ========== 简化版同步客户端 ==========
class SimpleWebSocketClient:
    """简化版 WebSocket 客户端（使用标准库）"""

    def __init__(self, url: str):
        self.url = url
        self.ws = None
        self.connected = False
        self.client_id = None
        self.received_messages = []

    def connect(self, user_id: str = None, room_id: str = None) -> bool:
        """连接（需要 websocket-client 库）"""
        try:
            import websocket
        except ImportError:
            error("请安装 websocket-client: pip install websocket-client")
            return False
        
        params = []
        if user_id:
            params.append(f"user_id={user_id}")
        if room_id:
            params.append(f"room_id={room_id}")
        
        url = self.url
        if params:
            url = f"{url}?{'&'.join(params)}"
        
        info(f"连接 WebSocket: {url}")
        
        try:
            self.ws = websocket.create_connection(url)
            self.connected = True
            success("WebSocket 连接成功")
            return True
        except Exception as e:
            error(f"WebSocket 连接失败: {e}")
            return False

    def send(self, msg_type: str, data: dict = None) -> bool:
        """发送消息"""
        if not self.connected or not self.ws:
            return False
        
        message = {
            'type': msg_type,
            'data': data or {},
            'timestamp': time.time()
        }
        
        try:
            self.ws.send(json.dumps(message))
            return True
        except Exception as e:
            error(f"发送失败: {e}")
            return False

    def receive(self, timeout: int = 1) -> Optional[dict]:
        """接收消息"""
        if not self.connected:
            return None
        
        try:
            self.ws.settimeout(timeout)
            message = self.ws.recv()
            data = json.loads(message)
            self.received_messages.append(data)
            return data
        except:
            return None

    def disconnect(self):
        """断开连接"""
        if self.ws:
            self.ws.close()
        self.connected = False


# ========== 测试用例 ==========

# 创建全局 HTTP 客户端
http_client = HTTPClient(BASE_URL)


def test_health():
    """测试健康检查接口"""
    print(f"\n{'='*50}")
    info("测试 1: 健康检查接口")
    print(f"{'='*50}")

    code, data = http_client.get("/health")
    if code == 200:
        if isinstance(data, dict) and data.get("status") == "ok":
            success(f"健康检查通过: {data}")
        else:
            success(f"健康检查通过: {data}")
    else:
        error(f"健康检查失败: code={code}, data={data}")


def test_ws_status():
    """测试 WebSocket 状态接口"""
    print(f"\n{'='*50}")
    info("测试 2: WebSocket 状态接口")
    print(f"{'='*50}")

    code, data = http_client.get("/api/v1/ws/status")
    if code == 200:
        if isinstance(data, dict):
            success(f"WebSocket 状态: {data}")
        else:
            success(f"WebSocket 状态: {data}")
    else:
        warn(f"WebSocket 状态接口: code={code}")


def test_ws_broadcast():
    """测试 WebSocket 广播接口"""
    print(f"\n{'='*50}")
    info("测试 3: WebSocket 广播接口")
    print(f"{'='*50}")

    code, data = http_client.post("/api/v1/ws/broadcast", {
        "room_id": "test_room",
        "type": "test_message",
        "data": {"message": "Hello from HTTP!"}
    })
    if code == 200 and isinstance(data, dict) and data.get("code") == 0:
        success(f"广播发送成功: {data}")
    else:
        warn(f"广播发送: code={code}")


def test_ws_realtime(room_id="test_room_001", user_id="test_user_001"):
    """测试 WebSocket 实时通信"""
    print(f"\n{'='*50}")
    info("测试 4: WebSocket 实时通信")
    print(f"{'='*50}")

    if not WEBSOCKETS_AVAILABLE:
        warn("websockets 未安装，跳过 WebSocket 测试")
        warn("安装命令: pip install websockets")
        return False

    client = WebSocketClient(WS_URL)
    
    # 注册消息处理器
    def on_connected(data):
        success(f"连接成功: {data}")
    
    def on_subscribed(data):
        success(f"订阅成功: {data}")
    
    def on_user_joined(data):
        info(f"用户加入: {data}")
    
    client.on('connected', on_connected)
    client.on('subscribed', on_subscribed)
    client.on('user_joined', on_user_joined)

    # 连接
    if client.connect(user_id=user_id, room_id=room_id):
        # 订阅房间
        client.subscribe_room(room_id)
        client.subscribe_private(user_id)
        
        # 发送测试消息
        time.sleep(1)
        client.send('ping')
        
        # 等待接收消息
        time.sleep(3)
        
        client.disconnect()
        return True
    
    return False


def test_room_management():
    """测试房间管理接口"""
    print(f"\n{'='*50}")
    info("测试 5: 房间管理接口")
    print(f"{'='*50}")

    room_id = "demo_room_001"

    # 创建房间
    info(f"创建房间: {room_id}")
    code, data = http_client.post("/api/v1/room", {
        "room_id": room_id,
        "name": "测试房间",
        "owner_id": "admin_001",
        "description": "这是一个测试房间"
    })
    if code == 200 and isinstance(data, dict) and data.get("code") == 0:
        success(f"创建房间成功: {data.get('data', {}).get('room_id')}")
    else:
        warn(f"创建房间: code={code}, data={data}")

    time.sleep(0.5)

    # 获取房间信息
    info("获取房间信息")
    code, data = http_client.get(f"/api/v1/room/{room_id}")
    if code == 200 and isinstance(data, dict) and data.get("code") == 0:
        room_name = data.get("data", {}).get("name", "未知")
        success(f"获取房间成功: {room_name}")
    else:
        error(f"获取房间失败: code={code}, data={data}")

    time.sleep(0.5)

    # 加入房间
    info("加入房间 user_001")
    code, data = http_client.post(f"/api/v1/room/{room_id}/join", {
        "user_id": "user_001",
        "role": "member"
    })
    if code == 200 and isinstance(data, dict) and data.get("code") == 0:
        success(f"加入房间成功")
    else:
        warn(f"加入房间: code={code}, data={data}")


def test_translation_api():
    """测试翻译接口"""
    print(f"\n{'='*50}")
    info("测试 6: 翻译接口")
    print(f"{'='*50}")

    room_id = "test_room"
    source_user = "user_001"
    target_user = "user_002"

    # 申请翻译
    info(f"申请翻译: {room_id}")
    code, data = http_client.post("/api/v1/translation/request", {
        "room_id": room_id,
        "source_user": source_user,
        "target_user": target_user,
        "target_lang": "en"
    })
    if code in (200, 201) and isinstance(data, dict) and data.get("code") == 0:
        success(f"翻译申请成功: {data}")
    else:
        warn(f"翻译申请: code={code}, data={data}")


def test_knock():
    """测试敲门功能"""
    print(f"\n{'='*50}")
    info("测试 7: 敲门功能")
    print(f"{'='*50}")

    knock_room_id = "knock_test_room"
    owner_id = "owner_test"
    knocker_id = "visitor_test"

    # 创建房间
    info(f"创建敲门目标房间: {knock_room_id}")
    code, data = http_client.post("/api/v1/room", {
        "room_id": knock_room_id,
        "name": "敲门测试房间",
        "owner_id": owner_id
    })
    if code in (200, 201):
        success(f"创建房间成功")
    else:
        warn(f"创建房间: code={code}")
        return

    time.sleep(0.5)

    # 敲门
    info(f"访客 {knocker_id} 敲门")
    code, data = http_client.post(f"/api/v1/room/{knock_room_id}/knock", {
        "user_id": knocker_id,
        "message": "你好，我想加入聊天"
    })
    if code == 200 and isinstance(data, dict) and data.get("code") == 0:
        success(f"敲门成功")
    else:
        error(f"敲门失败: code={code}, data={data}")

    time.sleep(0.5)

    # 接受敲门
    info(f"房主接受敲门")
    code, data = http_client.post(f"/api/v1/room/{knock_room_id}/knock/accept", {
        "operator_id": owner_id,
        "knocker_id": knocker_id,
        "role": "member"
    })
    if code == 200 and isinstance(data, dict) and data.get("code") == 0:
        success("敲门被接受")
    else:
        warn(f"接受敲门: code={code}, data={data}")

    time.sleep(0.5)

    # 清理
    info("清理测试房间")
    code, data = http_client.delete(f"/api/v1/room/{knock_room_id}")
    if code == 200:
        success("房间已删除")


# ========== 主程序 ==========

def main():
    """主入口"""
    print(f"\n{'#'*60}")
    print(f"# WebSocket API 测试客户端")
    print(f"# 服务器: {BASE_URL}")
    print(f"# WebSocket: {WS_URL}")
    print(f"{'#'*60}")

    # 测试 HTTP 接口
    test_health()
    test_ws_status()
    test_ws_broadcast()

    # 测试 WebSocket 实时通信
    test_ws_realtime()

    # 测试业务接口
    test_room_management()
    test_translation_api()
    test_knock()

    print(f"\n{'='*60}")
    success("所有测试完成")
    print(f"{'='*60}\n")


if __name__ == '__main__':
    main()
