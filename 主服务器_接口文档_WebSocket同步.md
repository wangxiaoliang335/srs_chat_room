# 主业务服务器 WebSocket 接口文档

> 本文档供主业务服务器（`8.138.45.176`）开发人员使用。
> 语音聊天室服务器（我们这边）会主动连接此服务器的 WebSocket 端点 `/sync`。
> 连接建立后，双方通过 JSON 消息双向通信。

---

## 1. 概述

### 1.1 架构

```
语音聊天室服务器（我们）                    主业务服务器（你）
         │                                        │
         │  1. 建立 WebSocket 长连接               │
         │  ws://8.138.45.176:9005/sync           │
         │ ─────────────────────────────────────► │
         │                                        │
         │  2. 发送 auth 认证                     │
         │  {type:"auth", data:{server_id, ts, signature}} │
         │ ─────────────────────────────────────► │
         │                                        │
         │  3. 收到 auth_ack                      │
         │  {type:"auth_ack", data:{ok:true}}     │
         │ ◄───────────────────────────────────── │
         │                                        │
         │  双方互发消息（见 §3）                 │
         │ ─────────────────────────────────────► │
         │ ◄───────────────────────────────────── │
         │                                        │
         │  4. 定期 ping/pong 心跳                 │
         │  {type:"ping"} ◄────────────────────── │
         │  {type:"pong"} ──────────────────────► │
         │                                        │
         │  5. 断线 → 自动重连（指数退避 1s→300s） │
```

### 1.2 WebSocket 端点

| 项目 | 值 |
|------|------|
| URL | `ws://8.138.45.176:9005/sync` |
| 子协议 | 无 |
| 文本帧 | UTF-8 JSON |

---

## 2. 认证

### 2.1 流程

连接建立后，语音聊天室服务器立即发送 `auth` 消息：

```json
{
  "type": "auth",
  "server_id": "server_chat_001",
  "timestamp": 1752576800,
  "data": {
    "server_id": "server_chat_001",
    "ts": 1752576800,
    "signature": "a3f8c7d2e1b..."
  }
}
```

主服务器验签后回复：

```json
{
  "type": "auth_ack",
  "data": { "ok": true }
}
```

验签失败回复：

```json
{
  "type": "auth_ack",
  "data": { "ok": false, "reason": "signature mismatch" }
}
```

### 2.2 签名验证算法

```
signature = HMAC-SHA256(
    key = 双方约定的 shared_secret,
    data = timestamp + "." + json.dumps(data_body, sort_keys=True)
)
hex 小写
```

**Python 验证示例：**

```python
import hashlib, hmac, time, json

SHARED_SECRET = "changeme_change_this_secret_before_production"

def verify_auth(server_id: str, timestamp: int, signature: str, data_body: dict) -> bool:
    # 1. 时间戳容差 ±30s
    if abs(time.time() - timestamp) > 30:
        return False

    # 2. HMAC-SHA256 验签
    raw = f"{timestamp}.{json.dumps(data_body, separators=(',', ':'), sort_keys=True)}"
    expected = hmac.new(
        SHARED_SECRET.encode(),
        raw.encode(),
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(expected, signature)
```

**注意**：`data_body` 必须是 `{"server_id": "...", "ts": ...}`（不含外层 `type/timestamp`）。

---

## 3. 消息格式

所有消息均为 **UTF-8 JSON**，单帧发送。

### 3.1 通用格式

```json
{
  "type": "消息类型",
  "server_id": "server_chat_001",     // 发送方 ID（我们这边固定）
  "timestamp": 1752576800,             // Unix 秒时间戳
  "room_id": "room_abc",              // 关联房间（可选）
  "data": { ... }                     // 消息体
}
```

---

## 4. 消息类型详解

### 4.1 客户端 → 服务端（我们发送）

#### 4.1.1 `auth` — 认证

已在 §2 说明。

---

#### 4.1.2 `room_created` — 房间创建

```json
{
  "type": "room_created",
  "server_id": "server_chat_001",
  "timestamp": 1752576800,
  "room_id": "room_abc",
  "data": {
    "room_id": "room_abc",
    "owner_id": "user_001",
    "name": "我的聊天室",
    "max_members": 100
  }
}
```

---

#### 4.1.3 `room_deleted` — 房间删除

```json
{
  "type": "room_deleted",
  "server_id": "server_chat_001",
  "timestamp": 1752576850,
  "room_id": "room_abc",
  "data": {
    "room_id": "room_abc",
    "deleted_by": "user_001"
  }
}
```

---

#### 4.1.4 `member_joined` — 成员加入

```json
{
  "type": "member_joined",
  "server_id": "server_chat_001",
  "timestamp": 1752576860,
  "room_id": "room_abc",
  "data": {
    "user_id": "user_002",
    "role": "member",
    "joined_at": "2026-07-15 13:30:00",
    "last_active": ""
  }
}
```

