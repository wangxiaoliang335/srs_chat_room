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

## 十、WebSocket / Socket.IO 实时对接说明

本章描述当前代码中真实可用的实时对接方式。当前服务同时支持：

1. **Socket.IO 房间订阅与事件广播**（推荐客户端优先使用）
2. **原生 WebSocket 房间订阅与事件广播**（兼容方案）
3. **服务端内部/管理用途的 HTTP 广播接口**（通常不是业务客户端直接调用）

### 37. 获取当前正在说话的用户列表
- **路径**: `/api/v1/room/<room_id>/speaking`
- **方法**: GET
- **描述**: 获取房间内当前处于“说话中”状态的用户列表。该状态基于服务端收到的推流开始/停止回调维护，不是逐帧静音检测（VAD）。

**路径参数**:
- `room_id`: 房间 ID

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

## 10.1 Socket.IO 实时接口（推荐）

### 38. Socket.IO 连接
- **连接地址**: `http://<server-host>:8085`
- **说明**: 客户端建立 Socket.IO 连接后，再通过 `subscribe` 事件加入指定房间。

### 39. Socket.IO 房间订阅
- **事件名**: `subscribe`
- **描述**: 客户端连接 Socket.IO 后，发送该事件订阅指定房间的广播事件。

**请求体**:
```json
{
  "room_id": "room_123",
  "user_id": "user_001"
}
```

**成功响应事件**: `subscribed`
```json
{
  "type": "subscribed",
  "room_id": "room_123",
  "user_id": "user_001"
}
```

**错误响应事件**: `error`
```json
{
  "message": "Missing room_id"
}
```

### 40. Socket.IO 取消订阅
- **事件名**: `unsubscribe`
- **描述**: 客户端取消订阅房间广播。

**请求体**:
```json
{
  "room_id": "room_123"
}
```

**成功响应事件**: `unsubscribed`
```json
{
  "type": "unsubscribed",
  "room_id": "room_123"
}
```

### 41. Socket.IO 心跳
- **事件名**: `ping`
- **描述**: 客户端主动发送心跳，服务端返回 `pong`。

**请求体**:
```json
{}
```

**成功响应事件**: `pong`
```json
{
  "type": "pong"
}
```

### 42. Socket.IO 房间广播事件
- **监听事件名**: `room_event`
- **描述**: 客户端订阅房间成功后，会在该事件中收到房间内的各种广播通知，包括成员变更、翻译状态、说话状态等。

#### 42.1 消息封装格式
当前 Socket.IO 广播消息统一为以下结构：

```json
{
  "type": "<event_type>",
  "data": {
    "event_id": "a1b2c3d4",
    "room_id": "room_123",
    "user_id": "user_001",
    "operator_id": "",
    "target_user_id": "",
    "data": {},
    "timestamp": "2026-06-07 10:10:10",
    "type": "<event_type>"
  }
}
```

说明：
- 外层 `type` 是事件类型，便于客户端快速分发。
- 内层 `data.type` 与外层 `type` 一致。
- 内层 `data.data` 为该事件的业务数据。

#### 42.2 公共字段说明
无论是 Socket.IO 的 `room_event`，还是原生 WebSocket 的平铺事件，房间事件主体字段含义基本一致：

| 字段 | 类型 | 说明 |
|------|------|------|
| `event_id` | string | 事件唯一标识，用于客户端去重或排查日志 |
| `room_id` | string | 房间 ID |
| `user_id` | string | 事件关联的主要用户 ID。对说话事件来说，就是开始/停止说话的用户 |
| `operator_id` | string | 操作者用户 ID。只有管理员操作类事件通常会有值 |
| `target_user_id` | string | 事件关联的目标用户 ID。定向事件中可能使用 |
| `data` | object | 事件业务字段，不同事件内容不同 |
| `timestamp` | string | 服务端生成的事件时间，格式 `YYYY-MM-DD HH:MM:SS` |
| `type` | string | 事件类型 |

对于 Socket.IO：
- 外层 `type`：事件类型，便于客户端分发
- 外层 `data`：上表这些公共字段的集合
- 真正业务字段位于 `data.data`

#### 42.3 用户开始说话 `user_speaking_start`
说明：当服务端收到某用户音频流开始发布时，向房间内订阅者广播。该事件语义是“开始推流”，不是逐帧 VAD 检测。

