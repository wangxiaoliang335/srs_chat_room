# FastAPI 服务器 API 文档

## 基础信息

- **服务器地址**: `http://47.107.33.154:8085`
- **WebSocket地址**: `ws://47.107.33.154:8085/ws`

---

## 健康检查

### GET /health
健康检查

**响应**:
```json
{
  "status": "ok"
}
```

### GET /api/v1/health
健康检查（V1版本）

**响应**:
```json
{
  "status": "ok"
}
```

---

## 房间管理

### GET /api/v1/rooms
获取所有房间列表

**响应**:
```json
{
  "status": "ok",
  "rooms": [
    {
      "room_id": "room_123",
      "owner_id": "user_001",
      "member_count": 5
    }
  ]
}
```

---

### POST /api/v1/room
创建房间

**请求体**:
```json
{
  "room_id": "room_123",
  "owner_id": "user_001"
}
```

**响应**:
```json
{
  "status": "ok",
  "room_id": "room_123"
}
```

**错误响应** (400):
```json
{
  "detail": "Room already exists"
}
```

---

### GET /api/v1/room/{room_id}
获取房间信息

**路径参数**:
- `room_id`: 房间ID

**响应**:
```json
{
  "status": "ok",
  "room": {
    "room_id": "room_123",
    "owner_id": "user_001",
    "member_count": 5,
    "allow_speak": true
  }
}
```

**错误响应** (404):
```json
{
  "detail": "Room not found"
}
```

---

### POST /api/v1/room/{room_id}/join
加入房间

**路径参数**:
- `room_id`: 房间ID

**请求体**:
```json
{
  "user_id": "user_002"
}
```

**响应**:
```json
{
  "status": "ok",
  "members": [
    {
      "user_id": "user_001",
      "room_id": "room_123",
      "role": "owner",
      "status": "normal",
      "joined_at": "2026-05-15 16:27:13",
      "last_active": "2026-05-15 16:27:13",
      "publish_allowed": true
    },
    {
      "user_id": "user_002",
      "room_id": "room_123",
      "role": "member",
      "status": "normal",
      "joined_at": "2026-05-15 16:30:00",
      "last_active": "2026-05-15 16:30:00",
      "publish_allowed": true
    }
  ]
}
```

---

### POST /api/v1/room/{room_id}/leave
离开房间

**路径参数**:
- `room_id`: 房间ID

**请求体**:
```json
{
  "user_id": "user_002"
}
```

**响应**:
```json
{
  "status": "ok"
}
```

---

### GET /api/v1/room/{room_id}/members
获取房间成员列表

**路径参数**:
- `room_id`: 房间ID

**响应**:
```json
{
  "status": "ok",
  "members": [
    {
      "user_id": "user_001",
      "room_id": "room_123",
      "role": "owner",
      "status": "normal",
      "publish_allowed": true
    },
    {
      "user_id": "user_002",
      "room_id": "room_123",
      "role": "member",
      "status": "normal",
      "publish_allowed": true
    }
  ]
}
```

---

### GET /api/v1/room/{room_id}/check-publish
检查用户是否可以发布（发言）

**路径参数**:
- `room_id`: 房间ID

**查询参数**:
- `user_id`: 用户ID

**响应**:
```json
{
  "status": "ok",
  "can_publish": true
}
```

---

## WebSocket

### WebSocket /ws
原生 WebSocket 连接

**连接参数** (Query String):
- `room`: 房间ID (必填)
- `user`: 用户ID (可选)

**示例**:
```
ws://47.107.33.154:8085/ws?room=room_123&user=user_001
```

**客户端发送消息**:

1. 订阅房间
```json
{
  "type": "subscribe",
  "room_id": "room_456"
}
```

2. 心跳
```json
{
  "type": "ping"
}
```

**服务器推送消息**:

1. 连接成功
```json
{
  "type": "connected",
  "room_id": "room_123",
  "user_id": "user_001"
}
```

2. 心跳响应
```json
{
  "type": "pong"
}
```

3. 用户加入
```json
{
  "type": "member_joined",
  "room_id": "room_123",
  "user_id": "user_002"
}
```

4. 用户离开
```json
{
  "type": "member_left",
  "room_id": "room_123",
  "user_id": "user_002"
}
```

5. 翻译开始
```json
{
  "type": "translation_started",
  "room_id": "room_123",
  "source_user": "user_001",
  "target_user": "user_002",
  "target_lang": "en"
}
```

6. 翻译文本
```json
{
  "type": "translation_text",
  "room_id": "room_123",
  "source_user": "user_001",
  "target_user": "user_002",
  "original_text": "你好",
  "translated_text": "Hello",
  "source_lang": "zh",
  "target_lang": "en"
}
```

7. 翻译停止
```json
{
  "type": "translation_stopped",
  "room_id": "room_123",
  "source_user": "user_001"
}
```

---

### POST /api/v1/ws/subscribe
WebSocket 订阅（兼容旧接口）

**请求体**:
```json
{
  "room_id": "room_123",
  "user_id": "user_001"
}
```

**响应**:
```json
{
  "status": "ok",
  "ws_url": "ws://47.107.33.154:8085/ws?room=room_123&user=user_001"
}
```

---

### GET /api/v1/ws/status
WebSocket 连接状态

**响应**:
```json
{
  "status": "ok",
  "active_connections": 10
}
```

---

## 翻译服务

### POST /api/v1/translation/start
开始翻译

**请求体**:
```json
{
  "room_id": "room_123",
  "source_user": "user_001",
  "source_lang": "zh",
  "target_user": "user_002",
  "target_lang": "en"
}
```

**响应**:
```json
{
  "status": "ok",
  "request_id": "trans_abc123"
}
```

---

### POST /api/v1/translation/text
发送翻译文本

**请求体**:
```json
{
  "room_id": "room_123",
  "source_user": "user_001",
  "target_user": "user_002",
  "original_text": "你好",
  "translated_text": "Hello",
  "source_lang": "zh",
  "target_lang": "en"
}
```

**响应**:
```json
{
  "status": "ok"
}
```

---

### POST /api/v1/translation/stop
停止翻译

**请求体**:
```json
{
  "request_id": "trans_abc123"
}
```

**响应**:
```json
{
  "status": "ok"
}
```

---

### GET /api/v1/translation/active
获取活跃翻译列表

**响应**:
```json
{
  "status": "ok",
  "translations": [
    {
      "request_id": "trans_abc123",
      "room_id": "room_123",
      "source_user": "user_001",
      "target_user": "user_002",
      "source_lang": "zh",
      "target_lang": "en",
      "status": "active"
    }
  ]
}
```

---

## SRS Webhook 回调

### POST /api/v1/hooks/on_publish
推流开始回调

**响应**:
```json
{
  "code": 0,
  "server": "rtmp://localhost:1935"
}
```

---

### POST /api/v1/hooks/on_unpublish
推流结束回调

**响应**:
```json
{
  "code": 0
}
```

---

### POST /api/v1/hooks/on_play
播放开始回调

**响应**:
```json
{
  "code": 0
}
```

---

### POST /api/v1/hooks/on_stop
播放结束回调

**响应**:
```json
{
  "code": 0
}
```

---

## 通用响应格式

### 成功响应
```json
{
  "status": "ok"
}
```

或包含数据:
```json
{
  "status": "ok",
  "data": { ... }
}
```

### 错误响应
```json
{
  "detail": "错误描述信息"
}
```

常见 HTTP 状态码:
- `200`: 成功
- `400`: 请求参数错误
- `404`: 资源不存在
- `422`: 请求体验证失败
- `500`: 服务器内部错误
