#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SRS HTTP回调服务器 - FastAPI版本
同时支持原生 WebSocket 和 HTTP API
"""

import os
import sys
import json
import logging
import asyncio
import threading
from typing import Dict, Set
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# 加载 .env 环境变量文件
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))

from translation_manager import (
    TranslationManager, TranslationRequest, TranslationStatus, translation_manager
)
from user_manager import (
    UserManager, UserRole, UserStatus, user_manager
)
from notification_service import notification_service

# 配置日志
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

log_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'callback_server.log')
file_handler = logging.FileHandler(log_file, encoding='utf-8')
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))

console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))

logger.addHandler(file_handler)
logger.addHandler(console_handler)

# 全局变量
port = int(os.getenv('CALLBACK_PORT', 8085))
SRS_URL = os.getenv("SRS_URL", "rtmp://localhost:1935")

# WebSocket 连接管理
native_ws_connections: Dict[str, Set] = {}
native_ws_lock = threading.Lock()


class ConnectionManager:
    """WebSocket 连接管理器"""
    
    def __init__(self):
        self.active_connections: Dict[str, list] = {}  # room_id -> [WebSocket]
        self.user_connections: Dict[str, WebSocket] = {}  # user_id -> WebSocket
    
    async def connect(self, websocket: WebSocket, room_id: str, user_id: str = ""):
        # 注意: websocket.accept() 应该由调用者调用，这里只处理业务逻辑

        with native_ws_lock:
            if room_id not in self.active_connections:
                self.active_connections[room_id] = []
            self.active_connections[room_id].append(websocket)

        if user_id:
            self.user_connections[user_id] = websocket

        logger.info(f"[WS] Connected: room={room_id}, user={user_id}")

        await websocket.send_json({
            "type": "connected",
            "room_id": room_id,
            "user_id": user_id
        })
    
    def disconnect(self, websocket: WebSocket, room_id: str):
        with native_ws_lock:
            if room_id in self.active_connections:
                if websocket in self.active_connections[room_id]:
                    self.active_connections[room_id].remove(websocket)
                if not self.active_connections[room_id]:
                    del self.active_connections[room_id]
        
        # 移除用户连接
        for uid, ws in list(self.user_connections.items()):
            if ws == websocket:
                del self.user_connections[uid]
        
        logger.info(f"[WS] Disconnected: room={room_id}")
    
    async def broadcast_to_room(self, room_id: str, message: dict):
        """向房间广播消息"""
        with native_ws_lock:
            connections = list(self.active_connections.get(room_id, []))
        
        disconnected = []
        for ws in connections:
            try:
                await ws.send_json(message)
            except:
                disconnected.append(ws)
        
        # 清理断开的连接
        for ws in disconnected:
            self.disconnect(ws, room_id)
    
    async def send_to_user(self, user_id: str, message: dict):
        """向特定用户发送消息"""
        if user_id in self.user_connections:
            try:
                await self.user_connections[user_id].send_json(message)
            except:
                del self.user_connections[user_id]


manager = ConnectionManager()

# FastAPI 应用
@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    logger.info("[FastAPI] Starting up...")
    
    # 启动心跳检查线程
    start_heartbeat_checker()
    
    # 设置 notification_service
    notification_service._native_ws_manager = manager
    
    yield
    
    logger.info("[FastAPI] Shutting down...")


app = FastAPI(title="SRS Callback Server", lifespan=lifespan)

# CORS 配置
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ========== WebSocket 端点 ==========

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """原生 WebSocket 端点"""
    # 解析 room 和 user 参数
    room_id = websocket.query_params.get("room", "")
    user_id = websocket.query_params.get("user", "")

    if not room_id:
        await websocket.close(code=1008, reason="Missing room parameter")
        return

    await websocket.accept()  # 接受连接
    await manager.connect(websocket, room_id, user_id)
    
    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get('type', '')
            
            if msg_type == 'ping':
                await websocket.send_json({"type": "pong"})
            elif msg_type == 'subscribe':
                new_room = data.get('room_id', room_id)
                manager.disconnect(websocket, room_id)
                await manager.connect(websocket, new_room, user_id)
                room_id = new_room
                
    except WebSocketDisconnect:
        manager.disconnect(websocket, room_id)
    except Exception as e:
        logger.warning(f"[WS] Error: {e}")
        manager.disconnect(websocket, room_id)


# ========== HTTP API 端点 ==========

class RoomCreateRequest(BaseModel):
    room_id: str
    owner_id: str

class RoomJoinRequest(BaseModel):
    user_id: str
    room_id: str = None  # 可选，URL 路径中有则不需要

class RoomLeaveRequest(BaseModel):
    user_id: str

class TranslationStartRequest(BaseModel):
    room_id: str
    source_user: str
    source_lang: str = None
    to_lang: str = None  # 兼容客户端字段名
    target_user: str
    target_lang: str = None

class TranslationTextRequest(BaseModel):
    room_id: str
    source_user: str
    target_user: str
    original_text: str
    translated_text: str
    source_lang: str
    target_lang: str

@app.get("/health")
async def health_check():
    return {"status": "ok"}

@app.get("/api/v1/health")
async def health_check_v1():
    return {"status": "ok"}

@app.get("/api/v1/rooms")
async def get_rooms():
    """获取所有房间列表"""
    try:
        rooms = user_manager.get_all_rooms()
        room_list = [
            {
                "room_id": room.room_id,
                "owner_id": room.owner_id,
                "member_count": len(room.members)
            }
            for room in rooms
        ]
        return {"status": "ok", "rooms": room_list}
    except Exception as e:
        logger.error(f"[API] Failed to get rooms: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/room")
async def create_room(req: RoomCreateRequest):
    """创建房间"""
    try:
        user_manager.create_room(req.room_id, req.owner_id)
        logger.info(f"[API] Created room: {req.room_id}, owner: {req.owner_id}")
        return {"status": "ok", "room_id": req.room_id}
    except Exception as e:
        logger.error(f"[API] Failed to create room: {e}")
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/v1/room/{room_id}/join")
async def join_room_rest(room_id: str, req: RoomJoinRequest):
    """加入房间（REST风格）"""
    try:
        # 使用路径中的 room_id（如果请求体中也有，则优先使用）
        actual_room_id = req.room_id if req.room_id else room_id
        user_manager.join_room(actual_room_id, req.user_id)
        members = user_manager.get_room_members(actual_room_id)

        # 广播用户加入事件
        await manager.broadcast_to_room(actual_room_id, {
            "type": "member_joined",
            "room_id": actual_room_id,
            "user_id": req.user_id
        })

        logger.info(f"[API] User {req.user_id} joined room {actual_room_id}")
        return {"status": "ok", "members": members}
    except Exception as e:
        logger.error(f"[API] Failed to join room: {e}")
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/v1/room/{room_id}/leave")
async def leave_room_rest(room_id: str, req: RoomLeaveRequest):
    """离开房间（REST风格）"""
    try:
        user_manager.leave_room(room_id, req.user_id)

        # 广播用户离开事件
        await manager.broadcast_to_room(room_id, {
            "type": "member_left",
            "room_id": room_id,
            "user_id": req.user_id
        })

        logger.info(f"[API] User {req.user_id} left room {room_id}")
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"[API] Failed to leave room: {e}")
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/api/v1/room/{room_id}")
async def get_room_info(room_id: str):
    """获取房间信息"""
    try:
        room = user_manager.get_room(room_id)
        if not room:
            raise HTTPException(status_code=404, detail="Room not found")
        return {
            "status": "ok",
            "room": {
                "room_id": room.room_id,
                "owner_id": room.owner_id,
                "member_count": len(room.members),
                "allow_speak": room.allow_speak
            }
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[API] Failed to get room: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/v1/room/{room_id}/members")
async def get_room_members_rest(room_id: str):
    """获取房间成员列表（REST风格）"""
    try:
        members = user_manager.get_room_members(room_id)
        member_list = [
            {
                "user_id": m.user_id,
                "room_id": m.room_id,
                "role": m.role.value if hasattr(m.role, 'value') else m.role,
                "status": m.status.value if hasattr(m.status, 'value') else m.status,
                "publish_allowed": m.publish_allowed
            }
            for m in members
        ]
        return {"status": "ok", "members": member_list}
    except Exception as e:
        logger.error(f"[API] Failed to get members: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/v1/room/{room_id}/check-publish")
async def check_publish_permission(room_id: str, user_id: str):
    """检查用户是否可以发布（发言）"""
    try:
        can_publish = user_manager.can_publish(room_id, user_id)
        return {"status": "ok", "can_publish": can_publish}
    except Exception as e:
        logger.error(f"[API] Failed to check publish permission: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/v1/ws/subscribe")
async def ws_subscribe(request: Request):
    """WebSocket 订阅（兼容旧接口）"""
    try:
        data = await request.json()
        room_id = data.get("room_id", "")
        user_id = data.get("user_id", "")
        return {
            "status": "ok",
            "ws_url": f"ws://47.107.33.154:8085/ws?room={room_id}&user={user_id}"
        }
    except Exception as e:
        logger.error(f"[API] Failed to subscribe: {e}")
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/api/v1/ws/status")
async def ws_status():
    """WebSocket 状态"""
    with native_ws_lock:
        active_count = sum(len(conns) for conns in native_ws_connections.values())
    return {"status": "ok", "active_connections": active_count}

@app.post("/api/v1/translation/start")
async def start_translation(req: TranslationStartRequest):
    """开始翻译"""
    try:
        # 兼容客户端字段名
        actual_source_lang = req.source_lang if req.source_lang else "auto"
        actual_target_lang = req.target_lang if req.target_lang else req.to_lang

        request_id = translation_manager.start_translation(
            room_id=req.room_id,
            source_user=req.source_user,
            target_user=req.target_user,
            to_lang=actual_target_lang,
            source_lang=actual_source_lang
        )

        # 启动翻译服务进程
        pid = start_translation_service(request_id, SRS_URL)
        if not pid:
            logger.warning(f"[API] Failed to start translation service process: {request_id}")

        # 通知客户端
        await manager.broadcast_to_room(req.room_id, {
            "type": "translation_started",
            "room_id": req.room_id,
            "source_user": req.source_user,
            "target_user": req.target_user,
            "target_lang": actual_target_lang
        })

        logger.info(f"[API] Started translation: {request_id}")
        return {"status": "ok", "request_id": request_id}
    except Exception as e:
        logger.error(f"[API] Failed to start translation: {e}")
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/v1/translation/text")
async def translation_text(req: TranslationTextRequest):
    """发送翻译文本"""
    try:
        # 广播翻译结果
        await manager.broadcast_to_room(req.room_id, {
            "type": "translation_text",
            "room_id": req.room_id,
            "source_user": req.source_user,
            "target_user": req.target_user,
            "original_text": req.original_text,
            "translated_text": req.translated_text,
            "source_lang": req.source_lang,
            "target_lang": req.target_lang
        })
        
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"[API] Failed to send translation text: {e}")
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/v1/translation/stop")
async def stop_translation(request_id: str):
    """停止翻译"""
    try:
        translation_manager.stop_translation_by_request(request_id)
        
        req = translation_manager.get_request(request_id)
        if req:
            await manager.broadcast_to_room(req.room_id, {
                "type": "translation_stopped",
                "room_id": req.room_id,
                "source_user": req.source_user
            })
        
        logger.info(f"[API] Stopped translation: {request_id}")
        return {"status": "ok"}
    except Exception as e:
        logger.error(f"[API] Failed to stop translation: {e}")
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/api/v1/rooms/{room_id}/members")
async def get_room_members(room_id: str):
    """获取房间成员"""
    members = user_manager.get_room_members(room_id)
    return {"status": "ok", "members": members}

@app.get("/api/v1/translation/active")
async def get_active_translations():
    """获取活跃翻译"""
    translations = translation_manager.get_active_translations()
    return {"status": "ok", "translations": translations}


# ========== SRS HTTP Hooks 回调端点 ==========

# SRS 配置的路径别名
@app.post("/api/v1/streams/on_publish")
async def streams_on_publish(request: Request):
    """SRS 推流开始回调"""
    try:
        data = await request.json()
        logger.info(f"[SRS Hook] on_publish: {data}")
        return {"code": 0, "server": SRS_URL}
    except Exception as e:
        logger.error(f"[SRS Hook] on_publish error: {e}")
        return {"code": 1, "msg": str(e)}

@app.post("/api/v1/streams/on_unpublish")
async def streams_on_unpublish(request: Request):
    """SRS 推流结束回调"""
    try:
        data = await request.json()
        logger.info(f"[SRS Hook] on_unpublish: {data}")
        return {"code": 0}
    except Exception as e:
        logger.error(f"[SRS Hook] on_unpublish error: {e}")
        return {"code": 1, "msg": str(e)}

@app.post("/api/v1/streams/on_play")
async def streams_on_play(request: Request):
    """SRS 播放开始回调"""
    try:
        data = await request.json()
        logger.info(f"[SRS Hook] on_play: {data}")
        return {"code": 0}
    except Exception as e:
        logger.error(f"[SRS Hook] on_play error: {e}")
        return {"code": 1, "msg": str(e)}

@app.post("/api/v1/streams/on_stop")
async def streams_on_stop(request: Request):
    """SRS 播放结束回调"""
    try:
        data = await request.json()
        logger.info(f"[SRS Hook] on_stop: {data}")
        return {"code": 0}
    except Exception as e:
        logger.error(f"[SRS Hook] on_stop error: {e}")
        return {"code": 1, "msg": str(e)}

# 原始路径别名（向后兼容）
@app.post("/api/v1/hooks/on_publish")
async def on_publish(request: Request):
    """SRS 推流开始回调"""
    try:
        data = await request.json()
        logger.info(f"[SRS Hook] on_publish: {data}")
        return {"code": 0, "server": SRS_URL}
    except Exception as e:
        logger.error(f"[SRS Hook] on_publish error: {e}")
        return {"code": 1, "msg": str(e)}

@app.post("/api/v1/hooks/on_unpublish")
async def on_unpublish(request: Request):
    """SRS 推流结束回调"""
    try:
        data = await request.json()
        logger.info(f"[SRS Hook] on_unpublish: {data}")
        
        # 停止该流的翻译
        stream_key = data.get("stream_key", "")
        if stream_key in translation_processes:
            request_id = translation_processes[stream_key]
            translation_manager.stop_translation_by_request(request_id)
            stop_translation_service(request_id)
        
        return {"code": 0}
    except Exception as e:
        logger.error(f"[SRS Hook] on_unpublish error: {e}")
        return {"code": 1, "msg": str(e)}

@app.post("/api/v1/hooks/on_play")
async def on_play(request: Request):
    """SRS 播放开始回调"""
    try:
        data = await request.json()
        logger.info(f"[SRS Hook] on_play: {data}")
        return {"code": 0}
    except Exception as e:
        logger.error(f"[SRS Hook] on_play error: {e}")
        return {"code": 1, "msg": str(e)}

@app.post("/api/v1/hooks/on_stop")
async def on_stop(request: Request):
    """SRS 播放结束回调"""
    try:
        data = await request.json()
        logger.info(f"[SRS Hook] on_stop: {data}")
        return {"code": 0}
    except Exception as e:
        logger.error(f"[SRS Hook] on_stop error: {e}")
        return {"code": 1, "msg": str(e)}


# ========== 心跳检查线程 ==========

heartbeat_check_thread = None
heartbeat_check_running = False

def heartbeat_check_worker():
    """心跳检查工作线程"""
    global heartbeat_check_running
    
    # 在线程内导入，避免循环引用
    from translation_manager import translation_manager
    
    logger.info("[HeartbeatCheck] Starting heartbeat check worker")
    
    while heartbeat_check_running:
        try:
            results = translation_manager.check_and_cleanup()
            
            for result in results:
                if result["stopped"]:
                    logger.info(f"[HeartbeatCheck] Cleaned up translation: {result['request_id']}")
                    
                    asyncio.run(manager.broadcast_to_room(result["room_id"], {
                        "type": "translation_stopped",
                        "room_id": result["room_id"],
                        "source_user": result["source_user"]
                    }))
        
        except Exception as e:
            logger.error(f"[HeartbeatCheck] Error: {e}", exc_info=True)
        
        for _ in range(10):
            if not heartbeat_check_running:
                break
            threading.Event().wait(1)


def start_heartbeat_checker():
    """启动心跳检查线程"""
    global heartbeat_check_thread, heartbeat_check_running
    
    if heartbeat_check_thread is not None and heartbeat_check_thread.is_alive():
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


# ========== 翻译进程管理 ==========

translation_processes: Dict[str, str] = {}  # stream_key -> request_id

def start_translation_service(request_id: str, srs_url: str):
    """启动翻译服务进程"""
    import subprocess

    # 获取请求信息
    req = translation_manager.get_request(request_id)
    if not req:
        logger.error(f"[Translation] Request not found: {request_id}")
        return None

    # 构建流名称 - 与客户端一致的命名格式
    stream_name = f"translation_{req.source_user}_{req.to_lang}"

    # 构建命令
    cmd = [
        sys.executable,
        os.path.join(os.path.dirname(__file__), "audio_translation_service_websocket.py")
    ]

    # 设置环境变量
    env = os.environ.copy()
    env['REQUEST_ID'] = request_id
    env['ROOM_ID'] = req.room_id
    env['SOURCE_USER'] = req.source_user
    env['TO_LANG'] = req.to_lang
    env['FROM_LANG'] = req.source_lang
    env['TARGET_USER'] = req.target_user
    env['STREAM_NAME'] = stream_name
    env['SRS_URL'] = srs_url
    
    # 输入音频保存配置
    env['INPUT_SAVE_ENABLED'] = os.getenv('INPUT_SAVE_ENABLED', 'true')
    env['INPUT_SAVE_DIR'] = os.getenv('INPUT_SAVE_DIR', 'input_recordings')

    try:
        log_file = f"translation_{request_id}.log"
        with open(log_file, "w") as f:
            proc = subprocess.Popen(cmd, env=env, stdout=f, stderr=subprocess.STDOUT)

        translation_processes[proc.pid] = request_id
        logger.info(f"[Translation] Started service: {request_id}, PID: {proc.pid}, stream: {stream_name}")
        return proc.pid
    except Exception as e:
        logger.error(f"[Translation] Failed to start service: {e}")
        return None

def stop_translation_service(request_id: str):
    """停止翻译服务进程"""
    for pid, rid in list(translation_processes.items()):
        if rid == request_id:
            try:
                import signal
                os.kill(pid, signal.SIGTERM)
                del translation_processes[pid]
                logger.info(f"[Translation] Stopped service: {request_id}")
            except:
                pass


# ========== 启动服务器 ==========

if __name__ == '__main__':
    import uvicorn
    
    logger.info(f"Starting FastAPI server on 0.0.0.0:{port}")
    logger.info(f"SRS URL: {SRS_URL}")
    
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        log_level="info"
    )
