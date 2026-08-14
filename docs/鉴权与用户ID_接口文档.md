# 鉴权与用户ID 接口文档

本文档重点说明 **4 种用户 ID** 的区别、来源、用法，以及涉及的核心 HTTP 接口。客户端必须按本文档的 ID 流转规则使用，否则会出现 `user not found` 等问题。

---

## 一、4 种用户 ID 速查表

| ID | 示例值 | 来源 | 生命周期 | 用途 |
|---|---|---|---|---|
| `principalId` | `990034` | App 业务后台签发的 `external_token` payload | 业务后台账号体系内稳定 | 仅作为**业务端本地用户标识**的参考 |
| `bus_id` | `790019` | 业务后台用户表的 `id` 字段 | 业务后台账号体系内稳定 | resolve 时用 `bus:<bus_id>` 查 chat_user_id |
| `user_id`（即 chat_user_id） | `user_d004625859f8` | 聊天服务端首次登录时自动生成（`user_` + 12 位 hex） | 业务用户在同一 app 内**永久唯一** | 聊天系统内部**唯一标识**，所有接口都用它 |
| `username`（name） | `Nuo` | 登录时传入或业务后台返回的 `nickname` / `username` | 可变（可由客户端更新） | 显示名 / 房间内展示 |

### 数据流关系

```
业务后台
    └── 用户表 id = 790019 ─────────────────┐
                                            ▼
业务后台签发 external_token                聊天服务端 user_store
    └── payload.principalId = 990034  ──→   (app_id, bus_id) → user_id
                                            （首次登录建立映射，永不变）
                                            ↓
App 本地存什么？                          user_id = user_d004625859f8
    ├── principalId = 990034（业务后台 token 里的）
    └── bus_id     = 790019（login 响应返回的，应同步更新）
```

**核心约束**：

1. **App 端的业务用户标识必须用 `bus_id`**（不是 `principalId`）。
2. `bus_id` 来源是 `/api/v1/auth/login` 响应里 `data.bus_id`。
3. 客户端登录成功后，应把 login 返回的 `bus_id` **写入本地存储**，覆盖（替换）原先存的 `principalId` 之类的旧值。
4. resolve 自己时，发送 `bus:<bus_id>`；后续调其他接口统一用 `user_id`。

---

## 二、ID 格式规范

### 2.1 `bus:<bus_id>` 业务用户ID

```
格式：  bus:<app_id>:<bus_id>     多 app 显式形式
       bus:<bus_id>              单 app，省略 app_id（用请求头 / JWT 的 app 兜底）
示例：  bus:790019
       bus:myapp:790019
字符集：app_id 与 bus_id 仅允许 [A-Za-z0-9_-]
app_id 缺省值：JWT 的 app 声明 → 请求头 X-App-Id → "default"
```

**注意**：这里的 `<bus_id>` 必须来自 `/api/v1/auth/login` 响应里的 `data.bus_id`，**不能**用 `external_token` payload 里的 `principalId`。

### 2.2 `chat:<chat_user_id>` 聊天用户ID

```
格式：  chat:<user_id>          带前缀
       <user_id>               存量格式（等价 chat:）
示例：  chat:user_d004625859f8
       user_d004625859f8
字符集：^user_[A-Za-z0-9]{12}$（user_ 前缀 + 12 位字母/数字）
```

`chat:` 前缀的元素会被直接透传，不查表（因为本身就是 chat_user_id）。

---

## 三、核心接口

### 3.1 登录 `POST /api/v1/auth/login`

业务后端给客户端签发的 `external_token`，客户端传给聊天服务端换取自己的 `chat_token` + `user_id` + `bus_id`。

**请求**

```
POST /api/v1/auth/login
Content-Type: application/json

{
    "external_token": "<业务后台签发的 token>",
    "user_name": "Nuo",
    "app_id": "default"            // 可选
}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `external_token` | string | 是 | 业务后台签发的 token，聊天服务端会拿去业务后台验签 |
| `user_name` | string | 否 | 客户端指定的显示名；不传则按业务后端返回的 nickname 兜底 |
| `app_id` | string | 否 | 多 app 隔离用，不传则按 JWT/请求头或 "default" |
| `username` + `password` | string | 否 | 内部账号模式（兼容旧业务，本文档不展开） |

**响应**

```json
{
    "code": 0,
    "message": "success",
    "data": {
        "username": "Nuo",
        "name": "Nuo",
        "user_id": "user_d004625859f8",
        "app_id": "default",
        "bus_id": "790019",
        "room_id": "alove_room_1785832247641",
        "role": "member",
        "token": "<chat_jwt>",
        "expires_at": 1786511577,
        "expires_in": 604800
    }
}
```

**关键字段**：

| 字段 | 用途 |
|---|---|
| `user_id` | 后续所有接口统一用这个 ID（带 `user_` 前缀） |
| `bus_id` | **必须**保存到本地，后续 resolve 自己用 `bus:<bus_id>` |
| `token` | `Authorization: Bearer <token>` 用于所有受保护接口 |
| `app_id` | 多 app 隔离标识 |

**客户端必须做的事**：

```text
登录成功 → 把 data.bus_id 写入本地存储（覆盖旧值）
         → 把 data.user_id 写入本地存储
         → 把 data.token 作为后续请求的 Authorization 头
```

---

### 3.2 解析用户ID `POST /api/v1/users/resolve`

把业务ID（`bus:`）或聊天ID（`chat:`）批量解析成统一的 `chat_user_id`。

**请求**

```
POST /api/v1/users/resolve
Authorization: Bearer <chat_token>
Content-Type: application/json

{
    "ids": [
        "bus:790019",                          // 自己（用 login 响应的 bus_id）
        "bus:myapp:790020",                    // 别人（多 app 显式）
        "chat:user_d004625859f8",              // 已知的 chat_user_id
        "user_d004625859f8"                    // 存量格式
    ]
}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `ids` | string[] | 是 | ≤100 个 ID，元素支持 `bus:` / `chat:` / `user_xxx` |

**响应**

