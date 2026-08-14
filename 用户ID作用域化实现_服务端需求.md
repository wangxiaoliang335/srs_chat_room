# 方案 E：用户ID前缀作用域（bus:id / chat:id）— 服务端实现文档

> 适用模块：聊天服务器（srs-project，`8085`）
> 客户端模块：`module_chat`（AAR）/ `module_chatroom`（业务接入）
> 关联文档：`用户ID字段扩展_服务端需求.md`（方案 A/B）、`用户ID方案选型与其它方案推荐_服务端需求.md`（方案 C–F）
> 编写日期：2026-08-03
> 版本：v2.0（改动接口数量最小化）

---

## 1. 背景与目标

方案 A 使用 JSON 对象 `UserID{type,id}` 表达用户来源；方案 E 将其演进为**单一字符串前缀语法**：

```text
bus:<业务用户id>      # 业务服务器用户，使用前需转换为 chat_user_id
chat:<chat_user_id>  # 聊天室唯一标识，直接使用
```

**核心目标：把改动接口数量降至最小。**

- 所有既有接口（join/leave、成员管理、踢人、禁言、敲门、邀请、查名、说话广播、WS 订阅、翻译等）
  **一律保持现状**，只接受 `chat_user_id`（`user_<12位>`），**零修改**。
- 所有 `bus:*` / `chat:*` 前缀解析**收敛到一个新增的解析接口**（`POST /api/v1/users/resolve`）。
- 业务方先调用该解析接口，把业务 id 换成 `chat_user_id` 并**缓存**（复用方案 D 思路），
  之后所有请求与存量客户端完全一致。

**改动面：新增 1 个接口，修改 0 个接口。**

---

## 2. ID 语法定义

### 2.1 基本语法

| 形式 | 正则 | 示例 | 处理方式 |
|------|------|------|----------|
| `bus:<业务id>` | `^bus:[A-Za-z0-9_-]+$` | `bus:123` | 业务 id，需转换 `→ chat_user_id` |
| `chat:<chat_user_id>` | `^chat:user_[A-Za-z0-9]{12}$` | `chat:user_50ebf1752dbf` | 聊天室 id，直接使用 |
| `user_<12位>`（存量） | `^user_[A-Za-z0-9]{12}$` | `user_50ebf1752dbf` | 聊天室 id，直接使用，等价 `chat:` |

> 大小写敏感：前缀固定为小写 `bus:` / `chat:`。
> 该语法**只**出现在解析接口（§4）的请求/响应中，其它接口不感知。

### 2.2 多 app 作用域（可选）

同一聊天服务器服务多个业务 app / 租户时，`bus:` 形式可携带 `app_id`：

| 形式 | 正则 | 示例 | 说明 |
|------|------|------|------|
| `bus:<app_id>:<业务id>` | `^bus:[A-Za-z0-9_-]+:[A-Za-z0-9_-]+$` | `bus:app_001:123` | app_id + 业务 id，按 `(app_id, 业务id)` 转换 |
| `bus:<业务id>` | `^bus:[A-Za-z0-9_-]+$` | `bus:123` | 省略 app_id，取默认 app（§3.3） |
| `chat:<chat_user_id>` | `^chat:user_[A-Za-z0-9]{12}$` | `chat:user_50ebf1752dbf` | `chat_user_id` 全局唯一，无需 app 作用域 |

> `app_id` 与业务 id 字符集均限定 `[A-Za-z0-9_-]`，**不含冒号**，避免切分歧义。

### 2.3 命名空间对照

| 概念 | 结构 | 示例 |
|------|------|------|
| 业务 id（BUS） | 业务后端用户主键 | `123` |
| `chat_user_id` | 聊天服务器内部唯一标识 | `user_50ebf1752dbf` |
| 完整 id（对外） | `bus:<app_id>:<业务id>` 或 `chat:<chat_user_id>` | `bus:app_001:123` / `chat:user_50ebf1752dbf` |

---

## 3. 解析与转换逻辑（服务端内部）

> 该逻辑仅被解析接口（§4）内部调用，**不**接入任何既有接口。

### 3.1 统一解析函数 `parseUserId(value, defaultAppId)`

