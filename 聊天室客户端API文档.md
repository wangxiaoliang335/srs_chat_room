# 聊天室客户端 API 文档

本文档面向客户端开发者，列出所有需要调用的 HTTP API 接口。

## 基础信息

- **服务器地址**: `http://localhost:8085`（生产环境替换为实际地址）
- **协议**: HTTP/HTTPS
- **响应格式**: JSON
- **编码**: UTF-8

---

## 通用响应格式

```json
{
  "code": 0,           // 0=成功，非0=失败
  "message": "success",
  "data": {...}        // 业务数据
}
```

## 错误码

| code | 说明 |
|------|------|
| 0 | 成功 |
| 400 | 参数错误 |
| 403 | 权限不足 |
| 404 | 资源不存在 |
| 409 | 资源冲突 |

---

## 一、房间管理

### 1. 创建房间
```
POST /api/v1/room
```

**请求**:
```json
{
  "room_id": "room_123",
  "owner_id": "user_001",
  "name": "测试房间"
}
```

**响应** (201):
```json
{
  "code": 0,
  "data": {
    "room_id": "room_123",
    "name": "测试房间",
    "owner_id": "user_001"
  }
}
```

---

### 2. 获取房间信息
```
GET /api/v1/room/<room_id>
```

**响应** (200):
```json
{
  "code": 0,
  "data": {
    "room_id": "room_123",
    "owner_id": "user_001",
    "member_count": 5,
    "allow_speak": true
  }
}
```

---

### 3. 获取所有房间
```
GET /api/v1/rooms
```

**响应**:
```json
{
  "code": 0,
  "data": {
    "rooms": [...],
    "total": 10
  }
}
```

---

### 4. 删除房间（仅房主）
```
DELETE /api/v1/room/<room_id>?operator_id=<owner_id>
```

**响应**: `{"code": 0, "message": "success"}`

---

## 二、用户管理

### 5. 加入房间
```
POST /api/v1/room/<room_id>/join
```

**请求**:
```json
{
  "user_id": "user_002",
  "role": "member"
}
```

**响应**:
```json
{
  "code": 0,
  "data": {
    "user_id": "user_002",
    "room_id": "room_123",
    "role": "member"
  }
}
```

---

### 6. 离开房间
```
POST /api/v1/room/<room_id>/leave
```

**请求**:
```json
{
  "user_id": "user_002"
}
```

**响应**: `{"code": 0}`

---

### 7. 获取房间成员
```
GET /api/v1/room/<room_id>/members
```

**响应**:
```json
{
  "code": 0,
  "data": {
    "member_count": 3,
    "members": [
      {
        "user_id": "user_001",
        "role": "owner",
        "status": "normal"
      }
    ]
  }
}
```

---

## 三、权限管理

### 8. 检查发布权限
```
GET /api/v1/room/<room_id>/check-publish?user_id=<user_id>
```

**响应**:
```json
{
  "code": 0,
  "data": {
    "user_id": "user_001",
    "can_publish": true,
    "status": "normal"
  }
}
```

---

### 9. 获取正在说话的用户
```
GET /api/v1/room/<room_id>/speaking
```

**响应**:
```json
{
  "code": 0,
  "data": {
    "speaking_users": ["user_001", "user_002"]
  }
}
```

---

## 四、翻译功能

### 10. 申请翻译
```
POST /api/v1/translation/request
```

**请求**:
```json
{
  "room_id": "room_123",
  "source_user": "A",
  "target_user": "B",
  "to_lang": "zh"
}
```

**响应**:
```json
{
  "code": 0,
  "data": {
    "request_id": "xxx-xxx",
    "stream_url": "rtmp://server/live/room123_A_to_zh"
  }
}
```

---

### 11. 取消翻译
```
POST /api/v1/translation/cancel
```

**请求** (方式1):
```json
{
  "request_id": "xxx-xxx"
}
```

或 (方式2):
```json
{
  "room_id": "room_123",
  "source_user": "A",
  "target_user": "B",
  "to_lang": "zh"
}
```

**响应**: `{"code": 0}`

---

### 12. 获取用户可用流
```
GET /api/v1/translation/streams/<room_id>/<user_id>
```

**响应**:
```json
{
  "code": 0,
  "data": {
    "user_id": "B",
    "streams": [
      {
        "type": "original",
        "url": "rtmp://server/live/room123_A"
      },
      {
        "type": "translation",
        "source_user": "A",
        "to_lang": "zh",
        "url": "rtmp://server/live/room123_A_to_zh"
      }
    ]
  }
}
```

