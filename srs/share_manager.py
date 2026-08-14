#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
分享链接管理模块
管理房间分享链接的创建、解析和过期清理
"""

import random
import string
import threading
import time
import logging
from dataclasses import dataclass, field
from typing import Dict, Optional

logger = logging.getLogger(__name__)

SHARE_LINK_EXPIRY_SECONDS = 24 * 60 * 60  # 1天
SHARE_ID_PREFIX = "share_"
SHARE_DOMAIN = "https://alove.app"


def _generate_share_id() -> str:
    chars = string.ascii_lowercase + string.digits
    suffix = ''.join(random.choices(chars, k=12))
    return f"{SHARE_ID_PREFIX}{suffix}"


@dataclass
class ShareLink:
    """分享链接"""
    share_id: str
    room_id: str
    room_name: str
    sharer_id: str
    sharer_name: str
    message: str
    created_at: int        # Unix 秒时间戳
    expires_at: int        # Unix 秒时间戳
    status: str = "active"  # "active" | "expired"

    def is_expired(self) -> bool:
        return self.status == "expired" or time.time() > self.expires_at

    def to_dict(self):
        return {
            "share_id": self.share_id,
            "room_id": self.room_id,
            "room_name": self.room_name,
            "sharer_id": self.sharer_id,
            "sharer_name": self.sharer_name,
            "message": self.message,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "status": self.status,
        }


class ShareManager:
    """分享链接管理器"""

    def __init__(self):
        self._links: Dict[str, ShareLink] = {}
        self._lock = threading.RLock()

    def create_share_link(
        self,
        room_id: str,
        room_name: str,
        sharer_id: str,
        sharer_name: str,
        message: str = "",
    ) -> ShareLink:
        """创建分享链接"""
        with self._lock:
            share_id = _generate_share_id()
            now = int(time.time())
            link = ShareLink(
                share_id=share_id,
                room_id=room_id,
                room_name=room_name,
                sharer_id=sharer_id,
                sharer_name=sharer_name,
                message=message[:200],  # 截断超长附言
                created_at=now,
                expires_at=now + SHARE_LINK_EXPIRY_SECONDS,
                status="active",
            )
            self._links[share_id] = link
            logger.info(f"[ShareManager] Created share link: {share_id} for room {room_id}")
            return link

    def get_share_link(self, share_id: str) -> Optional[ShareLink]:
        """获取分享链接"""
        with self._lock:
            link = self._links.get(share_id)
            if link and link.is_expired():
                link.status = "expired"
            return link

    def expire_share_link(self, share_id: str) -> bool:
        """标记分享链接为过期"""
        with self._lock:
            link = self._links.get(share_id)
            if not link:
                return False
            link.status = "expired"
            logger.info(f"[ShareManager] Expired share link: {share_id}")
            return True

    def cleanup_expired_links(self) -> int:
        """清理所有过期链接，返回清理数量"""
        with self._lock:
            count = 0
            now = time.time()
            for link in self._links.values():
                if link.status == "active" and now > link.expires_at:
                    link.status = "expired"
                    count += 1
            if count:
                logger.info(f"[ShareManager] Cleaned up {count} expired share links")
            return count


# 全局单例
share_manager = ShareManager()