```text
function parseUserId(value, defaultAppId):
    if value 为空/非字符串:          → 错误 "user_id 格式非法"

    if value 以 "chat:" 开头:
        chatUid = value[5:]
        if chatUid 不匹配 ^user_[A-Za-z0-9]{12}$: → 错误 "user_id 格式非法"
        return chatUid                            # 直接使用

    if value 以 "bus:" 开头:
        rest = value[4:]
        parts = rest.split(":")
        if len(parts) == 1:
            appId, bizId = defaultAppId, parts[0] # 单 app，省略 app_id
        elif len(parts) == 2:
            appId, bizId = parts[0], parts[1]     # 显式 app_id
        else:
            → 错误 "user_id 格式非法"
        if appId 或 bizId 为空或含非法字符:        → 错误 "user_id 格式非法"
        return busIdToChatUserId(appId, bizId)

    if value 匹配 ^user_[A-Za-z0-9]{12}$:
        return value                              # 存量 chat_user_id，兼容

    → 错误 "user_id 格式非法"
```

### 3.2 `defaultAppId` 的来源

- `bus:<业务id>` 省略 app_id 时，取**当前请求上下文**的默认 app：
  1. JWT 中的 `app_id` claim（登录/鉴权时写入，与 `POST /api/v1/app/verify` 应用体系对齐）；
  2. 若无，取请求头 `X-App-Id`；
  3. 仍无 → 约定常量 `"default"`。
- 显式 `bus:<app_id>:<业务id>` 时，以显式 app_id 为准。

### 3.3 `bus:*` → `chat_user_id` 转换逻辑

**转换参考：** `POST /api/v1/auth/login` 登录流程中，聊天服务器用业务后端 `external_token`
调 `getCurrentProfile` 拿到业务用户信息（含业务 `id`），并据其登记 `chat_user_id`
（`users` 表）。`(app_id, 业务id) → chat_user_id` 映射在登录时建立。

```text
function busIdToChatUserId(appId, bizId):
    chatUid = userMapping.query(appId, bizId)
    if chatUid 不存在:               → 404 "user not found"
    return chatUid
```

要求：

1. 查询来源：`users` 表（或等价映射表/缓存），按 `(app_id, biz_id)` 联合键反查 `chat_user_id`。
2. 查询不到（该业务用户从未登录过聊天服务器）→ 404 `user not found`，**不现场自动创建**。
3. 若业务后端提供"业务 id 反查 chat_user_id"接口，可委托其查询；要求与登录时生成的
   `chat_user_id` 一致。
4. 查询失败（业务后端不可用）→ 503 `业务后端不可用`。

---

## 4. 接口改动清单（最小化）

### 4.1 总览

| 类型 | 数量 | 说明 |
|------|------|------|
| **新增** | **1** | `POST /api/v1/users/resolve`（业务/聊天 id → `chat_user_id` 解析） |
| **修改** | **0** | 所有既有接口保持现状，仅接受 `chat_user_id` |
| 复用 | 1 | `POST /api/v1/auth/login`（登录响应已含 `chat_user_id`，作为解析后的缓存来源） |

### 4.2 新增接口：`POST /api/v1/users/resolve`

```
POST /api/v1/users/resolve
Authorization: Bearer <chat_token>
```

**请求体：**

```json
{
  "ids": ["bus:123", "bus:app_001:456", "chat:user_50ebf1752dbf", "user_50ebf1752dbf"]
}
```

- `ids` 数组长度 ≤ 100，元素为 §2 定义的前缀 id 或存量 `user_<12位>`。
- 元素可混排，服务器逐个按 §3.1 解析。

**成功响应：**

```json
{
  "code": 0,
  "message": "success",
  "data": {
    "resolved": [
      { "input": "bus:123",              "chat_user_id": "user_50ebf1752dbf", "username": "alice" },
      { "input": "chat:user_50ebf1752dbf", "chat_user_id": "user_50ebf1752dbf", "username": "alice" },
      { "input": "user_50ebf1752dbf",    "chat_user_id": "user_50ebf1752dbf", "username": "alice" }
    ],
    "unresolved": [
      { "input": "bus:99999", "reason": "user not found" }
    ]
  }
}
```

**字段说明：**

| 字段 | 类型 | 说明 |
|------|------|------|
| resolved[].input | string | 原样回传请求值，便于客户端对账 |
| resolved[].chat_user_id | string | 归一化后的聊天室唯一标识，后续请求使用 |
| resolved[].username | string | 用户显示名（`users` 表，无则回退 `chat_user_id`） |
| unresolved[].input | string | 解析失败的原值 |
| unresolved[].reason | string | 失败原因（`user not found` / `user_id 格式非法` / `业务后端不可用`） |

**错误码：**

