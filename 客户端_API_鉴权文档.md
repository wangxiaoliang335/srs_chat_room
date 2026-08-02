# 客户端 API 鉴权 + 接口文档（v2）

> 面向客户端开发者，覆盖登录鉴权、所有 HTTP 业务接口、WebSocket 协议。本版本起所有 `/api/v1/*` 业务接口（白名单除外）必须携带 `Authorization: Bearer <jwt>`。

---

## 1. 基础信息

| 项 | 值 |
|---|---|
| 服务器地址 | `http://<host>:8085`（开发环境 `localhost:8085`，生产替换为实际地址） |
| 协议 | HTTP/HTTPS |
| WebSocket | `ws://<host>:8085/ws?room=<room_id>&user=<user_id>` |
| 响应格式 | `application/json; charset=utf-8` |
| 时间 | 所有时间戳为 **UTC 秒级 Unix 时间戳**（`int`） |
| JWT 算法 | HS256，签发方 `iss="srs-project"` |

---

## 2. 鉴权流程（必须先看）

### 2.1 JWT 内容（payload）

```json
{
  "sub": "alice",                       // 用户名（username，唯一标识一个账号）
  "uid": "user_e7b45a63b1d3",           // user_id：稳定的用户身份 ID（加入/离开/被踢时使用这个）
  "room": "room_abc",                   // 当前所在房间（登录时若有进房则带；可能为 null）
  "role": "member",                     // 账号级角色：member / admin / owner / guest
  "jti": "uuid4hex...",                 // token 唯一标识，用于撤销
  "iat": 1783866606,                    // 签发时间（秒）
  "exp": 1784471406,                    // 过期时间（默认 7 天）
  "iss": "srs-project"
}
```

**注意**：

- `room` 与 `role` 字段是**登录那一刻的快照**，服务端中间件会**实时从 `user_store` / `user_manager` 刷新**这两个字段后再注入到 `request.state`。所以前端永远以**服务端当前真实状态**为准（通过 `/api/v1/auth/me` 查）。
- `user_id` 是稳定身份，注册时生成，**永久不变**。业务接口里的 `user_id` / `member_id` / `knocker_id` / `owner_id` 等都使用这个 ID，**不要再用 username**。
- `username` 仅用于登录和展示。

### 2.2 调用流程

```text
┌──────────────────────────────────────────────────────────────────┐
│  客户端启动                                                        │
│  1. POST /api/v1/auth/register  (新用户)                          │
│     或 POST /api/v1/auth/login     (已有账号)                       │
│     → 拿到 token + expires_at + user_id + room_id + role          │
│                                                                  │
│  2. 把 token 存到 localStorage/sessionStorage/Cookie               │
│                                                                  │
│  3. 之后每个 HTTP 请求：                                            │
│     Header: Authorization: Bearer <token>                         │
│                                                                  │
│  4. 收到 401 {"code":401,"message":"token expired"}              │
│     或 401 {"code":401,"message":"token revoked"}                │
│     → 重新走 login                                                │
│                                                                  │
│  5. 用户主动登出 → POST /api/v1/auth/logout                        │
│     或批量踢出全部设备 → POST /api/v1/auth/logout-all              │
└──────────────────────────────────────────────────────────────────┘
```

### 2.3 不需要鉴权的接口（白名单）

以下接口**无需 token**：

| 路径 | 方法 | 说明 |
|------|------|------|
| `/health` | GET | 服务存活检查 |
| `/api/v1/health` | GET | 同上 |
| `/api/v1/auth/register` | POST | 注册 |
| `/api/v1/auth/login` | POST | 登录 |
| `/api/v1/auth/revocation-status` | GET | 调试用，查看撤销列表规模 |
| `/api/v1/streams/on_*` | POST | SRS 内部回调（不允许外部调用） |
| `/api/v1/hooks/on_*` | POST | 同上 |
| `/ws` | WS | **WebSocket（当前无 token 鉴权，见 §10 风险说明）** |

---

## 3. 统一响应格式

**成功**：

```json
{
  "code": 0,
  "message": "success",
  "data": { ... }
}
```

**失败**：

```json
{
  "code": 401,
  "message": "token expired",
  "data": null
}
```

**错误码**：

