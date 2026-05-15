# 聊天室 API 接口文档

本文档列出了聊天室服务器的所有 HTTP API 接口。

## 基础信息

- **默认端口**: 8085
- **协议**: HTTP/HTTPS
- **响应格式**: JSON
- **编码**: UTF-8

---

## 通用响应格式

```json
{
  "code": 0,           // 0表示成功，非0表示错误
  "message": "success", // 状态消息
  "data": {...}        // 具体数据（根据接口不同而不同）
}
```

## 错误码说明

| 错误码 | 说明 |
|--------|------|
| 0 | 成功 |
| 400 | 参数错误 |
| 403 | 权限不足 |
| 404 | 资源不存在 |
| 409 | 资源冲突（如翻译请求已存在） |
| 500 | 服务器内部错误 |

---

## 一、房间管理接口

### 1. 创建房间
- **路径**: `/api/v1/room`
- **方法**: POST
- **描述**: 创建一个新房间

**请求体**:
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
  "message": "success",
  "data": {
    "room_id": "room_123",
    "name": "测试房间",
    "owner_id": "user_001",
    "created_at": "2024-01-01 10:00:00"
  }
}
```

---

### 2. 获取房间信息
- **路径**: `/api/v1/room/<room_id>`
- **方法**: GET
- **描述**: 获取指定房间的详细信息

**响应** (200):
```json
{
  "code": 0,
  "message": "success",
  "data": {
    "room_id": "room_123",
    "name": "测试房间",
    "owner_id": "user_001",
    "created_at": "2024-01-01 10:00:00",
    "member_count": 5,
    "allow_speak": true
  }
}
```

---

### 3. 获取所有房间
- **路径**: `/api/v1/rooms`
- **方法**: GET
- **描述**: 获取所有房间列表

**响应** (200):
```json
{
  "code": 0,
  "message": "success",
  "data": {
    "rooms": [
      {
        "room_id": "room_123",
        "owner_id": "user_001",
        "member_count": 5,
        "created_at": "2024-01-01 10:00:00"
      }
    ],
    "total": 10
  }
}
```

---

### 4. 删除房间
- **路径**: `/api/v1/room/<room_id>`
- **方法**: DELETE
- **描述**: 删除指定房间（仅房主可操作）

**查询参数**:
- `operator_id`: 操作者ID（必须是房主）

**响应** (200):
```json
{
  "code": 0,
  "message": "success"
}
```

---

## 二、用户管理接口

### 5. 用户加入房间
- **路径**: `/api/v1/room/<room_id>/join`
- **方法**: POST
- **描述**: 用户加入指定房间

**请求体**:
```json
{
  "user_id": "user_002",
  "role": "member"
}
```

**响应** (200):
```json
{
  "code": 0,
  "message": "success",
  "data": {
    "user_id": "user_002",
    "room_id": "room_123",
    "role": "member",
    "joined_at": "2024-01-01 10:00:00"
  }
}
```

---

### 6. 用户离开房间
- **路径**: `/api/v1/room/<room_id>/leave`
- **方法**: POST
- **描述**: 用户离开指定房间

**请求体**:
```json
{
  "user_id": "user_002"
}
```

**响应** (200):
```json
{
  "code": 0,
  "message": "success"
}
```

---

### 7. 获取房间成员列表
- **路径**: `/api/v1/room/<room_id>/members`
- **方法**: GET
- **描述**: 获取房间内所有成员列表

**查询参数**:
- `role`: 可选，按角色筛选 (owner, admin, member, guest)
- `status`: 可选，按状态筛选 (normal, muted, mic_off)

**响应** (200):
```json
{
  "code": 0,
  "message": "success",
  "data": {
    "room_id": "room_123",
    "owner_id": "user_001",
    "member_count": 2,
    "allow_speak": true,
    "members": [
      {
        "user_id": "user_001",
        "role": "owner",
        "status": "normal",
        "publish_allowed": true,
        "joined_at": "2024-01-01 10:00:00"
      },
      {
        "user_id": "user_002",
        "role": "member",
        "status": "normal",
        "publish_allowed": true,
        "joined_at": "2024-01-01 10:05:00"
      }
    ]
  }
}
```

---

### 8. 获取成员详细信息
- **路径**: `/api/v1/room/<room_id>/member/<user_id>`
- **方法**: GET
- **描述**: 获取指定成员的详细信息

**响应** (200):
```json
{
  "code": 0,
  "message": "success",
  "data": {
    "user_id": "user_001",
    "room_id": "room_123",
    "role": "owner",
    "status": "normal",
    "publish_allowed": true,
    "joined_at": "2024-01-01 10:00:00"
  }
}
```

---

### 9. 更新成员角色
- **路径**: `/api/v1/room/<room_id>/member/<user_id>/role`
- **方法**: PUT
- **描述**: 更新房间成员的角色（仅房主可操作）

**请求体**:
```json
{
  "operator_id": "owner_user_001",
  "role": "admin"
}
```

**响应** (200):
```json
{
  "code": 0,
  "message": "success",
  "data": {
    "user_id": "user_002",
    "role": "admin"
  }
}
```

---

## 三、禁言管理接口

### 10. 禁言用户
- **路径**: `/api/v1/room/<room_id>/member/<user_id>/mute`
- **方法**: POST
- **描述**: 禁言指定用户（房主或管理员可操作）

**请求体**:
```json
{
  "operator_id": "admin_user_001"
}
```

**响应** (200):
```json
{
  "code": 0,
  "message": "success",
  "data": {
    "user_id": "user_002",
    "status": "muted",
    "publish_allowed": false
  }
}
```

---

### 11. 解除禁言
- **路径**: `/api/v1/room/<room_id>/member/<user_id>/unmute`
- **方法**: POST
- **描述**: 解除对用户的禁言（房主或管理员可操作）

**请求体**:
```json
{
  "operator_id": "admin_user_001"
}
```

**响应** (200):
```json
{
  "code": 0,
  "message": "success",
  "data": {
    "user_id": "user_002",
    "status": "normal",
    "publish_allowed": true
  }
}
```

---

### 12. 禁麦（禁止发布）
- **路径**: `/api/v1/room/<room_id>/member/<user_id>/mic/disable`
- **方法**: POST
- **描述**: 禁止用户使用麦克风发布（房主或管理员可操作）

**请求体**:
```json
{
  "operator_id": "admin_user_001"
}
```

**响应** (200):
```json
{
  "code": 0,
  "message": "success",
  "data": {
    "user_id": "user_002",
    "status": "mic_off",
    "publish_allowed": false
  }
}
```

---

### 13. 解除禁麦
- **路径**: `/api/v1/room/<room_id>/member/<user_id>/mic/enable`
- **方法**: POST
- **描述**: 允许用户使用麦克风发布（房主或管理员可操作）

**请求体**:
```json
{
  "operator_id": "admin_user_001"
}
```

**响应** (200):
```json
{
  "code": 0,
  "message": "success",
  "data": {
    "user_id": "user_002",
    "status": "normal",
    "publish_allowed": true
  }
}
```

---

### 14. 全体禁言
- **路径**: `/api/v1/room/<room_id>/mute-all`
- **方法**: POST
- **描述**: 房间全体禁言（除房主外）

**请求体**:
```json
{
  "operator_id": "owner_user_001"
}
```

**响应** (200):
```json
{
  "code": 0,
  "message": "success",
  "data": {
    "room_id": "room_123",
    "allow_speak": false,
    "muted_count": 5
  }
}
```

---

### 15. 解除全体禁言
- **路径**: `/api/v1/room/<room_id>/unmute-all`
- **方法**: POST
- **描述**: 解除房间全体禁言

**请求体**:
```json
{
  "operator_id": "owner_user_001"
}
```

**响应** (200):
```json
{
  "code": 0,
  "message": "success",
  "data": {
    "room_id": "room_123",
    "allow_speak": true,
    "unmuted_count": 5
  }
}
```

---

### 16. 踢出用户
- **路径**: `/api/v1/room/<room_id>/member/<user_id>/kick`
- **方法**: DELETE
- **描述**: 将用户从房间中踢出（房主或管理员可操作）

**查询参数**:
- `operator_id`: 操作者ID

**响应** (200):
```json
{
  "code": 0,
  "message": "success"
}
```

---

## 四、权限检查接口

### 17. 检查发布权限
- **路径**: `/api/v1/room/<room_id>/check-publish`
- **方法**: GET
- **描述**: 检查用户是否可以发布（发言）

**查询参数**:
- `user_id`: 用户ID

**响应** (200):
```json
{
  "code": 0,
  "message": "success",
  "data": {
    "user_id": "user_001",
    "can_publish": true,
    "status": "normal"
  }
}
```

---

## 五、敲门管理接口

### 18. 敲门请求加入
- **路径**: `/api/v1/room/<room_id>/knock`
- **方法**: POST
- **描述**: 用户敲门请求加入房间

**请求体**:
```json
{
  "user_id": "visitor_001",
  "message": "想加入聊天"
}
```

**响应** (200):
```json
{
  "code": 0,
  "message": "success",
  "data": {
    "room_id": "room_123",
    "owner_id": "owner_001",
    "knocker_id": "visitor_001"
  }
}
```

---

### 19. 接受敲门
- **路径**: `/api/v1/room/<room_id>/knock/accept`
- **方法**: POST
- **描述**: 房主或管理员接受敲门者加入

**请求体**:
```json
{
  "operator_id": "owner_001",
  "knocker_id": "visitor_001",
  "role": "member"
}
```

**响应** (200):
```json
{
  "code": 0,
  "message": "success"
}
```

---

### 20. 拒绝敲门
- **路径**: `/api/v1/room/<room_id>/knock/reject`
- **方法**: POST
- **描述**: 房主或管理员拒绝敲门者加入

**请求体**:
```json
{
  "operator_id": "owner_001",
  "knocker_id": "visitor_001",
  "reason": "房间已满"
}
```

**响应** (200):
```json
{
  "code": 0,
  "message": "success"
}
```

---

## 六、说话状态接口

### 21. 获取正在说话的用户
- **路径**: `/api/v1/room/<room_id>/speaking`
- **方法**: GET
- **描述**: 获取房间中正在说话的用户列表

**响应** (200):
```json
{
  "code": 0,
  "message": "success",
  "data": {
    "room_id": "room_123",
    "speaking_users": ["user_001", "user_002"]
  }
}
```

---

## 七、翻译管理接口

### 22. 申请翻译
- **路径**: `/api/v1/translation/request`
- **方法**: POST
- **描述**: 申请翻译服务，将说话人的音频翻译成指定语言

**请求体**:
```json
{
  "room_id": "room1",
  "source_user": "A",
  "target_user": "B",
  "to_lang": "zh"
}
```

**响应** (200):
```json
{
  "code": 0,
  "message": "success",
  "data": {
    "request_id": "xxx",
    "stream_url": "rtmp://host/live/room1_A_to_zh"
  }
}
```

---

### 23. 取消翻译
- **路径**: `/api/v1/translation/cancel`
- **方法**: POST
- **描述**: 取消翻译请求

**请求体** (方式1，通过request_id):
```json
{
  "request_id": "xxx"
}
```

或 (方式2，通过参数):
```json
{
  "room_id": "room1",
  "source_user": "A",
  "target_user": "B",
  "to_lang": "zh"
}
```

**响应** (200):
```json
{
  "code": 0,
  "message": "success"
}
```

---

### 24. 获取用户可用流
- **路径**: `/api/v1/translation/streams/<room_id>/<user_id>`
- **方法**: GET
- **描述**: 查询用户的可用流列表（包括原音和翻译流）

**响应** (200):
```json
{
  "code": 0,
  "message": "success",
  "data": {
    "user_id": "B",
    "streams": [
      {
        "type": "original",
        "user_id": "A",
        "url": "rtmp://host/live/room1_A",
        "description": "原声音频"
      },
      {
        "type": "translation",
        "source_user": "A",
        "to_lang": "zh",
        "url": "rtmp://host/live/room1_A_to_zh",
        "description": "A的中文翻译"
      }
    ]
  }
}
```

---

### 25. 获取所有翻译请求
- **路径**: `/api/v1/translation/requests`
- **方法**: GET
- **描述**: 获取所有翻译请求（调试用）

**响应** (200):
```json
{
  "code": 0,
  "message": "success",
  "data": {
    "count": 2,
    "requests": [
      {
        "request_id": "xxx",
        "room_id": "room1",
        "source_user": "A",
        "target_user": "B",
        "to_lang": "zh",
        "status": "active",
        "stream_url": "rtmp://host/live/room1_A_to_zh"
      }
    ]
  }
}
```

---

### 26. 拉流者心跳
- **路径**: `/api/v1/translation/heartbeat`
- **方法**: POST
- **描述**: 拉流客户端定期上报心跳，表明仍在拉取翻译流

**请求体**:
```json
{
  "request_id": "xxx",
  "puller_id": "user_b",
  "source_stream_active": true
}
```

**响应** (200):
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

---

### 27. 注册拉流者
- **路径**: `/api/v1/translation/register_puller`
- **方法**: POST
- **描述**: 客户端开始拉取翻译流时注册

**请求体**:
```json
{
  "request_id": "xxx",
  "puller_id": "user_b"
}
```

**响应** (200):
```json
{
  "code": 0,
  "message": "success"
}
```

---

### 28. 注销拉流者
- **路径**: `/api/v1/translation/unregister_puller`
- **方法**: POST
- **描述**: 客户端停止拉取翻译流时注销

**请求体**:
```json
{
  "request_id": "xxx",
  "puller_id": "user_b"
}
```

**响应** (200):
```json
{
  "code": 0,
  "message": "success"
}
```

---

### 29. 获取拉流者列表
- **路径**: `/api/v1/translation/requests/<request_id>/pullers`
- **方法**: GET
- **描述**: 获取翻译请求的拉流者列表

**响应** (200):
```json
{
  "code": 0,
  "message": "success",
  "data": {
    "request_id": "xxx",
    "pullers": [
      {
        "puller_id": "user_b",
        "last_heartbeat": 1234567890,
        "seconds_ago": 3,
        "is_alive": true
      }
    ]
  }
}
```

---

## 八、翻译文本推送接口

### 30. 推送翻译文本
- **路径**: `/api/v1/translation/text/push`
- **方法**: POST
- **描述**: 接收翻译服务推送的翻译文本，转发给客户端

**请求体**:
```json
{
  "target_user": "B",
  "request_id": "xxx",
  "room_id": "room1",
  "source_user": "A",
  "original_text": "Hello",
  "translated_text": "你好",
  "source_lang": "en",
  "target_lang": "zh"
}
```

**响应** (200):
```json
{
  "code": 0,
  "message": "success"
}
```

---

### 31. 推送原语音识别文本
- **路径**: `/api/v1/original/speech/text/push`
- **方法**: POST
- **描述**: 接收原语音识别文字，广播给房间所有用户

**请求体**:
```json
{
  "room_id": "room1",
  "source_user": "A",
  "original_text": "Hello",
  "source_lang": "en"
}
```

**响应** (200):
```json
{
  "code": 0,
  "message": "success"
}
```

---

## 九、SRS 回调接口

### 32. 发布回调
- **路径**: `/api/v1/streams/on_publish`
- **方法**: POST
- **描述**: SRS 调用此接口通知用户开始发布流

**请求体**:
```json
{
  "stream": "room1_user001",
  "tcUrl": "rtmp://host/live",
  "client_ip": "192.168.1.1"
}
```

**响应** (200):
```json
{
  "code": 0
}
```

---

### 33. 停止发布回调
- **路径**: `/api/v1/streams/on_unpublish`
- **方法**: POST
- **描述**: SRS 调用此接口通知用户停止发布流

**请求体**:
```json
{
  "stream": "room1_user001"
}
```

**响应** (200):
```json
{
  "code": 0
}
```

---

### 34. 播放回调
- **路径**: `/api/v1/streams/on_play`
- **方法**: POST
- **描述**: SRS 调用此接口验证播放权限

**请求体**:
```json
{
  "stream": "room1_user001",
  "tcUrl": "rtmp://host/live",
  "client_ip": "192.168.1.1"
}
```

**响应** (200):
```json
{
  "code": 0
}
```

---

### 35. 停止播放回调
- **路径**: `/api/v1/streams/on_stop`
- **方法**: POST
- **描述**: SRS 调用此接口通知停止播放

**请求体**:
```json
{
  "stream": "room1_user001"
}
```

**响应** (200):
```json
{
  "code": 0
}
```

---

### 36. 获取流状态
- **路径**: `/api/v1/streams/status`
- **方法**: GET
- **描述**: 获取翻译服务状态

**响应** (200):
```json
{
  "active_requests": 2,
  "processes": ["request_id_1", "request_id_2"]
}
```

---

## 十、WebSocket 接口

### 37. 订阅房间
- **路径**: `/api/v1/ws/subscribe`
- **方法**: POST
- **描述**: 获取 WebSocket 连接地址

**请求体**:
```json
{
  "room_id": "room_123",
  "user_id": "user_001"
}
```

**响应** (200):
```json
{
  "code": 0,
  "message": "success",
  "data": {
    "ws_url": "ws://localhost:8085/ws?room=room_123&user=user_001",
    "room_id": "room_123",
    "user_id": "user_001"
  }
}
```

---

### 38. WebSocket 状态
- **路径**: `/api/v1/ws/status`
- **方法**: GET
- **描述**: 获取 WebSocket 连接状态

**响应** (200):
```json
{
  "code": 0,
  "message": "success",
  "data": {
    "total_connections": 10,
    "active_rooms": 3,
    "ws_port": 8085
  }
}
```

---

### 39. 广播消息
- **路径**: `/api/v1/ws/broadcast`
- **方法**: POST
- **描述**: 通过 WebSocket 向房间广播消息

**请求体**:
```json
{
  "room_id": "room_123",
  "type": "notification",
  "data": {
    "message": "hello"
  }
}
```

**响应** (200):
```json
{
  "code": 0,
  "message": "broadcast sent"
}
```

---

### 40. 发送私人消息
- **路径**: `/api/v1/ws/send`
- **方法**: POST
- **描述**: 向指定用户发送私人消息

**请求体**:
```json
{
  "user_id": "user_001",
  "type": "notification",
  "data": {
    "message": "private message"
  }
}
```

**响应** (200):
```json
{
  "code": 0,
  "message": "message sent"
}
```

---

## 十一、系统接口

### 41. 健康检查
- **路径**: `/health`
- **方法**: GET
- **描述**: 服务健康检查

**响应** (200):
```json
{
  "status": "ok"
}
```

---

## WebSocket 消息类型

客户端通过 WebSocket 连接可接收以下类型的消息：

| 消息类型 | 说明 | 数据字段 |
|----------|------|----------|
| `connected` | 连接成功 | client_id, user_id |
| `subscribed` | 订阅成功 | room_id, type |
| `user_joined` | 用户加入 | room_id, user_id |
| `user_left` | 用户离开 | room_id, user_id |
| `user_speaking_start` | 用户开始说话 | room_id, user_id, stream_url |
| `user_speaking_stop` | 用户停止说话 | room_id, user_id |
| `muted` | 用户被禁言 | room_id, user_id, operator_id |
| `unmuted` | 用户被解除禁言 | room_id, user_id, operator_id |
| `kicked` | 用户被踢出 | room_id, user_id, operator_id |
| `knock` | 有人敲门 | room_id, knocker_id |
| `knock_accepted` | 敲门被接受 | room_id, knocker_id |
| `knock_rejected` | 敲门被拒绝 | room_id, knocker_id, reason |
| `translation_text` | 翻译文本 | room_id, source_user, original_text, translated_text |
| `translation_started` | 翻译开始 | room_id, source_user, to_lang |
| `translation_stopped` | 翻译停止 | room_id, source_user, to_lang |
| `error` | 错误消息 | message |

---

## 接口分类总结

| 分类 | 接口数量 | 主要用途 |
|------|----------|----------|
| 房间管理 | 4 | 创建、获取、删除房间 |
| 用户管理 | 4 | 加入、离开、获取成员、更新角色 |
| 禁言管理 | 7 | 禁言、禁麦、全体禁言、踢人 |
| 权限检查 | 1 | 检查发布权限 |
| 敲门管理 | 3 | 敲门、接受、拒绝 |
| 说话状态 | 1 | 获取正在说话的用户 |
| 翻译管理 | 8 | 翻译请求、心跳、拉流者管理 |
| 翻译文本 | 2 | 推送翻译文本和原音识别文本 |
| SRS回调 | 5 | 流发布/停止、播放/停止、状态 |
| WebSocket | 4 | 订阅、状态、广播、私信 |
| 系统 | 1 | 健康检查 |

**总计**: 40 个 API 接口 + WebSocket 实时消息