| HTTP 状态码 | code | message | 说明 |
|-------------|------|---------|------|
| 400 | 400 | ids 不能为空 / 长度超过 100 | 请求体校验失败 |
| 400 | 400 | 部分 id 格式非法 | 单个元素格式非法时，建议**放入 `unresolved`** 而非整体 400（见 §4.3） |
| 401 | 401 | token 无效或已过期 | 未带/无效 token |
| 503 | 503 | 业务后端不可用 | `bus:*` 转换依赖的业务查询不可用 |

> 格式非法的单个元素放 `unresolved`、`resolved` 返回其余结果（**部分成功**）；
> 仅当整个请求体非法（`ids` 缺失/非数组/空/超长）时整体 400。

### 4.3 既有接口（全部不改）

| 接口 | 说明 |
|------|------|
| `/api/v1/rooms/{room_id}/join`、`/leave` | body 中 `user_id` 仍传 `chat_user_id`（可选；多数场景可省略，服务端从 JWT `uid` 取） |
| `/api/v1/room/{room_id}/member/{user_id}`、`/role`、`/kick`、`/mute`、`/unmute`、`/mic/disable`、`/mic/enable` | path 中 `{user_id}` 仍传 `chat_user_id` |
| `/api/v1/room/{room_id}/check-publish`、`/speaking/broadcast`、`/knock/accept`、`/knock/reject` | 相关 id 字段仍传 `chat_user_id` |
| `/api/v1/ws/subscribe`、WS 首帧 `/ws` | `user_id` 仍传 `chat_user_id`（服务端以 JWT `uid` 为准） |
| `/api/v1/users/{user_id}/room`、`/name`、`/names` | path/body 中 id 仍传 `chat_user_id` |
| `/api/v1/invite`、`/translation/*` | 相关 id 字段仍传 `chat_user_id` |

### 4.4 业务方接入流程（推荐，含客户端缓存）

```
1. 业务方持业务 id（如 123）
2. 调用 POST /api/v1/users/resolve { "ids": ["bus:123"] }
   → 得到 chat_user_id = user_50ebf1752dbf，缓存到本地（映射 key: "123" → "user_50ebf1752dbf"）
3. 后续请求一律携带 chat_user_id 调既有接口（与存量客户端一致）
4. 本地无映射/缓存失效时，重新调用 resolve 兜底（懒转换）
```

> 可选优化：resolve 的结果可由 `POST /api/v1/auth/login` 响应（`data.user_id`）顺带补齐，
> 业务方在登录时即可建立缓存，多数场景无需再调 resolve。

---

## 5. 校验规则

| 规则 | 说明 |
|------|------|
| 前缀 | 仅 `bus:` / `chat:`，小写，大小写敏感 |
| `chat:` 后 | 必须匹配 `^user_[A-Za-z0-9]{12}$` |
| `bus:` 后 | `[A-Za-z0-9_-]+`（单段=业务id）或 `[A-Za-z0-9_-]+:[A-Za-z0-9_-]+`（app_id:业务id） |
| 空值 / 纯空白 | 400 `user_id 格式非法` |
| 多余冒号段 | 400 `user_id 格式非法`（`bus:` 后超过 2 段） |
| 存量 `user_xxx` | 不强制带前缀，兼容现状 |
| `ids` 数组 | 1 ≤ length ≤ 100，缺失/非数组 → 400 |

---

## 6. 安全与一致性

1. 前缀解析与 `bus:*` 转换**只**存在于 resolve 接口内部，既有接口不感知、不承接。
2. resolve 接口需鉴权（`Authorization: Bearer <chat_token>`）。
3. `bus:*` 转换查询做单 IP 限频（建议 60 req/min），防止批量探测业务 id。
4. 日志、埋点、审计只记录归一化后的 `chat_user_id`，不落 `bus:*` 原始形式。
5. 显式 `bus:<app_id>:...` 的 `app_id` 与 JWT 上下文不一致时，按该显式 app 转换，且 resolve
   结果的使用仍受既有接口权限校验约束（resolve 本身不授予任何操作权限）。
6. 返回给调用方的 `chat_user_id` 与 JWT `uid` 属同一用户即可，无越权面（resolve 只做映射查询，
   不做任何状态变更）。

---

## 7. 向后兼容

- **所有既有接口零改动**：请求、响应、JWT、存储字段均保持 `chat_user_id`。
- resolve 为**纯新增接口**，不影响任何存量调用方。
- 存量客户端继续传 `user_<12位>`，行为与现状完全一致。
- 业务方通过 resolve + 缓存拿到 `chat_user_id` 后，与存量客户端共用同一套接口，无需双栈。

---

## 8. 数据库改动

