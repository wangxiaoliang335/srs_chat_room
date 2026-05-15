#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
用户管理模块
管理聊天室中的用户身份、角色和权限
"""

import os
import threading
from enum import Enum
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from datetime import datetime
import logging

logger = logging.getLogger(__name__)


class UserRole(Enum):
    """用户角色"""
    OWNER = "owner"           # 群主/房主
    ADMIN = "admin"           # 管理员
    MEMBER = "member"         # 普通成员
    GUEST = "guest"           # 访客


class UserStatus(Enum):
    """用户状态"""
    NORMAL = "normal"         # 正常
    MUTED = "muted"           # 被禁言
    MIC_OFF = "mic_off"       # 被禁麦


@dataclass
class User:
    """用户信息"""
    user_id: str
    room_id: str
    role: UserRole = UserRole.MEMBER
    status: UserStatus = UserStatus.NORMAL
    joined_at: str = ""
    last_active: str = ""
    publish_allowed: bool = True  # 是否允许发布（麦克风权限）
    
    def to_dict(self):
        return {
            'user_id': self.user_id,
            'room_id': self.room_id,
            'role': self.role.value,
            'status': self.status.value,
            'joined_at': self.joined_at,
            'last_active': self.last_active,
            'publish_allowed': self.publish_allowed
        }


@dataclass
class Room:
    """房间信息"""
    room_id: str
    name: str = ""
    owner_id: str = ""          # 群主ID
    created_at: str = ""
    max_members: int = 100
    allow_speak: bool = True    # 是否允许发言（全体禁言开关）
    members: Dict[str, User] = field(default_factory=dict)
    
    def to_dict(self):
        return {
            'room_id': self.room_id,
            'name': self.name,
            'owner_id': self.owner_id,
            'created_at': self.created_at,
            'max_members': self.max_members,
            'allow_speak': self.allow_speak,
            'member_count': len(self.members)
        }


class UserManager:
    """用户管理器"""
    
    def __init__(self):
        # 存储结构: {room_id: Room}
        self._rooms: Dict[str, Room] = {}
        # 流名称到用户的映射: {stream_name: user_id}
        # stream_name 格式: {room_id}_{user_id}
        self._stream_to_user: Dict[str, str] = {}
        self._lock = threading.RLock()
    
    def _get_current_time(self) -> str:
        return datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    def _repair_room_if_needed(self, room: Room) -> None:
        """修复历史异常数据：移除 user_id 为空的假成员，并补全 owner_id。"""
        if "" in room.members:
            del room.members[""]
        if room.owner_id and room.owner_id in room.members:
            return
        if not room.members:
            room.owner_id = ""
            return
        for uid, member in room.members.items():
            if member.role == UserRole.OWNER:
                room.owner_id = uid
                return
        uid = next(iter(room.members))
        room.owner_id = uid
        room.members[uid].role = UserRole.OWNER
    
    # ========== 房间管理 ==========
    
    def create_room(self, room_id: str, owner_id: str, name: str = "") -> Room:
        """创建房间"""
        with self._lock:
            if room_id in self._rooms:
                return self._rooms[room_id]
            
            room = Room(
                room_id=room_id,
                name=name or room_id,
                owner_id=owner_id,
                created_at=self._get_current_time()
            )
            
            # 创建者自动成为群主（owner_id 为空时不加入成员，避免 members 中出现 user_id 为空的假房主）
            if owner_id:
                owner = User(
                    user_id=owner_id,
                    room_id=room_id,
                    role=UserRole.OWNER,
                    joined_at=self._get_current_time(),
                    last_active=self._get_current_time()
                )
                room.members[owner_id] = owner
                # 同步创建流映射
                stream_name = f"{room_id}_{owner_id}"
                self._stream_to_user[stream_name] = owner_id
            
            self._rooms[room_id] = room
            logger.info(f"[UserManager] Created room: {room_id}, owner: {owner_id or '(pending first join)'}")
            return room
    
    def get_room(self, room_id: str) -> Optional[Room]:
        """获取房间信息"""
        with self._lock:
            room = self._rooms.get(room_id)
            if room:
                self._repair_room_if_needed(room)
            return room
    
    def get_or_create_room(self, room_id: str, creator_id: str = "") -> Room:
        """获取或创建房间"""
        with self._lock:
            if room_id not in self._rooms:
                self.create_room(room_id, creator_id)
            return self._rooms[room_id]
    
    def delete_room(self, room_id: str) -> bool:
        """删除房间"""
        with self._lock:
            if room_id not in self._rooms:
                return False
            
            # 清理流映射
            for user_id in list(self._rooms[room_id].members.keys()):
                stream_name = f"{room_id}_{user_id}"
                self._stream_to_user.pop(stream_name, None)
            
            del self._rooms[room_id]
            logger.info(f"[UserManager] Deleted room: {room_id}")
            return True
    
    def get_all_rooms(self) -> List[Room]:
        """获取所有房间"""
        with self._lock:
            return list(self._rooms.values())
    
    # ========== 成员管理 ==========
    
    def join_room(self, room_id: str, user_id: str, role: UserRole = UserRole.MEMBER) -> User:
        """用户加入房间"""
        with self._lock:
            # 确保房间存在（未先调用 create_room 时创建空房间，首位加入者在下方成为群主）
            if room_id not in self._rooms:
                self.create_room(room_id, "", "")
            
            room = self._rooms[room_id]
            self._repair_room_if_needed(room)
            
            if user_id in room.members:
                # 更新最后活跃时间
                room.members[user_id].last_active = self._get_current_time()
                return room.members[user_id]
            
            # 检查房间人数限制
            if len(room.members) >= room.max_members:
                raise ValueError(f"Room {room_id} is full")
            
            # 尚无群主且当前无人时：首位加入者自动成为群主（修复仅 join 且 role=member 时出现 user_id 为空的假房主）
            effective_role = role
            if not room.owner_id and len(room.members) == 0:
                effective_role = UserRole.OWNER
                room.owner_id = user_id
            
            # 创建新用户
            user = User(
                user_id=user_id,
                room_id=room_id,
                role=effective_role,
                joined_at=self._get_current_time(),
                last_active=self._get_current_time()
            )
            
            # 显式以群主身份加入且尚无群主记录时（房间已有其他成员的边缘情况）
            if not room.owner_id and role == UserRole.OWNER:
                room.owner_id = user_id
                user.role = UserRole.OWNER
            
            room.members[user_id] = user
            
            # 添加流映射
            stream_name = f"{room_id}_{user_id}"
            self._stream_to_user[stream_name] = user_id
            
            logger.info(f"[UserManager] User {user_id} joined room {room_id} as {role.value}")
            return user
    
    def leave_room(self, room_id: str, user_id: str) -> bool:
        """用户离开房间"""
        with self._lock:
            if room_id not in self._rooms:
                return False
            
            room = self._rooms[room_id]
            if user_id not in room.members:
                return False
            
            # 清理流映射
            stream_name = f"{room_id}_{user_id}"
            self._stream_to_user.pop(stream_name, None)
            
            del room.members[user_id]
            logger.info(f"[UserManager] User {user_id} left room {room_id}")
            
            # 如果房间空了，删除房间
            if not room.members:
                del self._rooms[room_id]
            
            return True
    
    def get_member(self, room_id: str, user_id: str) -> Optional[User]:
        """获取成员信息"""
        with self._lock:
            room = self._rooms.get(room_id)
            if not room:
                return None
            return room.members.get(user_id)
    
    def get_room_members(self, room_id: str) -> List[User]:
        """获取房间成员列表"""
        with self._lock:
            room = self._rooms.get(room_id)
            if not room:
                return []
            self._repair_room_if_needed(room)
            return list(room.members.values())
    
    def get_user_by_stream(self, stream_name: str) -> Optional[User]:
        """通过流名称获取用户"""
        with self._lock:
            # stream_name 格式: {room_id}_{user_id}，room_id 以 'room' 开头
            room_prefix_pos = stream_name.find('room')
            if room_prefix_pos == -1:
                return None
            underscore_pos = stream_name.find('_', room_prefix_pos + 4)
            if underscore_pos == -1:
                return None
            room_id = stream_name[:underscore_pos]
            user_id = stream_name[underscore_pos + 1:]
            if not room_id or not user_id:
                return None
            return self.get_member(room_id, user_id)
    
    def update_user_role(self, room_id: str, user_id: str, new_role: UserRole) -> bool:
        """更新用户角色"""
        with self._lock:
            room = self._rooms.get(room_id)
            if not room or user_id not in room.members:
                return False
            
            user = room.members[user_id]
            old_role = user.role
            
            # 不能修改群主的角色
            if user_id == room.owner_id and new_role != UserRole.OWNER:
                logger.warning(f"[UserManager] Cannot change owner's role")
                return False
            
            user.role = new_role
            
            # 如果是设置为群主，原群主降级为成员
            if new_role == UserRole.OWNER and room.owner_id != user_id:
                if room.owner_id in room.members:
                    room.members[room.owner_id].role = UserRole.MEMBER
                room.owner_id = user_id
            
            logger.info(f"[UserManager] Updated user {user_id} role from {old_role.value} to {new_role.value}")
            return True
    
    # ========== 禁言/禁麦管理 ==========
    
    def mute_user(self, room_id: str, user_id: str) -> bool:
        """禁言用户"""
        with self._lock:
            room = self._rooms.get(room_id)
            if not room or user_id not in room.members:
                return False
            
            user = room.members[user_id]
            
            # 群主不能被禁言
            if user_id == room.owner_id:
                logger.warning(f"[UserManager] Cannot mute room owner")
                return False
            
            user.status = UserStatus.MUTED
            user.publish_allowed = False  # 禁言同时禁止发布
            logger.info(f"[UserManager] User {user_id} muted in room {room_id}")
            return True
    
    def unmute_user(self, room_id: str, user_id: str) -> bool:
        """解除禁言"""
        with self._lock:
            room = self._rooms.get(room_id)
            if not room or user_id not in room.members:
                return False
            
            user = room.members[user_id]
            user.status = UserStatus.NORMAL
            user.publish_allowed = True
            logger.info(f"[UserManager] User {user_id} unmuted in room {room_id}")
            return True
    
    def disable_mic(self, room_id: str, user_id: str) -> bool:
        """禁麦用户（禁止使用麦克风发布）"""
        with self._lock:
            room = self._rooms.get(room_id)
            if not room or user_id not in room.members:
                return False
            
            user = room.members[user_id]
            
            # 群主不能被禁麦
            if user_id == room.owner_id:
                logger.warning(f"[UserManager] Cannot disable mic for room owner")
                return False
            
            user.status = UserStatus.MIC_OFF
            user.publish_allowed = False
            logger.info(f"[UserManager] User {user_id} mic disabled in room {room_id}")
            return True
    
    def enable_mic(self, room_id: str, user_id: str) -> bool:
        """解除禁麦"""
        with self._lock:
            room = self._rooms.get(room_id)
            if not room or user_id not in room.members:
                return False
            
            user = room.members[user_id]
            user.status = UserStatus.NORMAL
            user.publish_allowed = True
            logger.info(f"[UserManager] User {user_id} mic enabled in room {room_id}")
            return True
    
    def mute_all(self, room_id: str) -> int:
        """全体禁言（除群主外）"""
        with self._lock:
            room = self._rooms.get(room_id)
            if not room:
                return 0
            
            room.allow_speak = False
            count = 0
            for user_id, user in room.members.items():
                if user_id != room.owner_id:
                    user.status = UserStatus.MUTED
                    user.publish_allowed = False
                    count += 1
            
            logger.info(f"[UserManager] Muted {count} users in room {room_id}")
            return count
    
    def unmute_all(self, room_id: str) -> int:
        """解除全体禁言"""
        with self._lock:
            room = self._rooms.get(room_id)
            if not room:
                return 0
            
            room.allow_speak = True
            count = 0
            for user_id, user in room.members.items():
                if user.status == UserStatus.MUTED:
                    user.status = UserStatus.NORMAL
                    user.publish_allowed = True
                    count += 1
            
            logger.info(f"[UserManager] Unmuted {count} users in room {room_id}")
            return count
    
    def can_publish(self, room_id: str, user_id: str) -> bool:
        """检查用户是否可以发布（发言）"""
        with self._lock:
            room = self._rooms.get(room_id)
            if not room:
                return True  # 房间不存在时默认允许
            
            # 检查全体禁言
            if not room.allow_speak:
                user = room.members.get(user_id)
                if user and user_id == room.owner_id:
                    return True  # 群主始终可以发言
                return False
            
            # 检查个人禁言
            user = room.members.get(user_id)
            if not user:
                return True  # 新用户默认允许
            
            # 群主始终可以发言
            if user_id == room.owner_id:
                return True
            
            return user.publish_allowed
    
    # ========== 权限检查 ==========
    
    def can_manage_members(self, room_id: str, user_id: str) -> bool:
        """检查用户是否可以管理其他成员（群主或管理员）"""
        with self._lock:
            room = self._rooms.get(room_id)
            if not room:
                return False
            
            user = room.members.get(user_id)
            if not user:
                return False
            
            # 群主和管理员可以管理成员
            return user.role in [UserRole.OWNER, UserRole.ADMIN]
    
    def can_kick(self, room_id: str, operator_id: str, target_id: str) -> bool:
        """检查是否可以踢人"""
        with self._lock:
            room = self._rooms.get(room_id)
            if not room:
                return False
            
            # 不能踢自己
            if operator_id == target_id:
                return False
            
            # 群主可以踢任何人
            if operator_id == room.owner_id:
                return True
            
            # 管理员可以踢普通成员
            operator = room.members.get(operator_id)
            target = room.members.get(target_id)
            
            if not operator or not target:
                return False
            
            if operator.role == UserRole.ADMIN and target.role == UserRole.MEMBER:
                return True
            
            return False


# 全局单例
user_manager = UserManager()