| code | 含义 |
|------|------|
| 0 | 成功 |
| 400 | 参数错误 / 请求体不合法 |
| 401 | 未登录 / token 失效 / token 过期 / token 已撤销 |
| 403 | 权限不足（越权操作） |
| 404 | 资源不存在 |
| 409 | 资源冲突（如用户名已存在、房间有活跃资源不能删） |
| 422 | 参数校验失败（Pydantic） |
| 500 | 服务端内部错误 |

---

## 4. 账号与鉴权接口（`/api/v1/auth/*`）

### 4.1 注册

```
POST /api/v1/auth/register
```

无需 token。

**请求**：

```json
{
  "username": "alice",          // 3-32 位字母/数字/下划线/连字符
  "password": "secret123"       // 6-128 位
}
```

**响应 200**：

```json
{
  "code": 0,
  "message": "success",
  "data": {
    "username": "alice",
    "user_id": "user_e7b45a63b1d3",
    "room_id": null,
    "role": "member",
    "token": "eyJhbGc...",
    "expires_at": 1784471406,
    "expires_in": 604800
  }
}
```

**错误**：

- `400 用户名必须为 3-32 位字母/数字/下划线/连字符`
- `400 密码长度需在 6-128 之间`
- `409 用户名已存在`

### 4.2 登录

```
POST /api/v1/auth/login
```

无需 token。

**请求**：

```json
{
  "username": "alice",
  "password": "secret123"
}
```

**响应 200**：

```json
{
  "code": 0,
  "data": {
    "username": "alice",
    "user_id": "user_e7b45a63b1d3",
    "room_id": null,
    "role": "member",
    "token": "eyJhbGc...",
    "expires_at": 1784471406,
    "expires_in": 604800
  }
}
```

**错误**：

- `401 用户名或密码错误`

### 4.3 查询当前用户

```
GET /api/v1/auth/me
Authorization: Bearer <token>
```

**响应 200**：

```json
{
  "code": 0,
  "data": {
    "username": "alice",
    "user_id": "user_e7b45a63b1d3",
    "room_id": "room_abc",
    "role": "member"
  }
}
```

**注意**：`room_id` 与 `role` 是**服务端实时状态**，不是 JWT 里的快照。即使房间变了或被踢了，下次调用立刻反映。

### 4.4 登出（撤销当前 token）

```
POST /api/v1/auth/logout
Authorization: Bearer <token>
```

**响应 200**：

```json
{ "code": 0, "message": "logged out", "data": {} }
```

**效果**：当前 token 立即加入黑名单，后续任何接口调用都返回 `401 token revoked`。

### 4.5 撤销本账号全部 token

```
POST /api/v1/auth/logout-all
Authorization: Bearer <token>
Content-Type: application/json

{ "username": "" }              // 不填则撤销当前登录用户；填了必须 == 当前 username
```

**响应 200**：

```json
{ "code": 0, "message": "all tokens revoked", "data": { "username": "alice", "revoked_count": 3 } }
```

**错误**：

- `403 只能撤销自己的 token` —— 想撤销别人的 username 时

### 4.6 调试：撤销列表状态（白名单）

```
GET /api/v1/auth/revocation-status
```

无需 token，仅用于排查。

```json
{ "code": 0, "data": { "revoked_jtis": 3, "active_users": 2, "active_jtis_total": 5 } }
```


## 5. 房间管理（`/api/v1/room*`）

> 所有接口都需要 JWT。**身份字段（user_id / owner_id / operator_id）统一从 JWT 中取**，请求体里**带了就会校验一致性**——带错值会返回 `403 xxx mismatch`。

### 5.1 创建房间

```
POST /api/v1/room
Authorization: Bearer <token>
```

**请求**：

```json
{
  "room_id": "room_abc",          // 必填
  "owner_id": "user_xxx",         // 可选，不带则从 JWT 取当前 user_id；若带了必须 == 当前 user_id
  "name": "我的聊天室"              // 可选
}
```

**响应 200**：

```json
{
  "code": 0,
  "data": {
    "room_id": "room_abc",
    "name": "我的聊天室",
    "owner_id": "user_e7b45a63b1d3",
    "created_at": "2026-07-12 22:30:06",
    "max_members": 100,
    "allow_speak": true,
    "member_count": 1
  }
}
```

