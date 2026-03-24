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
import uuid
from flask import Flask, request, jsonify
from typing import Dict, Any, List

from translation_manager import (
    TranslationManager, TranslationRequest, TranslationStatus
)
from user_manager import (
    UserManager, UserRole, UserStatus, user_manager
)
from notification_service import notification_service

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# 翻译管理器
translation_manager = TranslationManager()

# 存储运行中的翻译服务进程
# 结构: {request_id: subprocess.Popen}
translation_processes: Dict[str, subprocess.Popen] = {}

# SRS配置
SRS_URL = os.getenv("SRS_URL", "rtmp://localhost:1935")

# WebSocket 端口（与 HTTP 端口相同）
WS_PORT = int(os.getenv('CALLBACK_PORT', 8085))

# Socket.IO 实例（延迟初始化）
sio = None

# 用户说话状态管理
# 结构: {room_id: {user_id: True/False}}
speaking_users: Dict[str, Dict[str, bool]] = {}
speaking_lock = threading.Lock()


def parse_stream_name(stream_name: str) -> dict:
    """解析流名称，提取 room_id、user_id 和流类型
    
    流名称格式:
    - 原声音频流: {room_id}_{user_id}
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
            # 翻译流的 user_id 是 source_user
            user_id = prefix
            result = {
                'type': 'translation',
                'room_id': None,  # 翻译流不直接包含 room_id，需要额外查找
                'user_id': user_id,
                'to_lang': to_lang
            }
    else:
        # 原声音频流: room_id_user_id
        # 使用 rsplit 从右边分割，只分割一次
        parts = stream_name.rsplit('_', 1)
        if len(parts) == 2:
            result = {
                'type': 'original',
                'room_id': parts[0],
                'user_id': parts[1],
                'to_lang': None
            }
    
    return result


def start_translation_service(request_id: str, room_id: str, source_user: str, to_lang: str, target_user: str = ""):
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
    env['TARGET_USER'] = target_user  # 添加目标用户
    env['STREAM_NAME'] = stream_name
    env['SRS_URL'] = SRS_URL
    env['TEXT_SERVER_URL'] = os.getenv("TEXT_SERVER_URL", "http://localhost:8086")  # 文本推送服务地址
    
    try:
        # 启动翻译服务进程
        # 使用绝对路径确保能找到脚本
        script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'audio_translation_service.py')
        logger.info(f"Starting translation service script: {script_path}")
        
        process = subprocess.Popen(
            [sys.executable, script_path],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        
        translation_processes[request_id] = process
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
        del translation_processes[request_id]
        logger.info(f"Stopped translation service for request: {request_id}")
        
    except subprocess.TimeoutExpired:
        process.kill()
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
        
        if not room_id or not source_user or not target_user:
            return jsonify({
                'code': 400,
                'message': 'Missing required parameters: room_id, source_user, target_user'
            }), 400
        
        # 生成请求ID
        request_id = str(uuid.uuid4())
        
        # 构建翻译流地址
        stream_name = f"{room_id}_{source_user}_to_{to_lang}"
        stream_url = f"{SRS_URL}/live/{stream_name}"
        
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
        start_translation_service(request_id, room_id, source_user, to_lang, target_user)
        
        # 更新状态为活跃
        translation_manager.update_status(request_id, TranslationStatus.ACTIVE)
        
        logger.info(f"[Translation] Request created successfully: {request_id}")
        
        return jsonify({
            'code': 0,
            'message': 'success',
            'data': {
                'request_id': request_id,
                'stream_url': stream_url
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
        
        if not stream_name:
            logger.warning("Received on_publish callback without stream name")
            return jsonify({'code': 0}), 200
        
        logger.info(f"Received on_publish callback for stream: {stream_name}")
        
        # 解析流名称
        parsed = parse_stream_name(stream_name)
        
        if parsed['type'] == 'original':
            # 原声音频流：通知用户开始说话
            room_id = parsed['room_id']
            user_id = parsed['user_id']
            
            if room_id and user_id:
                # 检查用户是否在房间中
                member = user_manager.get_member(room_id, user_id)
                if member:
                    with speaking_lock:
                        if room_id not in speaking_users:
                            speaking_users[room_id] = {}
                        speaking_users[room_id][user_id] = True
                    
                    stream_url = f"{SRS_URL}/live/{stream_name}"
                    notification_service.notify_user_speaking_start(room_id, user_id, stream_url)
                    logger.info(f"[Speaking] User {user_id} started speaking in room {room_id}")
                else:
                    logger.warning(f"[Speaking] User {user_id} not found in room {room_id}")
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


@app.route('/api/v1/streams/status', methods=['GET'])
def get_status():
    """获取翻译服务状态"""
    status = {
        'active_requests': len(translation_processes),
        'processes': list(translation_processes.keys())
    }
    return jsonify(status), 200


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

@app.route('/api/v1/ws/subscribe', methods=['POST'])
def ws_subscribe():
    """手动订阅房间（用于不支持 WebSocket 的客户端）
    
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
        
        # 返回 WebSocket 连接地址
        ws_host = os.getenv('WS_HOST', request.host.split(':')[0])
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


# ========== WebSocket 服务器（使用 gevent）==========

