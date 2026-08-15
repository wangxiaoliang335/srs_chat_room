# SRS 聊天室服务 — 客户端开发调试手册

> 文档版本：v1.0（基于 2026-08-13 文档 + P0~P6 实现）
> 服务地址（默认本地）：`http://127.0.0.1:8085` / `ws://127.0.0.1:8085/ws`
> 目标读者：客户端工程师（开发、联调、QA）

---

## 1. 一分钟总览

| 项 | 说明 |
|---|---|
| 鉴权 | JWT（HS256），通过 `Authorization: Bearer <token>` 注入；`/api/v1/auth/test_login` 可绕过业务后端直接签发（仅调试） |
| 房间 | 房主唯一；进入需要邀请码（owner/admin 除外）；2026-08-13 后 **无 `/leave` 接口**，离开通过关闭 WS / 主动断开 |
| 消息 | HTTP `/api/v1/messages/send` 或 WS `chat_message`；**`client_msg_id` 必须全局唯一**，幂等 |
| WebSocket | `/ws?room=<room_id>&user=<user_id>` 单连接；服务端主动 ping → 客户端必须回 pong |
| 限流 | 邀请 1 次/30s；敲门 1 次/30s + 同房间 3 次/小时；超出 429 |
| 邀请码 | 房主生成，默认 600s 过期；可用作"加群"凭证；缓存到 Redis |
| 错误格式 | 统一：`{"code": <int>, "message": "<str>"}`，`code=0` 为成功 |

---

## 2. 调试第一步：拿到一个 JWT

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
| `app_id` | � | 默认 `default`，三方登录时用业务方标识 |
| `bus_id` | ❌ | 业务后端用户 id；非业务账号留空 |
| `role` | � | `member` / `admin` / `owner`，调试时可以直接给 owner 测房主路径 |

> ⚠️ **生产部署务必把 `/api/v1/auth/test_login` 在反向代理层屏蔽！**

### 2.2 正式登录（业务后端三方）

```bash
curl -X POST http://127.0.0.1:8085/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"token":"<业务后端颁发>"}'
```

服务器用 `BUSINESS_APP_KEY` 调业务后端 `/api/frontend/app/external/users/me` 验证 `token`，查到业务用户后自动创建/复用本地账号，签发 JWT（7 天 TTL）。

### 2.3 验证 token 是否还有效

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

立即把当前 jti 加入撤销表，再访问业务接口返回 401。

---

## 3. 房间生命周期

### 3.1 创建房间

```bash
curl -X POST http://127.0.0.1:8085/api/v1/room \
  -H "Authorization: Bearer <owner-JWT>" \
  -H "Content-Type: application/json" \
  -d '{"room_id":"team_alpha","name":"团队 Alpha"}'
```

**关键规则（2026-08-13 §7.1）**：
- 一个用户**只能是一个房间的房主**。再次调用不会创建新房间，而是**覆盖更新**现有房间的 name 等字段
- `room_id` 是客户端定的；想完全换房间，先把旧 owner 关系释放掉（踢出原房间成员 + 转让 owner）

### 3.2 生成邀请码

```bash
curl -X POST "http://127.0.0.1:8085/api/v1/invite/code/generate?room_id=team_alpha&target_user_id=&expire_seconds=600" \
  -H "Authorization: Bearer <owner-JWT>"
```

| 参数 | 必填 | 说明 |
|---|---|---|
| `room_id` | ✅ | 房间路径 |
| `target_user_id` | ❌ | 限定给某用户；空 = 通用码 |
| `expire_seconds` | ❌ | 默认 600s（10 分钟） |

**返回**：`{"code":0, "data":{"code":"ABC123XYZ","expires_at":..., ...}}`

限流：同用户 1 次/30s → 429 `too many invites, retry in 30s`

### 3.3 加入房间（成员）

```bash
curl -X POST http://127.0.0.1:8085/api/v1/room/team_alpha/join \
  -H "Authorization: Bearer <member-JWT>" \
  -H "Content-Type: application/json" \
  -d '{"role":"member","invite_code":"ABC123XYZ"}'
```

