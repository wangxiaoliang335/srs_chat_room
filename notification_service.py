#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
通知服务模块
通过 Socket.IO 向客户端推送房间事件通知
"""

import json
import logging
import threading
from enum import Enum
from dataclasses import dataclass, field, asdict
from typing import Dict
from datetime import datetime
import uuid

logger = logging.getLogger(__name__)


class EventType(Enum):
    """事件类型"""
    MEMBER_JOINED = "member_joined"
    MEMBER_LEFT = "member_left"
    MEMBER_KICKED = "member_kicked"
    MEMBER_ROLE_CHANGED = "member_role_changed"
    MEMBER_MUTED = "member_muted"
    MEMBER_UNMUTED = "member_unmuted"
    MEMBER_MIC_DISABLED = "member_mic_disabled"
    MEMBER_MIC_ENABLED = "member_mic_enabled"
    ROOM_MUTED_ALL = "room_muted_all"
    ROOM_UNMUTED_ALL = "room_unmuted_all"
    ROOM_CREATED = "room_created"
    ROOM_DELETED = "room_deleted"
    ROOM_KNOCK = "room_knock"  # 敲门事件
    ROOM_KNOCK_ACCEPTED = "room_knock_accepted"  # 敲门被接受
    ROOM_KNOCK_REJECTED = "room_knock_rejected"  # 敲门被拒绝
    TRANSLATION_STARTED = "translation_started"
    TRANSLATION_STOPPED = "translation_stopped"
    USER_SPEAKING_START = "user_speaking_start"  # 用户开始说话
    USER_SPEAKING_STOP = "user_speaking_stop"  # 用户停止说话


@dataclass
class RoomEvent:
    """房间事件"""
    event_id: str = ""
    event_type: str = ""
    room_id: str = ""
    user_id: str = ""
    operator_id: str = ""
    target_user_id: str = ""
    data: Dict = field(default_factory=dict)
    timestamp: str = ""

    def __post_init__(self):
        if not self.event_id:
            self.event_id = str(uuid.uuid4())[:8]
        if not self.timestamp:
            self.timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    def to_dict(self):
        return asdict(self)

    def to_json(self):
        return json.dumps(self.to_dict(), ensure_ascii=False)


class NotificationService:
    """通知服务 - 通过 Socket.IO 广播房间事件"""

    def __init__(self):
        self._sio = None
        self._lock = threading.RLock()
        self._connections = set()  # 追踪所有 WebSocket 连接
        self._room_subscriptions = {}  # room_id -> set of ws connections
        self._user_connections = {}  # ws -> {user_id, room_id}
        logger.info("[Notification] Service initialized")

    def set_socketio(self, sio):
        """设置 Socket.IO 实例"""
        self._sio = sio
        logger.info("[Notification] Socket.IO instance set")

    def _emit(self, event: RoomEvent):
        """通过 Socket.IO 发送事件"""
        if self._sio is None:
            return

        try:
            room_name = f"room_{event.room_id}"
            event_data = event.to_dict()
            self._sio.emit(event.event_type, event_data, room=room_name)
            logger.info(f"[SocketIO] Sent {event.event_type} to {room_name}")
        except Exception as e:
            logger.warning(f"[SocketIO] Failed to send: {e}")

    def notify_member_joined(self, room_id: str, user_id: str, user_info: Dict = None):
        event = RoomEvent(
            event_type=EventType.MEMBER_JOINED.value,
            room_id=room_id,
            user_id=user_id,
            data=user_info or {}
        )
        self._emit(event)

    def notify_member_left(self, room_id: str, user_id: str):
        event = RoomEvent(
            event_type=EventType.MEMBER_LEFT.value,
            room_id=room_id,
            user_id=user_id
        )
        self._emit(event)

    def notify_member_kicked(self, room_id: str, user_id: str, operator_id: str):
        event = RoomEvent(
            event_type=EventType.MEMBER_KICKED.value,
            room_id=room_id,
            user_id=user_id,
            operator_id=operator_id
        )
        self._emit(event)

    def notify_member_role_changed(self, room_id: str, user_id: str, old_role: str, new_role: str, operator_id: str):
        event = RoomEvent(
            event_type=EventType.MEMBER_ROLE_CHANGED.value,
            room_id=room_id,
            user_id=user_id,
            operator_id=operator_id,
            data={"old_role": old_role, "new_role": new_role}
        )
        self._emit(event)

    def notify_member_muted(self, room_id: str, user_id: str, operator_id: str):
        event = RoomEvent(
            event_type=EventType.MEMBER_MUTED.value,
            room_id=room_id,
            user_id=user_id,
            operator_id=operator_id
        )
        self._emit(event)

    def notify_member_unmuted(self, room_id: str, user_id: str, operator_id: str):
        event = RoomEvent(
            event_type=EventType.MEMBER_UNMUTED.value,
            room_id=room_id,
            user_id=user_id,
            operator_id=operator_id
        )
        self._emit(event)

    def notify_member_mic_disabled(self, room_id: str, user_id: str, operator_id: str):
        event = RoomEvent(
            event_type=EventType.MEMBER_MIC_DISABLED.value,
            room_id=room_id,
            user_id=user_id,
            operator_id=operator_id
        )
        self._emit(event)

    def notify_member_mic_enabled(self, room_id: str, user_id: str, operator_id: str):
        event = RoomEvent(
            event_type=EventType.MEMBER_MIC_ENABLED.value,
            room_id=room_id,
            user_id=user_id,
            operator_id=operator_id
        )
        self._emit(event)

    def notify_room_muted_all(self, room_id: str, operator_id: str, muted_count: int):
        event = RoomEvent(
            event_type=EventType.ROOM_MUTED_ALL.value,
            room_id=room_id,
            operator_id=operator_id,
            data={"muted_count": muted_count}
        )
        self._emit(event)

    def notify_room_unmuted_all(self, room_id: str, operator_id: str, unmuted_count: int):
        event = RoomEvent(
            event_type=EventType.ROOM_UNMUTED_ALL.value,
            room_id=room_id,
            operator_id=operator_id,
            data={"unmuted_count": unmuted_count}
        )
        self._emit(event)

    def notify_room_created(self, room_id: str, owner_id: str, room_info: Dict = None):
        event = RoomEvent(
            event_type=EventType.ROOM_CREATED.value,
            room_id=room_id,
            user_id=owner_id,
            data=room_info or {}
        )
        self._emit(event)

    def notify_room_deleted(self, room_id: str, operator_id: str):
        event = RoomEvent(
            event_type=EventType.ROOM_DELETED.value,
            room_id=room_id,
            operator_id=operator_id
        )
        self._emit(event)

    def notify_room_knock(self, room_id: str, knocker_id: str, owner_id: str, knocker_info: Dict = None):
        """通知房主有人敲门 - 发送给特定用户
        
        Args:
            room_id: 房间ID
            knocker_id: 敲门者用户ID
            owner_id: 房主用户ID（接收通知的人）
            knocker_info: 敲门者信息
        """
        event = RoomEvent(
            event_type=EventType.ROOM_KNOCK.value,
            room_id=room_id,
            user_id=knocker_id,
            operator_id=owner_id,
            data=knocker_info or {}
        )
        self._emit_to_user(event, owner_id)

    def notify_knock_accepted(self, room_id: str, knocker_id: str, operator_id: str):
        """通知敲门者 - 申请被接受"""
        event = RoomEvent(
            event_type=EventType.ROOM_KNOCK_ACCEPTED.value,
            room_id=room_id,
            user_id=knocker_id,
            operator_id=operator_id
        )
        self._emit_to_user(event, knocker_id)

    def notify_knock_rejected(self, room_id: str, knocker_id: str, operator_id: str, reason: str = ""):
        """通知敲门者 - 申请被拒绝"""
        event = RoomEvent(
            event_type=EventType.ROOM_KNOCK_REJECTED.value,
            room_id=room_id,
            user_id=knocker_id,
            operator_id=operator_id,
            data={"reason": reason}
        )
        self._emit_to_user(event, knocker_id)

    def _emit_to_user(self, event: RoomEvent, target_user_id: str):
        """向特定用户发送事件"""
        if self._sio is None:
            return

        try:
            room_name = f"user_{target_user_id}"
            event_data = event.to_dict()
            self._sio.emit(event.event_type, event_data, room=room_name)
            logger.info(f"[SocketIO] Sent {event.event_type} to user {target_user_id}")
        except Exception as e:
            logger.warning(f"[SocketIO] Failed to send to user {target_user_id}: {e}")

    def notify_translation_started(self, room_id: str, source_user: str, to_lang: str, target_user: str):
        event = RoomEvent(
            event_type=EventType.TRANSLATION_STARTED.value,
            room_id=room_id,
            user_id=source_user,
            target_user_id=target_user,
            data={"to_lang": to_lang}
        )
        self._emit(event)

    def notify_translation_stopped(self, room_id: str, source_user: str, to_lang: str):
        event = RoomEvent(
            event_type=EventType.TRANSLATION_STOPPED.value,
            room_id=room_id,
            user_id=source_user,
            data={"to_lang": to_lang}
        )
        self._emit(event)

    def notify_user_speaking_start(self, room_id: str, user_id: str, stream_url: str = ""):
        """通知用户开始说话"""
        event = RoomEvent(
            event_type=EventType.USER_SPEAKING_START.value,
            room_id=room_id,
            user_id=user_id,
            data={"stream_url": stream_url}
        )
        self._emit(event)

    def notify_user_speaking_stop(self, room_id: str, user_id: str):
        """通知用户停止说话"""
        event = RoomEvent(
            event_type=EventType.USER_SPEAKING_STOP.value,
            room_id=room_id,
            user_id=user_id
        )
        self._emit(event)

    # ========== WebSocket 连接追踪方法 ==========

    def register_global(self, ws):
        """注册全局 WebSocket 连接"""
        with self._lock:
            self._connections.add(ws)
        logger.info(f"[WS] Registered global connection, total: {len(self._connections)}")

    def unregister(self, ws):
        """注销 WebSocket 连接"""
        with self._lock:
            self._connections.discard(ws)
            # 清理房间订阅
            for room_id in list(self._room_subscriptions.keys()):
                self._room_subscriptions[room_id].discard(ws)
                if not self._room_subscriptions[room_id]:
                    del self._room_subscriptions[room_id]
            # 清理用户连接
            if ws in self._user_connections:
                del self._user_connections[ws]
        logger.info(f"[WS] Unregistered connection, remaining: {len(self._connections)}")

    def subscribe_room(self, ws, room_id: str):
        """订阅房间"""
        with self._lock:
            if room_id not in self._room_subscriptions:
                self._room_subscriptions[room_id] = set()
            self._room_subscriptions[room_id].add(ws)
        logger.info(f"[WS] WebSocket subscribed to room {room_id}")

    def unsubscribe_room(self, ws, room_id: str):
        """取消订阅房间"""
        with self._lock:
            if room_id in self._room_subscriptions:
                self._room_subscriptions[room_id].discard(ws)
        logger.info(f"[WS] WebSocket unsubscribed from room {room_id}")

    def register_user(self, ws, user_id: str, room_id: str):
        """注册用户连接"""
        with self._lock:
            self._user_connections[ws] = {'user_id': user_id, 'room_id': room_id}
        logger.info(f"[WS] User {user_id} registered in room {room_id}")

    def get_total_connections(self) -> int:
        """获取总连接数"""
        with self._lock:
            return len(self._connections)

    def get_active_rooms(self) -> list:
        """获取活跃房间列表"""
        with self._lock:
            return list(self._room_subscriptions.keys())


# 全局单例
notification_service = NotificationService()
