# 主业务服务器接口文档（Server-to-Server 同步）

> 本文档供主业务服务器（`8.138.45.176:8085`）开发人员使用。
> 语音聊天室服务器（我们这边）会主动向此服务器推送房间/成员变更事件。
> 双方使用 **HMAC-SHA256** 签名验证身份。

---

## 1. 概述

### 1.1 角色

| 角色 | 地址 | 说明 |
|------|------|------|
| 语音聊天室服务器（主动方） | 本文档读者 | 监听房间事件，推送到主服务器 |
| 主业务服务器（被动方） | `8.138.45.176:8085` | 接收事件，存储/展示房间和成员信息 |

### 1.2 传输方式

- **协议**：HTTP / HTTPS
- **方向**：语音聊天室服务器 → 主业务服务器（POST/PUT/DELETE）
- **编码**：`UTF-8`，`Content-Type: application/json`

### 1.3 鉴权机制

每个请求必须携带三个 HTTP Header：

| Header | 说明 | 示例 |
|--------|------|------|
| `X-Server-ID` | 发送方身份 ID | `server_chat_001` |
| `X-Timestamp` | Unix 秒级时间戳 | `1752576800` |
| `X-Sign` | HMAC-SHA256 签名（见 §2） | `a3f8c...` |

**时间容差**：请求时间戳与服务器时间差须在 **±30 秒** 内，否则返回 `401 timestamp skew`。

---

## 2. 签名算法

### 2.1 签名公式

```
sign = HMAC-SHA256(
    key    = 双方约定的 shared_secret（由语音聊天室服务器提供）,
    data   = timestamp + "." + HTTP_METHOD + "." + URL_PATH + "." + BODY_JSON
)
hex 编码（小写）
```

### 2.2 注意事项

- `BODY_JSON` = 请求体（若没有 body 则为空字符串 `""`）
- `BODY_JSON` 必须与实际发送的 JSON 完全一致（不排序，不格式化）
- `URL_PATH` = **不含 Query String**，如 `/internal/room/r1/member`
- `HTTP_METHOD` 必须大写：`GET`、`POST`、`PUT`、`DELETE`

### 2.3 签名验证示例

#### Python（Flask / FastAPI / Django 通用）

```python
import hashlib
import hmac
import time

SHARED_SECRET = "changeme_change_this_secret_before_production"  # 联调时替换

def verify_signature(server_id: str, timestamp: str, sign: str,
                    method: str, path: str, body_json: str) -> bool:
    # 1. 时间戳校验（±30s）
    ts = int(timestamp)
    if abs(time.time() - ts) > 30:
        return False

    # 2. 签名
    raw = f"{ts}.{method.upper()}.{path}.{body_json}"
    expected = hmac.new(
        SHARED_SECRET.encode(),
        raw.encode(),
        hashlib.sha256,
    ).hexdigest()

    # 3. 防时序攻击比对
    return hmac.compare_digest(expected, sign)
```

#### Python + Flask 完整中间件

```python
from flask import Flask, request, jsonify, abort
import hashlib, hmac, time, json

SHARED_SECRET = "changeme_change_this_secret_before_production"
ALLOWED_SERVER_IDS = {"server_chat_001"}

app = Flask(__name__)

@app.before_request
def auth_middleware():
    if request.path.startswith("/internal"):
        sid  = request.headers.get("X-Server-ID", "")
        ts   = request.headers.get("X-Timestamp", "")
        sign = request.headers.get("X-Sign", "")

        if not all([sid, ts, sign]):
            abort(401, "Missing auth headers")

        if sid not in ALLOWED_SERVER_IDS:
            abort(403, "Unknown server_id")

        if abs(time.time() - int(ts)) > 30:
            abort(401, "timestamp skew too large")

        body = request.get_data(as_text=True) or ""
        raw = f"{ts}.{request.method.upper()}.{request.path}.{body}"
        expected = hmac.new(
            SHARED_SECRET.encode(), raw.encode(), hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(expected, sign):
            abort(401, "signature mismatch")

        # 请求通过，存入 request 供后续 handler 用
        request.server_id = sid
```

#### curl 联调测试

```bash
SECRET="changeme_change_this_secret_before_production"
TS=$(date +%s)

# === POST /internal/room ===
BODY='{"room_id":"r1","owner_id":"user_001","name":"测试房间","max_members":100}'
PATH="/internal/room"
METHOD="POST"
SIGN=$(echo -n "${TS}.${METHOD}.${PATH}.${BODY}" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $2}')
curl -X POST "http://localhost:8085${PATH}" \
  -H "Content-Type: application/json" \
  -H "X-Server-ID: server_chat_001" \
  -H "X-Timestamp: ${TS}" \
  -H "X-Sign: ${SIGN}" \
  -d "${BODY}"

# === GET /internal/ping ===
BODY=""
PATH="/internal/ping"
METHOD="GET"
SIGN=$(echo -n "${TS}.${METHOD}.${PATH}.${BODY}" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $2}')
curl "http://localhost:8085${PATH}" \
  -H "X-Server-ID: server_chat_001" \
  -H "X-Timestamp: ${TS}" \
  -H "X-Sign: ${SIGN}"
```

