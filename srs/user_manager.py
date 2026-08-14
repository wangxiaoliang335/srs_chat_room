#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
用户管理模块
管理聊天室中的用户身份、角色和权限

持久化：所有房间/成员/状态变更都同步写入 user_manager.json（与 auth.users.json 风格一致），
        服务器重启后自动从该文件恢复，避免房间/成员/mic 状态全部丢失。
"""

import os
import json
import threading
import time
from enum import Enum
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from datetime import datetime
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# 持久化文件：与 auth.py 的 USERS_FILE 同目录
ROOMS_FILE = Path(__file__).parent / "user_manager.json"


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


# 2026-08-13 文档：移除 OwnerStatus 枚举
# 所有成员（含房主）统一用 User.online_status 字段，由 room_socket 心跳判定
# class OwnerStatus(Enum):
#     ONLINE = "online"
#     OFFLINE = "offline"


class RoomStatus(Enum):
    """房间状态"""
    ACTIVE = "active"           # 正常运行
    CLOSED = "closed"           # 已被关闭，不可再加入


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
    # 2026-08-13 文档：成员在线/离线状态（由 room_socket 心跳判定，非 join 设置）
    online_status: str = "offline"  # online / offline
    offline_at: Optional[int] = None  # 最近离线时间戳（None = 在线）

    def to_dict(self):
        return {
            'user_id': self.user_id,
            'room_id': self.room_id,
            'role': self.role.value,
            'status': self.status.value,
            'joined_at': self.joined_at,
            'last_active': self.last_active,
            'publish_allowed': self.publish_allowed,
            'online_status': self.online_status,
            'offline_at': self.offline_at,
        }

    def mark_online(self) -> None:
        """标记为在线（由 room_socket 心跳建立连接时调用）"""
        self.online_status = "online"
        self.offline_at = None
        self.last_active = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    def mark_offline(self, ts: Optional[int] = None) -> None:
        """标记为离线（3 次心跳失败时调用）"""
        if ts is None:
            ts = int(datetime.now().timestamp())
        self.online_status = "offline"
        self.offline_at = ts


@dataclass
class Room:
    """房间信息"""
    room_id: str
    name: str = ""
    owner_id: str = ""          # 群主ID
    status: RoomStatus = RoomStatus.ACTIVE          # 房间状态
    closed_by: str = ""          # 关闭者 user_id（仅 closed 时有值）
    closed_at: str = ""          # 关闭时间戳（仅 closed 时有值）
    close_reason: str = ""       # 关闭原因（如 owner_entered_another_room）
    created_at: str = ""
    max_members: int = 100
    allow_speak: bool = True    # 是否允许发言（全体禁言开关）
    members: Dict[str, User] = field(default_factory=dict)

    def to_dict(self):
        return {
            'room_id': self.room_id,
            'name': self.name,
            'owner_id': self.owner_id,
            # 2026-08-13 文档：移除 owner_status 字段（统一用成员 online_status）
            'status': self.status.value,
            'closed_by': self.closed_by,
            'closed_at': self.closed_at,
            'close_reason': self.close_reason,
            'created_at': self.created_at,
            'max_members': self.max_members,
            'allow_speak': self.allow_speak,
            'member_count': len(self.members)
        }


class UserManager:
    """用户管理器"""

    def __init__(self, path: Path = ROOMS_FILE):
        # 存储结构: {room_id: Room}
        self._rooms: Dict[str, Room] = {}
        # 流名称到用户的映射: {stream_name: user_id}
        # stream_name 格式: {room_id}_{user_id}
        self._stream_to_user: Dict[str, str] = {}
        self._lock = threading.RLock()
        self._path: Path = path
        # 启动时从文件恢复
        self._load()

    # ========== 持久化 ==========
    # 写后立即同步落盘（与 user_store 风格一致），单文件 JSON + 原子写。
    # 锁：所有 _flush 都在 self._lock 持锁状态下调用，避免与业务写入竞争。
    def _serialize(self) -> dict:
        rooms_out = {}
        for rid, r in self._rooms.items():
            d = r.to_dict()
            d["members"] = {uid: m.to_dict() for uid, m in r.members.items()}
            rooms_out[rid] = d
        return {
            "version": 1,
            "saved_at": int(time.time()),
            "rooms": rooms_out,
            "stream_to_user": dict(self._stream_to_user),
        }

    def _flush(self) -> None:
        """原子写：先写 .tmp，再 rename，避免半写状态。"""
        try:
            data = self._serialize()
            tmp = self._path.with_suffix(".json.tmp")
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            tmp.replace(self._path)
        except Exception as e:
            # 落盘失败不应阻塞内存里的业务逻辑（避免用户被踢出房间）
            logger.error(f"[UserManager] _flush 失败: {e}")

    def _load(self) -> None:
        """从文件恢复：启动时调用一次。损坏/不存在时静默走空状态。"""
        if not self._path.exists():
            logger.info(f"[UserManager] 持久化文件不存在 ({self._path})，从空状态启动")
            return
        try:
            with self._path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                logger.warning("[UserManager] 持久化文件格式非法（根不是 dict），忽略")
                return

            rooms_in = data.get("rooms") or {}
            for rid, rd in rooms_in.items():
                if not isinstance(rd, dict):
                    continue
                try:
                    room = Room(
                        room_id=rd.get("room_id", rid),
                        name=rd.get("name", ""),
                        owner_id=rd.get("owner_id", ""),
                        # 2026-08-13：忽略旧 owner_status 字段
                        status=RoomStatus(rd.get("status", "active")),
                        closed_by=rd.get("closed_by", ""),
                        closed_at=rd.get("closed_at", ""),
                        close_reason=rd.get("close_reason", ""),
                        created_at=rd.get("created_at", ""),
                        max_members=rd.get("max_members", 100),
                        allow_speak=rd.get("allow_speak", True),
                    )
                except (ValueError, TypeError) as e:
                    logger.warning(f"[UserManager] 房间 {rid} 解析失败: {e}，跳过")
                    continue

                for uid, md in (rd.get("members") or {}).items():
                    if not isinstance(md, dict):
                        continue
                    try:
                        user = User(
                            user_id=md.get("user_id", uid),
                            room_id=rid,
                            role=UserRole(md.get("role", "member")),
                            status=UserStatus(md.get("status", "normal")),
                            joined_at=md.get("joined_at", ""),
                            last_active=md.get("last_active", ""),
                            publish_allowed=md.get("publish_allowed", True),
                        )
                        room.members[uid] = user
                    except (ValueError, TypeError) as e:
                        logger.warning(f"[UserManager] 房间 {rid} 成员 {uid} 解析失败: {e}，跳过")
                        continue
                self._rooms[rid] = room

            self._stream_to_user = dict(data.get("stream_to_user") or {})

            logger.info(
                f"[UserManager] 从 {self._path} 恢复 "
                f"{len(self._rooms)} 个房间，{len(self._stream_to_user)} 个流映射"
            )
        except Exception as e:
            # 损坏的 JSON 走空状态，不让启动失败
            logger.error(f"[UserManager] 加载持久化文件失败: {e}，从空状态启动")

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
            self._flush()
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
            self._flush()
            return True

    def close_room(self, room_id: str, closed_by: str, reason: str = "") -> bool:
        """关闭房间（保留房主占位，仅清理其他成员）"""
        with self._lock:
            if room_id not in self._rooms:
                return False
            
            room = self._rooms[room_id]
            
            # 清理流映射（保留房主）
            for user_id in list(room.members.keys()):
                if user_id != room.owner_id:
                    stream_name = f"{room_id}_{user_id}"
                    self._stream_to_user.pop(stream_name, None)
                    del room.members[user_id]
            
            # 标记房间关闭
            room.status = RoomStatus.CLOSED
            room.closed_by = closed_by
            room.closed_at = self._get_current_time()
            room.close_reason = reason

            # 2026-08-13：房主在线/离线改用 User.online_status（不在 Room 上）

            logger.info(f"[UserManager] Closed room {room_id} by {closed_by}, reason={reason}")
            self._flush()
            return True

    # 2026-08-13：移除 set_owner_online / set_owner_offline
    # 房主在线/离线统一通过 User.online_status 字段管理（在成员 User 上）
    # 不再提供房间级 owner_status 操作

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

            # 拒绝加入已关闭的房间
            if room.status == RoomStatus.CLOSED:
                raise ValueError(f"Room {room_id} is closed")

            if user_id in room.members:
                # 更新最后活跃时间
                user = room.members[user_id]
                user.last_active = self._get_current_time()
                # 2026-08-13：join 不再设在线状态（由心跳判定）
                return user
            
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
            self._flush()
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

            # 2026-08-13 文档：移除"房主离开 → owner_offline"概念
            # 文档要求成员仅有在线/离线，由心跳判定，leave 不存在
            # 踢人/关房间才会真正移除成员关联（见 kick_member / close_room）
            del room.members[user_id]

            # 如果房间空了，删除房间
            if not room.members:
                del self._rooms[room_id]

            self._flush()
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

    def find_room_for_user(self, user_id: str) -> Optional[str]:
        """反查：某 user_id 当前所在的 room_id（在线且在房间内）。仅返回第一个匹配。
        用于中间件实时同步 request.state.room_id。
        """
        if not user_id:
            return None
        with self._lock:
            for room_id, room in self._rooms.items():
                if user_id in room.members:
                    return room_id
            return None

    def find_owned_room(self, user_id: str) -> Optional[str]:
        """查找 user_id 作为房主（owner）的房间 ID（仅 ACTIVE）。

        2026-08-13 文档 §7.1：每账号最多拥有 1 个房主房间。
        用于创建房间时检查唯一性 + 覆盖更新。
        """
        if not user_id:
            return None
        with self._lock:
            for room_id, room in self._rooms.items():
                if room.status == RoomStatus.ACTIVE and room.owner_id == user_id:
                    return room_id
            return None

    def update_room(self, room_id: str, name: Optional[str] = None,
                    max_members: Optional[int] = None,
                    allow_speak: Optional[bool] = None) -> Optional[Room]:
        """覆盖更新房间字段（2026-08-13 文档 §7.1：已拥有房主房间时创建房间走覆盖语义）。

        仅更新非空字段；返回更新后的 Room 对象，不存在返回 None。
        """
        with self._lock:
            room = self._rooms.get(room_id)
            if not room:
                return None
            if name is not None:
                room.name = name
            if max_members is not None:
                room.max_members = max_members
            if allow_speak is not None:
                room.allow_speak = allow_speak
            self._flush()
            return room

    def get_user_room_info(self, user_id: str) -> Optional[dict]:
        """获取指定用户所在的房间信息（房主或成员），用于"查找好友房间"流程。
        返回 None 表示用户不在任何 active/closed 房间中。
        """
        if not user_id:
            return None
        with self._lock:
            for room_id, room in self._rooms.items():
                if room.status == RoomStatus.ACTIVE and user_id in room.members:
                    member = room.members[user_id]
                    # 在线成员数（不含 owner_offline 时的 owner，因为 owner 不在 members 里了）
                    # 2026-08-13：online_count 改为统计 User.online_status == 'online' 的成员
                    online_count = sum(
                        1 for m in room.members.values()
                        if m.online_status == "online"
                    )
                    your_role = "owner" if user_id == room.owner_id else member.role.value
                    owner_member = room.members.get(room.owner_id) if room.owner_id else None
                    owner_online_status = owner_member.online_status if owner_member else "offline"
                    return {
                        "room_id": room_id,
                        "room_name": room.name,
                        "owner_id": room.owner_id,
                        "owner_online_status": owner_online_status,  # 2026-08-13 替代 owner_status
                        "your_role": your_role,
                        "member_count": len(room.members),
                        "online_count": online_count,
                    }
            return None
    
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
            self._flush()
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
            self._flush()
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
            self._flush()
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
            self._flush()
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
            self._flush()
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
            if count > 0:
                self._flush()
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
            if count > 0:
                self._flush()
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