**校验顺序**：先看 role；owner/admin 直接放行；member 必传 invite_code，否则 403 `invite_code required`。

### 3.4 敲门（被拒绝后申请加群）

```bash
# 1) 敲门
curl -X POST http://127.0.0.1:8085/api/v1/room/team_alpha/knock \
  -H "Authorization: Bearer <knocker-JWT>" \
  -H "Content-Type: application/json" \
  -d '{"message":"hi 想加入"}'

# 2) 房主同意 / 拒绝
curl -X POST http://127.0.0.1:8085/api/v1/room/team_alpha/knock/accept \
  -H "Authorization: Bearer <owner-JWT>" \
  -H "Content-Type: application/json" \
  -d '{"knocker_id":"user_xyz","role":"member"}'

curl -X POST http://127.0.0.1:8085/api/v1/room/team_alpha/knock/reject \
  -H "Authorization: Bearer <owner-JWT>" \
  -H "Content-Type: application/json" \
  -d '{"knocker_id":"user_xyz","reason":"稍等"}'
```

限流：1 次/30s + 同房间 3 次/小时。超限 429。

### 3.5 离开房间（重要）

**没有 `/leave` 接口**。离开通过：

1. **关闭 WebSocket**（推荐）：触发服务端 disconnect 事件，其他成员收到 `member_left`
2. **被踢**：owner/admin 调 `DELETE /api/v1/room/<room>/member/<user>/kick`

### 3.6 房主操作

```bash
# 改成员角色
curl -X POST http://127.0.0.1:8085/api/v1/room/team_alpha/member/<user_id>/role \
  -H "Authorization: Bearer <owner-JWT>" \
  -H "Content-Type: application/json" \
  -d '{"role":"admin"}'

# 禁言 / 解除禁言
curl -X POST http://127.0.0.1:8085/api/v1/room/team_alpha/member/<user_id>/mute \
  -H "Authorization: Bearer <owner-JWT>" \
  -H "Content-Type: application/json" -d '{}'

# 踢人
curl -X DELETE http://127.0.0.1:8085/api/v1/room/team_alpha/member/<user_id>/kick \
  -H "Authorization: Bearer <owner-JWT>"

# 全员禁言 / 解除
curl -X POST http://127.0.0.1:8085/api/v1/room/team_alpha/mute-all \
  -H "Authorization: Bearer <owner-JWT>" -d '{}'
```

---

## 4. 消息收发

### 4.1 HTTP 发送（推荐用于发消息）

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
- `client_msg_id`：**全局唯一**（建议 UUID v4），用于幂等
- `content`：消息内容
- `type`：`text` / `image` / `file`

**幂等**：相同 `client_msg_id` 在 10 分钟内重发，第二次返回原消息 + `_idempotent=true`（不分配新 seq）。

**返回**：
```json
{
  "code": 0,
  "data": {
    "id": "m_1786...",
    "seq": 42,
    "client_msg_id": "...",
    "_idempotent": false,
    "ts": 1786761000.123
  }
}
```

### 4.2 WS 发送（实时性更优）

```javascript
ws.send(JSON.stringify({
  type: "chat_message",
  client_msg_id: "uuid-xxx",
  type: "text",  // 消息类型（不是 ws type）
  content: "大家好"
}));
```

服务端立刻返回 `chat_message_ack`，消息广播到房间内所有 WS 客户端。

### 4.3 历史消息

```bash
curl -X GET "http://127.0.0.1:8085/api/v1/messages/history?room_id=team_alpha&after_seq=0&limit=50" \
  -H "Authorization: Bearer <JWT>"
```

- `after_seq`：传 0 = 从头；传上次收到的最大 seq = 增量
- `limit`：1~200，默认 50

WS 也可以发 `{"type":"history_sync","after_seq":...}` 拉增量（仅自己）。

### 4.4 实时推送（服务端 → 客户端）