---

## 3. 接口列表

### 3.1 心跳（调试用）

```
GET /internal/ping
```

**请求 Header**：需要鉴权（见 §2）

**响应 200**：

```json
{ "status": "ok", "server_id": "server_chat_001" }
```

**用途**：主服务器确认语音聊天室服务器存活。

---

### 3.2 房间创建

```
POST /internal/room
```

**请求 Header**：需要鉴权

**请求体**：

```json
{
  "event": "room_created",
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

**响应 200 / 201**：

```json
{ "code": 0, "message": "ok" }
```

**说明**：`room_id` 全局唯一（语音聊天室服务器保证）。

---

### 3.3 房间删除

```
DELETE /internal/room/{room_id}
```

**请求 Header**：需要鉴权

**Query 参数**（可选）：

| 参数 | 说明 |
|------|------|
| `deleted_by` | 操作者 user_id |

**请求体**（可选）：

```json
{
  "event": "room_deleted",
  "server_id": "server_chat_001",
  "timestamp": 1752576900,
  "room_id": "room_abc",
  "data": {
    "room_id": "room_abc",
    "deleted_by": "user_001"
  }
}
```

**响应 200 / 204**：无 body 或 `{ "code": 0 }`

---

### 3.4 成员加入

```
POST /internal/room/{room_id}/member
```

**请求 Header**：需要鉴权

**请求体**：

```json
{
  "event": "member_joined",
  "server_id": "server_chat_001",
  "timestamp": 1752576850,
  "room_id": "room_abc",
  "data": {
    "user_id": "user_002",
    "role": "member",
    "joined_at": "2026-07-15 13:30:00",
    "last_active": ""
  }
}
```

**字段说明**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `user_id` | string | 用户的稳定身份 ID，格式 `user_<hex12>` |
| `role` | string | 角色：`owner` / `admin` / `member` / `guest` |
| `joined_at` | string | 加入时间，格式 `YYYY-MM-DD HH:MM:SS`，可为空 |
| `last_active` | string | 最后活跃时间，可为空 |

**响应 200 / 201**：`{ "code": 0 }`

---

### 3.5 成员离开

```
DELETE /internal/room/{room_id}/member/{user_id}
```

**请求 Header**：需要鉴权

**Query 参数**（可选）：

| 参数 | 说明 |
|------|------|
| `reason` | 离开原因：`left`（主动离开）/ `kicked`（被踢） |

**请求体**（可选）：

```json
{
  "event": "member_left",
  "server_id": "server_chat_001",
  "timestamp": 1752576880,
  "room_id": "room_abc",
  "data": {
    "user_id": "user_002",
    "reason": "left"
  }
}
```

**响应 200 / 204**：无 body 或 `{ "code": 0 }`

---

### 3.6 成员角色变更

```
PUT /internal/room/{room_id}/member/{user_id}
```

**请求 Header**：需要鉴权

**请求体**：

```json
{
  "event": "member_role_changed",
  "server_id": "server_chat_001",
  "timestamp": 1752576900,
  "room_id": "room_abc",
  "data": {
    "user_id": "user_002",
    "old_role": "member",
    "new_role": "admin",
    "operator_id": "user_001"
  }
}
```

**响应 200**：`{ "code": 0 }`

---

### 3.7 房间全体禁言状态变更

```
PUT /internal/room/{room_id}
```

**请求 Header**：需要鉴权

**请求体**：

```json
{
  "event": "room_mute_changed",
  "server_id": "server_chat_001",
  "timestamp": 1752576920,
  "room_id": "room_abc",
  "data": {
    "room_id": "room_abc",
    "allow_speak": false,
    "operator_id": "user_001"
  }
}
```

**字段说明**：

| 字段 | 说明 |
|------|------|
| `allow_speak` | `false` = 全员禁言；`true` = 解除全员禁言 |

**响应 200**：`{ "code": 0 }`

---

### 3.8 全量房间同步（定时推送）

```
POST /internal/rooms/sync
```

**请求 Header**：需要鉴权

**说明**：语音聊天室服务器每 **30 秒** 主动推送一次所有房间的完整状态。用于主服务器在断线重连后快速恢复数据，以及对账。

**请求体**：

```json
{
  "server_id": "server_chat_001",
  "timestamp": 1752576950,
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
        },
        {
          "user_id": "user_003",
          "role": "admin",
          "status": "muted",
          "publish_allowed": false,
          "joined_at": "2026-07-15 13:10:00"
        }
      ],
      "allow_speak": true
    }
  ]
}
```

**字段说明**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `member_count` | int | 成员数量 |
| `members[].status` | string | `normal` / `muted` |
| `members[].publish_allowed` | bool | 是否允许推流（被禁言/禁麦时为 `false`） |
| `allow_speak` | bool | 房间全员禁言状态 |

**响应 200 / 201**：`{ "code": 0 }`

---

## 4. 事件触发时机

| 事件 | 触发时机 |
|------|----------|
| `room_created` | 用户创建房间 |
| `room_deleted` | 群主删除房间 |
| `member_joined` | 用户主动加入，或被接受敲门 |
| `member_left` | 用户主动离开房间 |
| `member_kicked` | 用户被群主/管理员踢出（reason=kicked） |
| `member_role_changed` | 群主变更用户角色（admin/member/guest） |
| `room_mute_changed` | 全员禁言/解除全员禁言 |

**注意**：
- 成员被踢（`member_kicked`）走的是 `DELETE /internal/room/{room_id}/member/{user_id}` 接口，`reason` 字段为 `"kicked"`
- 禁言/禁麦单个成员（`member_muted`）**不推送**到主服务器（仅影响推流权限，不影响房间成员列表）

---

## 5. 错误处理

### 5.1 主服务器返回的错误

| HTTP 状态码 | 含义 | 处理建议 |
|-------------|------|----------|
| 200 / 201 / 204 | 成功 | 无需处理 |
| 400 | 参数错误 | 检查请求体 JSON 格式 |
| 401 | 签名失败 / timestamp skew | 确认 shared_secret 一致，时间同步 |
| 403 | 未知 server_id | 确认 X-Server-ID 值 |
| 404 | 资源不存在 | 如 DELETE 已删除的房间，正常忽略 |
| 409 | 资源冲突 | 如创建已存在的房间（主服务器可幂等处理） |
| 5xx | 服务端错误 | 语音聊天室服务器会自动重试（最多 3 次，指数退避） |

### 5.2 防抖说明

语音聊天室服务器内部有 **100ms 防抖**：同房间同类型事件（如连续多次 `member_joined`）在 100ms 内只推送**最后一次**。

---

## 6. 数据一致性建议

### 6.1 接收策略

- **幂等处理**：同一个 `event` + `room_id` + `timestamp` 的请求可能重复，服务器应能幂等处理（用 timestamp 或唯一事件 ID 做幂等键）
- **以主服务器为准**：语音聊天室服务器定时推送全量状态（`/internal/rooms/sync`），主服务器可在收到后**整体覆盖**本地缓存，保证最终一致

### 6.2 建议存储结构（SQL）

```sql
CREATE TABLE chat_rooms (
    room_id      VARCHAR(64) PRIMARY KEY,
    server_id   VARCHAR(32) NOT NULL,       -- 来源服务器 ID
    owner_id    VARCHAR(64) NOT NULL,
    name        VARCHAR(128),
    max_members INT DEFAULT 100,
    allow_speak BOOLEAN DEFAULT TRUE,
    created_at  DATETIME,
    updated_at  DATETIME
);