```json
{
    "code": 0,
    "message": "success",
    "data": {
        "resolved": [
            {"input": "bus:790019",            "chat_user_id": "user_d004625859f8", "username": "Nuo"},
            {"input": "bus:myapp:790020",      "chat_user_id": "user_aaaa1111bbbb", "username": "Alice"},
            {"input": "chat:user_d004625859f8","chat_user_id": "user_d004625859f8", "username": "Nuo"},
            {"input": "user_d004625859f8",     "chat_user_id": "user_d004625859f8", "username": "Nuo"}
        ],
        "unresolved": [
            {"input": "bus:990034", "reason": "user not found"}
        ]
    }
}
```

**关键字段**：

| 字段 | 含义 |
|---|---|
| `resolved[].input` | 客户端传入的原始 ID 字符串 |
| `resolved[].chat_user_id` | 服务端统一后的 chat_user_id |
| `resolved[].username` | 该用户的显示名 |
| `unresolved[].input` | 解析失败的原始输入 |
| `unresolved[].reason` | 失败原因（`user not found` / `user_id 格式非法`） |

**ID 解析规则**：

| 输入格式 | 解析行为 |
|---|---|
| `chat:<user_id>` | 校验格式后直接透传为 chat_user_id |
| `user_<12位hex>` | 视为 chat_user_id，校验格式后直接使用 |
| `bus:<app>:<bus_id>` | 按 `(app_id, bus_id)` 查表 → 返回 chat_user_id |
| `bus:<bus_id>` | 用 `default_app_id` 兜底，查表 → 返回 chat_user_id |
| 其他 | 加入 `unresolved`，reason = `user_id 格式非法` |

**`default_app_id` 来源优先级**：JWT claim `app` → 请求头 `X-App-Id` → `"default"`。

**典型用法**：

1. 客户端启动时，**先 resolve 自己**：`["bus:<本地存的 bus_id>"]`，确认能查到再继续。
2. 拿到自己/好友的 chat_user_id 后，缓存到本地，后续用 `user_id` 调其他接口。
3. `unresolved` 里的 ID 表示当前服务端不认识这个业务用户（要么没登录过，要么 `bus_id` 不对）。

---

### 3.3 房间健康检查 `GET /api/v1/rooms/{room_id}/health`

查询房间存活状态、房主、成员数。

**请求**

```
GET /api/v1/rooms/alove_room_1785832247641/health
Authorization: Bearer <chat_token>
```

**响应（成功）**

```json
{
    "code": 0,
    "message": "success",
    "data": {
        "room_id": "alove_room_1785832247641",
        "status": "active",
        "owner_id": "user_d004625859f8",
        "owner_status": "online",
        "member_count": 1
    }
}
```

**响应（房间不存在 / 已关闭）**

```json
{
    "code": 404,
    "message": "room not found or closed",
    "data": null
}
```

**字段说明**：

| 字段 | 含义 |
|---|---|
| `room_id` | 房间ID |
| `status` | `active` / `closed` |
| `owner_id` | 房主的 chat_user_id |
| `owner_status` | 房主在线状态 `online` / `offline` |
| `member_count` | 当前房间成员数 |

---

## 四、典型客户端流程

### 4.1 启动后 resolve 自己（先判定是否能查到）

```
1. 从本地读 bus_id（首次为空）
2. 调用 POST /api/v1/users/resolve
       ids = ["bus:<本地 bus_id>"]
3. 命中 resolved → 取出 chat_user_id，缓存到本地 → 进入房间
4. 未命中（unresolved）→ 调 /auth/login 刷新 token 与状态
5. login 返回后 → 用 data.bus_id 覆盖本地存的旧 bus_id
6. 再调一次 /api/v1/users/resolve，传入新的 bus:<bus_id>
7. 命中 → 进入房间
```

### 4.2 错误的做法（导致 user not found）

```
1. localStorage.principalId = 990034     ← 用 principalId 当 bus_id
2. POST /api/v1/users/resolve
       ids = ["bus:990034"]              ← 服务端存的是 790019
3. → unresolved: [{"input":"bus:990034","reason":"user not found"}]
```

**根因**：服务端存的是业务后台 `id` 字段（`790019`），不是 `external_token` payload 里的 `principalId`（`990034`）。

### 4.3 正确的做法

```
1. App 启动 → 拿 external_token
2. POST /api/v1/auth/login
       → 拿回 {bus_id: "790019", user_id: "user_d004625859f8", token: ...}
3. 本地保存：
       bus_id    = "790019"
       user_id   = "user_d004625859f8"
       token     = "..."
4. POST /api/v1/users/resolve
       ids = ["bus:790019"]
       → resolved: [{input:"bus:790019", chat_user_id:"user_d004625859f8", username:"Nuo"}]
5. 拿 chat_user_id 去进入房间 / 调其他接口
```

---

## 五、错误码

| code | 含义 |
|---|---|
| 0 | 成功 |
| 400 | 参数错误（如 ids 超过 100 个、格式非法） |
| 401 | 未登录 / token 无效 |
| 404 | 资源不存在（房间、用户） |
| 500 | 服务端内部错误 |
| 503 | 业务后端不可用 |

`/auth/login` 还可能返回：

| code | 含义 |
|---|---|
| 401 | external_token 业务后台验证失败 |
| 500 | 业务后端返回的数据缺身份字段 |

`/users/resolve` 的 `unresolved[].reason` 取值：

| reason | 含义 |
|---|---|
| `user not found` | 该 `bus_id` 在服务端没有对应记录（用户没登录过，或 `bus_id` 不对） |
| `user_id 格式非法` | 字符串格式不符合 `bus:` / `chat:` / `user_xxx` 规范 |

---

## 六、常见问题

**Q1：`principalId` 和 `bus_id` 是什么关系？**

都是业务后台账号体系内的用户标识，但**来源不同**：
- `principalId` 在 `external_token` payload 里（业务后台的 token 中间件可能有自己的命名）。
- `bus_id` 是聊天服务端从业务后台 `GET /profile` 接口返回的用户 `id` 字段取的，是聊天服务端认定的"业务ID"。

