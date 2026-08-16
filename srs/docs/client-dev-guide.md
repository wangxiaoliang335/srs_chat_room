# SRS 聊天室服务 — 客户端开发调试手册

> 文档版本：v2.0（基于 `需求文档.md` 2026-08-15 修订版 + R1~R7 改进需求）
> 服务地址（默认本地）：`http://127.0.0.1:8085` / `ws://127.0.0.1:8085/ws`
> 目标读者：客户端工程师（开发、联调、QA）

---

## 1. 一分钟总览

| 项 | 说明 |
|---|---|
| 鉴权 | JWT（HS256），通过 `Authorization: Bearer <token>` 注入；`/api/v1/auth/test_login` 可绕过业务后端直接签发（仅调试） |
| Socket 拆分（R1） | **room_socket**（8085，房间内事件）+ **notice_socket**（独立端口，跨房间事件）。**兼容期双通道推送**，客户端迁完可关 room_socket 的跨房间事件 |
| 房间 | 房主唯一（不可转让）；成员可多房间并存；无 `/leave` 接口（离开=关闭 WS）；房主房间不自动删除 |
| 邀请（R3/R4） | **邀请码**（一次性兑换凭证，默认 600s）+ **通行码**（兑换后服务端保存，可复用）；`join` 时无需客户端再传邀请码，服务端查通行码 |
| 单设备互斥（R7.3） | 同用户同房间只能 1 条活跃 WS 连接；另一设备要等旧连接心跳超时（3 次 ping）后才可加入 |
| 多设备同步房间（R7.2） | 新设备登录后调 `GET /api/v1/me/rooms` 拉全部房间列表 |
| 消息 | HTTP `/api/v1/messages/send` 或 WS `chat_message`；**`client_msg_id` 必须全局唯一**，幂等；room_id 在 **query** |
| 限流 | 邀请 1 次/30s；敲门 1 次/30s + 同房间 3 次/小时；邀请码校验 5 次/码失败锁定 5 分钟 |
| 错误格式 | 统一：`{"code": <int>, "message": "<str>"}`，`code=0` 为成功 |

---

## 2. 鉴权与登录

### 2.1 临时 token（仅调试）

```bash
curl -X POST http://127.0.0.1:8085/api/v1/auth/test_login \
  -H "Content-Type: application/json" \
  -d '{"user_name":"alice","app_id":"default","bus_id":"990001","role":"member"}'
```

**返回**

```json
{
  "code": 0,
  "message": "success",
  "data": {
    "token": "<JWT>",
    "user_id": "user_xxxxxxxxxxxx",
    "username": "alice",
    "role": "member",
    "room_id": null,
    "app_id": "default",
    "bus_id": "990001",
    "expires_at": 1786800000
  }
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `user_name` | ✅ | 显示名 |
| `user_id` | ❌ | 传了就复用已有记录，没传就新建 |
| `app_id` | ❌ | 默认 `default`，三方登录时用业务方标识 |
| `bus_id` | ❌ | 业务后端用户 id；非业务账号留空 |
| `role` | ❌ | `member` / `admin` / `owner`，调试时可以直接给 owner 测房主路径 |

> ⚠️ **生产部署务必把 `/api/v1/auth/test_login` 在反向代理层屏蔽！**

### 2.2 正式登录（业务后端三方）

```bash
curl -X POST http://127.0.0.1:8085/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"token":"<业务后端颁发>"}'
```

服务器用 `BUSINESS_APP_KEY` 调业务后端 `/api/frontend/app/external/users/me` 验证 `token`，查到业务用户后自动创建/复用本地账号，签发 JWT（7 天 TTL）。

### 2.3 验证 token

```bash
curl http://127.0.0.1:8085/api/v1/auth/me \
  -H "Authorization: Bearer <JWT>"
```

返回当前用户上下文。**401 = token 失效 / 被撤销**。

### 2.4 登出

```bash
curl -X POST http://127.0.0.1:8085/api/v1/auth/logout \
  -H "Authorization: Bearer <JWT>"
```

立即把当前 jti 加入撤销表。

---

## 3. 房间生命周期

### 3.1 创建房间

```bash
curl -X POST http://127.0.0.1:8085/api/v1/room \
  -H "Authorization: Bearer <owner-JWT>" \
  -H "Content-Type: application/json" \
  -d '{"room_id":"team_alpha","name":"团队 Alpha"}'
