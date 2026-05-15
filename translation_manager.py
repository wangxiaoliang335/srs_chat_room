#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
翻译请求管理器
管理多用户多翻译请求，支持拉流者心跳追踪和容错机制
"""

import logging
import threading
import time
from typing import Dict, List, Optional, Any, Callable, Tuple
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
    source_lang: str = "auto"    # 源语言，默认为 auto
    status: TranslationStatus = TranslationStatus.PENDING  # 状态
    stream_url: str = ""         # 翻译流地址
    created_at: float = field(default_factory=lambda: time.time())
    # ========== 容错机制新增字段 ==========
    pullers: Dict[str, float] = field(default_factory=dict)  # 拉流者ID -> 最后心跳时间
    last_source_stream_seen: float = field(default_factory=lambda: time.time())  # 最后检测到源流的时间
    source_stream_active: bool = field(default=False)  # 源流是否活跃
    stop_reason: str = field(default="")  # 停止原因
    _no_puller_since: Optional[float] = field(default=None, repr=False)  # 无拉流者开始时间


class TranslationManager:
    """翻译请求管理器 - 支持拉流者心跳追踪和容错机制"""
    
    def __init__(self):
        self._lock = threading.RLock()
        # 存储结构: {room_id: {source_user: {to_lang: TranslationRequest}}}
        self._requests: Dict[str, Dict[str, Dict[str, TranslationRequest]]] = {}
        # 便于查询: {request_id: TranslationRequest}
        self._requests_by_id: Dict[str, TranslationRequest] = {}
        
        # 回调函数：停止翻译服务
        self._stop_callback: Optional[Callable[[str], None]] = None
        
        # 心跳超时配置（秒）
        self.heartbeat_timeout = 15  # 拉流者心跳超时时间
        self.source_stream_timeout = 300  # 源流检测超时时间（5分钟）
        self.no_puller_stop_delay = 300  # 无拉流者多久后停止翻译（5分钟）
        
        logger.info("[TranslationManager] Initialized with fault tolerance support")

    def set_stop_callback(self, callback: Callable[[str], None]):
        """设置停止翻译服务的回调函数"""
        self._stop_callback = callback
        logger.info("[TranslationManager] Stop callback registered")

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
    
    # ========== 拉流者心跳管理 ==========
    
    def register_puller(self, request_id: str, puller_id: str) -> Tuple[bool, bool]:
        """注册拉流者
        
        Args:
            request_id: 翻译请求ID
            puller_id: 拉流者ID（通常是target_user）
        
        Returns:
            (success, restored): 
                - success: 是否注册成功（请求存在）
                - restored: 是否自动恢复了已停止的翻译服务
        """
        with self._lock:
            request = self._requests_by_id.get(request_id)
            if not request:
                logger.warning(f"[TranslationManager] Cannot register puller: request {request_id} not found")
                return False, False
            
            restored = False
            
            # 如果请求已停止但有拉流者尝试注册，自动恢复翻译服务
            if request.status == TranslationStatus.STOPPED:
                logger.info(f"[TranslationManager] Restoring stopped translation: request={request_id}")
                request.status = TranslationStatus.ACTIVE
                request.stop_reason = ""
                request.pullers.clear()
                restored = True
            
            request.pullers[puller_id] = time.time()
            # 重置无拉流者计时器
            request._no_puller_since = None
            
            logger.info(f"[TranslationManager] Puller registered: request={request_id}, puller={puller_id}, "
                       f"total_pullers={len(request.pullers)}, restored={restored}")
            return True, restored
    
    def unregister_puller(self, request_id: str, puller_id: str) -> bool:
        """注销拉流者
        
        Args:
            request_id: 翻译请求ID
            puller_id: 拉流者ID
        
        Returns:
            True if unregistered successfully
        """
        with self._lock:
            request = self._requests_by_id.get(request_id)
            if not request:
                logger.warning(f"[TranslationManager] Cannot unregister puller: request {request_id} not found")
                return False
            
            if puller_id in request.pullers:
                del request.pullers[puller_id]
                logger.info(f"[TranslationManager] Puller unregistered: request={request_id}, puller={puller_id}, "
                           f"remaining_pullers={len(request.pullers)}")
                return True
            return False
    
    def heartbeat_puller(self, request_id: str, puller_id: str) -> bool:
        """拉流者心跳
        
        Args:
            request_id: 翻译请求ID
            puller_id: 拉流者ID
        
        Returns:
            True if heartbeat recorded successfully
        """
        with self._lock:
            request = self._requests_by_id.get(request_id)
            if not request:
                logger.warning(f"[TranslationManager] Cannot heartbeat: request {request_id} not found")
                return False
            
            if puller_id not in request.pullers:
                # 自动注册
                request.pullers[puller_id] = time.time()
                request._no_puller_since = None
                logger.info(f"[TranslationManager] Auto-registered puller on heartbeat: {puller_id}")
            else:
                request.pullers[puller_id] = time.time()
            
            return True
    
    def get_pullers(self, request_id: str) -> List[Dict[str, Any]]:
        """获取拉流者列表及其心跳状态
        
        Returns:
            List of pullers with their heartbeat info
        """
        with self._lock:
            request = self._requests_by_id.get(request_id)
            if not request:
                return []
            
            now = time.time()
            puller_list = []
            for puller_id, last_heartbeat in request.pullers.items():
                elapsed = now - last_heartbeat
                puller_list.append({
                    "puller_id": puller_id,
                    "last_heartbeat": last_heartbeat,
                    "seconds_ago": int(elapsed),
                    "is_alive": elapsed < self.heartbeat_timeout
                })
            return puller_list

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

    def get_active_translations(self) -> List[Dict[str, Any]]:
        """获取所有活跃的翻译请求"""
        with self._lock:
            result = []
            for request in self._requests_by_id.values():
                if request.status == TranslationStatus.ACTIVE:
                    result.append({
                        "request_id": request.request_id,
                        "room_id": request.room_id,
                        "source_user": request.source_user,
                        "target_user": request.target_user,
                        "to_lang": request.to_lang,
                        "source_lang": request.source_lang,
                        "status": request.status.value if hasattr(request.status, 'value') else request.status
                    })
            return result

    def check_and_cleanup(self) -> List[Dict[str, Any]]:
        """检查并清理超时的翻译请求
        
        检查以下条件：
        1. 拉流者心跳超时 -> 移除该拉流者，如果无拉流者了且源流不活跃 -> 停止翻译
        2. 源流长时间未检测到 -> 停止翻译
        
        Returns:
            List of cleanup results
        """
        with self._lock:
            now = time.time()
            results = []
            
            for request_id in list(self._requests_by_id.keys()):
                request = self._requests_by_id[request_id]
                
                if request.status == TranslationStatus.STOPPED:
                    continue
                
                cleanup_info = {
                    "request_id": request_id,
                    "room_id": request.room_id,
                    "source_user": request.source_user,
                    "to_lang": request.to_lang,
                    "pullers_removed": [],
                    "stopped": False,
                    "stop_reason": ""
                }
                
                # 检查1: 拉流者心跳超时
                for puller_id in list(request.pullers.keys()):
                    last_heartbeat = request.pullers[puller_id]
                    if now - last_heartbeat > self.heartbeat_timeout:
                        del request.pullers[puller_id]
                        cleanup_info["pullers_removed"].append(puller_id)
                        logger.info(f"[TranslationManager] Puller heartbeat timeout removed: "
                                  f"request={request_id}, puller={puller_id}")
                
                # 检查2: 是否需要停止翻译
                needs_stop = False
                
                # 条件A: 源流不活跃且超时
                if not request.source_stream_active:
                    if now - request.last_source_stream_seen > self.source_stream_timeout:
                        needs_stop = True
                        cleanup_info["stop_reason"] = "source_stream_timeout"
                
                # 条件B: 没有拉流者了
                if not needs_stop and len(request.pullers) == 0:
                    if request._no_puller_since is None:
                        request._no_puller_since = now
                    elif now - request._no_puller_since > self.no_puller_stop_delay:
                        needs_stop = True
                        cleanup_info["stop_reason"] = "no_pullers_timeout"
                
                if needs_stop and request.status != TranslationStatus.STOPPED:
                    request.status = TranslationStatus.STOPPED
                    request.stop_reason = cleanup_info["stop_reason"]
                    
                    # 调用停止回调
                    if self._stop_callback:
                        try:
                            self._stop_callback(request_id)
                            cleanup_info["stopped"] = True
                            logger.info(f"[TranslationManager] Stopped translation: {request_id}, "
                                      f"reason={cleanup_info['stop_reason']}")
                        except Exception as e:
                            logger.error(f"[TranslationManager] Error stopping translation {request_id}: {e}")
                    else:
                        cleanup_info["stopped"] = True
                
                if cleanup_info["pullers_removed"] or cleanup_info["stopped"]:
                    results.append(cleanup_info)
            
            return results
    
    # ========== 源流状态管理 ==========
    
    def update_source_stream_active(self, room_id: str, source_user: str, active: bool):
        """更新源流的活跃状态
        
        Args:
            room_id: 房间ID
            source_user: 说话人用户ID
            active: 是否活跃
        """
        with self._lock:
            now = time.time()
            
            # 查找所有相关的翻译请求
            if room_id in self._requests and source_user in self._requests[room_id]:
                for to_lang, request in self._requests[room_id][source_user].items():
                    if request.source_stream_active != active:
                        request.source_stream_active = active
                        request.last_source_stream_seen = now
                        # 重置无拉流者计时器
                        request._no_puller_since = None
                        
                        logger.info(f"[TranslationManager] Source stream status updated: "
                                  f"room={room_id}, user={source_user}, active={active}")
    
    def get_request_by_stream(self, room_id: str, stream_name: str) -> Optional[TranslationRequest]:
        """根据流名称查找翻译请求
        
        Args:
            room_id: 房间ID
            stream_name: 流名称（如 room1_user1）
        
        Returns:
            TranslationRequest if found
        """
        with self._lock:
            # stream_name 格式: {room_id}_{user_id}，room_id 以 'room' 开头
            room_prefix_pos = stream_name.find('room')
            if room_prefix_pos == -1:
                return None
            underscore_pos = stream_name.find('_', room_prefix_pos + 4)
            if underscore_pos == -1:
                return None
            parsed_room_id = stream_name[:underscore_pos]
            source_user = stream_name[underscore_pos + 1:]

            # 验证 room_id
            if parsed_room_id != room_id:
                return None

            # 查找翻译请求
            if room_id in self._requests and source_user in self._requests[room_id]:
                # 返回第一个活跃的翻译请求
                for to_lang, request in self._requests[room_id][source_user].items():
                    if request.status == TranslationStatus.ACTIVE:
                        return request

            return None
    
    def get_request_by_source(self, room_id: str, source_user: str, to_lang: str = None) -> Optional[TranslationRequest]:
        """根据源用户和目标语言查找翻译请求
        
        Args:
            room_id: 房间ID
            source_user: 说话人用户ID
            to_lang: 目标语言（可选，如果不指定返回第一个）
        
        Returns:
            TranslationRequest if found
        """
        with self._lock:
            if room_id not in self._requests or source_user not in self._requests[room_id]:
                return None
            
            if to_lang:
                return self._requests[room_id][source_user].get(to_lang)
            else:
                # 返回第一个活跃的
                for lang, request in self._requests[room_id][source_user].items():
                    if request.status == TranslationStatus.ACTIVE:
                        return request
            return None

    def start_translation(self, room_id: str, source_user: str, target_user: str,
                          to_lang: str, source_lang: str = "auto",
                          srs_url: str = "rtmp://localhost:1935") -> str:
        """启动翻译请求 - 创建请求并生成流地址"""
        import uuid

        request_id = f"trans_{uuid.uuid4().hex[:12]}"

        # 构建流名称 - 使用与客户端一致的命名格式
        # 客户端期望: translation_{source_user}_{to_lang}
        stream_name = f"translation_{source_user}_{to_lang}"
        stream_url = f"{srs_url}/app/{stream_name}"

        # 创建翻译请求
        request = TranslationRequest(
            request_id=request_id,
            room_id=room_id,
            source_user=source_user,
            target_user=target_user,
            to_lang=to_lang,
            source_lang=source_lang,
            stream_url=stream_url,
            status=TranslationStatus.ACTIVE
        )

        # 添加到管理器
        self.add_request(request)

        logger.info(f"[TranslationManager] Started translation: {request_id}, "
                   f"room={room_id}, source={source_user}, target={target_user}, "
                   f"to_lang={to_lang}, stream={stream_name}")

        return request_id

    def stop_translation_by_request(self, request_id: str) -> bool:
        """通过请求ID停止翻译"""
        request = self.get_request(request_id)
        if not request:
            logger.warning(f"[TranslationManager] Request not found: {request_id}")
            return False

        self.update_status(request_id, TranslationStatus.STOPPED)
        logger.info(f"[TranslationManager] Stopped translation: {request_id}")
        return True


# 全局单例
translation_manager = TranslationManager()