聊天服务端只认 `bus_id`（=业务后台 `id`），不认 `principalId`。客户端必须用 login 响应里的 `bus_id` 去 resolve。

**Q2：同一个业务用户多次登录，`user_id` 会变吗？**

不会。聊天服务端按 `(app_id, bus_id)` 联合主键定位，同一业务用户在同一 app 下始终映射到同一个 `user_id`。

**Q3：为什么需要 resolve，不直接用业务ID？**

为了**解耦**：聊天系统内部统一用 `user_id`（带 `user_` 前缀），业务方只关心 `bus_id`。resolve 起到**适配层**的作用，业务系统升级不影响 chat 系统内部 ID。

**Q4：resolve 失败后调 login，再 resolve 还是失败？**

检查 login 响应里的 `bus_id` 是不是和你本地存的 `principalId` 一致。客户端必须用 `data.bus_id` 去 resolve，**不能继续用 `principalId`**。

---

## 七、用户与成员类接口（基于 chat_user_id）

以下所有接口中的 `user_id` 都必须用 **chat_user_id**（带 `user_` 前缀），不能用 `bus_id` 或 `principalId`。

### 7.1 查询当前登录用户 `GET /api/v1/auth/me`

**请求**

```
GET /api/v1/auth/me
Authorization: Bearer <chat_token>
```

**响应**

```json
{
    "code": 0,
    "message": "success",
    "data": {
        "user_id": "user_d004625859f8",
        "username": "Nuo",
        "app_id": "default",
        "bus_id": "790019",
        "room_id": "alove_room_1785832247641",
        "role": "member",
        "expires_at": 1786511577
    }
}
```

**用途**：登录态检测 / 刷新本地缓存。`data.user_id` 是当前登录的 chat_user_id，`data.bus_id` 是该用户的业务ID。

---

### 7.2 按 user_id 查询用户名 `GET /api/v1/users/{user_id}/name`

**请求**

```
GET /api/v1/users/user_d004625859f8/name
Authorization: Bearer <chat_token>
```

**响应**

```json
{
    "code": 0,
    "message": "success",
    "data": {
        "user_id": "user_d004625859f8",
        "username": "Nuo",
        "avatar": null
    }
}
```

**注意**：
- `user_id` 必须符合 `^user_[A-Za-z0-9]{12}$`，否则返回 `400 user_id 格式非法`。
- 不存在返回 `404 user not found`。

---

### 7.3 批量查询用户名 `POST /api/v1/users/names`

房间成员列表渲染用。

**请求**

```
POST /api/v1/users/names
Authorization: Bearer <chat_token>
Content-Type: application/json

{
    "user_ids": [
        "user_d004625859f8",
        "user_aaaa1111bbbb"
    ]
}
```

| 字段 | 类型 | 限制 |
|---|---|---|
| `user_ids` | string[] | ≤100，每个必须是 `user_xxx` 格式 |

**响应**

```json
{
    "code": 0,
    "message": "success",
    "data": {
        "users": [
            {"user_id": "user_d004625859f8", "username": "Nuo", "avatar": null},
            {"user_id": "user_aaaa1111bbbb", "username": "Alice", "avatar": null}
        ]
    }
}
```

**注意**：不存在的 `user_id` 会被**静默省略**（不进 `users` 列表），不会返回错误。建议在客户端用 resolve 拿到完整 chat_user_id 后再调本接口。

---

### 7.4 查询用户所在的房间 `GET /api/v1/users/{user_id}/room`

**请求**

```
GET /api/v1/users/user_d004625859f8/room
Authorization: Bearer <chat_token>
```

**响应**

```json
{
    "code": 0,
    "message": "success",
    "data": {
        "user_id": "user_d004625859f8",
        "room_id": "alove_room_1785832247641",
        "role": "member",
        "status": "online"
    }
}
```

**用途**：好友查找。返回 `data: null` 表示该用户当前不在任何房间。

---

### 7.5 房间成员列表 `GET /api/v1/room/{room_id}/members`

**请求**

```
GET /api/v1/room/alove_room_1785832247641/members
```

**响应**

```json
{
    "code": 0,
    "message": "success",
    "data": {
        "room_id": "alove_room_1785832247641",
        "member_count": 2,
        "allow_speak": true,
        "members": [
            {
                "user_id": "user_d004625859f8",
                "role": "owner",
                "status": "online",
                "publish_allowed": true,
                "joined_at": "2026-08-05T10:00:00Z",
                "last_active": "2026-08-05T10:30:00Z"
            },
            {
                "user_id": "user_aaaa1111bbbb",
                "role": "member",
                "status": "offline",
                "publish_allowed": false,
                "joined_at": "2026-08-05T10:05:00Z",
                "last_active": "2026-08-05T10:25:00Z"
            }
        ]
    }
}
```

**字段含义**：

| 字段 | 含义 |
|---|---|
| `allow_speak` | 房间是否允许发言（房主设置后影响全员） |
| `members[].user_id` | chat_user_id |
| `members[].role` | `owner` / `admin` / `member` / `guest` |
| `members[].status` | `online` / `offline` / `muted` |
| `members[].publish_allowed` | 是否能开麦推流（被禁麦时为 `false`） |

---

### 7.6 成员详情 `GET /api/v1/room/{room_id}/member/{user_id}`

```
GET /api/v1/room/alove_room_1785832247641/member/user_d004625859f8
```

**响应**

```json
{
    "code": 0,
    "message": "success",
    "data": {
        "user_id": "user_d004625859f8",
        "room_id": "alove_room_1785832247641",
        "role": "owner",
        "status": "online",
        "publish_allowed": true,
        "joined_at": "2026-08-05T10:00:00Z",
        "last_active": "2026-08-05T10:30:00Z"
    }
}
```

---

### 7.7 发言权限检查 `GET /api/v1/room/{room_id}/check-publish?user_id=...`

开麦前先调一次，避免推到一半被服务器拒。

**请求**

```
GET /api/v1/room/alove_room_1785832247641/check-publish?user_id=user_d004625859f8
Authorization: Bearer <chat_token>
```

