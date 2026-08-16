# SRS 聊天室服务 — 客户端联调手册 v2.1

> **版本**：v2.1（基于 `需求文档.md` 2026-08-15 修订版 + R1~R7 + 5 项审计修复）
> **服务地址（默认本地）**：HTTP `http://127.0.0.1:8085` ／ WebSocket `ws://127.0.0.1:8085/ws`
> **目标读者**：客户端工程师（开发、联调、QA、运维）
> **配套**：服务端需求文档 `需求文档.md`（同目录上层）

---

## 0. 阅读路径

| 阶段 | 章节 | 必读 |
|---|---|---|
| ① 跑通 | §1 总览 + §15 一键脚本 | ✅ |
| ② 接入 | §2 鉴权 + §3 房间 + §4 进房 | ✅ |
| ③ 实时 | §5 room_socket + §6 notice_socket | ✅ |
| ④ 消息 | §7 消息 | ✅ |
| ⑤ 邀请 | §8 邀请码 + §9 邀请链路 + §10 通行码 | ✅ |
| ⑥ 运维 | §11 错误码 + §12 调试 + §13 常见坑 | ✅ |

---

## 1. 一分钟总览

| 项 | 说明 |
|---|---|
| 鉴权 | JWT（HS256），通过 `Authorization: Bearer <token>` 注入；`/api/v1/auth/test_login` 仅调试用（绕过业务后端） |
| Socket 拆分 | **room_socket**（8085，房间内事件）+ **notice_socket**（8090，跨房间事件）。**兼容期双通道推送**，客户端迁完可关闭 room_socket 的跨房间事件 |
| 房间 | 房主唯一（不可转让）；成员可多房间并存；**无 `/leave` 接口**（离开=关闭 WS）；房主房间不自动删除 |
| 邀请（R1/R3） | **邀请码**（一次性 600s 兑换凭证）+ **通行码**（兑换后服务端保存，30 天有效）；`join` 时**无需客户端再传邀请码** |
| 邀请（R4） | **邀请链接**（一次性 token，仅 10min 有效）；持有链接 → 兑换通行码 → 加入房间 |
| 单设备互斥（R7.3） | 同用户同房间 1 条活跃 WS；新设备会收到 409，需等旧连接心跳超时（3×30s） |
| 多设备同步（R7.2） | 新设备登录后**必调** `GET /api/v1/me/rooms` 拉全部房间列表 |
| 消息 | HTTP `/api/v1/messages/send` 或 WS `chat_message`；`client_msg_id` 必须全局唯一（幂等） |
| 限流 | 邀请 1 次/30s；敲门 1 次/30s + 同房间 3 次/小时；邀请码校验 5 次失败锁定 5 分钟 |
| 错误格式 | `{"code": <int>, "message": "<str>"}`；`code=0` 成功；`data` 主响应体 |

---

## 2. 鉴权与登录

### 2.1 🎯 调试 token（绕过业务后端）

```bash
curl -X POST http://127.0.0.1:8085/api/v1/auth/test_login \
  -H "Content-Type: application/json" \
  -d '{"user_name":"alice", "role":"owner", "bus_id":"990001"}'
```

响应：
```json
{
  "code": 0,
  "data": {
    "token": "eyJhbGciOiJIUzI1NiIs...",
    "user_id": "user_a1b2c3",
    "user_name": "alice",
    "room_id": "",
    "bus_id": "990001",
    "role": "owner"
  }
}
```

> ⚠️ **生产禁用**：`test_login` 不验签业务后端，**绝不能**出现在线上环境。

### 2.2 真实三方登录（业务后端接入）

```bash
curl -X POST http://127.0.0.1:8085/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"external_token":"<业务后端签发的 JWT>"}'
```

响应结构同上。`external_token` 校验通过后服务端视情况创建用户并签发内部 `token`。

### 2.3 撤销 token

```bash
# 撤销自己全部 token
curl -X POST http://127.0.0.1:8085/api/v1/auth/logout-all \
  -H "Authorization: Bearer <JWT>"
```

### 2.4 token 注入规则

所有非白名单接口必须带 `Authorization: Bearer <JWT>`。白名单（无需 token）：

```
POST /api/v1/auth/login
POST /api/v1/auth/test_login
POST /api/v1/users/resolve
GET  /api/v1/health
GET  /api/v1/metrics
```

---

## 3. 房间生命周期

### 3.1 创建房间

```bash
curl -X POST http://127.0.0.1:8085/api/v1/room \
  -H "Authorization: Bearer <OWNER>" \
  -H "Content-Type: application/json" \
  -d '{"room_id":"r_team_001", "room_name":"项目组"}'
```