def start_websocket_server():
    """启动 WebSocket 服务器（使用 gevent）"""
    try:
        from gevent import monkey
        monkey.patch_all()
        
        from gevent.pywsgi import WSGIServer
        from geventwebsocket.handler import WebSocketHandler
        from geventwebsocket import WebSocketServer, WebSocketApplication
        
        class RoomApplication(WebSocketApplication):
            """房间 WebSocket 应用"""
            
            def on_open(self):
                logger.info("[WS] WebSocket connection opened")
                # 注册为全局连接
                notification_service.register_global(self.ws)
            
            def on_message(self, message):
                if message is None:
                    return
                
                try:
                    data = json.loads(message)
                    msg_type = data.get('type', '')
                    
                    if msg_type == 'subscribe':
                        room_id = data.get('room_id', '')
                        user_id = data.get('user_id', '')
                        
                        # 订阅房间
                        if room_id:
                            notification_service.subscribe_room(self.ws, room_id)
                        
                        # 注册用户
                        if user_id and room_id:
                            notification_service.register_user(self.ws, user_id, room_id)
                        
                        # 发送确认
                        self.ws.send(json.dumps({
                            'type': 'subscribed',
                            'room_id': room_id,
                            'user_id': user_id
                        }))
                    
                    elif msg_type == 'unsubscribe':
                        room_id = data.get('room_id', '')
                        if room_id:
                            notification_service.unsubscribe_room(self.ws, room_id)
                    
                    elif msg_type == 'ping':
                        self.ws.send(json.dumps({'type': 'pong'}))
                    
                except json.JSONDecodeError:
                    logger.warning(f"[WS] Invalid JSON message: {message}")
            
            def on_close(self, reason):
                logger.info(f"[WS] WebSocket connection closed: {reason}")
                notification_service.unregister(self.ws)
        
        class NotificationApp:
            """通知应用"""
            
            def __call__(self, environ, start_response):
                path = environ.get('PATH_INFO', '')
                
                if path == '/ws' or path.startswith('/ws?'):
                    # WebSocket 连接
                    environ['wsgi.websocket'] = environ.get('wsgi.websocket')
                    try:
                        app = RoomApplication(environ['wsgi.websocket'])
                        app()
                    except Exception:
                        pass
                    return []
                else:
                    # 普通 HTTP 请求
                    return app(environ, start_response)
        
        logger.info(f"[WS] Starting WebSocket server on port {WS_PORT}")
        server = WSGIServer(('0.0.0.0', WS_PORT), NotificationApp(), handler_class=WebSocketHandler)
        server.serve_forever()
        
    except ImportError as e:
        logger.warning(f"[WS] Cannot start WebSocket server: {e}")
        logger.info("[WS] Install gevent and gevent-websocket for WebSocket support")
    except Exception as e:
        logger.error(f"[WS] WebSocket server error: {e}")


if __name__ == '__main__':
    port = int(os.getenv('CALLBACK_PORT', 8085))
    host = os.getenv('CALLBACK_HOST', '0.0.0.0')
    SRS_URL = os.getenv("SRS_URL", "rtmp://localhost:1935")
    
    logger.info(f"Starting callback server on {host}:{port}")
    logger.info(f"SRS URL: {SRS_URL}")
    
    try:
        import socketio

        # 创建 Socket.IO 服务器
        sio = socketio.Server(async_mode='eventlet')

        # 设置给通知服务
        notification_service.set_socketio(sio)

        @sio.event
        def connect(sid, environ):
            logger.info(f"[SocketIO] Client connected: {sid}")
        
        @sio.event
        def disconnect(sid):
            logger.info(f"[SocketIO] Client disconnected: {sid}")
        
        @sio.event
        def subscribe(sid, data):
            """订阅房间事件"""
            room_id = data.get('room_id', '')
            user_id = data.get('user_id', '')
            if room_id:
                sio.enter_room(sid, f"room_{room_id}")
                logger.info(f"[SocketIO] {sid} subscribed to room {room_id}")
                sio.emit('subscribed', {'room_id': room_id, 'user_id': user_id}, room=sid)
            
            # 同时订阅私人通知房间（用于接收敲门等私人消息）
            if user_id:
                sio.enter_room(sid, f"user_{user_id}")
                logger.info(f"[SocketIO] {sid} subscribed to private notifications for user {user_id}")
        
        @sio.event
        def subscribe_private(sid, data):
            """订阅私人通知（用于接收敲门等私人消息）"""
            user_id = data.get('user_id', '')
            if user_id:
                sio.enter_room(sid, f"user_{user_id}")
                logger.info(f"[SocketIO] {sid} subscribed to private notifications for user {user_id}")
                sio.emit('subscribed_private', {'user_id': user_id}, room=sid)
        
        @sio.event
        def unsubscribe(sid, data):
            """取消订阅房间"""
            room_id = data.get('room_id', '')
            if room_id:
                sio.leave_room(sid, f"room_{room_id}")
                logger.info(f"[SocketIO] {sid} unsubscribed from room {room_id}")
        
        # 创建 Flask-SocketIO 应用
        app.wsgi_app = socketio.Middleware(sio, app.wsgi_app)
        
        logger.info(f"[Server] Running with Socket.IO support on {host}:{port}")
        
        # 使用 eventlet 运行
        import eventlet
        eventlet.wsgi.server(eventlet.listen((host, port)), app)
        
    except ImportError as e:
        logger.warning(f"[Server] socketio/eventlet not available: {e}")
        logger.info("[Server] Running Flask only (Socket.IO disabled)")
        app.run(host=host, port=port, debug=False, threaded=True)
    except Exception as e:
        logger.warning(f"[Server] Error starting Socket.IO: {e}")
        import traceback
        traceback.print_exc()
        logger.info("[Server] Running Flask only")
        app.run(host=host, port=port, debug=False, threaded=True)
