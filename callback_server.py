#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SRS HTTP回调服务器
接收SRS的HTTP Hooks回调，管理多用户多翻译请求，支持Socket.IO实时通知
"""

import os
import sys
import json
import logging
import subprocess
import threading
import time
import uuid
import websockets

from flask import Flask, request, jsonify, session
from flask_socketio import SocketIO, emit, join_room, leave_room

import socketio as python_socketio
from typing import Dict, Any, List, Set

# 加载 .env 环境变量文件
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))

from translation_manager import (
    TranslationManager, TranslationRequest, TranslationStatus
)
from user_manager import (
    UserManager, UserRole, UserStatus, user_manager
)
from notification_service import notification_service

# 配置日志：同时输出到控制台和文件
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# 文件日志处理器
log_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'callback_server.log')
file_handler = logging.FileHandler(log_file, encoding='utf-8')
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))

# 控制台日志处理器
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))

# 添加处理器到logger
logger.addHandler(file_handler)
logger.addHandler(console_handler)

app = Flask(__name__)
app.config['SECRET_KEY'] = 'srs-chatroom-secret-key'

# 初始化 Socket.IO
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading', ping_timeout=60, ping_interval=25)

# 设置 notification_service 使用 socketio
notification_service.set_socketio(socketio)


# ========== 原生 WebSocket 端点（兼容客户端）==========

import asyncio
from typing import Set
from werkzeug.wrappers import Response as WerkzeugResponse

# 原生 WebSocket 连接存储
native_ws_connections: Dict[str, Set] = {}
native_ws_lock = threading.Lock()


def ws_register_connection(room_id: str, websocket):
    """注册 WebSocket 连接"""
    with native_ws_lock:
        if room_id not in native_ws_connections:
            native_ws_connections[room_id] = set()
        native_ws_connections[room_id].add(websocket)


def ws_unregister_connection(room_id: str, websocket):
    """注销 WebSocket 连接"""
    with native_ws_lock:
        if room_id in native_ws_connections:
            native_ws_connections[room_id].discard(websocket)
            if not native_ws_connections[room_id]:
                del native_ws_connections[room_id]


def ws_get_connections(room_id: str):
    """获取房间的所有连接"""
    with native_ws_lock:
        return list(native_ws_connections.get(room_id, set()))


def ws_broadcast_to_room(room_id: str, message: str):
    """向房间广播消息"""
    with native_ws_lock:
        connections = list(native_ws_connections.get(room_id, set()))
    
    disconnected = []
    for ws in connections:
        try:
            ws.send(message)
        except Exception as e:
            logger.warning(f"[WS] Failed to send: {e}")
            disconnected.append(ws)
    
    for ws in disconnected:
        ws_unregister_connection(room_id, ws)


# 使用简单的 HTTP/WebSocket 升级处理
@app.route('/ws')
def websocket_endpoint():
    """原生 WebSocket 端点"""
    from flask import request
    
    room_id = request.args.get('room', '')
    user_id = request.args.get('user', '')
    
    if not room_id:
        return jsonify({'error': 'Missing room parameter'}), 400
    
    # 这是 Flask 的 WebSocket 支持（需要 gevent-websocket）
    # 如果 Flask-SocketIO 已经在运行，这个端点会被 Socket.IO 处理
    return jsonify({
        'message': 'Use Socket.IO client or native WebSocket on port 8087',
        'room_id': room_id
    })


# 在 Flask-SocketIO 中处理原生 WebSocket 升级请求
# Flask-SocketIO 会自动处理 WebSocket 握手

# ========== CORS 支持 ==========
# 允许跨域访问，支持从不同域名调用API
ALLOWED_ORIGINS = os.getenv('CORS_ALLOWED_ORIGINS', '*').split(',')

@app.after_request
def add_cors_headers(response):
    """为所有响应添加 CORS 头"""
    origin = request.headers.get('Origin', '*')
    
    # 检查origin是否在允许列表中
    if '*' in ALLOWED_ORIGINS or origin in ALLOWED_ORIGINS:
        response.headers['Access-Control-Allow-Origin'] = origin
    else:
        response.headers['Access-Control-Allow-Origin'] = ALLOWED_ORIGINS[0] if ALLOWED_ORIGINS else '*'
    
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, X-Requested-With'
    response.headers['Access-Control-Max-Age'] = '3600'
    return response

@app.route('/<path:path>', methods=['OPTIONS'])
@app.route('/', methods=['OPTIONS'])
def handle_options(path=None):
    """处理 OPTIONS 预检请求"""
    return '', 204

# 翻译管理器
translation_manager = TranslationManager()

# 存储运行中的翻译服务进程和日志文件
# 结构: {request_id: subprocess.Popen}
translation_processes: Dict[str, subprocess.Popen] = {}
translation_log_files: Dict[str, Any] = {}

# SRS配置
SRS_URL = os.getenv("SRS_URL", "rtmp://localhost:1935")
# HTTP-FLV 播放地址前缀（客户端需要使用这个来拉流）
SRS_HTTP_URL = os.getenv("SRS_HTTP_URL", "http://localhost:8089")

# WebSocket 端口（使用 8086，因为 waitress 不支持 WebSocket）
WS_PORT = int(os.getenv('WS_PORT', 8086))
HTTP_PORT = int(os.getenv('CALLBACK_PORT', 8085))

# Socket.IO 实例（延迟初始化）
sio = None

# 心跳检查定时器
heartbeat_check_thread = None
heartbeat_check_running = False

# 用户说话状态管理
# 结构: {room_id: {user_id: True/False}}
speaking_users: Dict[str, Dict[str, bool]] = {}
speaking_lock = threading.Lock()


# ========== 心跳检查线程 ==========

def heartbeat_check_worker():
    """心跳检查工作线程，每10秒检查一次"""
    global heartbeat_check_running
    
    logger.info("[HeartbeatCheck] Starting heartbeat check worker")
    
    while heartbeat_check_running:
        try:
            # 执行清理检查
            results = translation_manager.check_and_cleanup()
            
            if results:
                for result in results:
                    if result["stopped"]:
                        logger.info(f"[HeartbeatCheck] Cleaned up translation: {result['request_id']}, "
                                  f"reason: {result['stop_reason']}")
                        
                        # 通知客户端翻译已停止
                        notification_service.notify_translation_stopped(
                            room_id=result["room_id"],
                            source_user=result["source_user"],
                            to_lang=result["to_lang"]
                        )
        
        except Exception as e:
            logger.error(f"[HeartbeatCheck] Error in heartbeat check: {e}", exc_info=True)
        
        # 等待10秒
        for _ in range(10):
            if not heartbeat_check_running:
                break
            threading.Event().wait(1)


def start_heartbeat_checker():
    """启动心跳检查线程"""
    global heartbeat_check_thread, heartbeat_check_running
    
    if heartbeat_check_thread is not None and heartbeat_check_thread.is_alive():
        logger.warning("[HeartbeatCheck] Heartbeat checker already running")
        return
    
    heartbeat_check_running = True
    heartbeat_check_thread = threading.Thread(target=heartbeat_check_worker, daemon=True)
    heartbeat_check_thread.start()
    logger.info("[HeartbeatCheck] Heartbeat checker started")


def stop_heartbeat_checker():
    """停止心跳检查线程"""
    global heartbeat_check_running
    
    heartbeat_check_running = False
    if heartbeat_check_thread:
        heartbeat_check_thread.join(timeout=5)


# ========== 原生 WebSocket 广播 HTTP 端点 ==========

@app.route('/broadcast', methods=['POST'])
def broadcast_ws():
    """通过原生 WebSocket 广播消息到房间"""
    from flask import request
    
    data = request.get_json()
    room_id = data.get('room_id', '')
    message = data.get('message', '')
    
    if not room_id or not message:
        return jsonify({'error': 'Missing room_id or message'}), 400
    
    # 异步发送到原生 WebSocket 客户端
    def send_async():
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(_send_to_native_ws(room_id, message))
            finally:
                loop.close()
        except Exception as e:
            logger.warning(f"[WS] Failed to send broadcast: {e}")
    
    threading.Thread(target=send_async, daemon=True).start()
    
    return jsonify({'status': 'ok', 'sent': True})


async def _send_to_native_ws(room_id: str, message: str):
    """异步向原生 WebSocket 房间发送消息"""
    import asyncio
    
    # 获取所有连接的 websocket
    with native_ws_lock:
        if room_id not in native_ws_connections:
            return
        connections = list(native_ws_connections[room_id])
    
    # 并发发送
    if connections:
        await asyncio.gather(
            *[ws.send(message) for ws in connections],
            return_exceptions=True
        )
        logger.info(f"[WS] Broadcast to {len(connections)} native WS clients in room {room_id}")


@app.route('/ws/send', methods=['POST'])
def ws_send():
    """发送消息到原生 WebSocket（兼容旧接口）"""
    from flask import request
    
    data = request.get_json()
    room_id = data.get('room_id', '')
    user_id = data.get('user_id', '')
    event_type = data.get('type', '')
    event_data = data.get('data', {})
    
    if not room_id:
        return jsonify({'error': 'Missing room_id'}), 400
    
    # 构建消息
    message = json.dumps({
        'type': event_type,
        'room_id': room_id,
        'user_id': user_id,
        'data': event_data
    }, ensure_ascii=False)
    
    # 异步发送
    def send_async():
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                if user_id:
                    # 发送给特定用户
                    loop.run_until_complete(_send_to_native_ws_user(user_id, message))
                else:
                    # 广播到房间
                    loop.run_until_complete(_send_to_native_ws(room_id, message))
            finally:
                loop.close()
        except Exception as e:
            logger.warning(f"[WS] Failed to send message: {e}")
    
    threading.Thread(target=send_async, daemon=True).start()
    
    return jsonify({'status': 'ok'})


async def _send_to_native_ws_user(user_id: str, message: str):
    """异步向特定用户发送消息"""
    import asyncio
    
    with native_ws_lock:
        all_ws = []
        for room_conns in native_ws_connections.values():
            all_ws.extend(room_conns)
    
    for ws in all_ws:
        if getattr(ws, '_user_id', None) == user_id:
            try:
                await ws.send(message)
                logger.info(f"[WS] Sent message to user {user_id}")
                return
            except Exception as e:
                logger.warning(f"[WS] Failed to send to user {user_id}: {e}")
        heartbeat_check_thread = None
    logger.info("[HeartbeatCheck] Heartbeat checker stopped")


def parse_stream_name(stream_name: str) -> dict:
    """解析流名称，提取 room_id、user_id 和流类型

    流名称格式:
    - 原声音频流: {room_id}_{user_id}  (room_id 以 room 开头，如 room1777389806400)
    - 翻译流: {room_id}_{source_user}_to_{lang}

    Returns:
        dict: {
            'type': 'original' | 'translation',
            'room_id': str,
            'user_id': str,
            'to_lang': str (仅翻译流)
        }
    """
    result = {
        'type': None,
        'room_id': None,
        'user_id': None,
        'to_lang': None
    }

    # 检查是否是翻译流（包含 "_to_"）
    if '_to_' in stream_name:
        parts = stream_name.split('_to_')
        if len(parts) == 2:
            prefix = parts[0]
            to_lang = parts[1]
            # 从 prefix 中提取 room_id（prefix 格式为 room_id_source_user）
            room_id, source_user = extract_room_id(prefix)
            result = {
                'type': 'translation',
                'room_id': room_id,
                'user_id': source_user,
                'to_lang': to_lang
            }
    else:
        # 原声音频流: room_id_user_id（room_id 以 room 开头）
        room_id, user_id = extract_room_id(stream_name)
        if room_id and user_id:
            result = {
                'type': 'original',
                'room_id': room_id,
                'user_id': user_id,
                'to_lang': None
            }

    return result


def extract_room_id(stream_name: str) -> tuple:
    """从流名称中提取 room_id 和 user_id。

    支持两种格式：
    1. room_id_user_id: room1777389806400_user_70548 (room_id 无下划线)
       → room_id='room1777389806400', user_id='user_70548'

    2. room_数字_user_id: room_1778408735818_1 (room_id 带下划线分隔)
       → room_id='room_1778408735818', user_id='1'

    Args:
        stream_name: 流名称字符串

    Returns:
        (room_id, user_id) 元组，如果解析失败则返回 (None, None)
    """
    # 找到 'room' 开头部分的位置
    room_prefix_pos = stream_name.find('room')
    if room_prefix_pos == -1:
        return None, None

    # 检查 room 后面是否有下划线 (room_格式)
    first_underscore = stream_name.find('_', room_prefix_pos + 4)

    if first_underscore != -1 and stream_name[room_prefix_pos + 4] == '_':
        # room_格式: room_数字_user_id
        # 找到数字部分结束的位置（下一个下划线）
        second_underscore = stream_name.find('_', first_underscore + 1)
        if second_underscore == -1:
            return None, None

        room_id = stream_name[:second_underscore]
        user_id = stream_name[second_underscore + 1:]
    else:
        # 无下划线格式: room数字_user_id
        if first_underscore == -1:
            return None, None
        room_id = stream_name[:first_underscore]
        user_id = stream_name[first_underscore + 1:]

    if room_id and user_id:
        return room_id, user_id
    return None, None


def start_translation_service(request_id: str, room_id: str, source_user: str, to_lang: str, target_user: str = "", source_lang: str = "auto"):
    """启动指定翻译请求的服务"""
    if request_id in translation_processes:
        logger.warning(f"Translation service for request {request_id} is already running")
        return
    
    # 构建翻译流地址
    stream_name = f"{room_id}_{source_user}_to_{to_lang}"
    
    # 设置环境变量
    env = os.environ.copy()
    env['REQUEST_ID'] = request_id
    env['ROOM_ID'] = room_id
    env['SOURCE_USER'] = source_user
    env['TO_LANG'] = to_lang
    env['FROM_LANG'] = source_lang  # 源语言
    env['TARGET_USER'] = target_user  # 添加目标用户
    env['STREAM_NAME'] = stream_name
    env['SRS_URL'] = SRS_URL
    env['TEXT_SERVER_URL'] = os.getenv("TEXT_SERVER_URL", "http://localhost:8086")  # 文本推送服务地址
    
    # 从.env文件加载百度API密钥
    from dotenv import dotenv_values
    env_values = dotenv_values(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))
    if env_values:
        if env_values.get('BAIDU_API_KEY'):
            env['BAIDU_API_KEY'] = env_values['BAIDU_API_KEY']
        if env_values.get('BAIDU_SECRET_KEY'):
            env['BAIDU_SECRET_KEY'] = env_values['BAIDU_SECRET_KEY']
        # 百度翻译开放平台 App ID（可选，用于MT）
        if env_values.get('BAIDU_MT_APP_ID'):
            env['BAIDU_MT_APP_ID'] = env_values['BAIDU_MT_APP_ID']
        if env_values.get('BAIDU_MT_APP_SECRET'):
            env['BAIDU_MT_APP_SECRET'] = env_values['BAIDU_MT_APP_SECRET']
        # 百度实时语音翻译 WebSocket API 凭证
        if env_values.get('BAIDU_APP_ID'):
            env['BAIDU_APP_ID'] = env_values['BAIDU_APP_ID']
        if env_values.get('BAIDU_APP_KEY'):
            env['BAIDU_APP_KEY'] = env_values['BAIDU_APP_KEY']
    
    try:
        # WebSocket 翻译服务需要 Python 3.7+（requests 库要求）
        python_exe = "/usr/local/python3.11-ssl/bin/python3.11"
        if not os.path.exists(python_exe):
            # 回退到系统 Python
            python_exe = sys.executable
        
        # 设置 PYTHONPATH 确保使用正确的 site-packages
        env['PYTHONPATH'] = '/usr/local/python3.11-ssl/lib/python3.11/site-packages'
        env['PATH'] = f"/usr/local/python3.11-ssl/bin:{env.get('PATH', '')}"

        # 使用 WebSocket 实时翻译服务
        script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'audio_translation_service_websocket.py')
        log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'audio_translation_websocket.log')
        logger.info(f"Starting WebSocket translation service script: {script_path}")

        # 打开日志文件用于记录子进程输出
        log_file = open(log_path, 'a')

        process = subprocess.Popen(
            [python_exe, script_path],
            env=env,
            stdout=log_file,
            stderr=subprocess.STDOUT
        )
        
        translation_processes[request_id] = process
        translation_log_files[request_id] = log_file
        logger.info(f"Started translation service for request: {request_id}, "
                   f"stream: {stream_name}, PID: {process.pid}")
        
    except Exception as e:
        logger.error(f"Failed to start translation service for request {request_id}: {e}", exc_info=True)


def stop_translation_service(request_id: str):
    """停止指定翻译请求的服务"""
    if request_id not in translation_processes:
        logger.warning(f"Translation service for request {request_id} is not running")
        return
    
    process = translation_processes[request_id]
    
    try:
        process.terminate()
        process.wait(timeout=5)
        
        # 关闭日志文件
        if request_id in translation_log_files:
            translation_log_files[request_id].close()
            del translation_log_files[request_id]
        
        del translation_processes[request_id]
        logger.info(f"Stopped translation service for request: {request_id}")
        
    except subprocess.TimeoutExpired:
        process.kill()
        if request_id in translation_log_files:
            translation_log_files[request_id].close()
            del translation_log_files[request_id]
        del translation_processes[request_id]
        logger.warning(f"Force killed translation service for request: {request_id}")
    except Exception as e:
        logger.error(f"Error stopping translation service for request {request_id}: {e}")


# ========== 房间管理接口 ==========

@app.route('/api/v1/room', methods=['POST'])
def create_room():
    """创建房间
    
    请求体:
    {
        "room_id": "room_123",
        "owner_id": "user_001",
        "name": "测试房间"
    }
    
    返回:
    {
        "code": 0,
        "message": "success",
        "data": {
            "room_id": "room_123",
            "name": "测试房间",
            "owner_id": "user_001",
            "created_at": "2024-01-01 10:00:00"
        }
    }
    """
    try:
        data = request.json or {}
        room_id = data.get('room_id', '')
        owner_id = data.get('owner_id', '')
        name = data.get('name', '')
        
        if not room_id or not owner_id:
            return jsonify({
                'code': 400,
                'message': 'Missing required parameters: room_id, owner_id'
            }), 400
        
        # 创建房间
        room = user_manager.create_room(room_id, owner_id, name)
        
        logger.info(f"[API] Created room: {room_id}, owner: {owner_id}")
        
        # 发送 WebSocket 通知
        notification_service.notify_room_created(room_id, owner_id, room.to_dict())
        
        return jsonify({
            'code': 0,
            'message': 'success',
            'data': room.to_dict()
        }), 201
        
    except Exception as e:
        logger.error(f"Error creating room: {e}")
        return jsonify({'code': 500, 'message': str(e)}), 500


@app.route('/api/v1/room/<room_id>', methods=['GET'])
def get_room_info(room_id: str):
    """获取房间信息
    
    返回:
    {
        "code": 0,
        "message": "success",
        "data": {
            "room_id": "room_123",
            "name": "测试房间",
            "owner_id": "user_001",
            "created_at": "2024-01-01 10:00:00",
            "member_count": 5,
            "allow_speak": true
        }
    }
    """
    try:
        room = user_manager.get_room(room_id)
        if not room:
            return jsonify({
                'code': 404,
                'message': f'Room {room_id} not found'
            }), 404
        
        return jsonify({
            'code': 0,
            'message': 'success',
            'data': room.to_dict()
        }), 200
        
    except Exception as e:
        logger.error(f"Error getting room info: {e}")
        return jsonify({'code': 500, 'message': str(e)}), 500


@app.route('/api/v1/rooms', methods=['GET'])
def get_all_rooms():
    """获取所有房间列表
    
    返回:
    {
        "code": 0,
        "message": "success",
        "data": {
            "rooms": [
                {
                    "room_id": "room_123",
                    "owner_id": "user_001",
                    "member_count": 5,
                    "created_at": "2024-01-01 10:00:00",
                    "allow_speak": true,
                    "members": [...]
                },
                ...
            ],
            "total": 10
        }
    }
    """
    try:
        rooms = user_manager.get_all_rooms()
        room_list = [room.to_dict() for room in rooms]
        
        return jsonify({
            'code': 0,
            'message': 'success',
            'data': {
                'rooms': room_list,
                'total': len(room_list)
            }
        }), 200
        
    except Exception as e:
        logger.error(f"Error getting all rooms: {e}")
        return jsonify({'code': 500, 'message': str(e)}), 500


@app.route('/api/v1/room/<room_id>', methods=['DELETE'])
def delete_room(room_id: str):
    """删除房间
    
    请求参数:
        operator_id: 操作者ID（必须是群主）
    
    返回:
    {
        "code": 0,
        "message": "success"
    }
    """
    try:
        operator_id = request.args.get('operator_id', '')
        
        room = user_manager.get_room(room_id)
        if not room:
            return jsonify({
                'code': 404,
                'message': f'Room {room_id} not found'
            }), 404
        
        # 只有群主可以删除房间
        if operator_id != room.owner_id:
            return jsonify({
                'code': 403,
                'message': 'Permission denied: only room owner can delete room'
            }), 403
        
        user_manager.delete_room(room_id)
        logger.info(f"[API] Deleted room: {room_id}")
        
        # 发送 WebSocket 通知
        notification_service.notify_room_deleted(room_id, operator_id)
        
        return jsonify({
            'code': 0,
            'message': 'success'
        }), 200
        
    except Exception as e:
        logger.error(f"Error deleting room: {e}")
        return jsonify({'code': 500, 'message': str(e)}), 500


@app.route('/api/v1/room/<room_id>/join', methods=['POST'])
def join_room(room_id: str):
    """用户加入房间
    
    请求体:
    {
        "user_id": "user_002",
        "role": "member"  # 可选: owner, admin, member, guest
    }
    
    返回:
    {
        "code": 0,
        "message": "success",
        "data": {
            "user_id": "user_002",
            "room_id": "room_123",
            "role": "member",
            "joined_at": "2024-01-01 10:00:00"
        }
    }
    """
    try:
        data = request.json or {}
        user_id = data.get('user_id', '')
        role_str = data.get('role', 'member')
        
        if not user_id:
            return jsonify({
                'code': 400,
                'message': 'Missing required parameter: user_id'
            }), 400
        
        # 转换角色字符串
        try:
            role = UserRole(role_str)
        except ValueError:
            role = UserRole.MEMBER
        
        # 加入房间
        try:
            user = user_manager.join_room(room_id, user_id, role)
        except ValueError as e:
            return jsonify({
                'code': 400,
                'message': str(e)
            }), 400
        
        logger.info(f"[API] User {user_id} joined room {room_id}")
        
        # 发送 WebSocket 通知
        notification_service.notify_member_joined(room_id, user_id, user.to_dict())
        
        return jsonify({
            'code': 0,
            'message': 'success',
            'data': user.to_dict()
        }), 200
        
    except Exception as e:
        logger.error(f"Error joining room: {e}")
        return jsonify({'code': 500, 'message': str(e)}), 500


@app.route('/api/v1/room/<room_id>/leave', methods=['POST'])
def leave_room(room_id: str):
    """用户离开房间
    
    请求体:
    {
        "user_id": "user_002"
    }
    
    返回:
    {
        "code": 0,
        "message": "success"
    }
    """
    try:
        data = request.json or {}
        user_id = data.get('user_id', '')
        
        if not user_id:
            return jsonify({
                'code': 400,
                'message': 'Missing required parameter: user_id'
            }), 400
        
        if not user_manager.leave_room(room_id, user_id):
            return jsonify({
                'code': 404,
                'message': f'User {user_id} not found in room {room_id}'
            }), 404
        
        logger.info(f"[API] User {user_id} left room {room_id}")
        
        # 发送 WebSocket 通知
        notification_service.notify_member_left(room_id, user_id)
        
        return jsonify({
            'code': 0,
            'message': 'success'
        }), 200
        
    except Exception as e:
        logger.error(f"Error leaving room: {e}")
        return jsonify({'code': 500, 'message': str(e)}), 500


@app.route('/api/v1/room/<room_id>/member/<user_id>/role', methods=['PUT'])
def update_member_role(room_id: str, user_id: str):
    """更新成员角色
    
    请求体:
    {
        "operator_id": "owner_user_001",
        "role": "admin"
    }
    
    返回:
    {
        "code": 0,
        "message": "success",
        "data": {
            "user_id": "user_002",
            "role": "admin"
        }
    }
    """
    try:
        data = request.json or {}
        operator_id = data.get('operator_id', '')
        role_str = data.get('role', '')
        
        if not operator_id or not role_str:
            return jsonify({
                'code': 400,
                'message': 'Missing required parameters: operator_id, role'
            }), 400
        
        # 检查操作者是否是群主
        room = user_manager.get_room(room_id)
        if not room:
            return jsonify({
                'code': 404,
                'message': f'Room {room_id} not found'
            }), 404
        
        if operator_id != room.owner_id:
            return jsonify({
                'code': 403,
                'message': 'Permission denied: only room owner can change member roles'
            }), 403
        
        # 转换角色字符串
        try:
            new_role = UserRole(role_str)
        except ValueError:
            return jsonify({
                'code': 400,
                'message': f'Invalid role: {role_str}'
            }), 400
        
        if not user_manager.update_user_role(room_id, user_id, new_role):
            return jsonify({
                'code': 404,
                'message': f'User {user_id} not found in room {room_id}'
            }), 404
        
        member = user_manager.get_member(room_id, user_id)
        logger.info(f"[API] User {operator_id} changed role of {user_id} to {role_str}")
        
        # 发送 WebSocket 通知
        notification_service.notify_member_role_changed(room_id, user_id, member.role.value, role_str, operator_id)
        
        return jsonify({
            'code': 0,
            'message': 'success',
            'data': {
                'user_id': user_id,
                'role': member.role.value
            }
        }), 200
        
    except Exception as e:
        logger.error(f"Error updating member role: {e}")
        return jsonify({'code': 500, 'message': str(e)}), 500


# ========== 用户管理接口 ==========

@app.route('/api/v1/room/<room_id>/members', methods=['GET'])
def get_room_members(room_id: str):
    """获取房间成员列表
    
    路径参数:
        room_id: 房间ID
    
    查询参数:
        role: 可选，按角色筛选 (owner, admin, member, guest)
        status: 可选，按状态筛选 (normal, muted, mic_off)
    
    返回:
    {
        "code": 0,
        "message": "success",
        "data": {
            "room_id": "room_123",
            "owner_id": "user_001",
            "member_count": 5,
            "allow_speak": true,
            "members": [
                {
                    "user_id": "user_001",
                    "role": "owner",
                    "status": "normal",
                    "publish_allowed": true,
                    "joined_at": "2024-01-01 10:00:00",
                    "last_active": "2024-01-01 10:30:00"
                },
                {
                    "user_id": "user_002",
                    "role": "admin",
                    "status": "normal",
                    "publish_allowed": true,
                    "joined_at": "2024-01-01 10:05:00",
                    "last_active": "2024-01-01 10:25:00"
                }
            ]
        }
    }
    """
    try:
        # 获取房间信息
        room = user_manager.get_room(room_id)
        if not room:
            return jsonify({
                'code': 404,
                'message': f'Room {room_id} not found'
            }), 404
        
        # 获取所有成员
        members = user_manager.get_room_members(room_id)
        
        # 过滤参数
        role_filter = request.args.get('role')
        status_filter = request.args.get('status')
        
        filtered_members = []
        for member in members:
            member_dict = member.to_dict()
            
            # 按角色过滤
            if role_filter and member_dict['role'] != role_filter:
                continue
            
            # 按状态过滤
            if status_filter and member_dict['status'] != status_filter:
                continue
            
            filtered_members.append(member_dict)
        
        return jsonify({
            'code': 0,
            'message': 'success',
            'data': {
                'room_id': room_id,
                'owner_id': room.owner_id,
                'member_count': len(filtered_members),
                'allow_speak': room.allow_speak,
                'members': filtered_members
            }
        }), 200
        
    except Exception as e:
        logger.error(f"Error getting room members: {e}")
        return jsonify({'code': 500, 'message': str(e)}), 500


@app.route('/api/v1/room/<room_id>/member/<user_id>', methods=['GET'])
def get_member_info(room_id: str, user_id: str):
    """获取成员详细信息
    
    返回:
    {
        "code": 0,
        "message": "success",
        "data": {
            "user_id": "user_001",
            "room_id": "room_123",
            "role": "owner",
            "status": "normal",
            "publish_allowed": true,
            "joined_at": "2024-01-01 10:00:00",
            "last_active": "2024-01-01 10:30:00"
        }
    }
    """
    try:
        member = user_manager.get_member(room_id, user_id)
        if not member:
            return jsonify({
                'code': 404,
                'message': f'Member {user_id} not found in room {room_id}'
            }), 404
        
        return jsonify({
            'code': 0,
            'message': 'success',
            'data': member.to_dict()
        }), 200
        
    except Exception as e:
        logger.error(f"Error getting member info: {e}")
        return jsonify({'code': 500, 'message': str(e)}), 500


# ========== 禁言/禁麦接口 ==========

@app.route('/api/v1/room/<room_id>/member/<user_id>/mute', methods=['POST'])
def mute_member(room_id: str, user_id: str):
    """禁言用户
    
    请求体:
    {
        "operator_id": "admin_user_001"  # 操作者ID（必须是群主或管理员）
    }
    
    返回:
    {
        "code": 0,
        "message": "success",
        "data": {
            "user_id": "user_002",
            "status": "muted",
            "publish_allowed": false
        }
    }
    """
    try:
        data = request.json or {}
        operator_id = data.get('operator_id', '')
        
        if not operator_id:
            return jsonify({
                'code': 400,
                'message': 'Missing required parameter: operator_id'
            }), 400
        
        # 检查操作者权限
        if not user_manager.can_manage_members(room_id, operator_id):
            return jsonify({
                'code': 403,
                'message': 'Permission denied: only owner or admin can mute members'
            }), 403
        
        # 执行禁言
        if not user_manager.mute_user(room_id, user_id):
            return jsonify({
                'code': 404,
                'message': f'User {user_id} not found in room {room_id}'
            }), 404
        
        member = user_manager.get_member(room_id, user_id)
        logger.info(f"[API] User {operator_id} muted {user_id} in room {room_id}")
        
        # 发送 WebSocket 通知
        notification_service.notify_member_muted(room_id, user_id, operator_id)
        
        return jsonify({
            'code': 0,
            'message': 'success',
            'data': {
                'user_id': user_id,
                'status': member.status.value,
                'publish_allowed': member.publish_allowed
            }
        }), 200
        
    except Exception as e:
        logger.error(f"Error muting user: {e}")
        return jsonify({'code': 500, 'message': str(e)}), 500


@app.route('/api/v1/room/<room_id>/member/<user_id>/unmute', methods=['POST'])
def unmute_member(room_id: str, user_id: str):
    """解除禁言
    
    请求体:
    {
        "operator_id": "admin_user_001"  # 操作者ID
    }
    
    返回:
    {
        "code": 0,
        "message": "success",
        "data": {
            "user_id": "user_002",
            "status": "normal",
            "publish_allowed": true
        }
    }
    """
    try:
        data = request.json or {}
        operator_id = data.get('operator_id', '')
        
        if not operator_id:
            return jsonify({
                'code': 400,
                'message': 'Missing required parameter: operator_id'
            }), 400
        
        # 检查操作者权限
        if not user_manager.can_manage_members(room_id, operator_id):
            return jsonify({
                'code': 403,
                'message': 'Permission denied: only owner or admin can unmute members'
            }), 403
        
        # 执行解除禁言
        if not user_manager.unmute_user(room_id, user_id):
            return jsonify({
                'code': 404,
                'message': f'User {user_id} not found in room {room_id}'
            }), 404
        
        member = user_manager.get_member(room_id, user_id)
        logger.info(f"[API] User {operator_id} unmuted {user_id} in room {room_id}")
        
        # 发送 WebSocket 通知
        notification_service.notify_member_unmuted(room_id, user_id, operator_id)
        
        return jsonify({
            'code': 0,
            'message': 'success',
            'data': {
                'user_id': user_id,
                'status': member.status.value,
                'publish_allowed': member.publish_allowed
            }
        }), 200
        
    except Exception as e:
        logger.error(f"Error unmuting user: {e}")
        return jsonify({'code': 500, 'message': str(e)}), 500


@app.route('/api/v1/room/<room_id>/member/<user_id>/mic/disable', methods=['POST'])
def disable_member_mic(room_id: str, user_id: str):
    """禁麦（禁止使用麦克风发布）
    
    请求体:
    {
        "operator_id": "admin_user_001"  # 操作者ID
    }
    
    返回:
    {
        "code": 0,
        "message": "success",
        "data": {
            "user_id": "user_002",
            "status": "mic_off",
            "publish_allowed": false
        }
    }
    """
    try:
        data = request.json or {}
        operator_id = data.get('operator_id', '')
        
        if not operator_id:
            return jsonify({
                'code': 400,
                'message': 'Missing required parameter: operator_id'
            }), 400
        
        # 检查操作者权限
        if not user_manager.can_manage_members(room_id, operator_id):
            return jsonify({
                'code': 403,
                'message': 'Permission denied: only owner or admin can disable mic'
            }), 403
        
        # 执行禁麦
        if not user_manager.disable_mic(room_id, user_id):
            return jsonify({
                'code': 404,
                'message': f'User {user_id} not found in room {room_id}'
            }), 404
        
        member = user_manager.get_member(room_id, user_id)
        logger.info(f"[API] User {operator_id} disabled mic for {user_id} in room {room_id}")
        
        # 发送 WebSocket 通知
        notification_service.notify_member_mic_disabled(room_id, user_id, operator_id)
        
        return jsonify({
            'code': 0,
            'message': 'success',
            'data': {
                'user_id': user_id,
                'status': member.status.value,
                'publish_allowed': member.publish_allowed
            }
        }), 200
        
    except Exception as e:
        logger.error(f"Error disabling mic: {e}")
        return jsonify({'code': 500, 'message': str(e)}), 500


@app.route('/api/v1/room/<room_id>/member/<user_id>/mic/enable', methods=['POST'])
def enable_member_mic(room_id: str, user_id: str):
    """解除禁麦
    
    请求体:
    {
        "operator_id": "admin_user_001"  # 操作者ID
    }
    
    返回:
    {
        "code": 0,
        "message": "success",
        "data": {
            "user_id": "user_002",
            "status": "normal",
            "publish_allowed": true
        }
    }
    """
    try:
        data = request.json or {}
        operator_id = data.get('operator_id', '')
        
        if not operator_id:
            return jsonify({
                'code': 400,
                'message': 'Missing required parameter: operator_id'
            }), 400
        
        # 检查操作者权限
        if not user_manager.can_manage_members(room_id, operator_id):
            return jsonify({
                'code': 403,
                'message': 'Permission denied: only owner or admin can enable mic'
            }), 403
        
        # 执行解除禁麦
        if not user_manager.enable_mic(room_id, user_id):
            return jsonify({
                'code': 404,
                'message': f'User {user_id} not found in room {room_id}'
            }), 404
        
        member = user_manager.get_member(room_id, user_id)
        logger.info(f"[API] User {operator_id} enabled mic for {user_id} in room {room_id}")
        
        # 发送 WebSocket 通知
        notification_service.notify_member_mic_enabled(room_id, user_id, operator_id)
        
        return jsonify({
            'code': 0,
            'message': 'success',
            'data': {
                'user_id': user_id,
                'status': member.status.value,
                'publish_allowed': member.publish_allowed
            }
        }), 200
        
    except Exception as e:
        logger.error(f"Error enabling mic: {e}")
        return jsonify({'code': 500, 'message': str(e)}), 500


@app.route('/api/v1/room/<room_id>/mute-all', methods=['POST'])
def mute_all_members(room_id: str):
    """全体禁言（除群主外）
    
    请求体:
    {
        "operator_id": "owner_user_001"  # 操作者ID（必须是群主）
    }
    
    返回:
    {
        "code": 0,
        "message": "success",
        "data": {
            "room_id": "room_123",
            "allow_speak": false,
            "muted_count": 5
        }
    }
    """
    try:
        data = request.json or {}
        operator_id = data.get('operator_id', '')
        
        if not operator_id:
            return jsonify({
                'code': 400,
                'message': 'Missing required parameter: operator_id'
            }), 400
        
        # 检查是否是群主
        room = user_manager.get_room(room_id)
        if not room:
            return jsonify({
                'code': 404,
                'message': f'Room {room_id} not found'
            }), 404
        
        if operator_id != room.owner_id:
            return jsonify({
                'code': 403,
                'message': 'Permission denied: only room owner can mute all members'
            }), 403
        
        # 执行全体禁言
        muted_count = user_manager.mute_all(room_id)
        logger.info(f"[API] User {operator_id} muted all {muted_count} members in room {room_id}")
        
        # 发送 WebSocket 通知
        notification_service.notify_room_muted_all(room_id, operator_id, muted_count)
        
        return jsonify({
            'code': 0,
            'message': 'success',
            'data': {
                'room_id': room_id,
                'allow_speak': False,
                'muted_count': muted_count
            }
        }), 200
        
    except Exception as e:
        logger.error(f"Error muting all: {e}")
        return jsonify({'code': 500, 'message': str(e)}), 500


@app.route('/api/v1/room/<room_id>/unmute-all', methods=['POST'])
def unmute_all_members(room_id: str):
    """解除全体禁言
    
    请求体:
    {
        "operator_id": "owner_user_001"  # 操作者ID（必须是群主）
    }
    
    返回:
    {
        "code": 0,
        "message": "success",
        "data": {
            "room_id": "room_123",
            "allow_speak": true,
            "unmuted_count": 5
        }
    }
    """
    try:
        data = request.json or {}
        operator_id = data.get('operator_id', '')
        
        if not operator_id:
            return jsonify({
                'code': 400,
                'message': 'Missing required parameter: operator_id'
            }), 400
        
        # 检查是否是群主
        room = user_manager.get_room(room_id)
        if not room:
            return jsonify({
                'code': 404,
                'message': f'Room {room_id} not found'
            }), 404
        
        if operator_id != room.owner_id:
            return jsonify({
                'code': 403,
                'message': 'Permission denied: only room owner can unmute all members'
            }), 403
        
        # 执行解除全体禁言
        unmuted_count = user_manager.unmute_all(room_id)
        logger.info(f"[API] User {operator_id} unmuted all members in room {room_id}")
        
        # 发送 WebSocket 通知
        notification_service.notify_room_unmuted_all(room_id, operator_id, unmuted_count)
        
        return jsonify({
            'code': 0,
            'message': 'success',
            'data': {
                'room_id': room_id,
                'allow_speak': True,
                'unmuted_count': unmuted_count
            }
        }), 200
        
    except Exception as e:
        logger.error(f"Error unmuting all: {e}")
        return jsonify({'code': 500, 'message': str(e)}), 500


@app.route('/api/v1/room/<room_id>/member/<user_id>/kick', methods=['DELETE'])
def kick_member(room_id: str, user_id: str):
    """踢出用户
    
    请求参数:
        operator_id: 操作者ID（群主或管理员）
    
    返回:
    {
        "code": 0,
        "message": "success"
    }
    """
    try:
        operator_id = request.args.get('operator_id', '')
        
        if not operator_id:
            return jsonify({
                'code': 400,
                'message': 'Missing required parameter: operator_id'
            }), 400
        
        # 检查踢人权限
        if not user_manager.can_kick(room_id, operator_id, user_id):
            return jsonify({
                'code': 403,
                'message': 'Permission denied: cannot kick this user'
            }), 403
        
        # 执行踢人
        if not user_manager.leave_room(room_id, user_id):
            return jsonify({
                'code': 404,
                'message': f'User {user_id} not found in room {room_id}'
            }), 404
        
        logger.info(f"[API] User {operator_id} kicked {user_id} from room {room_id}")
        
        # 发送 WebSocket 通知
        notification_service.notify_member_kicked(room_id, user_id, operator_id)
        
        return jsonify({
            'code': 0,
            'message': 'success'
        }), 200
        
    except Exception as e:
        logger.error(f"Error kicking user: {e}")
        return jsonify({'code': 500, 'message': str(e)}), 500


@app.route('/api/v1/room/<room_id>/check-publish', methods=['GET'])
def check_publish_permission(room_id: str):
    """检查用户是否可以发布（发言）
    
    查询参数:
        user_id: 用户ID
    
    返回:
    {
        "code": 0,
        "message": "success",
        "data": {
            "user_id": "user_001",
            "can_publish": true,
            "status": "normal"
        }
    }
    """
    try:
        user_id = request.args.get('user_id', '')
        
        if not user_id:
            return jsonify({
                'code': 400,
                'message': 'Missing required parameter: user_id'
            }), 400
        
        can_publish = user_manager.can_publish(room_id, user_id)
        member = user_manager.get_member(room_id, user_id)
        
        return jsonify({
            'code': 0,
            'message': 'success',
            'data': {
                'user_id': user_id,
                'can_publish': can_publish,
                'status': member.status.value if member else 'unknown'
            }
        }), 200
        
    except Exception as e:
        logger.error(f"Error checking publish permission: {e}")
        return jsonify({'code': 500, 'message': str(e)}), 500


# ========== 敲门接口 ==========

@app.route('/api/v1/room/<room_id>/knock', methods=['POST'])
def knock_room(room_id: str):
    """敲门 - 请求加入房间
    
    请求体:
    {
        "user_id": "visitor_001",      # 敲门者ID
        "message": "想加入聊天"         # 可选留言
    }
    
    返回:
    {
        "code": 0,
        "message": "success",
        "data": {
            "room_id": "room_123",
            "owner_id": "owner_001",
            "knocker_id": "visitor_001"
        }
    }
    """
    try:
        data = request.json or {}
        knocker_id = data.get('user_id', '')
        message = data.get('message', '')
        
        if not knocker_id:
            return jsonify({
                'code': 400,
                'message': 'Missing required parameter: user_id'
            }), 400
        
        # 检查房间是否存在
        room = user_manager.get_room(room_id)
        if not room:
            return jsonify({
                'code': 404,
                'message': f'Room {room_id} not found'
            }), 404
        
        # 检查用户是否已经在房间内
        if user_manager.get_member(room_id, knocker_id):
            return jsonify({
                'code': 400,
                'message': 'You are already in this room'
            }), 400
        
        owner_id = room.owner_id
        logger.info(f"[API] User {knocker_id} knocked on room {room_id}, owner: {owner_id}")
        
        # 发送敲门通知给房主
        notification_service.notify_room_knock(
            room_id=room_id,
            knocker_id=knocker_id,
            owner_id=owner_id,
            knocker_info={
                'message': message,
                'room_name': room.name
            }
        )
        
        return jsonify({
            'code': 0,
            'message': 'success',
            'data': {
                'room_id': room_id,
                'owner_id': owner_id,
                'knocker_id': knocker_id
            }
        }), 200
        
    except Exception as e:
        logger.error(f"Error knocking room: {e}")
        return jsonify({'code': 500, 'message': str(e)}), 500


@app.route('/api/v1/room/<room_id>/knock/accept', methods=['POST'])
def accept_knock(room_id: str):
    """接受敲门 - 房主批准用户加入
    
    请求体:
    {
        "operator_id": "owner_001",      # 操作者ID（必须是房主或管理员）
        "knocker_id": "visitor_001",     # 被接受的敲门者ID
        "role": "member"                  # 分配的角色（可选）
    }
    
    返回:
    {
        "code": 0,
        "message": "success"
    }
    """
    try:
        data = request.json or {}
        operator_id = data.get('operator_id', '')
        knocker_id = data.get('knocker_id', '')
        role_str = data.get('role', 'member')
        
        if not operator_id or not knocker_id:
            return jsonify({
                'code': 400,
                'message': 'Missing required parameters: operator_id, knocker_id'
            }), 400
        
        # 检查操作者权限
        if not user_manager.can_manage_members(room_id, operator_id):
            return jsonify({
                'code': 403,
                'message': 'Permission denied: only owner or admin can accept knock'
            }), 403
        
        # 检查房间是否存在
        room = user_manager.get_room(room_id)
        if not room:
            return jsonify({
                'code': 404,
                'message': f'Room {room_id} not found'
            }), 404
        
        # 将用户加入房间
        try:
            role = UserRole(role_str)
        except ValueError:
            role = UserRole.MEMBER
        
        user = user_manager.join_room(room_id, knocker_id, role)
        logger.info(f"[API] Knock accepted: {knocker_id} joined room {room_id} by {operator_id}")
        
        # 通知敲门者申请被接受
        notification_service.notify_knock_accepted(
            room_id=room_id,
            knocker_id=knocker_id,
            operator_id=operator_id
        )
        
        # 通知房间内其他成员
        notification_service.notify_member_joined(room_id, knocker_id, user.to_dict())
        
        return jsonify({
            'code': 0,
            'message': 'success'
        }), 200
        
    except Exception as e:
        logger.error(f"Error accepting knock: {e}")
        return jsonify({'code': 500, 'message': str(e)}), 500


@app.route('/api/v1/room/<room_id>/knock/reject', methods=['POST'])
def reject_knock(room_id: str):
    """拒绝敲门 - 房主拒绝用户加入
    
    请求体:
    {
        "operator_id": "owner_001",      # 操作者ID（必须是房主或管理员）
        "knocker_id": "visitor_001",     # 被拒绝的敲门者ID
        "reason": "房间已满"              # 拒绝原因（可选）
    }
    
    返回:
    {
        "code": 0,
        "message": "success"
    }
    """
    try:
        data = request.json or {}
        operator_id = data.get('operator_id', '')
        knocker_id = data.get('knocker_id', '')
        reason = data.get('reason', '')
        
        if not operator_id or not knocker_id:
            return jsonify({
                'code': 400,
                'message': 'Missing required parameters: operator_id, knocker_id'
            }), 400
        
        # 检查操作者权限
        if not user_manager.can_manage_members(room_id, operator_id):
            return jsonify({
                'code': 403,
                'message': 'Permission denied: only owner or admin can reject knock'
            }), 403
        
        logger.info(f"[API] Knock rejected: {knocker_id} rejected from room {room_id} by {operator_id}, reason: {reason}")
        
        # 通知敲门者申请被拒绝
        notification_service.notify_knock_rejected(
            room_id=room_id,
            knocker_id=knocker_id,
            operator_id=operator_id,
            reason=reason
        )
        
        return jsonify({
            'code': 0,
            'message': 'success'
        }), 200
        
    except Exception as e:
        logger.error(f"Error rejecting knock: {e}")
        return jsonify({'code': 500, 'message': str(e)}), 500


# ========== 翻译管理接口 ==========

@app.route('/api/v1/translation/request', methods=['POST'])
@app.route('/api/v1/translation/start', methods=['POST'])
def translation_request():
    """申请翻译接口
    
    请求体:
    {
        "room_id": "room1",
        "source_user": "A",      # 说话人用户ID
        "target_user": "B",      # 听翻译的用户ID
        "to_lang": "zh"          # 目标语言
    }
    
    返回:
    {
        "code": 0,
        "message": "success",
        "data": {
            "request_id": "xxx",
            "stream_url": "rtmp://host/live/room1_A_to_zh"
        }
    }
    """
    try:
        data = request.json or {}
        room_id = data.get('room_id', '')
        source_user = data.get('source_user', '')
        target_user = data.get('target_user', '')
        to_lang = data.get('to_lang', 'zh')
        source_lang = data.get('source_lang', 'auto')  # 源语言，默认为 auto
        
        if not room_id or not source_user or not target_user:
            return jsonify({
                'code': 400,
                'message': 'Missing required parameters: room_id, source_user, target_user'
            }), 400
        
        # 生成请求ID
        request_id = str(uuid.uuid4())
        
        # 构建翻译流地址
        stream_name = f"{room_id}_{source_user}_to_{to_lang}"
        stream_url = f"{SRS_URL}/live/{stream_name}.flv"
        # 客户端使用的 HTTP-FLV 播放地址
        play_url = f"{SRS_HTTP_URL}/live/{stream_name}.flv"
        
        # 检查是否需要翻译（源语言=目标语言的情况需要翻译服务运行后才能知道）
        # 这里先创建请求，翻译服务启动后会检测实际源语言
        # 如果源语言=目标语言，翻译服务会自动停止推流
        
        # 创建翻译请求
        translation_req = TranslationRequest(
            request_id=request_id,
            room_id=room_id,
            source_user=source_user,
            target_user=target_user,
            to_lang=to_lang,
            source_lang=source_lang,  # 源语言
            status=TranslationStatus.PENDING,
            stream_url=stream_url
        )
        
        # 添加到管理器
        if not translation_manager.add_request(translation_req):
            # 已存在相同请求
            logger.warning(f"Translation request already exists: room={room_id}, source={source_user}, to_lang={to_lang}")
            return jsonify({
                'code': 409,
                'message': 'Translation request already exists'
            }), 409
        
        logger.info(f"[Translation] Creating request: request_id={request_id}, room={room_id}, "
                   f"source={source_user}, target={target_user}, to_lang={to_lang}, stream_url={stream_url}")
        
        # 启动翻译服务
        start_translation_service(request_id, room_id, source_user, to_lang, target_user, source_lang)
        
        # 更新状态为活跃
        translation_manager.update_status(request_id, TranslationStatus.ACTIVE)
        
        logger.info(f"[Translation] Request created successfully: {request_id}")
        
        return jsonify({
            'code': 0,
            'message': 'success',
            'data': {
                'request_id': request_id,
                'stream_url': stream_url,
                'play_url': play_url  # 客户端直接使用的 HTTP-FLV 地址
            }
        }), 200
        
    except Exception as e:
        logger.error(f"Error handling translation request: {e}")
        return jsonify({'code': 500, 'message': str(e)}), 500


@app.route('/api/v1/translation/cancel', methods=['POST'])
def translation_cancel():
    """取消翻译接口
    
    请求体:
    {
        "request_id": "xxx"  # 要取消的请求ID
    }
    
    或:
    {
        "room_id": "room1",
        "source_user": "A",
        "target_user": "B",
        "to_lang": "zh"
    }
    
    返回:
    {
        "code": 0,
        "message": "success"
    }
    """
    try:
        data = request.json or {}
        request_id = data.get('request_id', '')
        
        if request_id:
            # 通过request_id取消
            logger.info(f"[Translation] Cancel request by request_id: {request_id}")
            request_obj = translation_manager.get_request(request_id)
            if not request_obj:
                return jsonify({
                    'code': 404,
                    'message': 'Request not found'
                }), 404
            
            # 停止翻译服务
            stop_translation_service(request_id)
            
            # 移除请求
            translation_manager.remove_request(request_id)
            
            return jsonify({
                'code': 0,
                'message': 'success'
            }), 200
        
        else:
            # 通过参数取消
            room_id = data.get('room_id', '')
            source_user = data.get('source_user', '')
            target_user = data.get('target_user', '')
            to_lang = data.get('to_lang', '')
            
            if not all([room_id, source_user, target_user, to_lang]):
                return jsonify({
                    'code': 400,
                    'message': 'Missing required parameters'
                }), 400
            
            # 查找请求
            requests = translation_manager.get_all_requests()
            found_request = None
            for req in requests:
                if (req.room_id == room_id and 
                    req.source_user == source_user and 
                    req.target_user == target_user and 
                    req.to_lang == to_lang):
                    found_request = req
                    break
            
            if not found_request:
                return jsonify({
                    'code': 404,
                    'message': 'Request not found'
                }), 404
            
            # 停止翻译服务
            stop_translation_service(found_request.request_id)
            
            # 移除请求
            translation_manager.remove_request(found_request.request_id)
            
            return jsonify({
                'code': 0,
                'message': 'success'
            }), 200
        
    except Exception as e:
        logger.error(f"Error handling translation cancel: {e}")
        return jsonify({'code': 500, 'message': str(e)}), 500


@app.route('/api/v1/translation/streams/<room_id>/<user_id>', methods=['GET'])
def get_user_streams(room_id: str, user_id: str):
    """查询用户的可用流列表
    
    返回:
    {
        "code": 0,
        "message": "success",
        "data": {
            "user_id": "B",
            "streams": [
                {
                    "type": "original",
                    "user_id": "A",
                    "url": "rtmp://host/live/room1_A",
                    "description": "原声音频"
                },
                {
                    "type": "translation",
                    "source_user": "A",
                    "to_lang": "zh",
                    "url": "rtmp://host/live/room1_A_to_zh",
                    "description": "A的中文翻译"
                }
            ]
        }
    }
    """
    try:
        logger.info(f"[API] Getting user streams: room={room_id}, user={user_id}")
        
        # 获取用户可用的流列表
        streams = translation_manager.get_user_streams(room_id, user_id, SRS_URL)
        
        logger.info(f"[API] Found {len(streams)} streams for user {user_id} in room {room_id}")
        
        return jsonify({
            'code': 0,
            'message': 'success',
            'data': {
                'user_id': user_id,
                'streams': streams
            }
        }), 200
        
    except Exception as e:
        logger.error(f"Error getting user streams: {e}")
        return jsonify({'code': 500, 'message': str(e)}), 500


@app.route('/api/v1/translation/requests', methods=['GET'])
def get_all_requests():
    """获取所有翻译请求（调试用）"""
    try:
        requests = translation_manager.get_all_requests()
        return jsonify({
            'code': 0,
            'message': 'success',
            'data': {
                'count': len(requests),
                'requests': [
                    {
                        'request_id': req.request_id,
                        'room_id': req.room_id,
                        'source_user': req.source_user,
                        'target_user': req.target_user,
                        'to_lang': req.to_lang,
                        'status': req.status.value,
                        'stream_url': req.stream_url
                    }
                    for req in requests
                ]
            }
        }), 200
    except Exception as e:
        logger.error(f"Error getting all requests: {e}")
        return jsonify({'code': 500, 'message': str(e)}), 500


@app.route('/api/v1/translation/text/push', methods=['POST'])
def push_translation_text():
    """接收翻译服务推送的翻译文本，转发给客户端
    
    请求体:
    {
        "target_user": "B",           # 目标用户ID
        "request_id": "xxx",          # 翻译请求ID
        "room_id": "room1",           # 房间ID
        "source_user": "A",           # 说话人用户ID
        "original_text": "Hello",     # 原文
        "translated_text": "你好",     # 译文
        "source_lang": "en",          # 源语言
        "target_lang": "zh",          # 目标语言
        "timestamp": 1234567890       # 时间戳
    }
    
    返回:
    {
        "code": 0,
        "message": "success"
    }
    """
    try:
        data = request.json or {}
        target_user = data.get('target_user', '')
        room_id = data.get('room_id', '')
        source_user = data.get('source_user', '')
        original_text = data.get('original_text', '')
        translated_text = data.get('translated_text', '')
        source_lang = data.get('source_lang', '')
        target_lang = data.get('target_lang', '')
        
        if not target_user or not room_id or not source_user:
            return jsonify({
                'code': 400,
                'message': 'Missing required parameters: target_user, room_id, source_user'
            }), 400
        
        logger.info(f"[Translation] Pushing text to {target_user}: '{original_text[:30]}...' -> '{translated_text[:30]}...'")
        
        # 通过通知服务转发给客户端
        notification_service.notify_translation_text(
            room_id=room_id,
            source_user=source_user,
            target_user=target_user,
            original_text=original_text,
            translated_text=translated_text,
            source_lang=source_lang,
            target_lang=target_lang
        )
        
        return jsonify({
            'code': 0,
            'message': 'success'
        }), 200
        
    except Exception as e:
        logger.error(f"Error pushing translation text: {e}")
        return jsonify({'code': 500, 'message': str(e)}), 500


# ========== 原语音识别文字推送接口 ==========

@app.route('/api/v1/original/speech/text/push', methods=['POST'])
def push_original_speech_text():
    """接收翻译服务推送的原语音识别文字，广播给房间所有用户

    请求体:
    {
        "room_id": "room1",
        "source_user": "A",
        "original_text": "Hello",
        "source_lang": "en",
        "timestamp": 1234567890
    }

    返回:
    {
        "code": 0,
        "message": "success"
    }
    """
    try:
        data = request.json or {}
        room_id = data.get('room_id', '')
        source_user = data.get('source_user', '')
        original_text = data.get('original_text', '')
        source_lang = data.get('source_lang', '')

        if not room_id or not source_user:
            return jsonify({
                'code': 400,
                'message': 'Missing required parameters: room_id, source_user'
            }), 400

        logger.info(f"[OriginalSpeech] Pushing to room {room_id}: '{original_text[:30]}...'")

        # 通过通知服务广播给房间所有用户
        notification_service.notify_original_speech_text(
            room_id=room_id,
            source_user=source_user,
            original_text=original_text,
            source_lang=source_lang
        )

        return jsonify({
            'code': 0,
            'message': 'success'
        }), 200

    except Exception as e:
        logger.error(f"Error pushing original speech text: {e}")
        return jsonify({'code': 500, 'message': str(e)}), 500


# ========== 拉流者心跳接口 ==========

@app.route('/api/v1/translation/heartbeat', methods=['POST'])
def translation_heartbeat():
    """拉流者心跳接口
    
    拉流客户端需要定期调用此接口报告心跳，表明仍在拉取翻译流。
    如果超过15秒未收到心跳，服务器将认为该拉流者已断开。
    
    请求体:
    {
        "request_id": "xxx",     # 翻译请求ID
        "puller_id": "user_b",   # 拉流者ID
        "source_stream_active": true  # 拉流端检测到的源流状态（可选）
    }
    
    返回:
    {
        "code": 0,
        "message": "success",
        "data": {
            "received": true,
            "next_heartbeat_in": 5
        }
    }
    """
    try:
        data = request.json or {}
        request_id = data.get('request_id', '')
        puller_id = data.get('puller_id', '')
        source_stream_active = data.get('source_stream_active', None)
        
        if not request_id or not puller_id:
            return jsonify({
                'code': 400,
                'message': 'Missing required parameters: request_id, puller_id'
            }), 400
        
        # 记录心跳
        success = translation_manager.heartbeat_puller(request_id, puller_id)
        
        # 如果客户端报告了源流状态，也更新
        if source_stream_active is not None and success:
            request = translation_manager.get_request(request_id)
            if request:
                translation_manager.update_source_stream_active(
                    request.room_id, 
                    request.source_user, 
                    source_stream_active
                )
        
        logger.debug(f"[Heartbeat] Received: request={request_id}, puller={puller_id}")
        
        return jsonify({
            'code': 0,
            'message': 'success',
            'data': {
                'received': success,
                'next_heartbeat_in': 5
            }
        }), 200
        
    except Exception as e:
        logger.error(f"Error handling heartbeat: {e}")
        return jsonify({'code': 500, 'message': str(e)}), 500


@app.route('/api/v1/translation/register_puller', methods=['POST'])
def register_puller():
    """注册拉流者接口
    
    客户端在开始拉取翻译流时调用此接口。
    
    请求体:
    {
        "request_id": "xxx",     # 翻译请求ID
        "puller_id": "user_b"    # 拉流者ID
    }
    
    返回:
    {
        "code": 0,
        "message": "success"
    }
    """
    try:
        data = request.json or {}
        request_id = data.get('request_id', '')
        puller_id = data.get('puller_id', '')
        
        if not request_id or not puller_id:
            return jsonify({
                'code': 400,
                'message': 'Missing required parameters: request_id, puller_id'
            }), 400
        
        success, is_restored = translation_manager.register_puller(request_id, puller_id)
        
        if is_restored:
            # 翻译服务被自动恢复了，重新启动翻译服务
            req = translation_manager.get_request(request_id)
            if req:
                logger.info(f"[Translation] Auto-restoring translation service for request: {request_id}")
                # 启动翻译服务
                start_translation_service(
                    request_id=request_id,
                    room_id=req.room_id,
                    source_user=req.source_user,
                    to_lang=req.to_lang,
                    target_user=req.target_user,
                    source_lang=getattr(req, 'source_lang', 'auto')
                )
                return jsonify({
                    'code': 0,
                    'message': 'success',
                    'data': {
                        'stream_url': req.stream_url,
                        'restored': True
                    }
                }), 200
        
        if success:
            return jsonify({
                'code': 0,
                'message': 'success'
            }), 200
        else:
            return jsonify({
                'code': 404,
                'message': 'Translation request not found'
            }), 404
            
    except Exception as e:
        logger.error(f"Error registering puller: {e}")
        return jsonify({'code': 500, 'message': str(e)}), 500


@app.route('/api/v1/translation/unregister_puller', methods=['POST'])
def unregister_puller():
    """注销拉流者接口
    
    客户端主动停止拉取翻译流时调用此接口。
    
    请求体:
    {
        "request_id": "xxx",     # 翻译请求ID
        "puller_id": "user_b"    # 拉流者ID
    }
    
    返回:
    {
        "code": 0,
        "message": 'success'
    }
    """
    try:
        data = request.json or {}
        request_id = data.get('request_id', '')
        puller_id = data.get('puller_id', '')
        
        if not request_id or not puller_id:
            return jsonify({
                'code': 400,
                'message': 'Missing required parameters: request_id, puller_id'
            }), 400
        
        success = translation_manager.unregister_puller(request_id, puller_id)
        
        return jsonify({
            'code': 0,
            'message': 'success'
        }), 200
            
    except Exception as e:
        logger.error(f"Error unregistering puller: {e}")
        return jsonify({'code': 500, 'message': str(e)}), 500


@app.route('/api/v1/translation/requests/<request_id>/pullers', methods=['GET'])
def get_request_pullers(request_id: str):
    """获取翻译请求的拉流者列表
    
    返回:
    {
        "code": 0,
        "message": "success",
        "data": {
            "request_id": "xxx",
            "pullers": [
                {
                    "puller_id": "user_b",
                    "last_heartbeat": 1234567890,
                    "seconds_ago": 3,
                    "is_alive": true
                }
            ]
        }
    }
    """
    try:
        pullers = translation_manager.get_pullers(request_id)
        
        return jsonify({
            'code': 0,
            'message': 'success',
            'data': {
                'request_id': request_id,
                'pullers': pullers
            }
        }), 200
        
    except Exception as e:
        logger.error(f"Error getting pullers: {e}")
        return jsonify({'code': 500, 'message': str(e)}), 500


# ========== SRS回调接口 ==========

@app.route('/api/v1/streams/on_publish', methods=['POST'])
def on_publish():
    """处理发布流回调
    
    解析流名称，识别是原声音频流还是翻译流，
    并通知房间成员用户开始说话
    """
    try:
        data = request.json or {}
        stream_name = data.get('stream', '')
        tc_url = data.get('tcUrl', '')
        client_ip = data.get('client_ip', '')

        if not stream_name:
            logger.warning("Received on_publish callback without stream name")
            return jsonify({'code': 0}), 200

        logger.info(f"Received on_publish callback for stream: {stream_name}, tcUrl: {tc_url}, client_ip: {client_ip}")

        # 解析流名称
        parsed = parse_stream_name(stream_name)
        
        if parsed['type'] == 'original':
            # 原声音频流：通知用户开始说话
            room_id = parsed['room_id']
            user_id = parsed['user_id']
            
            if room_id and user_id:
                # 获取房间信息
                room = user_manager.get_room(room_id)
                if not room:
                    logger.warning(f"[Speaking] Room {room_id} does not exist")
                    return jsonify({'code': 0}), 200
                
                # 检查用户是否在房间中
                member = user_manager.get_member(room_id, user_id)
                if member:
                    with speaking_lock:
                        if room_id not in speaking_users:
                            speaking_users[room_id] = {}
                        speaking_users[room_id][user_id] = True
                    
                    stream_url = f"{SRS_URL}/live/{stream_name}"
                    notification_service.notify_user_speaking_start(room_id, user_id, stream_url)
                    
                    # ========== 容错机制：更新源流状态 ==========
                    translation_manager.update_source_stream_active(room_id, user_id, True)
                    
                    logger.info(f"[Speaking] User {user_id} started speaking in room {room_id}")
                else:
                    # 用户不在房间中，打印房间成员列表以便调试
                    member_ids = list(room.members.keys()) if room.members else []
                    logger.warning(f"[Speaking] User {user_id} not in room {room_id}. Room members: {member_ids}. Please call /api/v1/room/{room_id}/join first")
            else:
                logger.warning(f"[Speaking] Could not parse room_id/user_id from stream: {stream_name}")
                
        elif parsed['type'] == 'translation':
            # 翻译流：查找对应的翻译请求，通知目标用户
            user_id = parsed['user_id']
            to_lang = parsed['to_lang']
            
            # 从翻译管理器查找对应的请求
            all_requests = translation_manager.get_all_requests()
            for req in all_requests:
                if req.source_user == user_id and req.to_lang == to_lang:
                    target_user = req.target_user
                    room_id = req.room_id
                    
                    if room_id and target_user:
                        stream_url = f"{SRS_URL}/live/{stream_name}"
                        notification_service.notify_user_speaking_start(room_id, f"{user_id}_translation_{to_lang}", stream_url)
                        logger.info(f"[Speaking] Translation stream started: {user_id} -> {to_lang} in room {room_id}")
                    break
        
        return jsonify({'code': 0}), 200
        
    except Exception as e:
        logger.error(f"Error handling on_publish: {e}", exc_info=True)
        return jsonify({'code': 0}), 200


@app.route('/api/v1/streams/on_unpublish', methods=['POST'])
def on_unpublish():
    """处理停止发布回调
    
    解析流名称，通知房间成员用户停止说话，
    并停止相关的翻译服务
    """
    try:
        data = request.json or {}
        stream_name = data.get('stream', '')
        
        if not stream_name:
            logger.warning("Received on_unpublish callback without stream name")
            return jsonify({'code': 0}), 200
        
        logger.info(f"Received on_unpublish callback for stream: {stream_name}")
        
        # 解析流名称
        parsed = parse_stream_name(stream_name)
        
        if parsed['type'] == 'original':
            # 原声音频流：通知用户停止说话
            room_id = parsed['room_id']
            user_id = parsed['user_id']
            
            if room_id and user_id:
                with speaking_lock:
                    if room_id in speaking_users and user_id in speaking_users[room_id]:
                        del speaking_users[room_id][user_id]
                        if not speaking_users[room_id]:
                            del speaking_users[room_id]
                
                notification_service.notify_user_speaking_stop(room_id, user_id)
                
                # ========== 容错机制：更新源流状态 ==========
                translation_manager.update_source_stream_active(room_id, user_id, False)
                
                logger.info(f"[Speaking] User {user_id} stopped speaking in room {room_id}")
                
        elif parsed['type'] == 'translation':
            # 翻译流：查找并停止翻译服务，通知目标用户
            user_id = parsed['user_id']
            to_lang = parsed['to_lang']
            
            # 停止所有相关的翻译请求
            requests = translation_manager.get_all_requests()
            for req in requests[:]:  # 使用切片复制避免迭代时修改
                if (req.room_id and req.source_user == user_id and req.to_lang == to_lang):
                    stop_translation_service(req.request_id)
                    translation_manager.remove_request(req.request_id)
                    logger.info(f"[Translation] Stopped translation for {user_id} -> {to_lang}")
            
            # 通知停止说话
            # 需要找到 room_id
            for req in requests:
                if req.source_user == user_id and req.to_lang == to_lang:
                    room_id = req.room_id
                    target_user = req.target_user
                    if room_id:
                        notification_service.notify_user_speaking_stop(room_id, f"{user_id}_translation_{to_lang}")
                    break
        
        return jsonify({'code': 0}), 200
        
    except Exception as e:
        logger.error(f"Error handling on_unpublish: {e}", exc_info=True)
        return jsonify({'code': 0}), 200


@app.route('/api/v1/streams/on_play', methods=['POST'])
def on_play():
    """处理播放流回调（WebRTC拉流时SRS调用）

    此接口用于验证播放权限，可以在这里添加业务逻辑。
    如果返回非0 code或非200状态码，SRS会拒绝WebRTC连接。
    """
    try:
        data = request.json or {}
        stream_name = data.get('stream', '')
        tc_url = data.get('tcUrl', '')
        client_ip = data.get('client_ip', '')
        client_id = data.get('client_id', '')

        logger.info(f"Received on_play callback for stream: {stream_name}, tcUrl: {tc_url}, client_ip: {client_ip}, client_id: {client_id}")

        # 解析流名称获取房间信息
        parsed = parse_stream_name(stream_name) if stream_name else {}

        if parsed.get('type') == 'translation':
            # 翻译流：检查目标用户是否在房间中
            room_id = parsed.get('room_id')
            target_user = parsed.get('target_user')
            if room_id and target_user:
                member = user_manager.get_member(room_id, target_user)
                if member:
                    logger.info(f"[OnPlay] User {target_user} authorized to play translation stream in room {room_id}")
                else:
                    logger.warning(f"[OnPlay] User {target_user} not in room {room_id}, but allowing play")
            
            # 记录翻译流拉取日志
            logger.info(f"[TranslationPlay] Client {client_ip} started playing translation stream: {stream_name}")

        # 返回0表示允许播放
        return jsonify({'code': 0}), 200

    except Exception as e:
        logger.error(f"Error handling on_play: {e}", exc_info=True)
        return jsonify({'code': 0}), 200


@app.route('/api/v1/streams/on_stop', methods=['POST'])
def on_stop():
    """处理停止播放回调（WebRTC拉流停止时SRS调用）

    通知用户停止拉流。
    """
    try:
        data = request.json or {}
        stream_name = data.get('stream', '')
        client_ip = data.get('client_ip', '')

        logger.info(f"Received on_stop callback for stream: {stream_name}, client_ip: {client_ip}")

        # 解析流名称获取房间信息
        parsed = parse_stream_name(stream_name) if stream_name else {}
        
        if parsed.get('type') == 'translation':
            room_id = parsed.get('room_id')
            target_user = parsed.get('target_user')
            logger.info(f"[TranslationStop] Client {client_ip} stopped playing translation stream: {stream_name}")

        return jsonify({'code': 0}), 200

    except Exception as e:
        logger.error(f"Error handling on_stop: {e}", exc_info=True)
        return jsonify({'code': 0}), 200


@app.route('/api/v1/streams/status', methods=['GET'])
def get_status():
    """获取翻译服务状态"""
    status = {
        'active_requests': len(translation_processes),
        'processes': list(translation_processes.keys())
    }
    return jsonify(status), 200


@app.route('/api/v1/streams/translation_stats', methods=['GET'])
def get_translation_stats():
    """获取翻译流统计信息
    
    从SRS API获取翻译流的详细统计，包括：
    - send_bytes: 发送字节数
    - recv_bytes: 接收字节数
    - clients: 客户端数量
    - audio: 音频编码信息
    """
    try:
        # 从SRS获取流统计
        srs_api = os.getenv('SRS_API', 'http://127.0.0.1:1985')
        resp = requests.get(f"{srs_api}/api/v1/streams/", timeout=5)
        if resp.status_code != 200:
            return jsonify({'code': 1, 'message': 'Failed to get SRS stats'}), 500
        
        data = resp.json()
        streams = data.get('streams', [])
        
        # 筛选翻译流
        translation_streams = []
        for stream in streams:
            name = stream.get('name', '')
            if '_to_' in name:
                translation_streams.append({
                    'name': name,
                    'url': stream.get('url', ''),
                    'vhost': stream.get('vhost', ''),
                    'clients': stream.get('clients', 0),
                    'send_bytes': stream.get('send_bytes', 0),
                    'recv_bytes': stream.get('recv_bytes', 0),
                    'kbps': stream.get('kbps', {}),
                    'publish': stream.get('publish', {}),
                    'audio': stream.get('audio', None)
                })
        
        return jsonify({
            'code': 0,
            'message': 'success',
            'data': {
                'translation_streams': translation_streams,
                'total': len(translation_streams)
            }
        }), 200
        
    except Exception as e:
        logger.error(f"Error getting translation stats: {e}")
        return jsonify({'code': 1, 'message': str(e)}), 500


@app.route('/api/v1/room/<room_id>/speaking', methods=['GET'])
def get_speaking_users(room_id: str):
    """获取房间中正在说话的用户列表
    
    返回:
    {
        "code": 0,
        "message": "success",
        "data": {
            "room_id": "room_123",
            "speaking_users": ["user_001", "user_002"]
        }
    }
    """
    try:
        with speaking_lock:
            users = list(speaking_users.get(room_id, {}).keys())
        
        return jsonify({
            'code': 0,
            'message': 'success',
            'data': {
                'room_id': room_id,
                'speaking_users': users
            }
        }), 200
        
    except Exception as e:
        logger.error(f"Error getting speaking users: {e}")
        return jsonify({'code': 500, 'message': str(e)}), 500


@app.route('/health', methods=['GET'])
def health():
    """健康检查"""
    return jsonify({'status': 'ok'}), 200


# ========== WebSocket 通知端点 ==========
# 客户端通过 HTTP 长轮询或 WebSocket 连接此端点接收事件通知

# 用于缓存自动检测的外网IP
_auto_detected_ws_host = None
_auto_detect_attempted = False


def get_auto_detected_ws_host():
    """自动检测外网可访问的IP/域名
    
    检测优先级：
    1. 环境变量 WS_HOST
    2. 自动检测外网IP（通过外部API）
    3. 回退到 127.0.0.1
    """
    global _auto_detected_ws_host, _auto_detect_attempted
    
    if _auto_detected_ws_host:
        return _auto_detected_ws_host
    
    if _auto_detect_attempted:
        return None
    
    _auto_detect_attempted = True
    
    # 尝试多个IP检测服务
    ip_services = [
        'https://api.ipify.org',
        'https://icanhazip.com',
        'https://checkip.amazonaws.com',
    ]
    
    import urllib.request
    import urllib.error
    
    for service in ip_services:
        try:
            req = urllib.request.Request(service, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req, timeout=5) as response:
                ip = response.read().decode('utf-8').strip()
                if ip and ip != '127.0.0.1' and not ip.startswith('10.') and not ip.startswith('192.168.'):
                    _auto_detected_ws_host = ip
                    logger.info(f"[WS] Auto-detected external IP: {ip}")
                    return ip
        except Exception as e:
            logger.debug(f"[WS] Failed to get IP from {service}: {e}")
            continue
    
    logger.warning("[WS] Could not auto-detect external IP, falling back to 127.0.0.1")
    return None


def get_ws_host_from_request(request):
    """从请求中提取合适的主机名/IP"""
    # 优先使用 X-Forwarded-Host（通过反向代理时）
    forwarded_host = request.headers.get('X-Forwarded-Host')
    if forwarded_host:
        host = forwarded_host.split(':')[0]
        # 如果不是内网地址，直接使用
        if host not in ('127.0.0.1', 'localhost', '0.0.0.0'):
            return host
        # 如果是内网，检查端口
        port = forwarded_host.split(':')[1] if ':' in forwarded_host else None
        return host, port
    
    # 使用 Host 头
    host = request.host.split(':')[0]
    if host not in ('127.0.0.1', 'localhost', '0.0.0.0'):
        return host
    
    return host


@app.route('/api/v1/ws/subscribe', methods=['POST'])
def ws_subscribe():
    """手动订阅房间
    
    请求体:
    {
        "room_id": "room_123",
        "user_id": "user_001"
    }
    
    返回:
    {
        "code": 0,
        "message": "success",
        "data": {
            "ws_url": "ws://localhost:8086/ws?room=room_123&user=user_001"
        }
    }
    """
    try:
        data = request.json or {}
        room_id = data.get('room_id', '')
        user_id = data.get('user_id', '')

        if not room_id:
            return jsonify({
                'code': 400,
                'message': 'Missing required parameter: room_id'
            }), 400

        # 返回纯 WebSocket 连接地址
        # 优先级：WS_HOST环境变量 > 自动检测外网IP > X-Forwarded-Host > Host请求头
        ws_host = os.getenv('WS_HOST')
        
        if not ws_host:
            # 尝试从请求头获取
            request_host = get_ws_host_from_request(request)
            if isinstance(request_host, tuple):
                ws_host = request_host[0]
            else:
                ws_host = request_host
            
            # 如果是内网地址，尝试自动检测外网IP
            if ws_host in ('127.0.0.1', 'localhost', '0.0.0.0'):
                detected_ip = get_auto_detected_ws_host()
                if detected_ip:
                    ws_host = detected_ip
                else:
                    # 最终回退：使用主机名（如果有的话）
                    ws_host = os.getenv('HOSTNAME', ws_host)
        
        # 使用原生 WebSocket（端口 8086）
        ws_url = f"ws://{ws_host}:{WS_PORT}/ws?room={room_id}"
        if user_id:
            ws_url += f"&user={user_id}"
        
        return jsonify({
            'code': 0,
            'message': 'success',
            'data': {
                'ws_url': ws_url,
                'room_id': room_id,
                'user_id': user_id
            }
        }), 200
        
    except Exception as e:
        logger.error(f"Error generating ws subscription: {e}")
        return jsonify({'code': 500, 'message': str(e)}), 500


@app.route('/api/v1/ws/status', methods=['GET'])
def ws_status():
    """获取 WebSocket 连接状态"""
    return jsonify({
        'code': 0,
        'message': 'success',
        'data': {
            'total_connections': notification_service.get_total_connections(),
            'active_rooms': notification_service.get_active_rooms(),
            'ws_port': WS_PORT
        }
    }), 200


# ========== WebSocket 服务器（使用原生 WebSocket + websockets 库）==========

import asyncio
import threading
from typing import Set

# 保留向后兼容的空类
class NativeWebSocketHandler:
    pass

native_ws = None  # 保留向后兼容

sio = None  # 保留向后兼容

@app.after_request
def after_request(response):
    """添加 CORS 头"""
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type'
    return response


# ========== Flask-SocketIO 事件处理 ==========

@socketio.on('connect')
def handle_connect():
    """处理客户端连接"""
    logger.info(f"[SocketIO] Client connected: {request.sid}")


@socketio.on('disconnect')
def handle_disconnect():
    """处理客户端断开"""
    logger.info(f"[SocketIO] Client disconnected: {request.sid}")


@socketio.on('subscribe')
def handle_subscribe(data):
    """客户端订阅房间"""
    room_id = data.get('room_id', '')
    user_id = data.get('user_id', '')
    
    if not room_id:
        emit('error', {'message': 'Missing room_id'})
        return
    
    join_room(room_id)
    session['room_id'] = room_id
    session['user_id'] = user_id
    
    logger.info(f"[SocketIO] Client {user_id} subscribed to room {room_id}")
    emit('subscribed', {'type': 'subscribed', 'room_id': room_id, 'user_id': user_id})


@socketio.on('unsubscribe')
def handle_unsubscribe(data):
    """客户端取消订阅房间"""
    room_id = data.get('room_id', session.get('room_id', ''))
    if room_id:
        leave_room(room_id)
        emit('unsubscribed', {'type': 'unsubscribed', 'room_id': room_id})


@socketio.on('ping')
def handle_ping(data):
    """处理心跳 ping"""
    emit('pong', {'type': 'pong'})


# Socket.IO 广播辅助函数
def socketio_emit_to_room(room_id: str, event_type: str, data: dict):
    """通过 Socket.IO 发送事件到房间"""
    socketio.emit('room_event', {'type': event_type, 'data': data}, room=room_id)


# 更新 notification_service 的 _emit_socketio 方法
def _socketio_emit(event):
    message = {"type": event.event_type, "data": event.to_dict()}
    try:
        socketio.emit('room_event', message, room=event.room_id)
        logger.info(f"[SocketIO] Sent {event.event_type} to room {event.room_id}")
    except Exception as e:
        logger.warning(f"[SocketIO] Failed to send: {e}")

notification_service._emit_socketio = _socketio_emit


# ========== 原生 WebSocket 服务器（保留兼容）==========

import asyncio
from typing import Set

# 原生 WebSocket 连接存储
native_ws_connections: Dict[str, Set] = {}
native_ws_lock = threading.Lock()


def ws_register_connection(room_id: str, websocket):
    """注册 WebSocket 连接（同步）"""
    with native_ws_lock:
        if room_id not in native_ws_connections:
            native_ws_connections[room_id] = set()
        native_ws_connections[room_id].add(websocket)


def ws_unregister_connection(room_id: str, websocket):
    """注销 WebSocket 连接（同步）"""
    with native_ws_lock:
        if room_id in native_ws_connections:
            native_ws_connections[room_id].discard(websocket)
            if not native_ws_connections[room_id]:
                del native_ws_connections[room_id]


def ws_get_connections(room_id: str):
    """获取房间的所有连接（同步）"""
    with native_ws_lock:
        return list(native_ws_connections.get(room_id, set()))


def start_native_ws_server():
    """启动原生 WebSocket 服务器"""
    async def run_server():
        logger.info(f"[WS] Starting native WebSocket server on 0.0.0.0:{WS_PORT}/ws")
        async with websockets.serve(handle_native_ws, '0.0.0.0', WS_PORT, ping_interval=30, ping_timeout=10):
            logger.info(f"[WS] Native WebSocket server started on port {WS_PORT}")
            await asyncio.Future()
    
    def run_in_thread():
        asyncio.run(run_server())
    
    ws_thread = threading.Thread(target=run_in_thread, daemon=True)
    ws_thread.start()
    logger.info("[WS] Native WebSocket server thread started")


async def handle_native_ws(websocket):
    """处理原生 WebSocket 连接 (websockets 16.0+ API)"""
    from urllib.parse import parse_qs, urlparse
    
    room_id = None
    try:
        path = websocket.request.path if hasattr(websocket, 'request') else '/'
        parsed = urlparse(path)
        params = parse_qs(parsed.query)
        
        room_id = params.get('room', [''])[0]
        user_id = params.get('user', [''])[0]
        
        if not room_id:
            await websocket.close(1008, "Missing room parameter")
            return
        
        ws_register_connection(room_id, websocket)
        websocket._room_id = room_id
        websocket._user_id = user_id
        
        logger.info(f"[WS] Native WebSocket connected: room={room_id}, user={user_id}")
        
        await websocket.send(json.dumps({
            "type": "connected",
            "room_id": room_id,
            "user_id": user_id
        }))
        
        async for message in websocket:
            try:
                data = json.loads(message)
                msg_type = data.get('type', '')
                
                if msg_type == 'ping':
                    await websocket.send(json.dumps({"type": "pong"}))
                elif msg_type == 'subscribe':
                    new_room = data.get('room_id', room_id)
                    old_room = websocket._room_id
                    ws_unregister_connection(old_room, websocket)
                    ws_register_connection(new_room, websocket)
                    websocket._room_id = new_room
                    await websocket.send(json.dumps({
                        "type": "subscribed",
                        "room_id": new_room
                    }))
            except json.JSONDecodeError:
                pass
                
    except Exception as e:
        import traceback
        logger.warning(f"[WS] WebSocket error: {e}\n{traceback.format_exc()}")
    finally:
        if room_id is None:
            room_id = getattr(websocket, '_room_id', None)
        if room_id:
            ws_unregister_connection(room_id, websocket)
        logger.info(f"[WS] Native WebSocket disconnected: room={room_id}")


def start_websocket_server():
    """启动 WebSocket + HTTP 服务器（使用纯 asyncio websockets）
    
    HTTP 服务使用 waitress 托管 Flask，端口 8085
    WebSocket 服务使用 websockets 库，端口 8086
    """
    import websockets
    from urllib.parse import parse_qs, urlparse
    import asyncio
    from waitress import serve as waitress_serve
    
    http_port = int(os.getenv('CALLBACK_PORT', 8085))
    ws_port = int(os.getenv('WS_PORT', 8086))
    host = os.getenv('CALLBACK_HOST', '0.0.0.0')
    
    # 设置 notification_service 使用原生 WebSocket
    notification_service._native_ws_url = f"http://127.0.0.1:{http_port}"
    logger.info(f"[Notification] Native WS URL set to http://127.0.0.1:{http_port}")
    
    logger.info(f"[Server] Starting HTTP server (Flask) on port {http_port}")
    logger.info(f"[Server] Starting WebSocket server on port {ws_port}")
    
    async def handle_websocket(websocket):
        """处理 WebSocket 连接"""
        room_id = None
        try:
            # 解析 URL 参数 - websockets 16.0+ 使用 request.path
            request_path = websocket.request.path
            
            parsed = urlparse(request_path)
            params = parse_qs(parsed.query)
            
            room_id = params.get('room', [''])[0]
            user_id = params.get('user', [''])[0]
            
            if not room_id:
                await websocket.close(1008, "Missing room parameter")
                return
            
            # 注册连接
            ws_register_connection(room_id, websocket)
            websocket._room_id = room_id
            websocket._user_id = user_id
            
            logger.info(f"[WS] Connected: room={room_id}, user={user_id}")
            
            # 发送连接成功消息
            await websocket.send(json.dumps({
                "type": "connected",
                "room_id": room_id,
                "user_id": user_id
            }))
            
            # 处理消息循环
            async for message in websocket:
                if not message:
                    continue
                try:
                    data = json.loads(message)
                    msg_type = data.get('type', '')
                    
                    if msg_type == 'ping':
                        await websocket.send(json.dumps({"type": "pong"}))
                    elif msg_type == 'subscribe':
                        new_room = data.get('room_id', room_id)
                        old_room = websocket._room_id
                        ws_unregister_connection(old_room, websocket)
                        ws_register_connection(new_room, websocket)
                        websocket._room_id = new_room
                        await websocket.send(json.dumps({
                            "type": "subscribed",
                            "room_id": new_room
                        }))
                except json.JSONDecodeError:
                    pass
                    
        except websockets.exceptions.ConnectionClosed:
            pass
        except Exception as e:
            logger.warning(f"[WS] WebSocket error: {e}")
        finally:
            if room_id is None:
                room_id = getattr(websocket, '_room_id', None)
            if room_id:
                ws_unregister_connection(room_id, websocket)
            logger.info(f"[WS] Disconnected: room={room_id}")
    
    async def run_ws_server():
        """运行 WebSocket 服务器"""
        async with websockets.serve(handle_websocket, host, ws_port, ping_interval=30, ping_timeout=10) as ws_server:
            logger.info(f"[WS] WebSocket server running on ws://{host}:{ws_port}")
            await asyncio.Future()
    
    def run_http_server():
        """运行 HTTP 服务器（Flask + waitress）"""
        try:
            waitress_serve(app, host=host, port=http_port, threads=4)
        except Exception as e:
            logger.error(f"[HTTP] Server error: {e}")
    
    # 启动 HTTP 服务器（非守护线程）
    http_thread = threading.Thread(target=run_http_server, daemon=False, name="http-server")
    http_thread.start()
    logger.info(f"[HTTP] Started Flask HTTP server on port {http_port}")
    
    # 启动 WebSocket 服务器（非守护线程）
    ws_thread = threading.Thread(target=lambda: asyncio.run(run_ws_server()), daemon=False, name="ws-server")
    ws_thread.start()
    logger.info(f"[WS] Started WebSocket server thread on port {ws_port}")
    
    # 保持主线程运行
    logger.info("[Main] Server running, press Ctrl+C to stop")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("[Main] Shutting down...")


if __name__ == '__main__':
    SRS_URL = os.getenv("SRS_URL", "rtmp://localhost:1935")
    
    logger.info(f"Starting callback server with Flask-SocketIO + Native WebSocket")
    logger.info(f"SRS URL: {SRS_URL}")
    
    def on_translation_stop(request_id: str):
        logger.info(f"[Callback] Translation stop callback: {request_id}")
        if request_id in translation_processes:
            stop_translation_service(request_id)
        req = translation_manager.get_request(request_id)
        if req:
            notification_service.notify_translation_stopped(
                room_id=req.room_id,
                source_user=req.source_user,
                to_lang=req.to_lang
            )
    
    translation_manager.set_stop_callback(on_translation_stop)
    start_heartbeat_checker()
    start_websocket_server()