```

**关键规则**：
- 一个用户**只能是一个房间的房主**。再次调用不会创建新房间，而是**覆盖更新**现有房间（room_id 不变）
- `room_id` 是客户端定的；想完全换房间，关闭旧房间后重建
- **房主不可转让**——无 owner 变更接口

### 3.2 关闭房间（R5，仅房主）

```bash
curl -X DELETE http://127.0.0.1:8085/api/v1/room/team_alpha \
  -H "Authorization: Bearer <owner-JWT>"
```

关闭时级联：
- 该房间全部 `active` 通行码 → `revoked`
- 广播 `room_closed`（notice_socket + 兼容期 room_socket）
- 清理 Redis key（`room_online:*`、presence）

### 3.3 多设备同步房间列表（R7.2）

新设备登录后，**第一件事**就是拉这个：

```bash
curl http://127.0.0.1:8085/api/v1/me/rooms \
  -H "Authorization: Bearer <JWT>"
```

返回当前用户全部 `active` 通行码对应的房间列表（含 `room_id` / `room_name` / `role`）。新设备无需依赖旧设备导出。

---

## 4. 邀请 / 通行码 / 敲门（R3 + R4 重写）

> **核心模型变更（2026-08-15 文档 R3/R4）**：
> - **邀请码**：一次性兑换凭证，**默认 600s 过期**
> - **通行码**：兑换后生成，**绑定用户**、**服务端保存**、`active` 后长期有效
> - **join 时无需客户端再传邀请码**，服务端查「当前用户在该房间的 `active` 通行码」

### 4.1 房主生成邀请码（手动分享）

```bash
curl -X POST "http://127.0.0.1:8085/api/v1/invite/code/generate?room_id=team_alpha&target_user_id=&expire_seconds=600" \
  -H "Authorization: Bearer <owner-JWT>"
```

| 参数 | 必填 | 说明 |
|---|---|---|
| `room_id` | ✅ | 房间路径 |
| `target_user_id` | ❌ | 限定给某用户；空 = 通用码 |
| `expire_seconds` | ❌ | 默认 600s（10 分钟） |

返回 `{"code":0, "data":{"code":"ABC123XYZ","expires_at":..., ...}}`

限流：同用户 1 次/30s → 429。

### 4.2 房主定向发送邀请（自动带码 + 服务端暂存邀请记录）

```bash
curl -X POST http://127.0.0.1:8085/api/v1/invite \
  -H "Authorization: Bearer <owner-JWT>" \
  -H "Content-Type: application/json" \
  -d '{"room_id":"team_alpha","invitee_user_id":"user_xyz","message":"邀请你加入"}'