**重要**：
- 房主 **唯一不可转让**；同一用户已有 ACTIVE 房间时，**新创建会覆盖**该房间（`overwritten:true`）
- 关闭房间后房主可重新创建同名/同 id 房间

### 3.2 房间列表

```bash
# 全平台（服务端视角）
curl http://127.0.0.1:8085/api/v1/rooms

# 当前用户维度（R7.2 推荐，**新设备登录必调**）
curl http://127.0.0.1:8085/api/v1/me/rooms \
  -H "Authorization: Bearer <JWT>"
```

`me/rooms` 响应：
```json
{
  "code": 0,
  "data": {
    "count": 2,
    "rooms": [
      {"room_id":"r_team","room_name":"项目组","role":"owner","owner_id":"user_a","joined_at":"2026-08-15 22:00:00"},
      {"room_id":"r_party","room_name":"生日趴","role":"member","owner_id":"user_b","joined_at":"2026-08-15 23:00:00"},
      {"room_id":"r_x","room_name":"","role":"member","owner_id":"","pass_code_only":true,"joined_at":1786842974}
    ]
  }
}
```

> **注意**：`pass_code_only:true` 表示持有通行码但**尚未 join**（如刚兑换还没连 WS）。

### 3.3 房间详情

```bash
curl "http://127.0.0.1:8085/api/v1/room/{room_id}/info" \
  -H "Authorization: Bearer <JWT>"
```

返回 `room_id`、`owner_id`、`status`（active/closed）、`members` 列表、`online_count`。

### 3.4 关闭房间

```bash
curl -X DELETE http://127.0.0.1:8085/api/v1/room/{room_id} \
  -H "Authorization: Bearer <OWNER>"
```

**R5 副作用**：
- 房间状态 → `closed`
- 全员通行码 → `revoked`
- 广播 `room_closed` 通知
- `sync_client` 异步推送 `room_deleted` 到主服务

### 3.5 ❌ 已移除的接口

- `POST /api/v1/room/{id}/leave` — 离开 = 关闭 WS，**不提供 HTTP 撤出**
- `POST /api/v1/rooms/{id}/leave` — 同上

---

## 4. 加入房间（**R3/R4/R7**）

### 4.1 流程图

```
用户收到邀请       用户收到邀请链接
   (invite_code)    (token)
       │                  │
       ▼                  ▼
  ◄──────  [邀请者] 服务端自动生成 / 链接消费
       │
       ▼
 invite_received 事件
  / 客户端调 redeem
       │
       ▼
 ┌─────────────────────────┐
 │ /api/v1/invites/        │
 │   {id}/redeem           │  ← R4 邀请记录兑换
 │   │                     │
 │   code/redeem 或        │  ← R3 通用码兑换
 │   link/consume          │  ← R4 邀请链接兑换
 └─────────────────────────┘
       │
       ▼  服务端生成 active 通行码（30 天）
       │
       ▼
  POST /api/v1/room/{id}/join
       │
       ▼
  服务端查 (user_id, room_id) 是否有 active 通行码
       │
       ├── 是 → 200 加入
       └── 否 → 403 pass code not found or inactive
```

### 4.2 join 房间（核心）

```bash
curl -X POST http://127.0.0.1:8085/api/v1/room/{room_id}/join \
  -H "Authorization: Bearer <JWT>" \
  -H "Content-Type: application/json" \
  -d '{"role":"member"}'
```

**校验规则**：
| 角色 | 是否需要通行码 |
|---|---|
| owner | ❌ 免码（自己是房主） |
| admin | ❌ 免码 |
| member | ✅ 需 active |
| guest | ✅ 需 active |

**响应**：
```json
{
  "code": 0,
  "data": {
    "user_id": "user_a1b2c3",
    "room_id": "r_team_001",
    "role": "member",
    "status": "normal",
    "publish_allowed": true,
    "joined_at": "2026-08-15 22:30:00",
    "online_status": "offline",
    "offline_at": null
  }
}
```

### 4.3 R7.3 单设备互斥

```bash
# 同一用户同房间已有活跃 WS 时，再 join → 409
{
  "code": 409,
  "message": "user already active in this room on another device"
}
```

**客户端处理建议**：
1. 收到 409 弹 Toast："该账号已在另一设备登录"
2. 不自动重试；让用户主动选择关闭旧连接或取消加入

### 4.4 错误码速查