| event | 何时触发 | payload |
|---|---|---|
| `chat_message` | 房间内任何人发消息 | `{type, room_id, id, seq, user_id, client_msg_id, content, ts, ...}` |
| `chat_message_ack` | 你发的消息被持久化 | `{type, id, seq, client_msg_id, _idempotent}` |
| `history_sync` | 你拉历史时 | `{type, room_id, items[], latest_seq}` |
| `member_joined` | 新成员加入 | `{type, room_id, user_id, role, ts}` |
| `member_left` | 成员断开 WS | `{type, room_id, user_id, ts, reason}` |
| `member_kicked` | 你被踢了 | `{type, room_id, user_id, operator_id}` |
| `role_changed` | 你的角色被改 | `{type, room_id, user_id, old_role, new_role, operator_id}` |
| `room_mute_changed` | 全员禁言状态变 | `{type, room_id, allow_speak, operator_id}` |
| `muted` / `unmuted` | 你被禁言 / 解除 | `{type, room_id, user_id, operator_id}` |
| `mic_disabled` / `mic_enabled` | 你被禁麦 / 解除 | 同上结构 |
| `knock` | 有人敲门 | `{type, room_id, knocker_id, message}` |
| `knock_result` | 你的敲门被处理 | `{type, room_id, accepted, role?, reason?}` |
| `notify` | 新通知 | `{type, notification:{id, type, ...}}` |

---

## 5. WebSocket 接入

### 5.1 URL

```
ws://<host>:8085/ws?room=<room_id>&user=<user_id>
```

- `room`：房间 id（必填，否则 close 1008 "Missing room parameter"）
- `user`：用户 id（可选，但强烈建议传）

### 5.2 心跳（必须）

服务端每 **30s**（`HEARTBEAT_INTERVAL` 环境变量）主动 `ping`，客户端必须在 **3 次**（`HEARTBEAT_FAIL_THRESHOLD`）内回 `pong`，否则被服务端断开。

```javascript
ws.onmessage = (e) => {
  const msg = JSON.parse(e.data);
  if (msg.type === 'ping') {
    ws.send(JSON.stringify({type: 'pong'}));
  }
};
```

> 注意：服务端 ping 用的是应用层 JSON，不是 ws 协议层的 PING 帧。**别用 ws.onping，那是浏览器 ws 协议层事件，服务端没发。**

### 5.3 客户端发送事件

```javascript
ws.send(JSON.stringify({type: "subscribe", room_id: "team_alpha"}));  // 切换房间
ws.send(JSON.stringify({type: "chat_message", client_msg_id: "u-1", type: "text", content: "hi"}));
ws.send(JSON.stringify({type: "history_sync", after_seq: 0, limit: 100}));
ws.send(JSON.stringify({type: "ping"}));  // 兼容旧版，服务端回 pong（可选）
```

### 5.4 鉴权

WS 鉴权有两种方式：

**A. URL query**（简单）：
```
ws://host:8085/ws?room=r1&user=u1
```
不强制鉴权，**仅内网调试**。

**B. 子协议 / Sec-WebSocket-Protocol header**（生产）：
```
Sec-WebSocket-Protocol: jwt, <token>
```
服务端解析第二个 token，做 JWT 校验。

---

## 6. 通知（站内信）

```bash
# 拉未读
curl http://127.0.0.1:8085/api/v1/notifications/unread-count \
  -H "Authorization: Bearer <JWT>"

# 拉列表
curl http://127.0.0.1:8085/api/v1/notifications \
  -H "Authorization: Bearer <JWT>"

# 标记已读
curl -X POST http://127.0.0.1:8085/api/v1/notifications/<id>/read \
  -H "Authorization: Bearer <JWT>"

# 一键已读
curl -X POST http://127.0.0.1:8085/api/v1/notifications/read-all \
  -H "Authorization: Bearer <JWT>"
```

实时通知通过 WS 事件 `notify` 推送。

---

## 7. 用户查询