**字段说明：**

| 字段 | 说明 |
|------|------|
| `role` | `owner` / `admin` / `member` / `guest` |
| `joined_at` | 格式 `YYYY-MM-DD HH:MM:SS`，可为空 |
| `last_active` | 可为空 |

---

#### 4.1.5 `member_left` — 成员离开

```json
{
  "type": "member_left",
  "server_id": "server_chat_001",
  "timestamp": 1752576870,
  "room_id": "room_abc",
  "data": {
    "user_id": "user_002",
    "reason": "left"
  }
}
```

**reason 取值：** `left`（主动离开）/ `kicked`（被踢）

---

#### 4.1.6 `member_kicked` — 成员被踢

```json
{
  "type": "member_kicked",
  "server_id": "server_chat_001",
  "timestamp": 1752576880,
  "room_id": "room_abc",
  "data": {
    "user_id": "user_002",
    "operator_id": "user_001",
    "reason": "kicked"
  }
}
```

---

#### 4.1.7 `member_role_changed` — 成员角色变更

```json
{
  "type": "member_role_changed",
  "server_id": "server_chat_001",
  "timestamp": 1752576890,
  "room_id": "room_abc",
  "data": {
    "user_id": "user_002",
    "old_role": "member",
    "new_role": "admin",
    "operator_id": "user_001"
  }
}
```

---

#### 4.1.8 `room_mute_changed` — 全体禁言状态变更

```json
{
  "type": "room_mute_changed",
  "server_id": "server_chat_001",
  "timestamp": 1752576900,
  "room_id": "room_abc",
  "data": {
    "room_id": "room_abc",
    "allow_speak": false,
    "operator_id": "user_001"
  }
}
```

---

#### 4.1.9 `rooms_sync` — 全量房间同步（每 30 秒）

```json
{
  "type": "rooms_sync",
  "server_id": "server_chat_001",
  "timestamp": 1752576950,
  "room_id": "",
  "data": {
    "rooms": [
      {
        "room_id": "room_abc",
        "owner_id": "user_001",
        "name": "我的聊天室",
        "member_count": 3,
        "members": [
          {
            "user_id": "user_001",
            "role": "owner",
            "status": "normal",
            "publish_allowed": true,
            "joined_at": "2026-07-15 13:00:00"
          },
          {
            "user_id": "user_002",
            "role": "member",
            "status": "normal",
            "publish_allowed": true,
            "joined_at": "2026-07-15 13:05:00"
          }
        ],
        "allow_speak": true
      }
    ]
  }
}
```

**用途**：断线重连后快速恢复数据、对账。建议收到后整体覆盖本地缓存。

---

#### 4.1.10 `pong` — 心跳响应

收到 `ping` 后必须回复：

```json
{
  "type": "pong",
  "timestamp": 1752576950
}
```

---

### 4.2 服务端 → 客户端（你们发送）

#### 4.2.1 `ping` — 心跳探测

```json
{
  "type": "ping",
  "timestamp": 1752576950
}
```

**要求**：每 **15 秒** 发一次，语音聊天室服务器若 30 秒内未收到任何消息则断开重连。

---

#### 4.2.2 `command` — 主服务器主动命令（可选）

```json
{
  "type": "command",
  "server_id": "main_server",
  "timestamp": 1752577000,
  "room_id": "room_abc",
  "data": {
    "command": "kick_user",
    "user_id": "user_002",
    "operator_id": "admin_001",
    "reason": "违规发言"
  }
}
```

**支持的命令：**

| command | 说明 | data 字段 |
|---------|------|-----------|
| `kick_user` | 踢出用户 | `user_id`, `operator_id`, `reason` |
| `close_room` | 关闭房间 | `operator_id` |
| `mute_user` | 禁言用户 | `user_id`, `operator_id` |
| `unmute_user` | 解除禁言 | `user_id`, `operator_id` |

**示例 — 踢人：**

```json
{
  "type": "command",
  "server_id": "main_server",
  "timestamp": 1752577000,
  "room_id": "room_abc",
  "data": {
    "command": "kick_user",
    "user_id": "user_002",
    "operator_id": "admin_001",
    "reason": "违规发言"
  }
}
```

**示例 — 关闭房间：**

```json
{
  "type": "command",
  "server_id": "main_server",
  "timestamp": 1752577000,
  "room_id": "room_abc",
  "data": {
    "command": "close_room",
    "operator_id": "admin_001"
  }
}
```

---

## 5. 心跳与重连机制