---

## 五、WebSocket 实时通知

### 13. 获取 WebSocket 连接地址
```
POST /api/v1/ws/subscribe
```

**请求**:
```json
{
  "room_id": "room_123",
  "user_id": "user_001"
}
```

**响应**:
```json
{
  "code": 0,
  "data": {
    "ws_url": "ws://localhost:8085/ws?room=room_123&user=user_001"
  }
}
```

---

### 14. WebSocket 连接状态
```
GET /api/v1/ws/status
```

**响应**:
```json
{
  "code": 0,
  "data": {
    "total_connections": 10,
    "active_rooms": 3
  }
}
```

---

## 六、WebSocket 消息类型

客户端连接到 WebSocket 后，会收到以下消息：

### 连接相关
| 消息类型 | 说明 | 数据 |
|----------|------|------|
| `connected` | 连接成功 | client_id, user_id |
| `subscribed` | 订阅成功 | room_id, type |

### 用户事件
| 消息类型 | 说明 | 数据 |
|----------|------|------|
| `user_joined` | 用户加入 | room_id, user_id |
| `user_left` | 用户离开 | room_id, user_id |
| `user_speaking_start` | 开始说话 | room_id, user_id, stream_url |
| `user_speaking_stop` | 停止说话 | room_id, user_id |

### 权限事件
| 消息类型 | 说明 | 数据 |
|----------|------|------|
| `muted` | 被禁言 | room_id, user_id, operator_id |
| `unmuted` | 解除禁言 | room_id, user_id |
| `kicked` | 被踢出 | room_id, user_id, operator_id |

### 翻译事件
| 消息类型 | 说明 | 数据 |
|----------|------|------|
| `translation_text` | 翻译文本 | room_id, source_user, original_text, translated_text |
| `translation_started` | 翻译开始 | room_id, source_user, to_lang |
| `translation_stopped` | 翻译停止 | room_id, source_user, to_lang |

### 敲门事件
| 消息类型 | 说明 | 数据 |
|----------|------|------|
| `knock` | 有人敲门 | room_id, knocker_id |
| `knock_accepted` | 敲门被接受 | room_id, knocker_id |
| `knock_rejected` | 敲门被拒绝 | room_id, knocker_id |

---

## 七、客户端主动发送消息

客户端也可以主动发送消息到 WebSocket：

### 订阅房间
```json
{
  "type": "subscribe",
  "room_id": "room_123",
  "user_id": "user_001"
}
```

### 取消订阅
```json
{
  "type": "unsubscribe",
  "room_id": "room_123"
}
```

### 心跳
```json
{
  "type": "ping"
}
```

---

## 八、敲门申请（访客）

### 15. 敲门请求加入
```
POST /api/v1/room/<room_id>/knock
```

**请求**:
```json
{
  "user_id": "visitor_001",
  "message": "想加入聊天"
}
```

**响应**:
```json
{
  "code": 0,
  "data": {
    "room_id": "room_123",
    "owner_id": "owner_001"
  }
}
```

---

## 九、健康检查

### 16. 服务健康检查
```
GET /health
```

**响应**: `{"status": "ok"}`

---

## 接口速查表

| 功能 | 方法 | 路径 |
|------|------|------|
| 创建房间 | POST | `/api/v1/room` |
| 获取房间 | GET | `/api/v1/room/<room_id>` |
| 房间列表 | GET | `/api/v1/rooms` |
| 删除房间 | DELETE | `/api/v1/room/<room_id>` |
| 加入房间 | POST | `/api/v1/room/<room_id>/join` |
| 离开房间 | POST | `/api/v1/room/<room_id>/leave` |
| 成员列表 | GET | `/api/v1/room/<room_id>/members` |
| 发布权限 | GET | `/api/v1/room/<room_id>/check-publish` |
| 说话状态 | GET | `/api/v1/room/<room_id>/speaking` |
| 申请翻译 | POST | `/api/v1/translation/request` |
| 取消翻译 | POST | `/api/v1/translation/cancel` |
| 获取流 | GET | `/api/v1/translation/streams/<room_id>/<user_id>` |
| WebSocket地址 | POST | `/api/v1/ws/subscribe` |
| WS状态 | GET | `/api/v1/ws/status` |
| 敲门 | POST | `/api/v1/room/<room_id>/knock` |
| 健康检查 | GET | `/health` |