```bash
# 按 user_id 查名字
curl http://127.0.0.1:8085/api/v1/users/<user_id>/name

# 批量按 user_id 查
curl -X POST http://127.0.0.1:8085/api/v1/users/names \
  -H "Authorization: Bearer <JWT>" \
  -H "Content-Type: application/json" \
  -d '{"ids":["user_aaa","user_bbb"]}'

# 按业务 bus_id 查（前提是用户已走过三方登录）
curl -X POST http://127.0.0.1:8085/api/v1/users/resolve \
  -H "Authorization: Bearer <JWT>" \
  -H "Content-Type: application/json" \
  -d '{"ids":["bus:990001","user_aaa"]}'

# 查用户当前在哪个房间
curl http://127.0.0.1:8085/api/v1/users/<user_id>/room
```

`resolve` 支持混合 ID：`bus:<业务id>`（按业务后端 id）、`user_<uuid>`（按本地 user_id）。

---

## 8. 错误码速查

| HTTP | code | 含义 |
|---|---|---|
| 200 | 0 | 成功 |
| 400 | 400 | 参数错误（看 message：缺字段 / 格式不对） |
| 401 | 401 | 未鉴权 / token 失效 / 撤销 |
| 403 | 403 | 越权（如非 owner 试图生成邀请码） |
| 404 | 404 | 资源不存在 |
| 409 | 409 | 冲突（如用户名已存在） |
| 429 | 429 | 限流（看 message：`retry in Ns`） |
| 500 | 500 | 服务器异常（看 message + 看日志） |

**业务 message 关键字**：
- `not authenticated` → 没带 Authorization
- `token expired` → token 过期，重新登录
- `room not found` → 房间 id 不存在
- `invite_code required` → 加入房间需要邀请码
- `invite code not found or not unused` → 邀请码无效或已用过
- `too many invites, retry in 30s` → 限流
- `owner_id must match current user` → 越权

---

## 9. 调试工具 & 常用命令

### 9.1 实时指标

```bash
curl http://127.0.0.1:8085/api/v1/metrics | python3 -m json.tool
```

返回 counters / gauges / latencies。可看：消息吞吐、邀请码使用、限流命中、推送成功率等。

### 9.2 健康检查

```bash
curl http://127.0.0.1:8085/api/v1/health
# {"status":"ok"}
```

### 9.3 日志

```
srs/logs/server_fastapi.log    # 主服务（含 access log + 错误）
srs/logs/ws_server.log         # WS 服务
srs/audit.log                  # 审计日志（JSON Lines）
```

审计日志示例（`tail -f srs/audit.log`）：
```json
{"ts":1786760903,"iso":"2026-08-15T10:28:23","action":"room_created","actor_id":"user_f18c92924c71","target_id":"","room_id":"team_alpha","details":{"name":"team_alpha","overwritten":false}}
{"ts":1786761000,"iso":"2026-08-15T10:30:00","action":"invite_code_generated","actor_id":"user_f18c92924c71","target_id":"","room_id":"team_alpha","details":{"code":"ABC123","target_user_id":""}}
{"ts":1786761100,"iso":"2026-08-15T10:31:40","action":"invite_code_used","actor_id":"user_xyz","target_id":"","room_id":"team_alpha","details":{"code":"ABC123"}}
```

可审计的 action：`room_created` / `room_deleted` / `room_overwritten` / `member_joined` / `member_kicked` / `member_role_changed` / `member_muted` / `member_unmuted` / `room_mute_all` / `room_unmute_all` / `invite_code_generated` / `invite_code_used` / `invite_code_revoked` 等。

### 9.4 看 Redis 缓存

```bash
# 邀请码有效缓存
redis-cli get "invite:valid:ABC123"

# 在线状态
redis-cli get "presence:user_xxx"

# 房间在线列表
redis-cli smembers "room_online:team_alpha"
```

### 9.5 看 MySQL 用户表

```bash
mysql -uroot -e "SELECT user_id, username, role, bus_id FROM chat_room.chat_user WHERE username='alice';"
```

---

## 10. 常见坑

### 10.1 send_message 返回 `room_id required (query param)`

**room_id 在 query string，不在 body 里**：

```bash
# ❌
curl -X POST /api/v1/messages/send -d '{"room_id":"...","content":"hi"}'

# ✅
curl -X POST "/api/v1/messages/send?room_id=..." -d '{"content":"hi"}'
```

### 10.2 join 房间返回 `invite_code required`