CREATE TABLE chat_room_members (
    room_id    VARCHAR(64),
    user_id    VARCHAR(64),
    role       VARCHAR(16),   -- owner/admin/member/guest
    status     VARCHAR(16),   -- normal/muted
    publish_allowed BOOLEAN DEFAULT TRUE,
    joined_at  DATETIME,
    PRIMARY KEY (room_id, user_id)
);
```

### 6.3 断线重连

语音聊天室服务器重启或网络抖动后，会立即推送一次完整的 `/internal/rooms/sync`。主服务器收到后应整体刷新本地缓存。

---

## 7. 联调清单

| # | 检查项 | 预期结果 |
|---|--------|----------|
| 1 | `GET /internal/ping` 带正确签名 | 200 + `{ "status": "ok" }` |
| 2 | `POST /internal/room` 创建房间 | 200/201，`room_id` 入库 |
| 3 | `POST /internal/room/{id}/member` 添加成员 | 200/201，成员入库 |
| 4 | `DELETE /internal/room/{id}/member/{uid}` 删除成员 | 200/204 |
| 5 | 故意改签名/secret | 401 Unauthorized |
| 6 | 故意改 timestamp 超 ±30s | 401 timestamp skew |
| 7 | `POST /internal/rooms/sync` 全量同步 | 收到所有房间 + 成员列表 |

---

## 8. 变更记录

| 版本 | 日期 | 变更 |
|------|------|------|
| v1 | 2026-07-15 | 初始版本：房间 CRUD、成员加入/离开/踢出、角色变更、全员禁言、全量同步、心跳 |