**响应**

```json
{
    "code": 0,
    "message": "success",
    "data": {
        "user_id": "user_d004625859f8",
        "can_publish": true,
        "status": "online"
    }
}
```

**字段含义**：

| 字段 | 含义 |
|---|---|
| `can_publish` | `true` 才能推流；`false` 表示被禁麦或全员禁麦 |
| `status` | `normal` / `muted` / `offline` |

---

### 7.8 房间创建 `POST /api/v1/room`

**请求**

```
POST /api/v1/room
Authorization: Bearer <chat_token>
Content-Type: application/json

{
    "room_id": "alove_room_1785832247641",
    "owner_id": "user_d004625859f8",
    "name": "Nuo 的房间"
}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `room_id` | string | 是 | 客户端生成的房间ID |
| `owner_id` | string | 否 | **必须等于当前登录用户**（从 JWT 取），否则返回 `403 owner_id must match current user` |
| `name` | string | 否 | 房间名，默认等于 `room_id` |

**响应**

```json
{
    "code": 0,
    "message": "success",
    "data": {
        "room_id": "alove_room_1785832247641",
        "name": "Nuo 的房间",
        "owner_id": "user_d004625859f8",
        "created_at": "2026-08-05T10:00:00Z",
        "max_members": 8,
        "allow_speak": true,
        "member_count": 1
    }
}
```

---

### 7.9 加入房间 `POST /api/v1/room/{room_id}/join`

**请求**

```
POST /api/v1/room/alove_room_1785832247641/join
Authorization: Bearer <chat_token>
Content-Type: application/json

{
    "user_id": "user_d004625859f8",
    "role": "member"
}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `user_id` | string | 否 | **必须等于当前登录用户**（从 JWT 取），否则 `403` |
| `role` | string | 否 | `owner` / `admin` / `member` / `guest`，默认 `member` |
| `room_id` | string | 否 | 路径里已有则优先用路径的 |

**响应**

```json
{
    "code": 0,
    "message": "success",
    "data": {
        "user_id": "user_d004625859f8",
        "room_id": "alove_room_1785832247641",
        "role": "member",
        "status": "online",
        "publish_allowed": true,
        "joined_at": "2026-08-05T10:05:00Z"
    }
}
```

**重要**：
- 已关闭的房间 (`status=closed`) 拒绝加入 → `400 room is closed`。
- 房主重新加入会触发 `owner_online` 广播通知其他成员。

---

### 7.10 离开房间 `POST /api/v1/room/{room_id}/leave`

**请求**

```
POST /api/v1/room/alove_room_1785832247641/leave
Authorization: Bearer <chat_token>
Content-Type: application/json

{
    "user_id": "user_d004625859f8"
}
```

`user_id` 字段保留兼容，可不传；服务端以 JWT 里的用户为准。

**响应**：返回 `data: null` 表示成功。

**副作用**：
- 房主离开 → 广播 `owner_offline`
- 其他成员离开 → 广播 `member_left`

---

### 7.11 设置成员角色 `POST /api/v1/room/{room_id}/member/{user_id}/role`

**仅群主可操作**。

**请求**

```
POST /api/v1/room/alove_room_1785832247641/member/user_aaaa1111bbbb/role
Authorization: Bearer <chat_token>
Content-Type: application/json

{
    "role": "admin",
    "operator_id": "user_d004625859f8"
}
```

| 字段 | 取值 | 说明 |
|---|---|---|
| `role` | `owner` / `admin` / `member` / `guest` | 必填 |
| `operator_id` | chat_user_id | 可选；传入则必须等于当前登录用户（从 JWT 取） |

**响应**

```json
{
    "code": 0,
    "message": "success",
    "data": {
        "user_id": "user_aaaa1111bbbb",
        "room_id": "alove_room_1785832247641",
        "role": "admin"
    }
}
```

**错误**：
- `403 仅群主可设置成员角色` — 操作者不是群主
- `400 设置失败（不能修改群主角色或用户不存在）`

---

### 7.12 禁言 / 禁麦 / 踢人接口

| 接口 | 方法 | 权限 | 副作用 |
|---|---|---|---|
| `/room/{room_id}/member/{user_id}/mute` | POST | 群主 / 管理员 | 广播 `member_muted` |
| `/room/{room_id}/member/{user_id}/unmute` | POST | 群主 / 管理员 | 广播 `member_unmuted` |
| `/room/{room_id}/member/{user_id}/mic/disable` | POST | 群主 / 管理员 | 清空 `publish_allowed` |
| `/room/{room_id}/member/{user_id}/mic/enable` | POST | 群主 / 管理员 | 恢复 `publish_allowed` |
| `/room/{room_id}/member/{user_id}/kick` | DELETE | 群主 / 管理员 | 移除成员 |
| `/room/{room_id}/mute-all` | POST | 群主 / 管理员 | 全员禁麦 |
| `/room/{room_id}/unmute-all` | POST | 群主 / 管理员 | 解除全员禁麦 |

**请求体（mute/unmute/mic 这类）**：

```json
{
    "operator_id": "user_d004625859f8"
}
```

`operator_id` 可不传；服务端以 JWT 里的用户为准。

**响应**：返回 `data: null` 表示成功。

---

### 7.13 房间说话状态 `GET /api/v1/room/{room_id}/speaking`

基于 SRS 推流流记录判断当前哪些人在说话。

**请求**

```
GET /api/v1/room/alove_room_1785832247641/speaking
```

**响应**

```json
{
    "code": 0,
    "message": "success",
    "data": {
        "room_id": "alove_room_1785832247641",
        "speaking_users": ["user_d004625859f8"]
    }
}
```

`speaking_users` 里的 ID 是 chat_user_id（已从 stream_name `{room_id}_{user_id}` 提取）。

---

## 八、典型客户端流程（扩展）

### 8.1 进入房间标准流程