普通成员（role=member）必须传邀请码，owner/admin 直接通过：

```bash
# 房主/管理员：直接 join
curl -X POST /api/v1/room/<r>/join -d '{"role":"admin"}'

# 普通成员：必须带邀请码
curl -X POST /api/v1/room/<r>/join -d '{"role":"member","invite_code":"ABC123"}'
```

### 10.3 ws 一直断、提示 `member_left`

99% 是心跳没回。检查：
1. 是否处理了 `type=pong` / 收到 `ping` 是否回了 `pong`
2. 网络中间是否有 idle 切断（nginx 默认 60s idle close，需要在 nginx 侧配 `proxy_read_timeout 600s;`）

### 10.4 resolve 返回 `user not found`

可能是业务后端用户首次登录还没落库；让用户先走一次 `/api/v1/auth/login` 完成三方认证，再 resolve。

### 10.5 消息发不出去（400 / 403）

- 400：通常是 `client_msg_id` 缺失，或 body JSON 解析失败
- 403：被全员禁言（`room_mute_changed`=`allow_speak=false` 且你是普通成员）
- 403：你在黑名单（被踢后没真正退出）

### 10.6 token 经常 401

- 检查服务器时钟：`date`，偏差 ±30s 内才稳（`JWT_LEEWAY_SECONDS`）
- 检查是否调过 logout：撤销表 `srs/revoked_tokens.json` 里的 jti 无法复用

---

## 11. 端到端测试脚本（5 分钟跑通）

```bash
#!/bin/bash
set -e
BASE=http://127.0.0.1:8085

# 1) 拿两个 token（owner + member）
TOK_O=$(curl -s -X POST $BASE/api/v1/auth/test_login -H "Content-Type: application/json" \
  -d '{"user_name":"o","role":"owner","bus_id":"990001"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['data']['token'])")
TOK_M=$(curl -s -X POST $BASE/api/v1/auth/test_login -H "Content-Type: application/json" \
  -d '{"user_name":"m","role":"member","bus_id":"990002"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['data']['token'])")

# 2) Owner 建房间
curl -s -X POST $BASE/api/v1/room -H "Authorization: Bearer $TOK_O" \
  -H "Content-Type: application/json" -d '{"room_id":"r1"}' > /dev/null

# 3) Owner 生成邀请码
CODE=$(curl -s -X POST "$BASE/api/v1/invite/code/generate?room_id=r1" -H "Authorization: Bearer $TOK_O" | python3 -c "import sys,json;print(json.load(sys.stdin)['data']['code'])")

# 4) Member 加房间
curl -s -X POST $BASE/api/v1/room/r1/join -H "Authorization: Bearer $TOK_M" \
  -H "Content-Type: application/json" -d "{\"role\":\"member\",\"invite_code\":\"$CODE\"}"

# 5) Owner 发消息
curl -s -X POST "$BASE/api/v1/messages/send?room_id=r1" -H "Authorization: Bearer $TOK_O" \
  -H "Content-Type: application/json" -d '{"client_msg_id":"u-1","type":"text","content":"hi"}'

# 6) Member 拉历史
curl -s "$BASE/api/v1/messages/history?room_id=r1" -H "Authorization: Bearer $TOK_M"

echo "DONE"
```

---

## 12. 已知约束 / 调试备忘

- **用户表**：MySQL 主存储 + `users.json` 兜底；不要直接编辑 `users.json`（会被下次同步覆盖）
- **邀请码缓存**：Redis 主存 + 进程内兜底；删 Redis key 后邀请码仍可能短暂通过（用未过期的进程内副本）
- **房间**：纯内存（`user_manager.json`），重启会丢；如需持久化请走 MySQL（已规划 P6+）
- **消息**：纯内存（`messages.json`），重启会丢；如需持久化请走 MySQL（已规划 P6+）
- **WS 多实例**：当前单进程，多实例需要 Redis pub/sub 广播（已规划）
- **生产部署**：务必把 `/api/v1/auth/test_login` 加 nginx 白名单

---

> 文档维护：服务端团队（2026-08-15 起与 P0~P6 同步更新）