| 错误 | 原因 |
|---|---|
| `403 pass code not found or inactive` | 用户未兑换邀请码/通行码已失效 |
| `400 room is closed` | 房间已关闭 |
| `409 user already active in this room on another device` | R7.3 单设备互斥 |
| `400 user_id must match current user` | body 里的 user_id 与 JWT 不符 |

---

## 5. room_socket（房间内事件）

### 5.1 连接

```javascript
const ws = new WebSocket(
  `ws://127.0.0.1:8085/ws?room=${roomId}&user=${userId}`,
  [`jwt.${token}`]      // subprotocol 传 JWT
);
```

> **鉴权**：JWT 通过 `subprotocols[0]` 传入，**失败立即 close 1008**。

### 5.2 客户端 → 服务端

#### 5.2.1 聊天消息

```json
{
  "type": "chat_message",
  "client_msg_id": "u-2026-08-15-msg-001",
  "data": {
    "content": "你好",
    "content_type": "text",
    "extra": {}
  }
}
```

- `client_msg_id` 全局唯一（建议 UUID 或 `u-{user}-{ms}-{seq}`）；**重复会幂等返回**
- `content_type`: `text`(默认) / `image` / `file` / `audio` / `video`

#### 5.2.2 拉历史

```json
{
  "type": "history_sync",
  "data": {"limit": 50, "before_seq": null}
}
```

响应：
```json
{
  "type": "history_sync",
  "data": {
    "messages": [
      {"seq": 100, "user_id":"user_a", "content":"hi", "created_at":1786842974, "client_msg_id":"u-1"},
      ...
    ],
    "has_more": false
  }
}
```

#### 5.2.3 心跳

```json
{"type": "pong"}
```

> 服务端会发 `{"type":"ping"}`，**客户端必须回 pong**；3 次未回视为掉线。

### 5.3 服务端 → 客户端

| `type` | 触发 | `data` 关键字段 |
|---|---|---|
| `chat_message` | 收到其他人消息 | `user_id`, `content`, `seq`, `client_msg_id` |
| `member_joined` | 成员加入 | `user_id`, `role` |
| `member_offline` | 心跳超时 | `user_id`, `offline_at` |
| `member_kicked` | 被踢 | `user_id`, `operator_id` |
| `online_status_changed` | 上下线 | `user_id`, `online_status` |
| `room_mute_changed` | 全员禁言 | `allow_speak`, `operator_id` |
| `room_closed` | 房间关闭 | `room_id`, `operator_id` |
| `room_invite` | 被邀请（兼容） | `invite_id`, `invite_code`（可能为空） |
| `role_changed` | 角色变更 | `user_id`, `new_role` |
| `notification` | 兼容旧通知 | `notification_id`, `kind` |

**所有 WS 事件带 `server_ts` 字段**（time.time() 整数），可用于去重。

### 5.4 鉴权失败

```javascript
ws.onclose = (e) => {
  if (e.code === 1008) { console.log("token invalid"); }
};
```

---

## 6. notice_socket（跨房间事件）

> 浏览器访问速度快、加 WS 频繁，**建议客户端实现实时推送独立通道**。

### 6.1 连接

```javascript
const ws = new WebSocket(
  `ws://127.0.0.1:8090/ws/notice`,
  [`jwt.${token}`]
);
```

| 注意 | 说明 |
|---|---|
| 端口 | **8090**（不是 8085） |
| 鉴权 | 同 room_socket |
| 鉴权失败 | 立即 close 4001 |

### 6.2 推送事件

| `type` | 触发 | 本地路由 |
|---|---|---|
| `invite_received` | 被邀请 | 弹邀请卡片 |
| `invite_accepted` | 邀请被接受 | 通知发起人 |
| `member_kicked` | 被踢 | 提示并断 WS |
| `room_closed` | 房间关闭 | 提示并断 WS |
| `notification` | 私信/通知 | 通知中心 |
| `heartbeat` | 30s 一次 | 心跳可视化 |

### 6.3 离线重传

```bash
# 拉取上次同步后错过的通知
curl "http://127.0.0.1:8090/internal/sync?user_id={user_id}&since_ts={ts}" \
  -H "Authorization: Bearer <JWT>"
```

### 6.4 兼容期策略

- room_socket 仍推送 `invite_received` / `notification`（标记 deprecated）
- 新客户端**优先用 notice_socket**
- 老客户端**继续用 room_socket** 不受影响

---

## 7. 消息

### 7.1 HTTP 发送（推荐）

```bash
curl -X POST "http://127.0.0.1:8085/api/v1/messages/send?room_id=r_team_001" \
  -H "Authorization: Bearer <JWT>" \
  -H "Content-Type: application/json" \
  -d '{
    "client_msg_id": "u-2026-08-15-001",
    "type": "text",
    "content": "hello",
    "extra": {}
  }'
