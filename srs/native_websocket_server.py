#!/usr/bin/env python3
"""
独立的原生 WebSocket 服务器
处理客户端的 WebSocket 连接和消息
支持 HTTP API 用于广播消息和代理 Flask 请求
"""
import asyncio
import json
import logging
import os
import threading
from collections import defaultdict
from aiohttp import web, ClientSession
import aiohttp

# 设置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# WebSocket 连接存储
connections = defaultdict(set)  # room_id -> set of (websocket, user_id)
lock = threading.Lock()

# Flask 服务器地址
FLASK_PORT = 8086


async def websocket_handler(request):
    """处理 WebSocket 连接"""
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    
    room_id = request.query.get('room', '')
    user_id = request.query.get('user', '')
    
    logger.info(f"[WS] Client connected: room={room_id}, user={user_id}")
    
    # 注册连接
    with lock:
        connections[room_id].add((ws, user_id))
    
    # 发送订阅成功
    await ws.send_json({
        'type': 'subscribed',
        'room_id': room_id,
        'user_id': user_id
    })
    
    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    msg_type = data.get('type', '')
                    
                    if msg_type == 'subscribe':
                        new_room = data.get('room_id', room_id)
                        new_user = data.get('user_id', user_id)
                        
                        # 离开旧房间
                        with lock:
                            connections[room_id].discard((ws, user_id))
                        
                        # 加入新房间
                        room_id = new_room
                        user_id = new_user
                        with lock:
                            connections[room_id].add((ws, user_id))
                        
                        await ws.send_json({
                            'type': 'subscribed',
                            'room_id': room_id,
                            'user_id': user_id
                        })
                    elif msg_type == 'ping':
                        await ws.send_json({'type': 'pong'})
                except json.JSONDecodeError:
                    pass
            elif msg.type == web.WSMsgType.ERROR:
                logger.warning(f"[WS] Error: {ws.exception()}")
    finally:
        with lock:
            connections[room_id].discard((ws, user_id))
            if not connections[room_id]:
                del connections[room_id]
        logger.info(f"[WS] Client disconnected")
    
    return ws


async def proxy_handler(request):
    """HTTP 代理 - 转发到 Flask"""
    path = request.path
    method = request.method
    
    # 代理到 Flask
    flask_url = f"http://127.0.0.1:{FLASK_PORT}{path}"
    if request.query_string:
        flask_url += f"?{request.query_string}"
    
    headers = {}
    for k, v in request.headers.items():
        if k.lower() not in ('host', 'connection'):
            headers[k] = v
    
    try:
        async with aiohttp.ClientSession() as session:
            body = await request.read() if request.can_read_body else None
            async with session.request(
                method=method,
                url=flask_url,
                headers=headers,
                data=body,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                content = await resp.read()
                return web.Response(
                    body=content,
                    status=resp.status,
                    headers={k: v for k, v in resp.headers.items()
                            if k.lower() not in ('transfer-encoding', 'content-encoding')}
                )
    except Exception as e:
        logger.error(f"[HTTP] Proxy error: {e}")
        return web.Response(status=502, text=str(e))


async def broadcast_handler(request):
    """HTTP 广播 API"""
    data = await request.json()
    room_id = data.get('room_id', '')
    message = data.get('message', '')
    
    logger.info(f"[HTTP] Broadcast to room {room_id}: {message[:100]}")
    
    with lock:
        clients = list(connections.get(room_id, []))
    
    for ws, _ in clients:
        try:
            await ws.send_str(message)
        except Exception as e:
            logger.warning(f"[HTTP] Broadcast error: {e}")
    
    return web.json_response({'status': 'ok', 'sent': len(clients)})


async def status_handler(request):
    """状态 API"""
    with lock:
        rooms = {rid: len(clients) for rid, clients in connections.items()}
    return web.json_response({'rooms': rooms})


def run_server(port: int):
    """运行服务器"""
    app = web.Application()
    
    # CORS 中间件
    @web.middleware
    async def cors_middleware(request, handler):
        response = await handler(request)
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
        return response
    
    app.middlewares.append(cors_middleware)
    
    app.router.add_get('/ws', websocket_handler)
    app.router.add_post('/broadcast', broadcast_handler)
    app.router.add_get('/status', status_handler)
    # 其他所有 HTTP 请求代理到 Flask
    app.router.add_route('*', '/{tail:.*}', proxy_handler)
    app.router.add_route('*', '/', proxy_handler)
    
    logger.info(f"[WS] Starting server on 0.0.0.0:{port}, proxy to Flask on {FLASK_PORT}")
    web.run_app(app, host='0.0.0.0', port=port, print=None, access_log=None)


if __name__ == '__main__':
    port = int(os.getenv('WS_PORT', 8085))
    run_server(port)