### 8.1 映射表（方案 A 已有，此处补 app 作用域）

`users` 表（或等价映射表）新增/明确字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| `user_id` | string | `chat_user_id`（`user_<12位>`，唯一） |
| `app_id` | string | 所属业务应用，默认 `"default"` |
| `bus_id` | string | 业务服务器用户 id（`(app_id, bus_id)` 唯一） |
| `username` | string | 用户显示名（已有） |

索引：

```sql
-- (app_id, bus_id) 联合唯一索引，支撑 bus: 前缀反查
CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS idx_users_bus ON users(app_id, bus_id);
```

### 8.2 缓存

```text
key: chat:user:bus:{app_id}:{bus_id}  →  chat_user_id
TTL: 600s
```

- `POST /auth/login` 建立/更新映射时，同步刷新该缓存。
- resolve 接口先查缓存，未命中再查库并回填。

---

## 9. 错误码汇总（仅新增 resolve 接口）

| HTTP 状态码 | code | message | 说明 |
|-------------|------|---------|------|
| 400 | 400 | ids 不能为空 / 长度超过 100 | 请求体校验失败 |
| 400 | 400 | 部分 id 格式非法 | 单个元素格式非法 → 放 `unresolved`，整体不 400 |
| 401 | 401 | token 无效或已过期 | 未带/无效 token |
| 404 | 404 | user not found | `bus:*` 无对应映射（该项进 `unresolved`） |
| 503 | 503 | 业务后端不可用 | `bus:*` 转换依赖的业务查询不可用 |

> 既有接口错误码全部保持不变。

---

## 10. 验收用例

### 10.1 存量兼容（回归，不改接口）

```http
POST /api/v1/rooms/room_xxx/join
Authorization: Bearer <chat_token>

{ "user_id": "user_50ebf1752dbf", "role": "member" }
```

期望：`200`，行为与现状一致。

### 10.2 resolve：`bus:` 单 app 转换

```http
POST /api/v1/users/resolve
Authorization: Bearer <chat_token>

{ "ids": ["bus:123"] }
```

期望：`200`，`resolved[0].chat_user_id == "user_50ebf1752dbf"`。

### 10.3 resolve：`chat:` 与存量直接通过

```http
POST /api/v1/users/resolve
Authorization: Bearer <chat_token>

{ "ids": ["chat:user_50ebf1752dbf", "user_50ebf1752dbf"] }
```

期望：`200`，两项均解析为 `user_50ebf1752dbf`。

### 10.4 resolve：部分成功

```http
POST /api/v1/users/resolve
Authorization: Bearer <chat_token>

{ "ids": ["bus:123", "bus:99999", "guest:1"] }
```

期望：`200`，`resolved` 含 `bus:123`；`unresolved` 含 `bus:99999`（user not found）
与 `guest:1`（user_id 格式非法）。

### 10.5 resolve：请求体非法

```http
POST /api/v1/users/resolve
Authorization: Bearer <chat_token>

{ "ids": [] }
```

期望：`400`，`message == "ids 不能为空"`。

### 10.6 resolve：未鉴权

```http
POST /api/v1/users/resolve

{ "ids": ["bus:123"] }
```

期望：`401`，`message == "token 无效或已过期"`。

### 10.7 业务方全链路（resolve + 既有接口）

```text
1. POST /api/v1/users/resolve { "ids": ["bus:123"] } → chat_user_id = user_50ebf1752dbf
2. POST /api/v1/rooms/room_xxx/join { "user_id": "user_50ebf1752dbf" } → 200
```

期望：两步均成功，业务侧全程只见 `bus:123` 与其映射后的 `chat_user_id`。

---

## 11. 上线步骤

1. 服务端实现 `POST /api/v1/users/resolve`（内部复用 §3 解析逻辑），补 `(app_id, bus_id)` 索引与缓存。
2. 部署测试环境，用 Postman 跑通 §10 用例。
3. 回归存量纯字符串调用（10.1），确认无行为变化。
4. 业务方接入 resolve + 本地缓存（§4.4）。
5. 灰度观察转换命中率与错误码分布后全量上线。

---

## 12. 未尽事项

- `app_id` 与 `POST /api/v1/app/verify` 应用体系的对齐：登录时如何将 app_id 写入 JWT 与 `users` 表。
- resolve 响应是否附带 `avatar` 等扩展字段（当前仅 `username`）。
- `operator_id` 等操作者字段是否也需要"业务 id 可解析"：默认**不需要**（操作者身份从 JWT 取）。