**权限**：任何人（已登录）都可创建，创建后自动成为 owner。

### 5.2 查询所有房间

```
GET /api/v1/rooms
```

```json
{ "code": 0, "data": { "rooms": [{ "room_id": "room_abc", "owner_id": "user_xxx", "member_count": 3 }] } }
```

### 5.3 查询房间信息

```
GET /api/v1/room/{room_id}
```

### 5.4 删除房间

```
DELETE /api/v1/room/{room_id}
Authorization: Bearer <token>
```

**权限**：仅 `owner_id == JWT.user_id` 可删（403）。

**响应 200**：

```json
{ "code": 0, "data": { "room_id": "room_abc", "deleted_members": ["user_xxx", ...] } }
```

**错误**：

- `404 Room not found`
- `403 只有群主可以删除房间`
- `409 房间内存在活跃资源，无法删除`（含正在推流的用户）

### 5.5 查询房间成员

```
GET /api/v1/room/{room_id}/members
```

```json
{
  "code": 0,
  "data": {
    "room_id": "room_abc",
    "member_count": 3,
    "members": [
      {
        "user_id": "user_xxx",
        "role": "owner",
        "status": "normal",
        "publish_allowed": true,
        "joined_at": "2026-07-12 22:30:06",
        "last_active": "2026-07-12 22:35:00"
      }
    ]
  }
}
```

### 5.6 查询成员详情

```
GET /api/v1/room/{room_id}/member/{user_id}
```

### 5.7 加入房间

```
POST /api/v1/room/{room_id}/join
Authorization: Bearer <token>
```

**请求**：

```json
{
  "user_id": "",                  // 可选；不带则从 JWT 取
  "role": "member"                // 可选：owner / admin / member / guest，默认 member
}
```

**注意**：客户端通常**不要传 `user_id`**——服务端用 JWT 中的 user_id 防止越权（伪造加入别人）。

### 5.8 离开房间

```
POST /api/v1/room/{room_id}/leave
Authorization: Bearer <token>
```

请求体可空（user_id 也可不带）。

### 5.9 踢人

```
DELETE /api/v1/room/{room_id}/member/{user_id}/kick
Authorization: Bearer <token>
```

**权限**：

- **owner** 可踢任何人（owner / admin / member / guest）
- **admin** 仅可踢 `member` / `guest`
- 其他人 `403`

### 5.10 设置成员角色

```
POST /api/v1/room/{room_id}/member/{user_id}/role
Authorization: Bearer <token>
```

**请求**：

```json
{ "role": "admin" }              // owner / admin / member / guest
```

**权限**：仅 owner 可改。**不能改 owner 自己的角色**（防误操作锁死）。

### 5.11 敲门（访客请求加入）

```
POST /api/v1/room/{room_id}/knock
Authorization: Bearer <token>
```

**请求**：

```json
{ "message": "想加入" }
```

> `user_id` 不需要带，从 JWT 取。服务端通过 WS 通知房主。

### 5.12 房主接受敲门

```
POST /api/v1/room/{room_id}/knock/accept
Authorization: Bearer <token>
```

**请求**：

```json
{ "knocker_id": "user_xxx", "role": "member" }
```

**权限**：仅 owner 可接受。

### 5.13 房主拒绝敲门

```
POST /api/v1/room/{room_id}/knock/reject
Authorization: Bearer <token>
```

**请求**：

```json
{ "knocker_id": "user_xxx", "reason": "不欢迎" }
```

**权限**：仅 owner 可拒绝。

---

## 6. 权限控制（`/api/v1/room/{id}/member/...`）

> 所有控制接口都需要 JWT，且操作者身份从 JWT 取。body 里的 `operator_id` 字段保留兼容：带了必须等于 JWT user_id，否则 403。

### 6.1 禁言

```
POST /api/v1/room/{room_id}/member/{user_id}/mute
Authorization: Bearer <token>
```

请求体：`{}` 或 `{"operator_id": "user_xxx"}`

**权限**：owner + admin。不能禁言 owner 自身。

### 6.2 解除禁言

```
POST /api/v1/room/{room_id}/member/{user_id}/unmute
```

### 6.3 禁麦

```
POST /api/v1/room/{room_id}/member/{user_id}/mic/disable
```