**消息示例**:
```json
{
  "type": "user_speaking_start",
  "data": {
    "event_id": "a1b2c3d4",
    "room_id": "room_123",
    "user_id": "user_001",
    "operator_id": "",
    "target_user_id": "",
    "data": {
      "stream_url": "http://<srs-host>/live/room_123_user_001"
    },
    "timestamp": "2026-06-07 10:10:10",
    "type": "user_speaking_start"
  }
}
```

#### 42.4 用户停止说话 `user_speaking_stop`
说明：当服务端收到某用户音频流停止发布时，向房间内订阅者广播。

**消息示例**:
```json
{
  "type": "user_speaking_stop",
  "data": {
    "event_id": "e5f6g7h8",
    "room_id": "room_123",
    "user_id": "user_001",
    "operator_id": "",
    "target_user_id": "",
    "data": {},
    "timestamp": "2026-06-07 10:12:30",
    "type": "user_speaking_stop"
  }
}
```

#### 42.5 其他常见房间广播事件
以下事件类型也会通过 `room_event` 下发，结构同上：

- `member_joined`
- `member_left`
- `member_kicked`
- `member_role_changed`
- `member_muted`
- `member_unmuted`
- `member_mic_disabled`
- `member_mic_enabled`
- `room_muted_all`
- `room_unmuted_all`
- `room_created`
- `room_deleted`
- `room_knock`
- `room_knock_accepted`
- `room_knock_rejected`
- `translation_started`
- `translation_stopped`
- `original_speech_text`

说明：
- `translation_text` 除了可能广播到房间外，还会定向发送给目标用户。
- 不同事件的业务字段位于内层 `data.data` 中。

---

## 10.2 原生 WebSocket 实时接口（兼容）

### 43. 原生 WebSocket 连接
- **连接地址**: `ws://<server-host>:8086/ws?room=<room_id>&user=<user_id>`
- **描述**: 原生 WebSocket 接入方式，适用于不使用 Socket.IO 的客户端。

**连接成功后服务端主动发送**:
```json
{
  "type": "connected",
  "room_id": "room_123",
  "user_id": "user_001",
  "room_info": {
    "online_users": 2,
    "translation_status": "idle"
  }
}
```

### 44. 原生 WebSocket 心跳
- **消息类型**: `ping`
- **描述**: 客户端发送心跳，服务端返回 `pong`。

**客户端发送**:
```json
{
  "type": "ping"
}
```

**服务端返回**:
```json
{
  "type": "pong"
}
```

### 45. 原生 WebSocket 订阅/切换房间
- **消息类型**: `subscribe`
- **描述**: 已连接客户端切换到新的房间并接收该房间广播。

**客户端发送**:
```json
{
  "type": "subscribe",
  "room_id": "room_456"
}
```

**服务端返回**:
```json
{
  "type": "subscribed",
  "room_id": "room_456"
}
```

### 46. 原生 WebSocket 房间广播事件
- **描述**: 原生 WebSocket 收到的房间广播为平铺 JSON 结构，不像 Socket.IO 那样额外包一层 `data`。

#### 46.1 消息封装格式
```json
{
  "event_id": "a1b2c3d4",
  "room_id": "room_123",
  "user_id": "user_001",
  "operator_id": "",
  "target_user_id": "",
  "data": {},
  "timestamp": "2026-06-07 10:10:10",
  "type": "<event_type>"
}
```

#### 46.2 用户开始说话 `user_speaking_start`
```json
{
  "event_id": "a1b2c3d4",
  "room_id": "room_123",
  "user_id": "user_001",
  "operator_id": "",
  "target_user_id": "",
  "data": {
    "stream_url": "http://<srs-host>/live/room_123_user_001"
  },
  "timestamp": "2026-06-07 10:10:10",
  "type": "user_speaking_start"
}
```

#### 46.3 用户停止说话 `user_speaking_stop`
```json
{
  "event_id": "e5f6g7h8",
  "room_id": "room_123",
  "user_id": "user_001",
  "operator_id": "",
  "target_user_id": "",
  "data": {},
  "timestamp": "2026-06-07 10:12:30",
  "type": "user_speaking_stop"
}
```

---

## 10.3 服务端内部/管理用途 HTTP 推送接口

以下接口在当前实现中存在，但更适合作为服务端内部调用或管理用途，不建议普通业务客户端把它当成主要实时接入方式。

### 47. 原生 WebSocket 广播 HTTP 接口
- **路径**: `/broadcast`
- **方法**: POST
- **描述**: 向指定房间的原生 WebSocket 连接广播一段已序列化消息。