| 规则 | 值 |
|------|------|
| 主服务器 ping 间隔 | 15 秒 |
| 语音聊天室服务器无响应超时 | 30 秒 |
| 断线重连初始间隔 | 1 秒 |
| 重连最大间隔 | 300 秒（5 分钟） |
| 重连退避策略 | 指数退避 + 随机抖动 |

**断线重连流程**：
1. 检测到连接断开
2. 等待 1s → 重新建立 WebSocket 连接
3. 发送 `auth`
4. 若认证成功，恢复正常；若失败，等待 2s 重试
5. 间隔翻倍（2s → 4s → 8s → ...），最长 300s

---

## 6. 完整 Python（FastAPI）服务端示例

```python
"""
WebSocket 服务端示例（供主业务服务器参考）
基于 FastAPI + websockets
"""

import asyncio
import hashlib
import hmac
import json
import logging
import time
from typing import Set

import fastapi
from fastapi import WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState

logger = logging.getLogger(__name__)

# ============================================================
# 配置
# ============================================================
SHARED_SECRET = "changeme_change_this_secret_before_production"  # 联调时替换
ALLOWED_SERVER_IDS = {"server_chat_001"}  # 已登记的语音聊天室服务器
HB_INTERVAL = 15  # ping 间隔（秒）
HB_TIMEOUT = 30    # 无响应超时（秒）

app = fastapi.FastAPI()


# ============================================================
# 连接管理器
# ============================================================
class ConnectionManager:
    def __init__(self):
        self.active: Set[WebSocket] = set()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.add(ws)
        logger.info(f"[WS] client connected, total={len(self.active)}")

    def disconnect(self, ws: WebSocket):
        self.active.discard(ws)
        logger.info(f"[WS] client disconnected, total={len(self.active)}")

    async def broadcast(self, msg: dict):
        for ws in list(self.active):
            try:
                if ws.client_state == WebSocketState.CONNECTED:
                    await ws.send_json(msg)
            except Exception:
                self.active.discard(ws)


manager = ConnectionManager()


# ============================================================
# WebSocket 端点
# ============================================================
@app.websocket("/sync")
async def ws_endpoint(ws: WebSocket):
    await manager.connect(ws)
    authed = False
    last_recv = time.time()

    try:
        while True:
            # 带超时接收，检测心跳
            try:
                raw = await asyncio.wait_for(ws.receive_text(), timeout=HB_TIMEOUT)
            except asyncio.TimeoutError:
                logger.warning("[WS] client timeout, closing")
                break

            last_recv = time.time()

            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning(f"[WS] invalid JSON: {raw[:100]}")
                continue

            msg_type = msg.get("type", "")

            # ── auth ──
            if msg_type == "auth":
                sid       = msg.get("server_id", "")
                ts        = msg.get("timestamp", 0)
                data_body = msg.get("data", {})
                sig       = data_body.get("signature", "")

                if sid not in ALLOWED_SERVER_IDS:
                    await ws.send_json({"type": "auth_ack", "data": {"ok": False, "reason": "unknown server"}})
                    break

                if abs(time.time() - ts) > 30:
                    await ws.send_json({"type": "auth_ack", "data": {"ok": False, "reason": "timestamp skew"}})
                    break

                # 验签：HMAC-SHA256(secret, ts.json.dumps(data_body))
                raw_sig = f"{ts}.{json.dumps(data_body, separators=(',', ':'), sort_keys=True)}"
                expected = hmac.new(SHARED_SECRET.encode(), raw_sig.encode(), hashlib.sha256).hexdigest()
                if not hmac.compare_digest(expected, sig):
                    await ws.send_json({"type": "auth_ack", "data": {"ok": False, "reason": "signature mismatch"}})
                    break

                authed = True
                await ws.send_json({"type": "auth_ack", "data": {"ok": True}})
                logger.info(f"[WS] {sid} auth succeeded")
                continue

            if not authed:
                await ws.send_json({"type": "error", "data": {"reason": "not authenticated"}})
                continue

            # ── 处理业务消息 ──
            room_id = msg.get("room_id", "")
            data    = msg.get("data", {})
            logger.info(f"[WS] {msg_type} room={room_id} data={data}")

            if msg_type == "room_created":
                await handle_room_created(room_id, data)
            elif msg_type == "room_deleted":
                await handle_room_deleted(room_id, data)
            elif msg_type == "member_joined":
                await handle_member_joined(room_id, data)
            elif msg_type == "member_left":
                await handle_member_left(room_id, data)
            elif msg_type == "member_kicked":
                await handle_member_kicked(room_id, data)
            elif msg_type == "member_role_changed":
                await handle_role_changed(room_id, data)
            elif msg_type == "room_mute_changed":
                await handle_mute_changed(room_id, data)
            elif msg_type == "rooms_sync":
                await handle_rooms_sync(data)
            elif msg_type == "pong":
                pass  # 心跳响应，无需处理
            else:
                logger.warning(f"[WS] unknown type: {msg_type}")

    except WebSocketDisconnect:
        logger.info("[WS] client disconnected")
    except Exception as e:
        logger.error(f"[WS] error: {e}")
    finally:
        manager.disconnect(ws)


# ============================================================
# 业务处理函数（替换为你的实际逻辑）
# ============================================================

async def handle_room_created(room_id: str, data: dict):
    logger.info(f"[DB] INSERT room {room_id} owner={data.get('owner_id')}")
    # TODO: 写入数据库

async def handle_room_deleted(room_id: str, data: dict):
    logger.info(f"[DB] DELETE room {room_id}")
    # TODO: 从数据库删除

async def handle_member_joined(room_id: str, data: dict):
    logger.info(f"[DB] INSERT member {data.get('user_id')} into {room_id} role={data.get('role')}")
    # TODO: 写入数据库

async def handle_member_left(room_id: str, data: dict):
    uid = data.get("user_id")
    reason = data.get("reason", "left")
    logger.info(f"[DB] DELETE member {uid} from {room_id} reason={reason}")
    # TODO: 从数据库删除

async def handle_member_kicked(room_id: str, data: dict):
    uid = data.get("user_id")
    logger.info(f"[DB] KICK member {uid} from {room_id}")
    # TODO: 从数据库删除

async def handle_role_changed(room_id: str, data: dict):
    uid = data.get("user_id")
    new_role = data.get("new_role")
    logger.info(f"[DB] UPDATE member {uid} role={new_role} in {room_id}")
    # TODO: 更新数据库

async def handle_mute_changed(room_id: str, data: dict):
    allow = data.get("allow_speak")
    logger.info(f"[DB] UPDATE room {room_id} allow_speak={allow}")
    # TODO: 更新数据库

async def handle_rooms_sync(data: dict):
    rooms = data.get("rooms", [])
    logger.info(f"[DB] FULL SYNC: {len(rooms)} rooms")
    # TODO: 整体覆盖数据库（rooms_sync 的幂等处理策略）


# ============================================================
# 心跳任务（定时 ping）
# ============================================================
@app.on_event("startup")
async def start_heartbeat():
    asyncio.create_task(heartbeat_loop())


async def heartbeat_loop():
    while True:
        await asyncio.sleep(HB_INTERVAL)
        if not manager.active:
            continue
        msg = {"type": "ping", "timestamp": int(time.time())}
        disconnected = []
        for ws in manager.active:
            try:
                await ws.send_json(msg)
            except Exception:
                disconnected.append(ws)
        for ws in disconnected:
            manager.disconnect(ws)


# ============================================================
# 运行
# ============================================================
# uvicorn main:app --host 0.0.0.0 --port 9005 --ws-max-size 2097152
```