```
1. POST /api/v1/auth/login          → 拿回 {bus_id, user_id, token}
2. POST /api/v1/users/resolve       ids = ["bus:<bus_id>"]
   → 拿回 chat_user_id（一般就是 login 返回的 user_id，做一次确认）
3. POST /api/v1/room/{room_id}/join → 加入房间
4. GET  /api/v1/room/{room_id}/members → 拉成员列表
5. POST /api/v1/users/names body={user_ids: [...]} → 批量显示名
6. GET  /api/v1/room/{room_id}/check-publish?user_id=<自己> → 看能否开麦
7. （可选）建立 WebSocket 监听房间事件
```

### 8.2 邀请好友场景

```
1. 客户端已知好友的 bus_id（如 790020）
2. POST /api/v1/users/resolve  ids = ["bus:790020"]
   → 拿到 chat_user_id = user_aaaa1111bbbb
3. POST /api/v1/invite
       body = {room_id, invitee_id: "user_aaaa1111bbbb", message: "..."}
4. 好友端：
   GET  /api/v1/invites/pending                  → 拉取待处理邀请
   POST /api/v1/invite/{invitation_id}/accept    → 接受
   POST /api/v1/invite/{invitation_id}/reject    → 拒绝
```

### 8.3 群主把成员升为管理员

```
POST /api/v1/room/{room_id}/member/{user_id}/role
body = {role: "admin"}
→ 客户端拿到的 user_id 必须是 chat_user_id（user_xxx），不能用 bus_id
```

---

## 九、ID 使用速查

| 场景 | 用哪种 ID |
|---|---|
| 登录请求携带 | `external_token`（来自业务后台） |
| 登录响应里读你自己的身份 | `bus_id` + `user_id` 都要读 |
| 存到本地缓存 | `bus_id`（替代旧 principalId）、`user_id`、`token` |
| `/api/v1/users/resolve` 输入 | `bus:<bus_id>` / `chat:<user_id>` / `user_xxx` |
| `/api/v1/users/resolve` 输出 | `chat_user_id`（即 `user_xxx`） |
| 房间、成员、推流、邀请等所有业务接口 | `chat_user_id`（`user_xxx`）必须 |
| 房间里显示名称 | 通过 `/api/v1/users/{user_id}/name` 或 `/users/names` 查 `username` |
| 推流地址 RTMP | `{room_id}_{user_id}` 中的 `user_id` 是 `chat_user_id` |
| WS 事件 `user_id` 字段 | `chat_user_id` |
| 业务后台侧关联 | `bus_id` |

**唯一例外**：`/api/v1/auth/login` 请求体里的 `external_token` 是业务后台签发的原始 token，不属于这 4 种 ID 范畴。

---

## 十、翻译模块（ID 字段说明）

翻译模块里所有 `user_id` / `source_user` / `target_user` 都是 **chat_user_id**（`user_xxx`）。

### 10.1 启动翻译 `POST /api/v1/translation/start`

**请求**

```
POST /api/v1/translation/start
Content-Type: application/json

{
    "room_id": "alove_room_1785832247641",
    "source_user": "user_d004625859f8",
    "target_user": "user_aaaa1111bbbb",
    "source_lang": "auto",
    "target_lang": "en"
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `room_id` | 是 | 房间ID |
| `source_user` | 是 | 说话人 chat_user_id（要听翻译的目标人之外那个人） |
| `target_user` | 是 | 申请翻译的人 chat_user_id（翻译内容只推给这个 user） |
| `source_lang` | 否 | 默认 `auto` |
| `target_lang` | 否 | 不传则取 `to_lang`（旧字段兼容） |

**响应**

```json
{"code": 0, "message": "success", "data": {"request_id": "xxx"}}
```

**副作用**：
- 杀掉同 `(room_id, source_user, target_lang)` 旧翻译（幂等）。
- 广播 `translation_started` 给房间。

---

### 10.2 翻译文本推送 `POST /api/v1/translation/text`

**内部接口**：由翻译服务进程调用，**不要**从客户端直接调用。

**请求**

```
POST /api/v1/translation/text

{
    "room_id": "alove_room_1785832247641",
    "user_id": "user_d004625859f8",
    "target_user": "user_aaaa1111bbbb",
    "original_text": "你好",
    "translated_text": "Hello",
    "source_lang": "zh",
    "target_lang": "en"
}
```

**关键**：翻译文本只推送给 `target_user`（**点对点**，不是房间广播）。

---

### 10.3 翻译心跳 `POST /api/v1/translation/heartbeat`

**请求**

```
POST /api/v1/translation/heartbeat

{
    "request_id": "xxx",
    "room_id": "alove_room_1785832247641",
    "source_user": "user_d004625859f8",
    "to_lang": "en",
    "client_id": "client_xxx"
}
```

`request_id` 与 `(room_id, source_user, to_lang)` 任传一组即可。

**用途**：客户端每 20~30 秒调用一次，告知服务端"我还在听翻译"。心跳超时（默认 60 秒无心跳）服务端会自动停止翻译并广播 `translation_stopped`。

---

### 10.4 停止翻译 `POST /api/v1/translation/stop`

**请求（按 request_id 精确）**

```
POST /api/v1/translation/stop?request_id=xxx
```

**请求（按房间+说话人+目标语言）**

```
POST /api/v1/translation/stop?room_id=alove_room_1785832247641&source_user=user_d004625859f8&to_lang=en
```

全部参数走 **query string**。

**响应**：`{"code": 0, "message": "success", "data": null}`。

---

### 10.5 查询活跃翻译 `GET /api/v1/translation/active`

**响应**

```json
{
    "code": 0,
    "message": "success",
    "data": {
        "translations": [
            {
                "request_id": "xxx",
                "room_id": "alove_room_1785832247641",
                "source_user": "user_d004625859f8",
                "target_user": "user_aaaa1111bbbb",
                "target_lang": "en",
                "status": "ACTIVE",
                "started_at": 1785832250,
                "last_heartbeat": 1785832270
            }
        ]
    }
}
```

---

### 10.6 原文广播 `POST /api/v1/original-speech`

**内部接口**：翻译服务用，**不要**从客户端直接调用。

**请求**

```
POST /api/v1/original-speech