```

服务端**自动生成邀请码** + 建 `invite_record(status=pending, invite_code)` 暂存 → 经 `notice_socket` 推送 `invite_received`（**含邀请码**）给被邀请人。

被邀请人收到的事件：

```json
{
  "type": "invite_received",
  "data": {
    "id": "inv_xxx",           // 邀请记录 id
    "from_user_id": "user_owner",
    "room_id": "team_alpha",
    "message": "邀请你加入",
    "invite_code": "ABC123XYZ",
    "created_at": 1786761000
  }
}
```

### 4.3 兑换通行码（邀请码 → 通行码）

**两种方式，效果相同**：兑换成功后邀请码 → `used`，服务端生成绑定当前用户的通行码。

> 路径说明：本服务邀请类接口使用 `/api/v1/invites/*`（复数）。
> 旧的 `/api/v1/invite/{id}/...` 路由已迁移到 `/api/v1/invites/{id}/...` 避免与 `code/link` 子路径冲突。

**方式一：邀请兑换**（用邀请信息中的 id）

```bash
curl -X POST http://127.0.0.1:8085/api/v1/invites/<invite_id>/redeem \
  -H "Authorization: Bearer <member-JWT>"
# 返回 {"code":0,"data":{"room_id":"...","expires_at":..., "pass_code":"..."}}
```

**方式二：通用码兑换**（没有邀请关系，直接拿码）

```bash
curl -X POST http://127.0.0.1:8085/api/v1/invites/code/redeem \
  -H "Authorization: Bearer <member-JWT>" \
  -H "Content-Type: application/json" \
  -d '{"invite_code":"ABC123XYZ"}'
```

**校验顺序**：`码归属房间` → `码状态（未过期/未撤销/未使用）` → **`target_user_id` 为空或等于当前 user_id`**。

| 结果 | 状态码 / message |
|---|---|
| 兑换成功 | `200`，邀请码 → `used`，`invite_record.status=accepted`，通知房主 `invite_accepted`（notice_socket） |
| 绑定他人 | `403 invite code bound to another user` |
| 码无效 | `404 invite code not found or not unused` |
| 5 次校验失败 | 锁码 5 分钟 |

### 4.4 拒绝邀请

```bash
curl -X POST http://127.0.0.1:8085/api/v1/invites/<invite_id>/reject \
  -H "Authorization: Bearer <JWT>" \
  -H "Content-Type: application/json" \
  -d '{"reason":"不感兴趣"}'
```

→ `invite_record.status=rejected`，邀请码作废 → 通知房主 `invite_rejected`。

### 4.5 邀请链接（房间转发 → 通行码）

```bash
# 1) 房主生成链接
curl -X POST "http://127.0.0.1:8085/api/v1/invites/link/generate?room_id=team_alpha" \
  -H "Authorization: Bearer <owner-JWT>"
# 返回 {"code":0,"data":{"link":"https://.../room_invite/<token>","expires_at":...}}

# 2) 用户兑换链接（需带 JWT）
curl -X POST http://127.0.0.1:8085/api/v1/invites/link/consume \
  -H "Authorization: Bearer <member-JWT>" \
  -H "Content-Type: application/json" \
  -d '{"link_token":"<token>"}'
```

link token **一次性**、默认 10 分钟，存 Redis；consume 走 4.3 兑换逻辑。

### 4.6 加入房间（成员）

```bash
curl -X POST http://127.0.0.1:8085/api/v1/room/team_alpha/join \
  -H "Authorization: Bearer <member-JWT>" \
  -H "Content-Type: application/json" \
  -d '{"role":"member"}'
```

> **join 时无需带邀请码**——服务端按 `(user_id, room_id)` 查 `active` 通行码。

校验顺序（R7.1）：
1. owner → 免码放行
2. member：服务端查 `active` 通行码 → 无则 403 `pass code not found or inactive`
3. **单设备活跃互斥（R7.3）**：该用户在该房间已有活跃 WS → 409 `user already active in this room on another device`
4. 心跳超时后其他设备才可加入

### 4.7 敲门流程

```bash
# 1) 敲门
curl -X POST http://127.0.0.1:8085/api/v1/room/team_alpha/knock \
  -H "Authorization: Bearer <knocker-JWT>" \
  -H "Content-Type: application/json" \
  -d '{"message":"hi 想加入"}'

# 2) 房主同意（knocker 直接成为成员）
curl -X POST http://127.0.0.1:8085/api/v1/room/team_alpha/knock/accept \
  -H "Authorization: Bearer <owner-JWT>" \
  -H "Content-Type: application/json" \
  -d '{"knocker_id":"user_xyz","role":"member"}'

# 3) 房主拒绝
curl -X POST http://127.0.0.1:8085/api/v1/room/team_alpha/knock/reject \
  -H "Authorization: Bearer <owner-JWT>" \
  -H "Content-Type: application/json" \
  -d '{"knocker_id":"user_xyz","reason":"稍等"}'
```

推送：
- 敲门 → 房主收 `knock`（notice_socket，**R1 后**；兼容期也在 room_socket）
- 接受 → 敲门人收 `knock_result{accepted:true}`，**直接成为成员**
- 拒绝 → 敲门人收 `knock_result{accepted:false, reason}`

限流：1 次/30s + 同房间 3 次/小时。

### 4.8 邀请/通行码状态机

```
邀请码: unused ──兑换──▶ used        （兑换通行码成功）
        unused ──过期──▶ expired     （超 10 分钟）
        unused ──撤销──▶ revoked     （房主撤销 / 拒绝邀请）

通行码: active ──被踢──▶ revoked     （kick 级联）
        active ──房间关闭──▶ revoked  （R5 级联）
        active ──撤销──▶ revoked
        （未生效码超时 → expired）
```

---

## 5. 房主操作

```bash
# 改成员角色
curl -X POST http://127.0.0.1:8085/api/v1/room/team_alpha/member/<user_id>/role \
  -H "Authorization: Bearer <owner-JWT>" \
  -H "Content-Type: application/json" \
  -d '{"role":"admin"}'

# 禁言 / 解除
curl -X POST http://127.0.0.1:8085/api/v1/room/team_alpha/member/<user_id>/mute \
  -H "Authorization: Bearer <owner-JWT>" -H "Content-Type: application/json" -d '{}'

# 踢人（级联撤销该用户本房间 active 通行码）
curl -X DELETE http://127.0.0.1:8085/api/v1/room/team_alpha/member/<user_id>/kick \
  -H "Authorization: Bearer <owner-JWT>"

# 全员禁言 / 解除
curl -X POST http://127.0.0.1:8085/api/v1/room/team_alpha/mute-all \
  -H "Authorization: Bearer <owner-JWT>" -d '{}'
```

---

## 6. 消息收发

### 6.1 HTTP 发送（推荐）

```bash
curl -X POST "http://127.0.0.1:8085/api/v1/messages/send?room_id=team_alpha" \
  -H "Authorization: Bearer <JWT>" \
  -H "Content-Type: application/json" \
  -d '{
    "client_msg_id": "客户端生成的 UUID",
    "type": "text",
    "content": "大家好"
  }'
```

**必填**：
- `room_id`：**query 参数**（不在 body 里）
- `client_msg_id`：**全局唯一**（UUID v4），用于幂等
- `content`：消息内容
- `type`：`text` / `image` / `file`

**幂等**：相同 `client_msg_id` 在 10 分钟内重发，第二次返回原消息 + `_idempotent=true`。

### 6.2 WS 发送

```javascript
ws.send(JSON.stringify({
  type: "chat_message",
  client_msg_id: "uuid-xxx",
  type: "text",       // 消息类型
  content: "大家好"
}));
```

服务端立刻返回 `chat_message_ack`，消息广播到房间内所有 WS 客户端。

### 6.3 历史消息

```bash
curl -X GET "http://127.0.0.1:8085/api/v1/messages/history?room_id=team_alpha&after_seq=0&limit=50" \
  -H "Authorization: Bearer <JWT>"
```

- `after_seq`：0 = 从头；上次收到的最大 seq = 增量
- `limit`：1~200，默认 50

### 6.4 文件上传

```bash
curl -X POST "http://127.0.0.1:8085/api/v1/messages/upload?room_id=team_alpha&type=image" \
  -H "Authorization: Bearer <JWT>" \
  -F "file=@./photo.jpg"
```

**安全约束**：
- 图片 ≤ 10MB、文件 ≤ 100MB
- 扩展名 / MIME 白名单
- 服务端二次校验（不信任客户端 Content-Type）
- 私有存储 + 签名 URL（防越权访问）

上传成功返回 `file_id / url`，后续消息 `content` 字段写 URL 或 file_id。

---

## 7. Socket 协议（R1：双通道）

> **R1 改进**：跨房间事件（敲门、邀请、通知）迁入 `notice_socket`；房间内事件保留 `room_socket`。
> **兼容期**：双通道推送，客户端迁完后再关 room_socket 的跨房间事件。

### 7.1 room_socket（8085，房间内事件）

**连接**：`ws://{host}:8085/ws?room={room_id}&user={user_id}`（生产建议带 token）

**URL 参数**：
- `room`：房间 id（必填，否则 close 1008 "Missing room parameter"）
- `user`：用户 id（强烈建议传）

**鉴权**（推荐生产）：
```
Sec-WebSocket-Protocol: jwt, <token>
```
服务端解析第二个 token，做 JWT 校验。

**心跳**：服务端每 30s 主动 `ping`，客户端必须在 3 次内回 `pong`，否则被断开。

```javascript
ws.onmessage = (e) => {
  const msg = JSON.parse(e.data);
  if (msg.type === 'ping') {
    ws.send(JSON.stringify({type: 'pong'}));
  }
};
```

**客户端发送事件**：

```javascript
ws.send(JSON.stringify({type: "subscribe", room_id: "team_alpha"}));  // 切换房间
ws.send(JSON.stringify({type: "chat_message", client_msg_id: "u-1", type: "text", content: "hi"}));
ws.send(JSON.stringify({type: "history_sync", after_seq: 0, limit: 100}));
```

**服务端推送（房间内）**：

| 事件 | 触发 | payload |
|---|---|---|
| `chat_message` | 房间内任何人发消息 | `{type, room_id, id, seq, user_id, client_msg_id, content, ts, ...}` |
| `chat_message_ack` | 你发的消息被持久化 | `{type, id, seq, client_msg_id, _idempotent}` |
| `history_sync` | 你拉历史 | `{type, room_id, items[], latest_seq}` |
| `member_joined` | 新成员加入 | `{type, room_id, user_id, role, ts}` |
| `member_left` | 成员断开 WS（离线判定后） | `{type, room_id, user_id, ts, reason}` |
| `member_online_status_changed` | 在线/离线状态变更 | `{type, room_id, user_id, online_status, offline_at}` |
| `member_kicked` | 你被踢了 | `{type, room_id, user_id, operator_id}` |
| `role_changed` | 你的角色被改 | `{type, room_id, user_id, old_role, new_role, operator_id}` |
| `room_mute_changed` | 全员禁言状态变 | `{type, room_id, allow_speak, operator_id}` |
| `muted` / `unmuted` | 你被禁言 / 解除 | `{type, room_id, user_id, operator_id}` |
| `mic_disabled` / `mic_enabled` | 你被禁麦 / 解除 | 同上 |
| `speaking_start` / `speaking_stop` | 说话状态广播 | `{type, room_id, user_id, ts}` |
| `room_closed` | 房间被关闭 | `{type, room_id, operator_id}` |

> **R1 兼容期**：跨房间事件（`knock` / `knock_result` / `invite_*` / `notify`）**也走这里**，待客户端迁移完成后关闭。

### 7.2 notice_socket（独立端口，跨房间）

**连接**：`ws://{host}:{custom_port}/ws/notice?user={user_id}&token=<jwt>`

- 不依赖房间参数，按 `user_id` 订阅；**同一用户一条连接** 即可接收所有房间的跨房间事件
- 鉴权：JWT（query 或子协议）
- 心跳：沿用 ping/pong，3 次无响应判离线

**服务端推送（跨房间）**：

| 事件 | 触发 | payload |
|---|---|---|
| `invite_received` | 收到房间邀请 | `{type, data:{id, from_user_id, room_id, message, invite_code, created_at}}` |
| `invite_accepted` | 你发送的邀请被接受 | `{type, data:{id, room_id, by_user_id}}` |
| `invite_rejected` | 你发送的邀请被拒绝 | `{type, data:{id, room_id, by_user_id, reason}}` |
| `knock` | 有人敲门（推给房主） | `{type, data:{room_id, knocker_id, message}}` |
| `knock_result` | 敲门结果 | `{type, data:{room_id, accepted, role?, reason?}}` |
| `notify` | 通用通知 | `{type, data:{id, type, title, content, room_id, related_user_id, data, created_at}}` |
| `notification_sync` | 重连补推未读通知 | `{type, data:{unread_count, notifications[]}}` |
| `room_closed` | 房间关闭（跨房间） | `{type, data:{room_id, operator_id}}` |

**离线补投**：用户离线期间产生的通知只落库；`notice_socket` 重连后按 `created_at > 最后已读时间` 补推未读 + 刷新未读数。

### 7.3 通用事件结构

```json
{
  "event_id": "...",
  "type": "...",
  "room_id": "...",
  "user_id": "...",
  "operator_id": "...",
  "target_user_id": "...",
  "data": {...},
  "timestamp": 1786761000
}
```

---

## 8. 通知（站内信）

```bash
# 拉未读数
curl http://127.0.0.1:8085/api/v1/notifications/unread-count \
  -H "Authorization: Bearer <JWT>"

# 拉列表（时间倒序 + 分页）
curl http://127.0.0.1:8085/api/v1/notifications \
  -H "Authorization: Bearer <JWT>"

# 标记已读
curl -X POST http://127.0.0.1:8085/api/v1/notifications/<id>/read \
  -H "Authorization: Bearer <JWT>"

# 一键已读
curl -X POST http://127.0.0.1:8085/api/v1/notifications/read-all \
  -H "Authorization: Bearer <JWT>"

# 删除
curl -X DELETE http://127.0.0.1:8085/api/v1/notifications/<id> \
  -H "Authorization: Bearer <JWT>"
```

实时通知通过 notice_socket 事件 `notify` 推送。

**通知 type 枚举**：

| type | 触发时机 |
|---|---|
| `invite_received` | 收到房间邀请（含邀请码） |
| `invite_accepted` | 你发送的邀请被接受 |
| `invite_rejected` | 你发送的邀请被拒绝 |
| `knock_received` | 有人敲门 |
| `knock_accepted` | 敲门被接受 |
| `knock_rejected` | 敲门被拒绝 |
| `member_joined` | 新成员加入房间 |
| `member_offline` | 成员离线（原 `member_left` 改名） |
| `member_kicked` | 你被踢出房间 |
| `role_updated` | 你的角色被更改 |
| `room_closed` | 房间被关闭 |

**推送可靠性**：推送失败走失败队列 + 指数退避（1s/5s/30s）；目标离线则落库待重连补投。

---

## 9. 用户查询

```bash
# 按 user_id 查名字
curl http://127.0.0.1:8085/api/v1/users/<user_id>/name

# 批量按 user_id 查
curl -X POST http://127.0.0.1:8085/api/v1/users/names \
  -H "Authorization: Bearer <JWT>" \
  -H "Content-Type: application/json" \
  -d '{"ids":["user_aaa","user_bbb"]}'

# 按业务 bus_id 查
curl -X POST http://127.0.0.1:8085/api/v1/users/resolve \
  -H "Authorization: Bearer <JWT>" \
  -H "Content-Type: application/json" \
  -d '{"ids":["bus:990001","user_aaa"]}'

# 查用户当前在哪个房间
curl http://127.0.0.1:8085/api/v1/users/<user_id>/room
```

`resolve` 支持混合 ID：`bus:<业务id>`（按业务后端 id）、`user_<uuid>`（按本地 user_id）。

---

## 10. 错误码速查

| HTTP | code | 含义 |
|---|---|---|
| 200 | 0 | 成功 |
| 400 | 400 | 参数错误 |
| 401 | 401 | 未鉴权 / token 失效 / 撤销 |
| 403 | 403 | 越权（如非 owner 试图生成邀请码） |
| 404 | 404 | 资源不存在 |
| 409 | 409 | 冲突（如单设备互斥 `user already active in this room on another device`） |
| 429 | 429 | 限流（看 message：`retry in Ns`） |
| 500 | 500 | 服务器异常 |

**业务 message 关键字**：

| message | 场景 |
|---|---|
| `not authenticated` | 没带 Authorization |
| `token expired` | token 过期，重新登录 |
| `room not found` | 房间 id 不存在 |
| `invite code not found or not unused` | 邀请码无效或已用过 |
| `invite code bound to another user` | 邀请码绑定他人（R2 校验失败） |
| `pass code not found or inactive` | join 时未找到该用户该房间的 active 通行码 |
| `user already active in this room on another device` | 单设备互斥触发 |
| `too many invites, retry in 30s` | 邀请限流 |
| `too many knocks, retry in Ns` | 敲门限流 |
| `owner_id must match current user` | 越权 |

---

## 11. 调试工具 & 常用命令

### 11.1 实时指标

```bash
curl http://127.0.0.1:8085/api/v1/metrics | python3 -m json.tool
```

返回 counters / gauges / latencies。可看：消息吞吐、邀请码使用、限流命中、推送成功率、推送延迟（ms）等。

### 11.2 健康检查

```bash
curl http://127.0.0.1:8085/api/v1/health
# {"status":"ok"}
```

### 11.3 日志

```
srs/logs/server_fastapi.log    # 主服务
srs/logs/ws_server.log         # WS 服务
srs/audit.log                  # 审计日志（JSON Lines）
```

审计日志示例（`tail -f srs/audit.log`）：

```json
{"ts":1786760903,"iso":"2026-08-15T10:28:23","action":"room_created","actor_id":"user_f18c","target_id":"","room_id":"team_alpha","details":{"name":"team_alpha","overwritten":false}}
{"ts":1786761000,"iso":"2026-08-15T10:30:00","action":"invite_code_generated","actor_id":"user_f18c","target_id":"","room_id":"team_alpha","details":{"code":"ABC123","target_user_id":""}}
{"ts":1786761100,"iso":"2026-08-15T10:31:40","action":"invite_code_used","actor_id":"user_xyz","target_id":"","room_id":"team_alpha","details":{"code":"ABC123"}}
{"ts":1786761200,"iso":"2026-08-15T10:32:20","action":"room_closed","actor_id":"user_f18c","target_id":"","room_id":"team_alpha","details":{"reason":"owner_close"}}
```

可审计的 action：`room_created` / `room_closed` / `room_overwritten` / `member_joined` / `member_kicked` / `member_role_changed` / `member_muted` / `member_unmuted` / `room_mute_all` / `room_unmute_all` / `invite_code_generated` / `invite_code_used` / `invite_code_revoked` 等。

### 11.4 看 Redis 缓存

```bash
# 邀请码有效缓存
redis-cli get "invite:valid:ABC123"

# 通行码有效缓存（用户绑定的）
redis-cli get "pass:valid:user_xyz:team_alpha"

# 在线状态
redis-cli get "presence:user_xxx"

# 房间在线列表
redis-cli smembers "room_online:team_alpha"

# 邀请码校验失败计数（5 次锁码）
redis-cli get "invite_fail:ABC123"
```

### 11.5 看 MySQL

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

---

## 12. 常见坑

### 12.1 send_message 返回 `room_id required (query param)`

**room_id 在 query string，不在 body 里**：

```bash
# ❌
curl -X POST /api/v1/messages/send -d '{"room_id":"...","content":"hi"}'

# ✅
curl -X POST "/api/v1/messages/send?room_id=..." -d '{"content":"hi"}'
```

### 12.2 join 房间返回 `pass code not found or inactive`

新文档下 join **不再传邀请码**——服务端查通行码。流程应该是：

1. 用户通过邀请链接 / 邀请事件拿到邀请码
2. 调 `/api/v1/invites/<id>/redeem` 或 `/api/v1/invites/code/redeem` → 服务端生成通行码存服务端
3. 调 `/api/v1/room/<room>/join`（不带 invite_code）→ 服务端查通行码 → 放行

直接 join 没兑换过会失败。

### 12.3 ws 一直断、提示 `member_left`

99% 是心跳没回。检查：
1. 是否处理了 `type=ping` 回 `pong`
2. nginx 侧 `proxy_read_timeout` 是否 ≥ 服务端 `HEARTBEAT_INTERVAL` × `HEARTBEAT_FAIL_THRESHOLD`

### 12.4 resolve 返回 `user not found`

业务后端用户首次登录还没落库；让用户先走一次 `/api/v1/auth/login` 完成三方认证，再 resolve。

### 12.5 消息发不出去（400 / 403）

- 400：通常 `client_msg_id` 缺失，或 body JSON 解析失败
- 403：被全员禁言（`room_mute_changed`=`allow_speak=false` 且你是普通成员）
- 403：你在黑名单（被踢后没真正退出；被踢后通行码也被撤销，必须重新获取）

### 12.6 token 经常 401

- 检查服务器时钟：`date`，偏差 ±30s 内才稳（`JWT_LEEWAY_SECONDS`）
- 检查是否调过 logout：撤销表 `srs/revoked_tokens.json` 里的 jti 无法复用

### 12.7 新设备登录收不到房间

新设备登录后**必须主动调** `GET /api/v1/me/rooms` 拉房间列表。服务端不主动推送。

### 12.8 同一账号在两台设备想加入同一个房间

触发 **R7.3 单设备互斥**：后加入的设备会收到 409 `user already active in this room on another device`。等旧设备心跳超时（3 × 30s = 90s）或主动关闭旧设备的 WS 后，新设备才能加入。

---

## 13. 端到端测试脚本（10 分钟跑通 R3/R4/R7）

```bash
#!/bin/bash
set -e
BASE=http://127.0.0.1:8085

# 1) 三个 token（owner / invitee / knocker）
TOK_O=$(curl -s -X POST $BASE/api/v1/auth/test_login -H "Content-Type: application/json" \
  -d '{"user_name":"o","role":"owner","bus_id":"990001"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['data']['token'])")
TOK_M=$(curl -s -X POST $BASE/api/v1/auth/test_login -H "Content-Type: application/json" \
  -d '{"user_name":"m","role":"member","bus_id":"990002"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['data']['token'])")
TOK_K=$(curl -s -X POST $BASE/api/v1/auth/test_login -H "Content-Type: application/json" \
  -d '{"user_name":"k","role":"member","bus_id":"990003"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['data']['token'])")

# 2) Owner 建房间
curl -s -X POST $BASE/api/v1/room -H "Authorization: Bearer $TOK_O" \
  -H "Content-Type: application/json" -d '{"room_id":"r1"}' > /dev/null

# 3) Owner 定向邀请 m（自动生成邀请码 + 推送 invite_received）
INVITE=$(curl -s -X POST $BASE/api/v1/invite -H "Authorization: Bearer $TOK_O" \
  -H "Content-Type: application/json" -d '{"room_id":"r1","invitee_user_id":"user_m","message":"hi"}')
echo "invite response: $INVITE"
INVITE_ID=$(echo $INVITE | python3 -c "import sys,json;print(json.load(sys.stdin)['data']['id'])")
INVITE_CODE=$(echo $INVITE | python3 -c "import sys,json;print(json.load(sys.stdin)['data']['invite_code'])")

# 4) M 兑换邀请 → 通行码存服务端
curl -s -X POST $BASE/api/v1/invites/$INVITE_ID/redeem -H "Authorization: Bearer $TOK_M"

# 5) M 加入房间（不带邀请码）
curl -s -X POST $BASE/api/v1/room/r1/join -H "Authorization: Bearer $TOK_M" \
  -H "Content-Type: application/json" -d '{"role":"member"}'

# 6) Owner 发消息
curl -s -X POST "$BASE/api/v1/messages/send?room_id=r1" -H "Authorization: Bearer $TOK_O" \
  -H "Content-Type: application/json" -d '{"client_msg_id":"u-1","type":"text","content":"hi m"}'

# 7) M 拉历史
curl -s "$BASE/api/v1/messages/history?room_id=r1" -H "Authorization: Bearer $TOK_M"

# 8) K 敲门
curl -s -X POST $BASE/api/v1/room/r1/knock -H "Authorization: Bearer $TOK_K" \
  -H "Content-Type: application/json" -d '{"message":"想加入"}'

# 9) Owner 接受敲门（K 直接成为成员）
curl -s -X POST $BASE/api/v1/room/r1/knock/accept -H "Authorization: Bearer $TOK_O" \
  -H "Content-Type: application/json" -d '{"knocker_id":"user_k","role":"member"}'

# 10) K 现在可以 join
curl -s -X POST $BASE/api/v1/room/r1/join -H "Authorization: Bearer $TOK_K" \
  -H "Content-Type: application/json" -d '{"role":"member"}'

# 11) 新设备登录：拉房间列表
TOK_M2=$(curl -s -X POST $BASE/api/v1/auth/test_login -H "Content-Type: application/json" \
  -d '{"user_name":"m","role":"member","bus_id":"990002"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['data']['token'])")
echo "M new device rooms:"
curl -s "$BASE/api/v1/me/rooms" -H "Authorization: Bearer $TOK_M2"

# 12) Owner 关闭房间（级联撤销通行码 + room_closed）
curl -s -X DELETE $BASE/api/v1/room/r1 -H "Authorization: Bearer $TOK_O"

echo "DONE"
```

---

## 14. 已知约束 / 调试备忘

- **用户表**：MySQL 主存储 + `users.json` 兜底；不要直接编辑 `users.json`（会被下次同步覆盖）
- **邀请码缓存**：Redis 主存 + 进程内兜底
- **通行码**：服务端 `pass_codes` 表保存（MySQL）；删 Redis 缓存后仍以 DB 为准
- **房间/成员/消息**：纯内存（`user_manager.json`/`messages.json`），重启会丢；如需持久化请走 MySQL（规划中）
- **WS 多实例**：当前单进程；多实例需要 Redis pub/sub 广播（规划中）
- **notice_socket 端口**：当前与 room_socket 共用 8085（兼容期），未来 R1 完成后独立端口（待产品定）
- **生产部署**：
  - 务必把 `/api/v1/auth/test_login` 加 nginx 白名单
  - 文件上传加 virus scan + 私有存储 + 签名 URL
  - 邀请码校验失败锁定防爆破（5 次/码 → 锁 5 分钟）

---

> 文档维护：服务端团队（2026-08-15 起与 `需求文档.md` 同步更新）
> 参照需求：`需求文档.md`（R1~R7 改进）