---

## 7. 消息触发时机速查

| 消息类型 | 触发时机 |
|---------|---------|
| `room_created` | 用户创建房间 |
| `room_deleted` | 群主删除房间 |
| `member_joined` | 用户主动加入 / 被接受敲门 |
| `member_left` | 用户主动离开 |
| `member_kicked` | 用户被踢出（reason=kicked） |
| `member_role_changed` | 群主变更用户角色 |
| `room_mute_changed` | 全员禁言/解除全员禁言 |
| `rooms_sync` | 每 30 秒定时推送所有房间状态 |
| `ping` | 主服务器每 15 秒发一次 |

---

## 8. 联调清单

| # | 检查项 | 预期 |
|---|--------|------|
| 1 | 主服务器 WS 端点 `/sync` 启动成功 | 无报错 |
| 2 | 语音聊天室服务器连接上 | 日志：`SyncClient] connecting to ws://...` → `connected` |
| 3 | 语音聊天室服务器发送 `auth` | 服务端收到 JSON `{type:"auth"...}` |
| 4 | 服务端验签正确，回复 `auth_ack` `{ok:true}` | 客户端日志：`auth succeeded` |
| 5 | 创建房间 → 服务端收到 `room_created` | 收到消息 |
| 6 | 用户加入 → 服务端收到 `member_joined` | 收到消息 |
| 7 | 用户离开 → 服务端收到 `member_left` | 收到消息 |
| 8 | 主服务器发 `command` → 语音聊天室服务器收到 | 命令被执行 |
| 9 | 故意改签名 | 服务端回复 `auth_ack {ok:false}` |
| 10 | 主服务器 30s 不发 ping | 语音聊天室服务器自动重连 |

---

## 9. 变更记录

| 版本 | 日期 | 变更 |
|------|------|------|
| v1 | 2026-07-15 | 初始 WebSocket 版本，支持双向消息、认证、心跳、命令下发 |