```

**响应**：
```json
{
  "code": 0,
  "data": {
    "seq": 102,
    "client_msg_id": "u-2026-08-15-001",
    "duplicate": false,
    "server_ts": 1786842974
  }
}
```

### 7.2 历史消息

```bash
# 最近 50 条
curl "http://127.0.0.1:8085/api/v1/messages/history?room_id=r_team_001&limit=50" \
  -H "Authorization: Bearer <JWT>"

# 增量（before_seq 不含）
curl "http://127.0.0.1:8085/api/v1/messages/history?room_id=r_team_001&before_seq=100&limit=50" \
  -H "Authorization: Bearer <JWT>"
```

### 7.3 重要规则

| 规则 | 说明 |
|---|---|
| `room_id` 在 **query** | 路径设计如此 |
| `client_msg_id` **全局唯一** | 重复会幂等，返回原 `seq` |
| `seq` 递增 | 同房间内严格递增 |
| 自己消息不通过 WS 给自己 | 减少回声；客户端用 `seq` 关联 |
| 失败重试 | 用同一 `client_msg_id` 即可 |

---

## 8. 邀请码（R1）

### 8.1 定向邀请（推荐交互）

```bash
# 邀请者 → 服务端自动生成邀请码 + 推送 invite_received
curl -X POST http://127.0.0.1:8085/api/v1/invite \
  -H "Authorization: Bearer <INVITER>" \
  -H "Content-Type: application/json" \
  -d '{"room_id":"r_team_001", "invitee_id":"user_b1c2d3", "message":"来加入项目组"}'
```

**响应**：
```json
{
  "code": 0,
  "data": {
    "id": "inv_a1b2c3d4e5",
    "invite_code": "ABCD1234EF",
    "expires_at": 1786843574
  }
}
```

被邀请者通过 **notice_socket** 收到 `invite_received` 事件（数据一致）。

### 8.2 通用邀请码（分享用）

```bash
# 房主生成通用码（绑定房间，不绑定用户）
curl -X POST "http://127.0.0.1:8085/api/v1/invite/code/generate?room_id=r_team_001&expire_seconds=600" \
  -H "Authorization: Bearer <OWNER>"
```

**响应**：
```json
{
  "code": 0,
  "data": {"code": "XY7Z9ABCDE", "expires_at": 1786843574}
}
```

### 8.3 邀请码状态

```bash
curl "http://127.0.0.1:8085/api/v1/invite/code/list?room_id=r_team_001&status=unused" \
  -H "Authorization: Bearer <OWNER>"
```

### 8.4 撤销邀请码

```bash
curl -X DELETE "http://127.0.0.1:8085/api/v1/invite/code/revoke?code=XY7Z9ABCDE" \
  -H "Authorization: Bearer <OWNER>"
```

### 8.5 限流

- 每用户发送邀请：**1 次/30s**（HTTP 429）
- 邀请码校验失败：**5 次/码 → 锁定 5 分钟**

---

## 9. 邀请链路（R4）

### 9.1 房主生成邀请链接

```bash
curl -X POST "http://127.0.0.1:8085/api/v1/invites/link/generate?room_id=r_team_001&expire_seconds=600" \
  -H "Authorization: Bearer <OWNER>"
```

**响应**：
```json
{
  "code": 0,
  "data": {
    "link": "http://127.0.0.1:8085/room_invite/abc123token",
    "token": "abc123token",
    "expires_at": 1786843574,
    "room_id": "r_team_001"
  }
}
```

> 链接是 **一次性**：消费后立即失效。

### 9.2 用户消费链接

```bash
curl -X POST http://127.0.0.1:8085/api/v1/invites/link/consume \
  -H "Authorization: Bearer <USER>" \
  -H "Content-Type: application/json" \
  -d '{"link_token":"abc123token"}'
```

**响应**：
```json
{
  "code": 0,
  "data": {
    "room_id": "r_team_001",
    "expires_at": 1789435239,
    "pass_code": "ABCDEF1234567890"
  }
}
```

### 9.3 微信/H5 适配

```html
<!-- 仿微信：链接落地页自动跳 App -->
<script>
  const token = new URLSearchParams(location.search).get('token');
  if (token) {
    // 调 App Scheme；失败降级到登录页
    window.location = `myapp://invite?token=${token}`;
  }