{
    "room_id": "alove_room_1785832247641",
    "user_id": "user_d004625859f8",
    "original_text": "你好",
    "source_lang": "zh"
}
```

**关键**：原文广播给房间**所有**在线用户（与 `translation_text` 的点对点不同）。

---

### 10.7 翻译 RTMP 推流地址

```
rtmp://<host>:1935/live/{room_id}_{source_user}_to_{target_lang}
```

**示例**：`rtmp://x.x.x.x:1935/live/alove_room_1785832247641_user_d004625859f8_to_en`

**HTTP-FLV 播放地址**：`http://<host>:8080/live/{room_id}_{source_user}_to_{target_lang}.flv`

---

## 十一、邀请模块

邀请模块的所有 `invitee_id` / `inviter_id` / `knocker_id` 都是 **chat_user_id**（`user_xxx`）。

**业务规则**：

- `invitee_id` 必须是 `user_<12hex>` 格式，传业务 ID 或 username 会 `400 invitee_id 必须是聊天服务器分配的 user_id`
- 不能邀请自己
- 邀请者必须在该房间中（非房主成员发邀请也允许，但被禁言者不行）
- 同一 `(invitee_id, room_id)` 不能有重复 pending 邀请

### 11.1 发送邀请 `POST /api/v1/invite`

**请求**

```
POST /api/v1/invite
Authorization: Bearer <chat_token>
Content-Type: application/json

{
    "room_id": "alove_room_1785832247641",
    "invitee_id": "user_aaaa1111bbbb",
    "message": "来听我唱"
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `room_id` | 是 | 房间ID |
| `invitee_id` | 是 | **chat_user_id**（不能是 bus_id 或 username） |
| `message` | 否 | 邀请附言 |

**响应**

```json
{"code": 0, "message": "success", "data": {"id": "inv_xxx"}}
```

**WS 事件**：推送给被邀请者 `room_invite`

---

### 11.2 待处理邀请列表 `GET /api/v1/invites/pending`

**请求**

```
GET /api/v1/invites/pending
Authorization: Bearer <chat_token>
```

**响应**（数组）

```json
{
    "code": 0,
    "message": "success",
    "data": [
        {
            "id": "inv_xxx",
            "room_id": "alove_room_1785832247641",
            "room_name": "Nuo 的房间",
            "inviter_id": "user_d004625859f8",
            "inviter_name": "Nuo",
            "created_at": 1785832250,
            "message": "来听我唱"
        }
    ]
}
```

> 只有 `invitee_id` 属于当前 token 的记录会被返回。

---

### 11.3 接受邀请 `POST /api/v1/invite/{invitation_id}/accept`

**请求**

```
POST /api/v1/invite/inv_xxx/accept
Authorization: Bearer <chat_token>
```

**响应**

```json
{"code": 0, "message": "success", "data": {"room_id": "alove_room_1785832247641", "user_id": "user_aaaa1111bbbb"}}
```

**副作用**：
- 邀请状态置 `accepted`
- 用户加入房间（成员角色 MEMBER）
- 房间广播 `member_joined`
- 推送给邀请者 `room_invite_accepted`

**错误**：
- `404 invitation not found`
- `403 not your invitation`
- `400 invitation expired`
- `409 invitation status changed, please retry`

---

### 11.4 拒绝邀请 `POST /api/v1/invite/{invitation_id}/reject`

**请求**

```
POST /api/v1/invite/inv_xxx/reject
Authorization: Bearer <chat_token>
Content-Type: application/json

{
    "reason": "忙"
}
```

**响应**：`{"code": 0, "message": "success", "data": null}`，邀请者收到 `room_invite_rejected`。

---

### 11.5 批量查询邀请 `POST /api/v1/invites/batch`

**请求**

```
POST /api/v1/invites/batch
Authorization: Bearer <chat_token>
Content-Type: application/json

{
    "ids": ["inv_xxx", "inv_yyy"]
}
```

**响应**（数组，只返回与当前用户有关的邀请）

```json
{
    "code": 0,
    "message": "success",
    "data": [
        {
            "id": "inv_xxx",
            "room_id": "alove_room_1785832247641",
            "room_name": "Nuo 的房间",
            "inviter_id": "user_d004625859f8",
            "inviter_name": "Nuo",
            "invitee_id": "user_aaaa1111bbbb",
            "created_at": 1785832250,
            "expires_at": 1785915050,
            "status": "pending",
            "message": "来听我唱"
        }
    ]
}
```

---

## 十二、分享链接模块

### 12.1 生成分享链接 `POST /api/v1/share`

**仅房主可调用**。

**请求**

```
POST /api/v1/share
Authorization: Bearer <chat_token>
Content-Type: application/json