**权限**：owner + admin。

### 6.4 解除禁麦

```
POST /api/v1/room/{room_id}/member/{user_id}/mic/enable
```

### 6.5 全体禁言

```
POST /api/v1/room/{room_id}/mute-all
```

**权限**：owner + admin。

```json
{ "code": 0, "data": { "room_id": "room_abc", "allow_speak": false, "muted_count": 3 } }
```

### 6.6 解除全体禁言

```
POST /api/v1/room/{room_id}/unmute-all
```

---

## 7. 推流 / 说话状态

### 7.1 查询发布权限

```
GET /api/v1/room/{room_id}/check-publish?user_id=user_xxx
```

### 7.2 查询正在说话的用户

```
GET /api/v1/room/{room_id}/speaking
```

```json
{ "code": 0, "data": { "room_id": "room_abc", "speaking_users": ["user_001"], "count": 1 } }
```

### 7.3 上报说话状态（客户端 → 服务端 → 广播给房间）

```
POST /api/v1/room/{room_id}/speaking/broadcast
Authorization: Bearer <token>
```

**请求**：

```json
{ "user_id": "user_xxx", "stream_url": "rtmp://...", "speaking": true }
```

> 服务端通过 WS 广播 `user_speaking_start` / `user_speaking_stop` 事件给房间所有人。

---

## 8. 翻译服务

### 8.1 启动翻译

```
POST /api/v1/translation/start
Authorization: Bearer <token>
```

**请求**：

```json
{
  "room_id": "room_abc",
  "source_user": "user_alice",
  "target_user": "user_bob",
  "source_lang": "auto",         // 可选
  "target_lang": "zh",           // 必填
  "to_lang": "zh"                // 兼容旧字段
}
```

### 8.2 推送翻译文本

```
POST /api/v1/translation/text
Authorization: Bearer <token>
```

```json
{
  "room_id": "room_abc",
  "source_user": "user_alice",
  "target_user": "user_bob",
  "original_text": "Hello",
  "translated_text": "你好"
}
```

### 8.3 停止翻译

```
POST /api/v1/translation/stop
Authorization: Bearer <token>
```

**请求**：

```json
{ "request_id": "xxx-xxx" }    // 或 { "room_id":..., "source_user":..., "target_user":..., "to_lang":"zh" }
```

### 8.4 翻译心跳

```
POST /api/v1/translation/heartbeat
Authorization: Bearer <token>
```

```json
{
  "room_id": "room_abc",
  "source_user": "user_alice",
  "client_id": "client_xxx",
  "to_lang": "zh",
  "request_id": "xxx-xxx"
}
```

### 8.5 当前活跃翻译

```
GET /api/v1/translation/active?room_id=room_abc
```

### 8.6 广播原文文本（不依赖翻译）

```
POST /api/v1/original-speech
Authorization: Bearer <token>
```

```json
{
  "room_id": "room_abc",
  "user_id": "user_alice",
  "original_text": "Hello everyone",
  "source_lang": "auto"
}
```

---

## 9. WebSocket 实时通知

### 9.1 连接

```
ws://<host>:8085/ws?room=<room_id>&user=<user_id>
```

> ⚠️ **当前版本 WS 鉴权未启用**：URL 中的 `room` 与 `user` 是**自我声明**的，服务端会照单全收。**详见 §10 风险**。

### 9.2 客户端主动发送

```json
{ "type": "subscribe", "room_id": "room_abc" }   // 订阅（连接时已带房间参数，可省略）
{ "type": "unsubscribe", "room_id": "room_abc" }
{ "type": "ping" }                                // 心跳
```

### 9.3 服务端推送事件