</script>
```

---

## 10. 通行码（R3）

### 10.1 概念

- **邀请码**：一次性凭证（10 分钟），用于兑换
- **通行码**：兑换后服务端保存，**30 天有效**，可复用
- 客户端无需主动管理通行码——查/撤销都发生在服务端

### 10.2 兑换邀请记录（R4）

```bash
curl -X POST http://127.0.0.1:8085/api/v1/invites/{invitation_id}/redeem \
  -H "Authorization: Bearer <USER>"
```

**响应**：
```json
{
  "code": 0,
  "data": {
    "room_id": "r_team_001",
    "expires_at": 1789435239,
    "pass_code": "ABCDEF1234567890"
  }
}
```

### 10.3 兑换通用邀请码（R3）

```bash
curl -X POST http://127.0.0.1:8085/api/v1/invites/code/redeem \
  -H "Authorization: Bearer <USER>" \
  -H "Content-Type: application/json" \
  -d '{"invite_code":"XY7Z9ABCDE"}'
```

### 10.4 兑换邀请链接

见 §9.2。

### 10.5 错误码

| 错误 | 原因 |
|---|---|
| `404 invitation not found` | 邀请记录不存在 |
| `403 not your invitation` | 邀请的 `invitee_id` ≠ 当前用户 |
| `400 invitation status is accepted, not pending` | 已兑换过 |
| `403 invalid invite code: ...` | 码无效/已用/过期 |
| `403 invite code bound to another user` | 该码绑定他人 |

### 10.6 联调建议

> **客户端无需感知通行码**。流程是：
> 1. 收到邀请事件（任意通道）
> 2. 调对应 `/redeem` → 服务端生成通行码
> 3. 调 `/join` → 服务端校验通行码
>
> 失败的话是 403，可重试（重新兑换）或引导用户重新获取邀请

---

## 11. 错误码速查（**建议客户端全文收录**）

### 11.1 通用错误

| code | message | 客户端处理 |
|---|---|---|
| 0 | `success` | OK |
| 400 | `xxx required` | 检查 body / query 参数 |
| 401 | `not authenticated` | token 过期或缺失 |
| 403 | `xxx forbidden` | 权限不足 |
| 404 | `not found` | 资源不存在 |
| 409 | `user already active in this room on another device` | R7.3 互斥 |
| 429 | `too many xxx, retry in Ns` | 限流，N 秒后重试 |
| 500 | `server error` | 通用服务端错误 |

### 11.2 业务错误

| 错误 message | 含义 | 客户端处理 |
|---|---|---|
| `token expired` | JWT 过期 | 重新登录 |
| `token invalid` | JWT 签名错误 | 重新登录 |
| `room not found` | 房间 id 不存在 | 提示用户 |
| `room is closed` | 房间已关闭 | 提示并解除 UI |
| `invite code not found or not unused` | 邀请码无效/已用 | 重新获取 |
| `invite code bound to another user` | 邀请码绑定他人 | 提示 |
| `invite code locked` | 校验失败超限被锁 | 5 分钟后再试 |
| `invitation not found` | 邀请记录不存在 | 提示 |
| `invitation status is X, not pending` | 邀请已处理 | 查状态 |
| `not your invitation` | 邀请归属错误 | 提示 |
| `pass code not found or inactive` | 通行码失效 | 重新兑换 |
| `user already active in this room on another device` | R7.3 | 弹提示 |
| `too many invites, retry in 30s` | 邀请限流 | 30s 重试 |
| `too many knocks, retry in 30s` | 敲门限流 | 30s 重试 |

### 11.3 WS 错误

| 错误 | code | 处理 |
|---|---|---|
| 鉴权失败 | 1008 / 4001 | 重新登录 |
| 心跳超时 | 1006 | 自动重连 |
| 服务端关闭 | 1011 | 等 1s 重连 |

---

## 12. 调试工具

### 12.1 Service Health

```bash
curl http://127.0.0.1:8085/api/v1/health
# {"status":"ok","ts":...}
```

### 12.2 在线状态

```bash
curl "http://127.0.0.1:8085/api/v1/users/online?room_id=r_team_001" \
  -H "Authorization: Bearer <JWT>"
```

### 12.3 解析业务用户

```bash
# bus_id → user_id 映射（无需鉴权）
curl -X POST http://127.0.0.1:8085/api/v1/users/resolve \
  -H "Content-Type: application/json" \
  -d '{"bus_id":"990001"}'
