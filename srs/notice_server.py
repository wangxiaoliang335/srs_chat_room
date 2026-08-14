#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
notice_socket 独立服务（2026-08-13 文档 §1.1 / §8.2）

跨房间 Socket 服务，处理：
- 邀请 / 敲门 / 通知推送
- 通过主业务 API（8085）落库的实时通知推送

约束（文档 §1.2 强制）：
- notice_socket 仅处理跨房间事件
- 房间内事件（聊天消息、心跳变更等）必须经 room_socket（8085），禁止绕过

启动：
  python notice_server.py
  默认端口 8090（NOTICE_SOCKET_PORT 可覆盖）
"""
import asyncio
import inspect
import json
import logging
import os
import sys
import threading
import time
from typing import Optional

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request

from notice_manager import notice_manager
from auth import verify_token, AuthError
from notification_store import notification_store

# =============================================================================
# 日志
# =============================================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
)
logger = logging.getLogger("notice_server")

# 持久化日志
log_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'notice_server.log')
fh = logging.FileHandler(log_file, encoding='utf-8')
fh.setLevel(logging.INFO)
fh.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
logger.addHandler(fh)

# =============================================================================
# 配置
# =============================================================================
NOTICE_PORT = int(os.getenv("NOTICE_SOCKET_PORT", "8090"))

# =============================================================================
# FastAPI app
# =============================================================================
app = FastAPI(title="NoticeSocket", version="1.0")

_stop_event = threading.Event()


@app.get("/health")
async def health():
    return {"status": "ok", "service": "notice_socket", "port": NOTICE_PORT}


@app.websocket("/ws/notice")
async def notice_websocket_endpoint(websocket: WebSocket):
    """§8.2 notice_socket：
        ws://{host}:{port}/ws/notice?user={user_id}&token=<jwt>

    鉴权：JWT 校验 + user_id 与连接参数一致（与 room_socket 同策略）。
    """
    user_id = websocket.query_params.get("user", "")
    token = websocket.query_params.get("token", "")

    # 鉴权：JWT 必须有效
    if not token:
        await websocket.close(code=1008, reason="missing token")
        return
    try:
        payload = verify_token(token)
    except AuthError as e:
        await websocket.close(code=1008, reason=f"auth failed: {e}")
        return

    # 鉴权：JWT 的 user_id 必须与连接参数一致
    # 注意：actor.py 里 user_id 存在 "uid" claim（不是 "user_id"）
    jwt_user_id = payload.get("uid", "") or payload.get("user_id", "")
    if not user_id or user_id != jwt_user_id:
        await websocket.close(code=1008, reason=f"user_id mismatch: token={jwt_user_id} arg={user_id}")
        return

    await websocket.accept()
    await notice_manager.connect(websocket, user_id)

    # 离线重投：拉取 created_at > last_sync_ts 的未读通知
    last_sync = notice_manager.get_last_sync_ts(user_id)
    if last_sync == 0:
        # 首次连接：取 1 天内全部未读，避免风暴
        last_sync = int(time.time()) - 86400
    pending = notification_store.list(user_id, limit=200, before_ts=None)
    pending_recent = [n for n in pending if n.get("created_at", 0) > last_sync]
    if pending_recent:
        try:
            await websocket.send_json({
                "type": "notification_sync",
                "user_id": user_id,
                "items": pending_recent,
                "count": len(pending_recent),
            })
            notice_manager.reset_last_sync_ts(user_id)
        except Exception as e:
            logger.warning(f"[NoticeSocket] notification_sync send failed: {e}")

    try:
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type", "")
            if msg_type == "ping":
                await websocket.send_json({"type": "pong"})
            elif msg_type == "pong":
                # notice_socket 暂时不需要 3 次失败判离线（用户在线状态由 room_socket 判定）
                pass
    except WebSocketDisconnect:
        notice_manager.disconnect(websocket, user_id)
    except Exception as e:
        logger.warning(f"[NoticeSocket] Error: {e}")
        notice_manager.disconnect(websocket, user_id)


# =============================================================================
# 后台 retry loop
# =============================================================================
_retry_task: Optional[asyncio.Task] = None


@app.post("/internal/push")
async def internal_push(payload: dict):
    """内部端点：8085 主进程调用，把通知推给已连的 notice_socket 客户端。

    Body: {"user_id": "...", "item": {...通知 dict...}}
    Returns: {"delivered": bool}
    """
    user_id = payload.get("user_id", "")
    item = payload.get("item", {})
    if not user_id or not item:
        return {"delivered": False, "error": "user_id/item required"}
    delivered = await notice_manager.push_notification(user_id, {
        "type": "notification",
        "data": item,
    })
    return {"delivered": delivered}


@app.on_event("startup")
async def start_retry_loop():
    global _retry_task
    _stop_event.clear()
    _retry_task = asyncio.create_task(notice_manager.retry_loop(_stop_event))
    logger.info("[NoticeSocket] retry loop started")


@app.on_event("shutdown")
async def stop_retry_loop():
    _stop_event.set()
    if _retry_task:
        _retry_task.cancel()
        try:
            await _retry_task
        except Exception:
            pass
    notification_store.flush_and_stop()
    logger.info("[NoticeSocket] shutdown complete")


# =============================================================================
# 入口
# =============================================================================
if __name__ == "__main__":
    logger.info(f"Starting NoticeSocket on 0.0.0.0:{NOTICE_PORT}")
    uvicorn.run(app, host="0.0.0.0", port=NOTICE_PORT, log_level="info")