| `type` | 触发时机 | 关键字段 |
|--------|----------|----------|
| `connected` | WS 建立成功 | `client_id`, `user_id` |
| `subscribed` | 订阅成功 | `room_id` |
| `member_joined` | 用户加入 | `room_id`, `user_id` |
| `member_left` | 用户离开 | `room_id`, `user_id` |
| `member_kicked` | 用户被踢 | `room_id`, `user_id`, `operator_id` |
| `room_deleted` | 房间被删 | `room_id`, `deleted_by` |
| `user_speaking_start` | 有人开始推流 | `room_id`, `user_id`, `stream_url` |
| `user_speaking_stop` | 有人停推 | `room_id`, `user_id` |
| `muted` | 被禁言 | `room_id`, `user_id`, `operator_id` |
| `unmuted` | 解除禁言 | `room_id`, `user_id` |
| `mute_all_changed` | 全体禁言状态变化 | `room_id`, `allow_speak` |
| `room_knock` | 有人敲门 | `room_id`, `knocker_id`, `data.message` |
| `room_knock_accepted` | 敲门被接受 | `room_id`, `knocker_id` |
| `room_knock_rejected` | 敲门被拒绝 | `room_id`, `knocker_id`, `data.reason` |
| `translation_started` | 翻译启动 | `room_id`, `source_user`, `to_lang` |
| `translation_stopped` | 翻译停止 | `room_id`, `source_user`, `to_lang` |
| `translation_text` | 翻译文本 | `room_id`, `source_user`, `original_text`, `translated_text` |
| `original_speech_text` | 原文文本广播 | `room_id`, `user_id`, `data.original_text` |
| `pong` | 心跳响应 |  |

**注意**：所有事件里的 `user_id` 都是 `user_<hex12>` 格式（即 `/auth/me` 返回的 `user_id`），**不是 username**。

---

## 10. 已知风险与限制

### 10.1 WebSocket 鉴权未启用（中等风险）

当前 WS 路径 `/ws?room=&user=` 不校验任何身份。任何拿到服务器地址的客户端都可以：

- 假装自己是任何 `user_id`，收到该用户的所有事件
- 冒充某用户广播说话状态（受限：服务端不会因为 WS 广播就修改 `user_manager` 状态，但其他客户端会收到）

**计划**：在 `?token=<jwt>` 中传 JWT，服务端在校验后再建立连接。当前版本请**只在受信网络部署**或配合 VPN/反代使用。

### 10.2 用户名 vs user_id

| 用途 | 用哪个 |
|------|--------|
| 登录表单 | `username` |
| 业务接口里标识一个用户 | **`user_id`**（如踢人、被禁言者、敲门者、推流者） |
| 显示在 UI 上 | `username`（友好） |

### 10.3 用户管理接口（白名单相关）

- `POST /api/v1/auth/register` 可被任何人调用，**没有防脚本注册**。如需限制请加 IP 限流或图形验证码。
- `GET /api/v1/auth/revocation-status` 暴露内部状态，**生产环境应删白名单**或加管理员鉴权。

### 10.4 token 默认 TTL

- 默认 **7 天**（604800 秒），过期需重新登录
- 服务端**不会主动续期**，客户端应在临近过期时（建议剩 1 天）调一次 `/auth/login` 拿新 token 并替换旧 token

---

## 11. 客户端最佳实践

### 11.1 登录后立即做的事

```javascript
// 1. 保存登录返回
const { token, user_id, room_id, role, expires_at } = loginResp.data;
localStorage.setItem('auth', JSON.stringify({token, user_id, username, expires_at}));

// 2. 如果 room_id != null：直接进入房间；否则进入大厅
if (room_id) {
  enterRoom(room_id);
} else {
  showLobby();
}

// 3. 建立 WS（用 user_id，不是 username）
const ws = new WebSocket(`ws://${host}:8085/ws?room=${room_id}&user=${user_id}`);
```

### 11.2 HTTP 拦截器（401 自动重登）

```javascript
async function fetchAPI(url, options = {}) {
  let token = localStorage.getItem('auth_token');
  const headers = { 'Content-Type': 'application/json', ...options.headers };
  if (token) headers['Authorization'] = `Bearer ${token}`;

  let resp = await fetch(url, { ...options, headers });

  if (resp.status === 401) {
    const data = await resp.json();
    if (data.message === 'token expired' || data.message === 'token revoked') {
      // 跳登录
      window.location.href = '/login';
      return Promise.reject(data);
    }
  }
  return resp.json();
}
```

### 11.3 调用业务接口时不要带身份字段

```javascript
// ❌ 错误：前端不需要传 user_id / owner_id / operator_id
await fetchAPI('/api/v1/room/r1/join', {
  method: 'POST',
  body: JSON.stringify({ user_id: 'alice', role: 'member' })  // 后端会 403
});