```

### 12.4 指标（**Prometheus 格式**）

```bash
curl http://127.0.0.1:8085/api/v1/metrics
```

提供以下指标：
- `ws_connect_total`、`ws_disconnect_total`、`ws_active_connections`
- `msg_send_total`、`msg_idempotent_hits_total`
- `notif_total`、`notif_push_success_total`、`notif_push_offline_total`
- `invite_total`、`invite_valid_fail_total`
- `notif_push_latency_ms{avg,p50,p95}`
- `join_latency_ms{avg,p50,p95}`

### 12.5 看日志

```bash
# 实时跟服务端日志
tail -f srs/callback_server.log | grep -E "(API|Invite|WS)"

# 审计日志（关键操作）
tail -f srs/audit.log

# 关注特定用户
grep -E "user_a1b2c3" srs/callback_server.log
```

### 12.6 看持久化

```bash
# 邀请码
mysql -uroot -e "SELECT code, room_id, status, target_user_id FROM chat_room.invite_codes WHERE code='ABC123';"

# 通行码
mysql -uroot -e "SELECT id, user_id, room_id, status FROM chat_room.pass_codes WHERE user_id='user_xyz';"

# 邀请记录
mysql -uroot -e "SELECT id, from_user_id, to_user_id, status, invite_code FROM chat_room.invite_records WHERE to_user_id='user_xyz';"

# 房间
mysql -uroot -e "SELECT room_id, room_name, owner_id, status FROM chat_room.rooms;"

# 用户
mysql -uroot -e "SELECT user_id, username, role FROM chat_room.chat_user WHERE username='alice';"
```

### 12.7 服务启停

```bash
cd srs
bash restart_all.sh restart    # 全量重启
bash restart_all.sh status     # 查看状态
```

---

## 13. 常见坑（FAQ）

### 13.1 `send_message` 返回 `room_id required (query param)`

**room_id 在 query string，不在 body 里**：

```bash
# ❌ 错误
curl -X POST /api/v1/messages/send -d '{"room_id":"...","content":"hi"}'

# ✅ 正确
curl -X POST "/api/v1/messages/send?room_id=..." -d '{"content":"hi"}'
```

### 13.2 `join` 房间返回 `pass code not found or inactive`

正确流程：先 redeeem → 再 join。

```bash
# ❌ 错误：直接 join
curl -X POST /api/v1/room/r1/join ...

# ✅ 正确
curl -X POST /api/v1/invites/$INVITE_ID/redeem ...    # 先兑换
curl -X POST /api/v1/room/r1/join ...                  # 再 join
```

### 13.3 WS 一直断、提示 `member_offline`

99% 是心跳没回。检查：
1. 是否处理了 `type=ping` 回 `pong`
2. nginx 侧 `proxy_read_timeout` ≥ 服务端 `HEARTBEAT_INTERVAL × HEARTBEAT_FAIL_THRESHOLD`（默认 30s × 3 = 90s）

### 13.4 `resolve` 返回 `user not found`

业务后端用户首次登录还没落库；让用户先走一次 `/api/v1/auth/login` 完成三方认证，再 resolve。

### 13.5 消息发不出去（400 / 403）

- 400：通常 `client_msg_id` 缺失，或 body JSON 解析失败
- 403：被全员禁言（`room_mute_changed`=`allow_speak=false` 且你是普通成员）
- 403：你在黑名单（被踢后通行码也被撤销，必须重新获取）

### 13.6 token 经常 401

- 检查服务器时钟：`date`，偏差 ±30s 内才稳（`JWT_LEEWAY_SECONDS`）
- 检查是否调过 logout：撤销表里的 jti 无法复用

### 13.7 新设备登录收不到房间

新设备登录后**必须主动调** `GET /api/v1/me/rooms` 拉房间列表。服务端不主动推送。

### 13.8 同一账号两台设备加入同一房间

触发 **R7.3 单设备互斥**：后加入的设备会收到 409。等旧设备心跳超时（3 × 30s = 90s）或主动关闭旧设备的 WS 后，新设备才能加入。

### 13.9 邀请码被锁定

校验失败 5 次后该码被锁 5 分钟。**不要循环重试同一个码**——前端应该重置输入框让用户重新获取。

### 13.10 invite_received 收不到

1. 检查 notice_socket 是否连接（8090 端口 + `jwt.{token}` subprotocol）
2. 检查目标用户是否在线 — 离线事件会缓存，**上线后通过 `internal/sync` 拉取**
3. 检查房间是否还在邀请范围内（用户没被踢）

### 13.11 房间成员显示缺失

`get_room_info` 只返回在 `room.members` 里的成员。**已经 redeem 但还没 join** 的成员**不会出现**在成员列表中——只出现在发送方的 `pass_code_only:true` 列表里。

### 13.12 多次 join 报错

只有第一次 join 会改 room.members；后续重复 join 是 **幂等** 的（返回当前 user 对象）。**但** R7.3 WS 互斥会拦截二次 join。

---

## 14. 已知约束

- **用户表**：MySQL 主存储 + `users.json` 兜底；不要直接编辑 `users.json`（会被下次同步覆盖）
- **邀请码**：Redis 主存 + 进程内兜底
- **通行码**：服务端 `pass_codes` 表保存（MySQL）；删 Redis 缓存后仍以 DB 为准
- **房间/成员/消息**：纯内存（`user_manager.json`/`messages.json`），重启会丢；如需持久化请走 MySQL（规划中）
- **WS 多实例**：当前单进程；多实例需要 Redis pub/sub 广播（规划中）
- **限流**：进程内（单实例）；多实例需要 Redis 原子 incr

---

## 15. 一键 10 分钟跑通脚本

```bash
#!/bin/bash
set -e
BASE=http://127.0.0.1:8085
RID="r_$(date +%s)"