{
    "room_id": "alove_room_1785832247641",
    "message": "今晚 8 点直播"
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `room_id` | 是 | 房间ID |
| `message` | 否 | ≤200 字符 |

**响应**

```json
{
    "code": 0,
    "message": "success",
    "data": {
        "share_id": "share_xxx",
        "share_url": "https://chat.example.com/room/share_xxx",
        "room_id": "alove_room_1785832247641",
        "room_name": "Nuo 的房间",
        "expires_at": 1785915050
    }
}
```

**副作用**：房间广播 `room_shared`（除房主外）。

---

### 12.2 解析分享链接（App 内） `GET /api/v1/share/{share_id}/resolve`

**请求**

```
GET /api/v1/share/share_xxx/resolve
Authorization: Bearer <chat_token>
```

**响应**

```json
{
    "code": 0,
    "message": "success",
    "data": {
        "share_id": "share_xxx",
        "room_id": "alove_room_1785832247641",
        "room_name": "Nuo 的房间",
        "sharer_id": "user_d004625859f8",
        "sharer_name": "user_d004625859f8",
        "message": "今晚 8 点直播",
        "room_status": "active",
        "member_count": 2,
        "owner_id": "user_d004625859f8",
        "your_role": "none",
        "expires_at": 1785915050
    }
}
```

`your_role`：`owner` / `admin` / `member` / `guest` / `none`（未加入）。

---

### 12.3 解析分享链接（公开） `GET /api/v1/share/{share_id}`

**不需要 JWT**，给微信/QQ 内嵌 H5 用。

**请求**

```
GET /api/v1/share/share_xxx
```

**响应**

```json
{
    "code": 0,
    "message": "success",
    "data": {
        "share_id": "share_xxx",
        "room_id": "alove_room_1785832247641",
        "room_name": "Nuo 的房间",
        "sharer_name": "user_d004625859f8",
        "message": "今晚 8 点直播",
        "room_status": "active",
        "member_count": 2,
        "expires_at": 1785915050
    }
}
```

注意：公开版**不返回** `owner_id` 和 `your_role`。

---

### 12.4 通过分享链接加入 `POST /api/v1/share/{share_id}/join`

**请求**

```
POST /api/v1/share/share_xxx/join
Authorization: Bearer <chat_token>
```

**响应**：`{"code": 0, "message": "success", "data": null}` 并加入房间。

---

### 12.5 分享链接状态码

| code | 含义 |
|---|---|
| 404 | 房间不存在 / 已关闭 |
| 410 | 分享链接过期 |

---

## 十三、敲门模块（在房间已满 / 私密时使用）

### 13.1 敲门 `POST /api/v1/room/{room_id}/knock`

**请求**

```
POST /api/v1/room/alove_room_1785832247641/knock
Authorization: Bearer <chat_token>
Content-Type: application/json

{
    "message": "我也要进来听"
}
```

`user_id` 兼容字段可不传，服务端从 JWT 取。

**响应**

```json
{
    "code": 0,
    "message": "success",
    "data": {
        "room_id": "alove_room_1785832247641",
        "owner_id": "user_d004625859f8",
        "knocker_id": "user_xxxxxx000111"
    }
}
```

**WS 事件**：推送给房主 `room_knock`。

---

### 13.2 接受敲门 `POST /api/v1/room/{room_id}/knock/accept`

**仅房主可操作**。

**请求**

```
POST /api/v1/room/alove_room_1785832247641/knock/accept
Authorization: Bearer <chat_token>
Content-Type: application/json

{
    "knocker_id": "user_xxxxxx000111",
    "role": "member"
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `knocker_id` | 是 | 敲门者的 chat_user_id |
| `role` | 否 | `owner` / `admin` / `member` / `guest`，默认 `member` |

**副作用**：
- 敲门者加入房间
- 敲门者收到 `room_knock_accepted`
- 房间广播 `member_joined`

---

### 13.3 拒绝敲门 `POST /api/v1/room/{room_id}/knock/reject`

**请求**

```
POST /api/v1/room/alove_room_1785832247641/knock/reject
Authorization: Bearer <chat_token>
Content-Type: application/json

{
    "knocker_id": "user_xxxxxx000111",
    "reason": "房间已满"
}
```

**副作用**：敲门者收到 `room_knock_rejected`。

---

## 十四、WebSocket 事件总览

**连接地址**：`ws://<host>:8086/ws?room={room_id}&user={user_id}`

`user_id` 用 chat_user_id（`user_xxx`），不传则仅按房间订阅。

**客户端发送**：

```json
{"type": "ping"}
```

服务器回复：

```json
{"type": "pong"}
```

**房间切换（subscribe）**：

```json
{"type": "subscribe", "room_id": "alove_room_1785832247641"}
```

---

### 14.1 事件清单

下表所有 ID 字段都是 **chat_user_id**。

| type | 推送对象 | 触发场景 |
|---|---|---|
| `connected` | 当前连接 | WS 连接成功 |
| `member_joined` | 房间全员 | 有人加入（含 owner 重入、邀请、敲门、分享链接） |
| `member_left` | 房间全员（非房主离开） | 成员离开 |
| `member_kicked` | 房间全员 | 被踢 |
| `owner_online` | 房间全员（除房主） | 房主重新加入 |
| `owner_offline` | 房间全员（除房主） | 房主离开 |
| `room_closed` | 房间全员 | 房间关闭（房主进入其他房间） |
| `member_muted` | 房间全员 | 单人禁言 |
| `member_unmuted` | 房间全员 | 解除禁言 |
| `member_mic_disabled` | 房间全员 | 单人禁麦 |
| `member_mic_enabled` | 房间全员 | 解除禁麦 |
| `room_muted_all` | 房间全员 | 全员禁言 |
| `room_unmuted_all` | 房间全员 | 解除全员禁言 |
| `room_knock` | 房主 | 有人敲门 |
| `room_knock_accepted` | 敲门者 | 房主接受了敲门 |
| `room_knock_rejected` | 敲门者 | 房主拒绝了敲门 |
| `room_invite` | 被邀请者 | 收到邀请 |
| `room_invite_accepted` | 邀请者 | 对方接受了邀请 |
| `room_invite_rejected` | 邀请者 | 对方拒绝了邀请 |
| `room_shared` | 房间全员（除房主） | 房主生成了分享链接 |
| `user_speaking_start` | 房间全员 | 有人开始说话 |
| `user_speaking_stop` | 房间全员 | 有人停止说话 |
| `translation_started` | 房间全员 | 翻译启动 |
| `translation_text` | **target_user 点对点** | 翻译文本片段 |
| `translation_stopped` | 房间全员（心跳超时） | 翻译被停止 |
| `original_speech_text` | 房间全员 | 原文广播 |
| `pong` | 当前连接 | 心跳响应 |

---

### 14.2 事件 payload

**`member_joined`**

```json
{
    "type": "member_joined",
    "room_id": "alove_room_1785832247641",
    "user_id": "user_xxxxxx000111",
    "data": {"name": "Alice", "role": "member"},
    "timestamp": 1785832250
}
```

**`member_left` / `member_kicked`**

```json
{
    "type": "member_kicked",
    "room_id": "alove_room_1785832247641",
    "user_id": "user_xxxxxx000111",
    "operator_id": "user_d004625859f8",
    "timestamp": 1785832250
}
```

**`owner_online` / `owner_offline`**

```json
{
    "type": "owner_offline",
    "room_id": "alove_room_1785832247641",
    "data": {
        "owner_id": "user_d004625859f8",
        "owner_name": "Nuo",
        "timestamp": 1785832250
    },
    "timestamp": 1785832250
}
```

**`room_closed`**

```json
{
    "type": "room_closed",
    "room_id": "alove_room_1785832247641",
    "data": {
        "closed_by": "user_d004625859f8",
        "reason": "owner_entered_another_room",
        "timestamp": 1785832250
    }
}
```

**`member_muted` / `member_mic_disabled` 等**

```json
{
    "type": "member_mic_disabled",
    "room_id": "alove_room_1785832247641",
    "user_id": "user_xxxxxx000111",
    "operator_id": "user_d004625859f8",
    "timestamp": 1785832250
}
```

**`room_muted_all`**

```json
{
    "type": "room_muted_all",
    "room_id": "alove_room_1785832247641",
    "operator_id": "user_d004625859f8",
    "data": {"muted_count": 3},
    "timestamp": 1785832250
}
```

**`room_knock`**

```json
{
    "type": "room_knock",
    "room_id": "alove_room_1785832247641",
    "knocker_id": "user_xxxxxx000111",
    "data": {"message": "我也要进来听", "name": "Alice"},
    "timestamp": 1785832250
}
```

**`room_knock_accepted`**

```json
{
    "type": "room_knock_accepted",
    "room_id": "alove_room_1785832247641",
    "timestamp": 1785832250
}
```

**`room_knock_rejected`**

```json
{
    "type": "room_knock_rejected",
    "room_id": "alove_room_1785832247641",
    "data": {"reason": "房间已满"},
    "timestamp": 1785832250
}
```

**`room_invite`**

```json
{
    "type": "room_invite",
    "room_id": "alove_room_1785832247641",
    "data": {
        "invitation_id": "inv_xxx",
        "inviter_id": "user_d004625859f8",
        "inviter_name": "Nuo",
        "room_name": "Nuo 的房间",
        "message": "来听我唱",
        "created_at": 1785832250
    },
    "timestamp": 1785832250
}
```

**`room_invite_accepted`**

```json
{
    "type": "room_invite_accepted",
    "room_id": "alove_room_1785832247641",
    "data": {
        "invitation_id": "inv_xxx",
        "invitee_id": "user_xxxxxx000111",
        "invitee_name": "Alice"
    },
    "timestamp": 1785832250
}
```

**`room_invite_rejected`**

```json
{
    "type": "room_invite_rejected",
    "room_id": "alove_room_1785832247641",
    "data": {
        "invitation_id": "inv_xxx",
        "invitee_id": "user_xxxxxx000111",
        "invitee_name": "Alice",
        "reason": "忙"
    },
    "timestamp": 1785832250
}
```

**`room_shared`**

```json
{
    "type": "room_shared",
    "room_id": "alove_room_1785832247641",
    "data": {
        "share_id": "share_xxx",
        "sharer_id": "user_d004625859f8",
        "sharer_name": "user_d004625859f8",
        "share_url": "https://chat.example.com/room/share_xxx",
        "timestamp": 1785832250
    }
}
```

**`user_speaking_start` / `user_speaking_stop`**

```json
{
    "type": "user_speaking_start",
    "room_id": "alove_room_1785832247641",
    "user_id": "user_d004625859f8",
    "data": {
        "stream_url": "rtmp://x.x.x.x:1935/live/alove_room_xxx_user_d004625859f8",
        "user_name": "Nuo"
    }
}
```

**`translation_started`**

```json
{
    "type": "translation_started",
    "room_id": "alove_room_1785832247641",
    "user_id": "user_d004625859f8",
    "target_user": "user_aaaa1111bbbb",
    "data": {"to_lang": "en"}
}
```

**`translation_text`（点对点，只推给 target_user）**

```json
{
    "type": "translation_text",
    "room_id": "alove_room_1785832247641",
    "user_id": "user_d004625859f8",
    "target_user": "user_aaaa1111bbbb",
    "data": {
        "original_text": "你好",
        "translated_text": "Hello",
        "source_lang": "zh",
        "target_lang": "en"
    }
}
```

**`translation_stopped`**

```json
{
    "type": "translation_stopped",
    "room_id": "alove_room_1785832247641",
    "user_id": "user_d004625859f8",
    "target_user": "user_aaaa1111bbbb",
    "data": {}
}
```

**`original_speech_text`（房间全员）**

```json
{
    "type": "original_speech_text",
    "room_id": "alove_room_1785832247641",
    "user_id": "user_d004625859f8",
    "data": {
        "original_text": "你好",
        "source_lang": "zh"
    }
}
```

---

## 十五、跨模块 ID 使用注意事项

| 场景 | ID 类型 | 备注 |
|---|---|---|
| 邀请 `invitee_id` | **chat_user_id** | 服务端强制格式校验，传 bus_id 直接 400 |
| 敲门 `knocker_id` | **chat_user_id** | 接受方校验 |
| 角色设置 `user_id`（路径） | **chat_user_id** | 被操作者 |
| 邀请/敲门广播接收方 | **chat_user_id** | WS 推送按 user_id 路由 |
| 推流 URL `user_id` | **chat_user_id** | `{room_id}_{user_id}` 中的 user_id |
| WS 推送路由 | **chat_user_id** | 服务端按 user 维度的连接定向推送 |
| 翻译 `source_user` / `target_user` | **chat_user_id** | 翻译 stream URL 与推送路由 |
| 分享链接 `sharer_id` | **chat_user_id** | 字段名为 `user_id` 时本质都是 chat_user_id |

**核心原则**：

> **业务端用 `bus_id`，聊天系统内部用 `user_id`（chat_user_id），两侧之间用 `/api/v1/users/resolve` 做桥梁。**
>
> 任何 HTTP 接口 / WS 事件的 `user_id` 字段，**都应为 chat_user_id**。如果客户端报错"user not found"，先确认传的是不是 `user_xxx` 格式。