// ✅ 正确：服务端从 JWT 取
await fetchAPI('/api/v1/room/r1/join', {
  method: 'POST',
  body: JSON.stringify({ role: 'member' })
});
```

### 11.4 退出登录清理

```javascript
async function logout() {
  try {
    await fetchAPI('/api/v1/auth/logout', { method: 'POST' });
  } catch (e) {}
  localStorage.removeItem('auth');
  window.location.href = '/login';
}
```


## 12. 接口速查表

| 功能 | 方法 | 路径 | 需要 token |
|------|------|------|-----------|
| 注册 | POST | `/api/v1/auth/register` | ❌ |
| 登录 | POST | `/api/v1/auth/login` | ❌ |
| 当前用户 | GET | `/api/v1/auth/me` | ✅ |
| 登出 | POST | `/api/v1/auth/logout` | ✅ |
| 撤销全部 token | POST | `/api/v1/auth/logout-all` | ✅ |
| 创建房间 | POST | `/api/v1/room` | ✅ |
| 房间列表 | GET | `/api/v1/rooms` | ✅ |
| 房间信息 | GET | `/api/v1/room/{id}` | ✅ |
| 删除房间 | DELETE | `/api/v1/room/{id}` | ✅ (owner) |
| 房间成员 | GET | `/api/v1/room/{id}/members` | ✅ |
| 成员详情 | GET | `/api/v1/room/{id}/member/{uid}` | ✅ |
| 加入房间 | POST | `/api/v1/room/{id}/join` | ✅ |
| 离开房间 | POST | `/api/v1/room/{id}/leave` | ✅ |
| 踢人 | DELETE | `/api/v1/room/{id}/member/{uid}/kick` | ✅ (owner/admin) |
| 设置角色 | POST | `/api/v1/room/{id}/member/{uid}/role` | ✅ (owner) |
| 敲门 | POST | `/api/v1/room/{id}/knock` | ✅ |
| 接受敲门 | POST | `/api/v1/room/{id}/knock/accept` | ✅ (owner) |
| 拒绝敲门 | POST | `/api/v1/room/{id}/knock/reject` | ✅ (owner) |
| 禁言 | POST | `/api/v1/room/{id}/member/{uid}/mute` | ✅ (owner/admin) |
| 解除禁言 | POST | `/api/v1/room/{id}/member/{uid}/unmute` | ✅ (owner/admin) |
| 禁麦 | POST | `/api/v1/room/{id}/member/{uid}/mic/disable` | ✅ (owner/admin) |
| 解除禁麦 | POST | `/api/v1/room/{id}/member/{uid}/mic/enable` | ✅ (owner/admin) |
| 全体禁言 | POST | `/api/v1/room/{id}/mute-all` | ✅ (owner/admin) |
| 解除全体禁言 | POST | `/api/v1/room/{id}/unmute-all` | ✅ (owner/admin) |
| 发布权限 | GET | `/api/v1/room/{id}/check-publish` | ✅ |
| 说话列表 | GET | `/api/v1/room/{id}/speaking` | ✅ |
| 上报说话 | POST | `/api/v1/room/{id}/speaking/broadcast` | ✅ |
| 启动翻译 | POST | `/api/v1/translation/start` | ✅ |
| 推送翻译文本 | POST | `/api/v1/translation/text` | ✅ |
| 停止翻译 | POST | `/api/v1/translation/stop` | ✅ |
| 翻译心跳 | POST | `/api/v1/translation/heartbeat` | ✅ |
| 活跃翻译 | GET | `/api/v1/translation/active` | ✅ |
| 原文广播 | POST | `/api/v1/original-speech` | ✅ |
| WS 订阅（兼容） | POST | `/api/v1/ws/subscribe` | ✅ |
| WS 状态 | GET | `/api/v1/ws/status` | ✅ |
| 健康检查 | GET | `/health` 或 `/api/v1/health` | ❌ |

---

## 13. 变更历史

| 版本 | 变更 |
|------|------|
| v2 (2026-07-12) | 接入 JWT（user_id/room_id/role/jti）；加入鉴权、撤销、admin 角色下放、角色设置接口、middleware 实时同步状态 |
| v1 | 旧版无鉴权，user_id 即 username |