# ============================================
# Step 1: 三个 token（owner / member / knocker）
# ============================================
TOK_O=$(curl -s -X POST $BASE/api/v1/auth/test_login \
  -H "Content-Type: application/json" \
  -d '{"user_name":"o","role":"owner","bus_id":"990001"}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['data']['token'])")

TOK_M=$(curl -s -X POST $BASE/api/v1/auth/test_login \
  -H "Content-Type: application/json" \
  -d '{"user_name":"m","role":"member","bus_id":"990002"}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['data']['token'])")
M_ID=$(curl -s -X POST $BASE/api/v1/auth/test_login \
  -H "Content-Type: application/json" \
  -d '{"user_name":"m","role":"member","bus_id":"990002"}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['data']['user_id'])")

TOK_K=$(curl -s -X POST $BASE/api/v1/auth/test_login \
  -H "Content-Type: application/json" \
  -d '{"user_name":"k","role":"member","bus_id":"990003"}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['data']['token'])")
K_ID=$(curl -s -X POST $BASE/api/v1/auth/test_login \
  -H "Content-Type: application/json" \
  -d '{"user_name":"k","role":"member","bus_id":"990003"}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['data']['user_id'])")

echo "==> owner / member($M_ID) / knocker($K_ID) tokens ready"

# ============================================
# Step 2: Owner 建房间
# ============================================
echo "==> [1] Owner 建房间 $RID"
curl -s -X POST $BASE/api/v1/room -H "Authorization: Bearer $TOK_O" \
  -H "Content-Type: application/json" -d "{\"room_id\":\"$RID\"}" \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print('   code=',d['code'],'overwritten=',d['data'].get('overwritten'))"

# ============================================
# Step 3: Owner 定向邀请 member
# ============================================
echo "==> [2] Owner 邀请 M（自动生成邀请码 + 推送 invite_received）"
sleep 31  # 限流冷却
INVITE_RESP=$(curl -s -X POST $BASE/api/v1/invite -H "Authorization: Bearer $TOK_O" \
  -H "Content-Type: application/json" \
  -d "{\"room_id\":\"$RID\",\"invitee_id\":\"$M_ID\",\"message\":\"hi\"}")
echo "   $INVITE_RESP"
INVITE_ID=$(echo "$INVITE_RESP" | python3 -c "import sys,json;print(json.load(sys.stdin)['data']['id'])")
INVITE_CODE=$(echo "$INVITE_RESP" | python3 -c "import sys,json;print(json.load(sys.stdin)['data']['invite_code'])")

# ============================================
# Step 4: M 兑换邀请 → 通行码存服务端
# ============================================
echo "==> [3] M 兑换邀请 $INVITE_ID"
sleep 31
curl -s -X POST $BASE/api/v1/invites/$INVITE_ID/redeem \
  -H "Authorization: Bearer $TOK_M" \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print('   code=',d['code'],'pass_code=',d['data'].get('pass_code'))"

# ============================================
# Step 5: M 加入房间（不带邀请码）
# ============================================
echo "==> [4] M 加入房间"
curl -s -X POST $BASE/api/v1/room/$RID/join -H "Authorization: Bearer $TOK_M" \
  -H "Content-Type: application/json" -d '{"role":"member"}' \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print('   code=',d['code'],'role=',d['data'].get('role'))"

# ============================================
# Step 6: me/rooms（M 看到自己在 $RID 是 member）
# ============================================
echo "==> [5] M 拉 me/rooms"
curl -s $BASE/api/v1/me/rooms -H "Authorization: Bearer $TOK_M" \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print('   rooms=',[(r['room_id'],r['role']) for r in d['data']['rooms']])"

# ============================================
# Step 7: Owner 发消息 + M 拉历史
# ============================================
echo "==> [6] Owner 发消息"
curl -s -X POST "$BASE/api/v1/messages/send?room_id=$RID" \
  -H "Authorization: Bearer $TOK_O" -H "Content-Type: application/json" \
  -d '{"client_msg_id":"u-1","type":"text","content":"hi m"}' \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print('   code=',d['code'],'seq=',d['data'].get('seq'))"

echo "==> [7] M 拉历史"
curl -s "$BASE/api/v1/messages/history?room_id=$RID&limit=10" \
  -H "Authorization: Bearer $TOK_M" \
  | python3 -c "import sys,json;d=json.load(sys.stdin);ms=d['data'].get('messages',[]);print('   count=',len(ms),'first=',ms[0]['content'] if ms else None)"

# ============================================
# Step 8: K 敲门
# ============================================
echo "==> [8] K 敲门"
sleep 31
curl -s -X POST $BASE/api/v1/room/$RID/knock -H "Authorization: Bearer $TOK_K" \
  -H "Content-Type: application/json" -d '{"message":"求加入"}' \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print('   code=',d['code'])"

# ============================================
# Step 9: Owner 接受敲门
# ============================================
echo "==> [9] Owner 接受敲门"
curl -s -X POST $BASE/api/v1/room/$RID/knock/accept -H "Authorization: Bearer $TOK_O" \
  -H "Content-Type: application/json" \
  -d "{\"knocker_id\":\"$K_ID\",\"role\":\"member\"}" \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print('   code=',d['code'])"

# ============================================
# Step 10: K 必须 join 才能算成员
# ============================================
echo "==> [10] K join"
curl -s -X POST $BASE/api/v1/room/$RID/join -H "Authorization: Bearer $TOK_K" \
  -H "Content-Type: application/json" -d '{"role":"member"}' \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print('   code=',d['code'])"

# ============================================
# Step 11: 新设备登录 → 拉房间列表（R7.2）
# ============================================
echo "==> [11] M 新设备登录并拉 me/rooms"
sleep 31
TOK_M2=$(curl -s -X POST $BASE/api/v1/auth/test_login \
  -H "Content-Type: application/json" \
  -d '{"user_name":"m","role":"member","bus_id":"990002"}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['data']['token'])")
curl -s $BASE/api/v1/me/rooms -H "Authorization: Bearer $TOK_M2" \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print('   rooms=',[(r['room_id'],r['role']) for r in d['data']['rooms']])"

# ============================================
# Step 12: Owner 关闭房间（级联撤销通行码 + room_closed）
# ============================================
echo "==> [12] Owner 关闭房间"
curl -s -X DELETE $BASE/api/v1/room/$RID -H "Authorization: Bearer $TOK_O" \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print('   code=',d['code'])"

# ============================================
# Step 13: M 重新 join 应 403（pass_code revoked）
# ============================================
echo "==> [13] M 重新 join（应 403）"
curl -s -X POST $BASE/api/v1/room/$RID/join -H "Authorization: Bearer $TOK_M" \
  -H "Content-Type: application/json" -d '{"role":"member"}' \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print('   code=',d['code'],'msg=',d.get('message'))"

echo ""
echo "==> ALL DONE"
```

---

## 16. 版本演进

| 版本 | 日期 | 变更 |
|---|---|---|
| v1.0 | 2026-08-13 | 初始版（基于 P0 需求） |
| v2.0 | 2026-08-15 | R1~R7 全面接入：邀请码 + 通行码 + 邀请链接 + 单设备互斥 + 多设备同步 |
| v2.1 | 2026-08-16 | 5 项审计修复：kick 撤销通行码、knock_accept 走通行码、邀请限流、pass_code 性能优化、文档同步 |

---

## 17. 反馈与异常上报

- **联调问题**：附 `client_msg_id` / `room_id` / `user_id` / `correlation_id`(JWT jti)
- **服务端指标**：`curl /api/v1/metrics` → 截屏
- **日志查询**：`grep -E "user_id|<JWT-jti>" srs/callback_server.log`
- **审计日志**：`srs/audit.log` 关键操作追踪

> 服务端日志保留 7 天；MySQL 数据按业务需要保留。