**请求体**:
```json
{
  "room_id": "room_123",
  "message": "{\"type\":\"custom_event\",\"data\":{}}"
}
```

**响应** (200):
```json
{
  "status": "ok",
  "sent": true
}
```

### 48. 原生 WebSocket 发送/广播兼容接口
- **路径**: `/ws/send`
- **方法**: POST
- **描述**: 向指定用户发送消息，或在 `user_id` 为空时广播到指定房间。该接口主要用于兼容旧逻辑。

**请求体**:
```json
{
  "room_id": "room_123",
  "user_id": "user_001",
  "type": "notification",
  "data": {
    "message": "hello"
  }
}
```

说明：
- `room_id` 必填
- `user_id` 非空时，优先发送给指定用户
- `user_id` 为空时，广播到 `room_id`

**响应** (200):
```json
{
  "status": "ok"
}
```

---

## 十一、系统接口

### 49. 健康检查
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

## WebSocket / Socket.IO 消息类型

以下为当前实时通道中常见且真实存在的消息类型。不同接入方式的消息封装略有不同：

- **Socket.IO**: 主要监听 `room_event`，外层结构通常为 `{ "type": "<event_type>", "data": {...} }`
- **原生 WebSocket**: 直接接收平铺结构消息，通常为 `{ "type": "<event_type>", ... }`

### 1. 连接与控制类消息

| 消息类型 | 适用通道 | 说明 | 主要字段 |
|----------|----------|------|----------|
| `connected` | 原生 WebSocket | 连接建立后服务端主动发送 | `room_id`, `user_id`, `room_info` |
| `subscribed` | Socket.IO / 原生 WebSocket | 订阅房间成功 | `room_id`, `user_id`（Socket.IO） |
| `unsubscribed` | Socket.IO | 取消订阅成功 | `room_id` |
| `pong` | Socket.IO / 原生 WebSocket | 心跳响应 | `type` |
| `error` | Socket.IO | 订阅参数错误等异常提示 | `message` |

### 2. 房间事件类消息

以下事件会广播给房间内订阅者：

| 消息类型 | 说明 | 主要业务字段位置 |
|----------|------|------------------|
| `member_joined` | 用户加入房间 | `data.data`（Socket.IO） / `data`（原生 WS） |
| `member_left` | 用户离开房间 | 同上 |
| `member_kicked` | 用户被踢出 | 同上 |
| `member_role_changed` | 用户角色变更 | 同上 |
| `member_muted` | 用户被禁言 | 同上 |
| `member_unmuted` | 用户被解除禁言 | 同上 |
| `member_mic_disabled` | 用户被禁麦 | 同上 |
| `member_mic_enabled` | 用户被解除禁麦 | 同上 |
| `room_muted_all` | 全体禁言 | 同上 |
| `room_unmuted_all` | 解除全体禁言 | 同上 |
| `room_created` | 房间创建 | 同上 |
| `room_deleted` | 房间删除 | 同上 |
| `room_knock` | 有人敲门 | 同上 |
| `room_knock_accepted` | 敲门被接受 | 同上 |
| `room_knock_rejected` | 敲门被拒绝 | 同上 |
| `user_speaking_start` | 用户开始说话（开始推流） | 同上 |
| `user_speaking_stop` | 用户停止说话（停止推流） | 同上 |
| `translation_started` | 翻译开始 | 同上 |
| `translation_stopped` | 翻译停止 | 同上 |
| `original_speech_text` | 原语音识别文字 | 同上 |

### 3. 翻译文本消息

| 消息类型 | 说明 | 备注 |
|----------|------|------|
| `translation_text` | 翻译文本消息 | 当前实现中既可能定向发送给目标用户，也可能广播到房间，客户端应按 `type` 分发处理 |

### 4. 对接注意事项

1. `user_speaking_start` / `user_speaking_stop` 的语义是**推流开始 / 推流停止**，不是逐帧 VAD。
2. Socket.IO 下建议客户端统一监听 `room_event`，再根据外层 `type` 进行分发。
3. 原生 WebSocket 下建议直接根据收到消息的 `type` 字段分发。
4. 如果客户端需要初始化当前说话状态，请先调用 `GET /api/v1/room/<room_id>/speaking`，再开始监听实时事件。

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
| 实时通信 | 12 | Socket.IO、原生 WebSocket、内部广播接口 |
| 系统 | 1 | 健康检查 |

**总计**: 49 个 HTTP 接口/说明项 + WebSocket / Socket.IO 实时消息
