# 翻译文本推送 API 接口文档（客户端版）

> 本文档为客户端开发人员提供翻译文本推送及相关功能的接口说明。服务端地址默认 `http://localhost:8085`（回调服务器）和 `http://localhost:8086`（文本推送服务）。

---

## 目录

- [1. 服务地址与协议](#1-服务地址与协议)
- [2. 通用响应格式](#2-通用响应格式)
- [3. 翻译文本推送（WebSocket）](#3-翻译文本推送websocket)
- [4. 翻译文本推送（HTTP API）](#4-翻译文本推送http-api)
- [5. 原语音识别文字推送](#5-原语音识别文字推送)
- [6. 房间事件通知（Socket.IO / WebSocket）](#6-房间事件通知socketio--websocket)
- [7. 翻译请求管理](#7-翻译请求管理)
- [8. 拉流者心跳](#8-拉流者心跳)
- [9. 房间管理](#9-房间管理)
- [10. 用户状态管理](#10-用户状态管理)
- [11. 说话状态通知](#11-说话状态通知)
- [12. 错误码说明](#12-错误码说明)
- [13. 客户端集成示例](#13-客户端集成示例)

---

## 1. 服务地址与协议

| 服务 | 地址 | 协议 | 端口 | 说明 |
|------|------|------|------|------|
| 回调服务器 | `http://localhost:8085` | HTTP / Socket.IO | 8085 | 提供所有业务 API |
| 文本推送服务 | `http://localhost:8086` | HTTP / WebSocket | 8086 | 翻译文本 WebSocket 推送 |
| SRS 流媒体 | `rtmp://localhost:1935` | RTMP | 1935 | 音视频流媒体服务 |
| SRS HTTP-FLV | `http://localhost:8080` | HTTP-FLV | 8080 | FLV 拉流播放 |

**环境变量配置**（服务端）：
- `CALLBACK_PORT` / `CALLBACK_HOST` — 回调服务器地址（默认 `0.0.0.0:8085`）
- `TEXT_SERVER_HOST` / `TEXT_SERVER_PORT` — 文本推送服务地址（默认 `0.0.0.0:8086`）
- `SRS_URL` — SRS 服务器地址（默认 `rtmp://localhost:1935`）

---

## 2. 通用响应格式

所有 HTTP API 遵循统一响应格式：

```json
{
    "code": 0,
    "message": "success",
    "data": {}
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `code` | int | 0 = 成功，非 0 = 失败 |
| `message` | string | 结果描述 |
| `data` | object | 业务数据（成功时有值） |

---

## 3. 翻译文本推送（WebSocket）

翻译文本推送服务运行在 **8086 端口**，提供纯 WebSocket 方式实时接收翻译文本。

### 3.1 建立连接

**WebSocket 地址：**

```
ws://<服务器IP>:8086/ws?user_id=<用户ID>
```

**示例：**

```
ws://localhost:8086/ws?user_id=user_B
```

连接建立后，服务器立即返回连接确认消息。

---

### 3.2 服务端 → 客户端 消息

#### 连接成功

```json
{
    "type": "connected",
    "data": {
        "client_id": "12345678",
        "user_id": "user_B",
        "message": "Connected to translation text service"
    }
}
```

#### 订阅确认

客户端发送 `subscribe` 消息后，服务器返回：

```json
{
    "type": "subscribed",
    "data": {
        "user_id": "user_B",
        "message": "Subscribed to translation texts"
    }
}
```

#### 翻译文本推送

当有翻译文本产生时，服务器主动推送：

```json
{
    "type": "translation_text",
    "data": {
        "request_id": "a1b2c3d4-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
        "room_id": "room1",
        "source_user": "user_A",
        "target_user": "user_B",
        "original_text": "Hello everyone",
        "translated_text": "大家好",
        "source_lang": "en",
        "target_lang": "zh",
        "timestamp": 1714100000.123
    }
}
```

#### 离线缓存消息

用户重连后，未读消息一次性推送：

```json
{
    "type": "cached_texts",
    "data": {
        "count": 3,
        "texts": [
            {
                "original_text": "Hello",
                "translated_text": "你好",
                "timestamp": 1714100000.123
            }
        ]
    }
}
```

#### 心跳

服务端也可能推送：

```json
{
    "type": "pong"
}
```

#### 错误消息

```json
{
    "type": "error",
    "data": {
        "message": "Missing user_id"
    }
}
```

---

### 3.3 客户端 → 服务端 消息

#### 订阅

```json
{
    "type": "subscribe",
    "data": {
        "user_id": "user_B"
    }
}
```

#### 取消订阅

```json
{
    "type": "unsubscribe",
    "data": {
        "user_id": "user_B"
    }
}
```

#### 心跳

```json
{
    "type": "ping"
}
```

---

### 3.4 推送消息类型汇总

| type（服务端推送） | 说明 |
|------------------|------|
| `connected` | WebSocket 连接成功 |
| `subscribed` | 订阅成功确认 |
| `unsubscribed` | 取消订阅确认 |
| `translation_text` | 翻译文本数据（核心） |
| `cached_texts` | 离线缓存的翻译文本 |
| `error` | 错误信息 |
| `ping` / `pong` | 心跳保活 |

---

## 4. 翻译文本推送（HTTP API）

通过 HTTP 接口主动查询或管理翻译文本。

### 4.1 推送翻译文本

**主动将翻译文本推送给目标用户**（服务端内部调用，客户端一般不直接使用）。

```
POST http://localhost:8086/api/v1/translation/text/push
```

**请求体：**

```json
{
    "target_user": "user_B",
    "request_id": "a1b2c3d4-xxxx",
    "room_id": "room1",
    "source_user": "user_A",
    "original_text": "Hello",
    "translated_text": "你好",
    "source_lang": "en",
    "target_lang": "zh",
    "timestamp": 1714100000.123
}
```

**响应：**

```json
{
    "code": 0,
    "message": "success"
}
```

---

### 4.2 查询翻译历史

```
GET http://localhost:8086/api/v1/translation/text/history?user_id=user_B&limit=20&offset=0
```

**参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `user_id` | string | 是 | 用户 ID |
| `limit` | int | 否 | 返回条数，默认 100 |
| `offset` | int | 否 | 偏移量，默认 0 |

**响应：**

```json
{
    "code": 0,
    "data": {
        "texts": [
            {
                "original_text": "Hello",
                "translated_text": "你好",
                "source_lang": "en",
                "target_lang": "zh",
                "timestamp": 1714100000.123
            }
        ],
        "limit": 20,
        "offset": 0
    }
}
```

---

### 4.3 查询连接状态

```
GET http://localhost:8086/api/v1/translation/text/connections
```

**响应：**

```json
{
    "total_users": 5,
    "users": {
        "user_A": 1,
        "user_B": 2
    }
}
```

---

### 4.4 健康检查

```
GET http://localhost:8086/health
```

**响应：**

```json
{
    "status": "ok",
    "service": "translation-text-server",
    "ws_clients": 10,
    "ws_users": 7
}
```

---

## 5. 原语音识别文字推送

当房间中有用户说话时，ASR（语音识别）会实时将识别出的**原文**推送给房间内的**所有用户**。

### 5.1 推送时机

```
用户说话 → ASR 语音识别 → 原语音文字立即推送 → 机器翻译 → 翻译文本推送
```

### 5.2 WebSocket 接收（8086 端口）

连接时携带 `room_id` 参数，即可接收房间内的原语音文字：

```
ws://localhost:8086/ws?room_id=room1&user_id=user_A
```

**服务端 → 客户端推送：**

```json
{
    "type": "original_speech_text",
    "data": {
        "room_id": "room1",
        "source_user": "user_A",
        "original_text": "Hello everyone",
        "source_lang": "en",
        "timestamp": 1714100000.123
    }
}
```

### 5.3 Socket.IO 接收（8085 端口）

客户端订阅房间后，自动接收 `original_speech_text` 事件：

```javascript
socket.on('original_speech_text', (data) => {
    console.log('说话人:', data.data.user_id);
    console.log('原文字:', data.data.data.original_text);
    console.log('语言:', data.data.data.source_lang);
});
```

**事件数据结构：**

```json
{
    "type": "original_speech_text",
    "data": {
        "event_id": "abc12348",
        "event_type": "original_speech_text",
        "room_id": "room1",
        "user_id": "user_A",
        "timestamp": "2024-04-25 12:15:00",
        "data": {
            "original_text": "Hello everyone",
            "source_lang": "en"
        }
    }
}
```

### 5.4 WebSocket 订阅消息

连接后发送订阅消息，指定要订阅的房间：

```json
{
    "type": "subscribe",
    "data": {
        "room_id": "room1"
    }
}
```

### 5.5 推送消息类型

| type | 说明 | 推送范围 |
|------|------|---------|
| `original_speech_text` | 原语音识别文字 | **房间所有用户** |

---

## 6. 房间事件通知（纯 WebSocket）

回调服务器（8085 端口）提供房间事件实时通知，使用**纯 WebSocket 协议**。

### 6.1 WebSocket 连接方式

**WebSocket 地址：**

```
ws://localhost:8085/ws?room=<room_id>&user=<user_id>
```

**连接并订阅房间：**

```javascript
// 浏览器原生 WebSocket，无需安装任何库
const ws = new WebSocket('ws://localhost:8085/ws?room=room1&user=user_A');

ws.onopen = () => {
  console.log('WebSocket connected');
  // 发送订阅消息
  ws.send(JSON.stringify({
    type: 'subscribe',
    room_id: 'room1',
    user_id: 'user_A'
  }));
};

// 监听事件
ws.onmessage = (event) => {
  const msg = JSON.parse(event.data);
  console.log('Event:', msg.type, msg.data);
  
  switch (msg.type) {
    case 'subscribed':
      console.log('订阅成功');
      break;
    case 'member_joined':
      console.log('用户加入:', msg.data.user_id);
      break;
    case 'member_left':
      console.log('用户离开:', msg.data.user_id);
      break;
    case 'translation_text':
      console.log('翻译文本:', msg.data);
      break;
  }
};

ws.onerror = (err) => console.error('WebSocket error:', err);
ws.onclose = () => console.log('WebSocket closed');

// 定期发送心跳
setInterval(() => {
  if (ws.readyState === WebSocket.OPEN) {
    ws.send(JSON.stringify({ type: 'ping' }));
  }
}, 30000);
```

**订阅消息：**

```json
{
    "type": "subscribe",
    "room_id": "room1",
    "user_id": "user_A"
}
```

**订阅确认：**

```json
{
    "type": "subscribed",
    "room_id": "room1",
    "user_id": "user_A"
}
```

---

### 6.3 房间事件类型汇总

| 事件名 | 说明 | 推送对象 |
|--------|------|----------|
| `member_joined` | 用户加入房间 | 房间全体成员 |
| `member_left` | 用户离开房间 | 房间全体成员 |
| `member_kicked` | 用户被踢出 | 被踢用户 + 房间其他成员 |
| `member_role_changed` | 用户角色变更 | 房间全体成员 |
| `member_muted` | 用户被禁言 | 房间全体成员 |
| `member_unmuted` | 用户被解除禁言 | 房间全体成员 |
| `member_mic_disabled` | 用户被禁麦 | 房间全体成员 |
| `member_mic_enabled` | 用户被解除禁麦 | 房间全体成员 |
| `room_muted_all` | 全体禁言 | 房间全体成员 |
| `room_unmuted_all` | 解除全体禁言 | 房间全体成员 |
| `room_knock` | 有人敲门 | 房主 |
| `room_knock_accepted` | 敲门被接受 | 敲门者 |
| `room_knock_rejected` | 敲门被拒绝 | 敲门者 |
| `translation_started` | 翻译开始 | 房间全体成员 |
| `translation_stopped` | 翻译结束 | 房间全体成员 |
| `translation_text` | 翻译文本 | 目标用户 |
| `original_speech_text` | 原语音识别文字 | 房间全体成员 |
| `user_speaking_start` | 用户开始说话 | 房间全体成员 |
| `user_speaking_stop` | 用户停止说话 | 房间全体成员 |

---

### 6.4 事件数据结构

所有事件推送格式统一为：

```json
{
    "type": "<事件名>",
    "data": {
        "event_id": "abc12345",
        "room_id": "room1",
        "user_id": "user_A",
        "operator_id": "",
        "target_user_id": "",
        "timestamp": "2024-04-25 12:00:00",
        "data": {
            // 事件特有数据
        }
    }
}
```

#### 成员加入事件

```json
{
    "type": "member_joined",
    "data": {
        "event_id": "abc12345",
        "event_type": "member_joined",
        "room_id": "room1",
        "user_id": "user_B",
        "timestamp": "2024-04-25 12:00:00",
        "data": {
            "user_id": "user_B",
            "role": "member",
            "status": "normal",
            "publish_allowed": true,
            "joined_at": "2024-04-25 12:00:00"
        }
    }
}
```

#### 成员离开事件

```json
{
    "type": "member_left",
    "data": {
        "event_id": "abc12346",
        "event_type": "member_left",
        "room_id": "room1",
        "user_id": "user_B",
        "timestamp": "2024-04-25 12:05:00",
        "data": {}
    }
}
```

#### 用户被禁言事件

```json
{
    "type": "member_muted",
    "data": {
        "event_id": "abc12347",
        "event_type": "member_muted",
        "room_id": "room1",
        "user_id": "user_B",
        "operator_id": "owner_A",
        "timestamp": "2024-04-25 12:10:00",
        "data": {}
    }
}
```

#### 翻译文本事件（Socket.IO 推送）

```json
{
    "type": "translation_text",
    "data": {
        "event_id": "abc12348",
        "event_type": "translation_text",
        "room_id": "room1",
        "user_id": "user_A",
        "target_user_id": "user_B",
        "timestamp": "2024-04-25 12:15:00",
        "data": {
            "original_text": "Hello",
            "translated_text": "你好",
            "source_lang": "en",
            "target_lang": "zh"
        }
    }
}
```

#### 用户开始说话事件

```json
{
    "type": "user_speaking_start",
    "data": {
        "event_id": "abc12349",
        "event_type": "user_speaking_start",
        "room_id": "room1",
        "user_id": "user_A",
        "timestamp": "2024-04-25 12:20:00",
        "data": {
            "stream_url": "rtmp://localhost:1935/live/room1_user_A"
        }
    }
}
```

#### 敲门事件

```json
{
    "type": "room_knock",
    "data": {
        "event_id": "abc12350",
        "event_type": "room_knock",
        "room_id": "room1",
        "user_id": "visitor_001",
        "operator_id": "owner_A",
        "timestamp": "2024-04-25 12:25:00",
        "data": {
            "message": "想加入聊天",
            "room_name": "测试房间"
        }
    }
}
```

---

## 7. 翻译请求管理

### 7.1 申请翻译

客户端请求对某个用户的音频流进行翻译。

```
POST http://localhost:8085/api/v1/translation/request
Content-Type: application/json
```

**请求体：**

```json
{
    "room_id": "room1",
    "source_user": "user_A",
    "target_user": "user_B",
    "to_lang": "zh"
}
```

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `room_id` | string | 是 | 房间 ID |
| `source_user` | string | 是 | 说话人用户 ID |
| `target_user` | string | 是 | 听翻译的用户 ID |
| `to_lang` | string | 否 | 目标语言代码，默认 `zh` |

**响应：**

```json
{
    "code": 0,
    "message": "success",
    "data": {
        "request_id": "a1b2c3d4-xxxx-xxxx-xxxx-xxxxxxxxxxxx",
        "stream_url": "rtmp://localhost:1935/live/room1_user_A_to_zh"
    }
}
```

客户端可使用返回的 `stream_url` 拉取翻译后的音频流进行播放。

---

### 7.2 取消翻译

```
POST http://localhost:8085/api/v1/translation/cancel
Content-Type: application/json
```

**方式一：通过 request_id 取消**

```json
{
    "request_id": "a1b2c3d4-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
}
```

**方式二：通过参数取消**

```json
{
    "room_id": "room1",
    "source_user": "user_A",
    "target_user": "user_B",
    "to_lang": "zh"
}
```

**响应：**

```json
{
    "code": 0,
    "message": "success"
}
```

---

### 7.3 查询用户可用流

获取用户在房间中可收听的音频流列表（包含原声和翻译流）。

```
GET http://localhost:8085/api/v1/translation/streams/{room_id}/{user_id}
```

**示例：**

```
GET http://localhost:8085/api/v1/translation/streams/room1/user_B
```

**响应：**

```json
{
    "code": 0,
    "message": "success",
    "data": {
        "user_id": "user_B",
        "streams": [
            {
                "type": "original",
                "user_id": "user_A",
                "url": "rtmp://localhost:1935/live/room1_user_A",
                "description": "原声音频"
            },
            {
                "type": "translation",
                "source_user": "user_A",
                "to_lang": "zh",
                "url": "rtmp://localhost:1935/live/room1_user_A_to_zh",
                "description": "A的中文翻译"
            }
        ]
    }
}
```

---

### 7.4 推送翻译文本（HTTP）

```
POST http://localhost:8085/api/v1/translation/text/push
Content-Type: application/json
```

**请求体：**

```json
{
    "target_user": "user_B",
    "request_id": "a1b2c3d4-xxxx",
    "room_id": "room1",
    "source_user": "user_A",
    "original_text": "Hello",
    "translated_text": "你好",
    "source_lang": "en",
    "target_lang": "zh",
    "timestamp": 1714100000.123
}
```

**响应：**

```json
{
    "code": 0,
    "message": "success"
}
```

> **注意**：此接口一般由翻译服务（`audio_translation_service.py`）内部调用，用于将翻译后的文本推送给客户端。客户端一般通过 WebSocket/Socket.IO 接收推送，无需直接调用此接口。

---

## 8. 拉流者心跳

客户端在拉取翻译流时应定期发送心跳，表明仍在拉流。

### 8.1 注册拉流者

```
POST http://localhost:8085/api/v1/translation/register_puller
Content-Type: application/json
```

```json
{
    "request_id": "a1b2c3d4-xxxx",
    "puller_id": "user_B"
}
```

**响应：**

```json
{
    "code": 0,
    "message": "success"
}
```

---

### 8.2 发送心跳

```
POST http://localhost:8085/api/v1/translation/heartbeat
Content-Type: application/json
```

```json
{
    "request_id": "a1b2c3d4-xxxx",
    "puller_id": "user_B",
    "source_stream_active": true
}
```

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `request_id` | string | 是 | 翻译请求 ID |
| `puller_id` | string | 是 | 拉流者 ID |
| `source_stream_active` | bool | 否 | 源流是否活跃 |

**响应：**

```json
{
    "code": 0,
    "message": "success",
    "data": {
        "received": true,
        "next_heartbeat_in": 5
    }
}
```

> **心跳间隔建议**：建议每 **5 秒** 发送一次心跳。

---

### 8.3 注销拉流者

```
POST http://localhost:8085/api/v1/translation/unregister_puller
Content-Type: application/json
```

```json
{
    "request_id": "a1b2c3d4-xxxx",
    "puller_id": "user_B"
}
```

---

### 8.4 查询拉流者列表

```
GET http://localhost:8085/api/v1/translation/requests/{request_id}/pullers
```

**响应：**

```json
{
    "code": 0,
    "message": "success",
    "data": {
        "request_id": "a1b2c3d4-xxxx",
        "pullers": [
            {
                "puller_id": "user_B",
                "last_heartbeat": 1714100000,
                "seconds_ago": 3,
                "is_alive": true
            }
        ]
    }
}
```

---

## 9. 房间管理

### 9.1 创建房间

```
POST http://localhost:8085/api/v1/room
Content-Type: application/json
```

**请求体：**

```json
{
    "room_id": "room1",
    "owner_id": "user_A",
    "name": "测试房间"
}
```

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `room_id` | string | 是 | 房间 ID（唯一） |
| `owner_id` | string | 是 | 房主用户 ID |
| `name` | string | 否 | 房间名称 |

**响应：**

```json
{
    "code": 0,
    "message": "success",
    "data": {
        "room_id": "room1",
        "name": "测试房间",
        "owner_id": "user_A",
        "created_at": "2024-04-25 12:00:00"
    }
}
```

---

### 9.2 获取房间信息

```
GET http://localhost:8085/api/v1/room/{room_id}
```

**响应：**

```json
{
    "code": 0,
    "message": "success",
    "data": {
        "room_id": "room1",
        "name": "测试房间",
        "owner_id": "user_A",
        "created_at": "2024-04-25 12:00:00",
        "member_count": 3,
        "allow_speak": true
    }
}
```

---

### 9.3 删除房间

```
DELETE http://localhost:8085/api/v1/room/{room_id}?operator_id={owner_id}
```

> 只有房主可以删除房间。

---

### 9.4 加入房间

```
POST http://localhost:8085/api/v1/room/{room_id}/join
Content-Type: application/json
```

```json
{
    "user_id": "user_B",
    "role": "member"
}
```

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `user_id` | string | 是 | 用户 ID |
| `role` | string | 否 | 角色：`owner`、`admin`、`member`（默认）、`guest` |

**响应：**

```json
{
    "code": 0,
    "message": "success",
    "data": {
        "user_id": "user_B",
        "room_id": "room1",
        "role": "member",
        "joined_at": "2024-04-25 12:05:00"
    }
}
```

---

### 9.5 离开房间

```
POST http://localhost:8085/api/v1/room/{room_id}/leave
Content-Type: application/json
```

```json
{
    "user_id": "user_B"
}
```

---

### 9.6 获取房间成员列表

```
GET http://localhost:8085/api/v1/room/{room_id}/members
```

**可选过滤参数：**

| 参数 | 说明 |
|------|------|
| `role` | 按角色筛选：`owner`、`admin`、`member`、`guest` |
| `status` | 按状态筛选：`normal`、`muted`、`mic_off` |

**示例：**

```
GET http://localhost:8085/api/v1/room/room1/members?role=admin
```

**响应：**

```json
{
    "code": 0,
    "message": "success",
    "data": {
        "room_id": "room1",
        "owner_id": "user_A",
        "member_count": 3,
        "allow_speak": true,
        "members": [
            {
                "user_id": "user_A",
                "role": "owner",
                "status": "normal",
                "publish_allowed": true,
                "joined_at": "2024-04-25 12:00:00",
                "last_active": "2024-04-25 12:30:00"
            },
            {
                "user_id": "user_B",
                "role": "admin",
                "status": "normal",
                "publish_allowed": true,
                "joined_at": "2024-04-25 12:05:00",
                "last_active": "2024-04-25 12:25:00"
            }
        ]
    }
}
```

---

### 9.7 敲门（请求加入）

```
POST http://localhost:8085/api/v1/room/{room_id}/knock
Content-Type: application/json
```

```json
{
    "user_id": "visitor_001",
    "message": "想加入聊天"
}
```

**响应：**

```json
{
    "code": 0,
    "message": "success",
    "data": {
        "room_id": "room1",
        "owner_id": "user_A",
        "knocker_id": "visitor_001"
    }
}
```

> 房主会收到 `room_knock` 事件通知。

---

### 9.8 接受敲门

```
POST http://localhost:8085/api/v1/room/{room_id}/knock/accept
Content-Type: application/json
```

```json
{
    "operator_id": "user_A",
    "knocker_id": "visitor_001",
    "role": "member"
}
```

---

### 9.9 拒绝敲门

```
POST http://localhost:8085/api/v1/room/{room_id}/knock/reject
Content-Type: application/json
```

```json
{
    "operator_id": "user_A",
    "knocker_id": "visitor_001",
    "reason": "房间已满"
}
```

---

## 10. 用户状态管理

### 10.1 禁言用户

```
POST http://localhost:8085/api/v1/room/{room_id}/member/{user_id}/mute
Content-Type: application/json
```

```json
{
    "operator_id": "admin_or_owner"
}
```

> 需要操作者是群主或管理员。

---

### 10.2 解除禁言

```
POST http://localhost:8085/api/v1/room/{room_id}/member/{user_id}/unmute
Content-Type: application/json
```

```json
{
    "operator_id": "admin_or_owner"
}
```

---

### 10.3 禁麦（禁止麦克风发布）

```
POST http://localhost:8085/api/v1/room/{room_id}/member/{user_id}/mic/disable
Content-Type: application/json
```

```json
{
    "operator_id": "admin_or_owner"
}
```

---

### 10.4 解除禁麦

```
POST http://localhost:8085/api/v1/room/{room_id}/member/{user_id}/mic/enable
Content-Type: application/json
```

```json
{
    "operator_id": "admin_or_owner"
}
```

---

### 10.5 全体禁言（仅房主）

```
POST http://localhost:8085/api/v1/room/{room_id}/mute-all
Content-Type: application/json
```

```json
{
    "operator_id": "owner_id"
}
```

**响应：**

```json
{
    "code": 0,
    "message": "success",
    "data": {
        "room_id": "room1",
        "allow_speak": false,
        "muted_count": 5
    }
}
```

---

### 10.6 解除全体禁言

```
POST http://localhost:8085/api/v1/room/{room_id}/unmute-all
Content-Type: application/json
```

```json
{
    "operator_id": "owner_id"
}
```

---

### 10.7 踢出用户

```
DELETE http://localhost:8085/api/v1/room/{room_id}/member/{user_id}?operator_id={operator_id}
```

> 只有群主或管理员可踢人，且管理员不能踢其他管理员和群主。

---

### 10.8 检查发布权限

```
GET http://localhost:8085/api/v1/room/{room_id}/check-publish?user_id={user_id}
```

**响应：**

```json
{
    "code": 0,
    "message": "success",
    "data": {
        "user_id": "user_B",
        "can_publish": true,
        "status": "normal"
    }
}
```

---

## 11. 说话状态通知

### 10.1 查询正在说话的用户

```
GET http://localhost:8085/api/v1/room/{room_id}/speaking
```

**响应：**

```json
{
    "code": 0,
    "message": "success",
    "data": {
        "room_id": "room1",
        "speaking_users": ["user_A", "user_C"]
    }
}
```

---

### 10.2 获取 WebSocket 连接状态

```
GET http://localhost:8085/api/v1/ws/status
```

**响应：**

```json
{
    "code": 0,
    "message": "success",
    "data": {
        "total_connections": 10,
        "active_rooms": ["room1", "room2"],
        "ws_port": 8085
    }
}
```

---

## 12. 错误码说明

| code | 说明 |
|------|------|
| 0 | 成功 |
| 400 | 请求参数错误（缺少必填参数等） |
| 401 | 未授权（一般不会返回） |
| 403 | 权限不足（无权操作该房间/用户） |
| 404 | 资源不存在（房间、用户、翻译请求等未找到） |
| 409 | 冲突（翻译请求已存在等） |
| 500 | 服务器内部错误 |

---

## 13. 客户端集成示例

### 13.1 WebSocket 接收翻译文本（8086 端口）

```javascript
class TranslationTextClient {
    constructor(serverUrl, userId, roomId = '') {
        this.serverUrl = serverUrl;
        this.userId = userId;
        this.roomId = roomId;
        this.ws = null;
        this.reconnectInterval = 3000;
        this.isConnected = false;
    }

    connect() {
        let wsUrl = `${this.serverUrl}/ws?user_id=${this.userId}`;
        if (this.roomId) {
            wsUrl += `&room_id=${this.roomId}`;
        }
        this.ws = new WebSocket(wsUrl);

        this.ws.onopen = () => {
            console.log('Connected to translation text service');
            this.isConnected = true;

            // 发送订阅消息（同时订阅用户和房间）
            this.subscribe(this.userId, this.roomId);
        };

        this.ws.onmessage = (event) => {
            const message = JSON.parse(event.data);
            this.handleMessage(message);
        };

        this.ws.onclose = () => {
            console.log('Connection closed, reconnecting...');
            this.isConnected = false;
            setTimeout(() => this.connect(), this.reconnectInterval);
        };

        this.ws.onerror = (error) => {
            console.error('WebSocket error:', error);
        };

        // 心跳保活
        this.heartbeatTimer = setInterval(() => {
            if (this.isConnected) {
                this.ws.send(JSON.stringify({ type: 'ping' }));
            }
        }, 30000);
    }

    subscribe(userId, roomId = '') {
        this.ws.send(JSON.stringify({
            type: 'subscribe',
            data: {
                user_id: userId,
                room_id: roomId
            }
        }));
    }

    handleMessage(message) {
        switch (message.type) {
            case 'connected':
                console.log('Service connected:', message.data.message);
                break;

            case 'subscribed':
                console.log('Subscribed to:', message.data.user_id, 'room:', message.data.room_id);
                break;

            case 'translation_text':
                console.log('Original:', message.data.original_text);
                console.log('Translated:', message.data.translated_text);
                console.log('Speaker:', message.data.source_user);
                break;

            case 'original_speech_text':
                // 原语音识别文字，房间内所有用户均可收到
                console.log('[Original Speech] Speaker:', message.data.source_user);
                console.log('[Original Speech] Text:', message.data.original_text);
                console.log('[Original Speech] Lang:', message.data.source_lang);
                break;

            case 'cached_texts':
                console.log('Cached messages:', message.data.count);
                message.data.texts.forEach(text => {
                    console.log('-', text.translated_text);
                });
                break;

            case 'error':
                console.error('Error:', message.data.message);
                break;
        }
    }

    disconnect() {
        if (this.heartbeatTimer) {
            clearInterval(this.heartbeatTimer);
        }
        if (this.ws) {
            this.ws.close();
        }
    }
}

// 使用示例
// 订阅翻译文本（发给 user_B 的翻译）+ 房间内原语音文字（所有用户可收）
const client = new TranslationTextClient('ws://localhost:8086', 'user_B', 'room1');
client.connect();
```

---

### 13.2 纯 WebSocket 接收房间事件（8085 端口）

```javascript
class RoomNotificationClient {
    constructor(wsUrl) {
        this.wsUrl = wsUrl;
        this.ws = null;
    }

    connect() {
        this.ws = new WebSocket(this.wsUrl);

        this.ws.onopen = () => {
            console.log('WebSocket connected');
        };

        this.ws.onmessage = (event) => {
            const msg = JSON.parse(event.data);
            this.handleMessage(msg);
        };

        this.ws.onerror = (error) => {
            console.error('WebSocket error:', error);
        };

        this.ws.onclose = () => {
            console.log('WebSocket closed');
        };
    }

    subscribe(roomId, userId) {
        this.ws.send(JSON.stringify({
            type: 'subscribe',
            room_id: roomId,
            user_id: userId
        }));
    }

    handleMessage(msg) {
        switch (msg.type) {
            case 'subscribed':
                console.log('Subscribed to room:', msg.room_id);
                break;

            case 'member_joined':
                console.log('User joined:', msg.data.user_id);
                break;

            case 'member_left':
                console.log('User left:', msg.data.user_id);
                break;

            case 'member_muted':
                console.log('You were muted by:', msg.data.operator_id);
                break;

            case 'member_unmuted':
                console.log('You were unmuted');
                break;

            case 'room_muted_all':
                console.log('Room muted all, muted:', msg.data.muted_count, 'members');
                break;

            case 'translation_text':
                console.log('Translation:', msg.data.original_text, '->',
                    msg.data.translated_text);
                break;

            case 'user_speaking_start':
                console.log('User speaking:', msg.data.user_id);
                break;

            case 'user_speaking_stop':
                console.log('User stopped speaking:', msg.data.user_id);
                break;

            case 'original_speech_text':
                console.log('[Original Speech] Speaker:', msg.data.user_id);
                console.log('[Original Speech] Text:', msg.data.original_text);
                break;

            case 'room_knock':
                console.log('Knock from:', msg.data.user_id,
                    'message:', msg.data.message);
                break;

            case 'room_knock_accepted':
                console.log('Your knock was accepted, you can join now');
                break;

            case 'room_knock_rejected':
                console.log('Your knock was rejected, reason:',
                    msg.data.reason);
                break;

            case 'pong':
                console.log('Heartbeat response');
                break;
        }
    }

    disconnect() {
        if (this.ws) {
            this.ws.close();
        }
    }
}

// 使用示例
const roomClient = new RoomNotificationClient(
    'ws://localhost:8085/ws?room=room1&user=user_B'
);
roomClient.connect();
roomClient.subscribe('room1', 'user_B');
```

---

### 13.3 完整的翻译申请流程

```javascript
async function requestTranslation(roomId, sourceUser, targetUser, toLang) {
    try {
        const response = await fetch(
            'http://localhost:8085/api/v1/translation/request',
            {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    room_id: roomId,
                    source_user: sourceUser,
                    target_user: targetUser,
                    to_lang: toLang
                })
            }
        );

        const result = await response.json();

        if (result.code === 0) {
            console.log('Translation started');
            console.log('Request ID:', result.data.request_id);
            console.log('Stream URL:', result.data.stream_url);

            // 注册为拉流者
            await registerPuller(result.data.request_id, targetUser);

            // 开始拉流
            startPullingStream(result.data.stream_url);

            return result.data;
        } else {
            console.error('Failed to request translation:', result.message);
        }
    } catch (error) {
        console.error('Error:', error);
    }
}

async function registerPuller(requestId, pullerId) {
    await fetch(
        'http://localhost:8085/api/v1/translation/register_puller',
        {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ request_id: requestId, puller_id: pullerId })
        }
    );
}

// 定期发送心跳
function startHeartbeat(requestId, pullerId) {
    setInterval(async () => {
        await fetch(
            'http://localhost:8085/api/v1/translation/heartbeat',
            {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({
                    request_id: requestId,
                    puller_id: pullerId,
                    source_stream_active: true
                })
            }
        );
    }, 5000);
}
```

---

## 附录 A：WebSocket 连接地址汇总

| 服务 | WebSocket 地址 |
|------|---------------|
| 房间事件通知（8085端口） | `ws://localhost:8085/ws?room=xxx&user=xxx` |
| 翻译文本推送（指定用户） | `ws://localhost:8086/ws?user_id=xxx` |
| 翻译文本推送（加入房间） | `ws://localhost:8086/ws?user_id=xxx&room_id=yyy` |

---

## 附录 B：流地址命名规则

| 流类型 | 流名称格式 | 示例 |
|--------|-----------|------|
| 原声音频流 | `{room_id}_{user_id}` | `room1_user_A` |
| 翻译音频流 | `{room_id}_{source_user}_to_{lang}` | `room1_user_A_to_zh` |

| 流类型 | RTMP 拉流地址 |
|--------|-------------|
| 原声音频流 | `rtmp://localhost:1935/live/room1_user_A` |
| 翻译音频流 | `rtmp://localhost:1935/live/room1_user_A_to_zh` |
