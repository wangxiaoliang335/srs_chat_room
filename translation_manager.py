#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
翻译请求管理器
管理多用户多翻译请求
"""

import logging
import threading
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field
from enum import Enum

logger = logging.getLogger(__name__)


class TranslationStatus(Enum):
    """翻译请求状态"""
    PENDING = "pending"      # 待处理
    ACTIVE = "active"        # 翻译中
    STOPPED = "stopped"      # 已停止


@dataclass
class TranslationRequest:
    """翻译请求"""
    request_id: str              # 请求ID
    room_id: str                 # 房间ID
    source_user: str             # 说话人用户ID
    target_user: str             # 听翻译的用户ID
    to_lang: str                 # 目标语言
    status: TranslationStatus   # 状态
    stream_url: str              # 翻译流地址
    created_at: float = field(default_factory=lambda: __import__('time').time())


class TranslationManager:
    """翻译请求管理器"""
    
    def __init__(self):
        self._lock = threading.RLock()
        # 存储结构: {room_id: {source_user: {to_lang: TranslationRequest}}}
        self._requests: Dict[str, Dict[str, Dict[str, TranslationRequest]]] = {}
        # 便于查询: {request_id: TranslationRequest}
        self._requests_by_id: Dict[str, TranslationRequest] = {}
    
    def add_request(self, request: TranslationRequest) -> bool:
        """添加翻译请求"""
        with self._lock:
            room_id = request.room_id
            source_user = request.source_user
            to_lang = request.to_lang
            
            # 检查是否已存在相同请求
            if room_id in self._requests:
                if source_user in self._requests[room_id]:
                    if to_lang in self._requests[room_id][source_user]:
                        existing = self._requests[room_id][source_user][to_lang]
                        if existing.status == TranslationStatus.ACTIVE:
                            logger.warning(f"[TranslationManager] Translation already exists: "
                                         f"room={room_id}, source={source_user}, to_lang={to_lang}")
                            return False
            
            # 添加请求
            if room_id not in self._requests:
                self._requests[room_id] = {}
            if source_user not in self._requests[room_id]:
                self._requests[room_id][source_user] = {}
            
            self._requests[room_id][source_user][to_lang] = request
            self._requests_by_id[request.request_id] = request
            
            logger.info(f"[TranslationManager] Added request: request_id={request.request_id}, "
                       f"room={room_id}, source={source_user}, target={request.target_user}, to_lang={to_lang}")
            return True
    
    def remove_request(self, request_id: str) -> bool:
        """移除翻译请求"""
        with self._lock:
            if request_id not in self._requests_by_id:
                logger.warning(f"[TranslationManager] Request not found: {request_id}")
                return False
            
            request = self._requests_by_id[request_id]
            room_id = request.room_id
            source_user = request.source_user
            to_lang = request.to_lang
            
            del self._requests[room_id][source_user][to_lang]
            del self._requests_by_id[request_id]
            
            # 清理空结构
            if not self._requests[room_id][source_user]:
                del self._requests[room_id][source_user]
            if not self._requests[room_id]:
                del self._requests[room_id]
            
            logger.info(f"[TranslationManager] Removed request: request_id={request_id}, "
                       f"room={room_id}, source={source_user}, to_lang={to_lang}")
            return True
    
    def get_request(self, request_id: str) -> Optional[TranslationRequest]:
        """获取翻译请求"""
        with self._lock:
            return self._requests_by_id.get(request_id)
    
    def get_user_streams(self, room_id: str, user_id: str, srs_url: str) -> List[Dict[str, Any]]:
        """获取用户的可用流列表（某用户在某房间中可以听到的流）
        
        Args:
            room_id: 房间ID
            user_id: 听者用户ID
            srs_url: SRS服务器地址
        """
        with self._lock:
            streams = []
            
            # 添加房间内所有说话人的原声流
            # 只要有翻译请求存在，就说明该说话人在房间内，可以听到其原声
            if room_id in self._requests:
                for source_user in self._requests[room_id].keys():
                    original_stream_url = f"{srs_url}/live/{room_id}_{source_user}"
                    streams.append({
                        "type": "original",
                        "user_id": source_user,
                        "url": original_stream_url,
                        "description": f"{source_user}的原声音频"
                    })
            
            # 添加翻译流（当前用户申请的翻译）
            if room_id in self._requests:
                for source_user, lang_requests in self._requests[room_id].items():
                    for to_lang, request in lang_requests.items():
                        if request.target_user == user_id and request.status == TranslationStatus.ACTIVE:
                            streams.append({
                                "type": "translation",
                                "source_user": source_user,
                                "to_lang": to_lang,
                                "url": request.stream_url,
                                "description": f"{source_user}的{to_lang}翻译"
                            })
            
            logger.info(f"[TranslationManager] get_user_streams: room={room_id}, user={user_id}, "
                       f"found {len(streams)} streams")
            return streams
    
    def get_requests_by_source(self, room_id: str, source_user: str) -> List[TranslationRequest]:
        """获取某说话人的所有翻译请求"""
        with self._lock:
            if room_id not in self._requests:
                return []
            if source_user not in self._requests[room_id]:
                return []
            return list(self._requests[room_id][source_user].values())
    
    def update_status(self, request_id: str, status: TranslationStatus) -> bool:
        """更新请求状态"""
        with self._lock:
            if request_id not in self._requests_by_id:
                return False
            self._requests_by_id[request_id].status = status
            return True
    
    def get_all_requests(self) -> List[TranslationRequest]:
        """获取所有翻译请求"""
        with self._lock:
            return list(self._requests_by_id.values())


# 全局单例
translation_manager = TranslationManager()
