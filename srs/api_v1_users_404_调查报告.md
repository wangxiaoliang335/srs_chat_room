# /api/v1/users/* 404 调查与修复

## 1. 现象

`logs/server_fastapi.log` 中 2026-08-02 15:48:19 ~ 17:53 之间，客户端 IP `112.24.62.163` 大量调用 `/api/v1/users/*` 接口返回 **404**：

| 接口 | 调用次数 | 状态 |
|------|---------|------|
| `GET /api/v1/users/{user_id}/name` | 24 | 全部 404 |
| `POST /api/v1/users/names` | 55 | 全部 404 |
| `GET /api/v1/users/names` | 2 | 全部 404（客户端发了 GET，path 只支持 POST） |

被查的 user_id 集中在两个：

- `user_a6e69f185da0`（username `MHH1`）
- `user_50ebf1752dbf`（username `Nuo`）

## 2. 排查过程

### 2.1 路由是否注册

`uvicorn` 进程当前 / 重启后路由表均已正确注册：

```python
['GET'] /api/v1/users/{user_id}/name
['POST'] /api/v1/users/names
['GET'] /api/v1/users/{user_id}/room
```

### 2.2 handler 行为

| 路径 | 期望 |
|------|------|
| 无 token | 401 |
| token 校验失败 | 401 |
| user_id 格式非法 | 400 |
| 用户不存在 | 404 `user not found` |
| 用户存在 | 200 返回 `{user_id, username, avatar}` |

### 2.3 当时为何 404

`server_fastapi.py` 的 `get_user_name` handler 在 404 命中分支**没有打日志**，加上回调层 `callback_server.log`（业务日志）也没记录，所以当时无法判断 404 来自哪个分支。

将 `user_a6e69f185da0` / `user_50ebf1752dbf` 在**当前** `user_store._users` 中查询：

- `user_a6e69f185da0` → 存在，username=`MHH1`，已在 `alove_room_1785652028316`
- `user_50ebf1752dbf` → 存在，username=`Nuo`，已在 `alove_room_1785660481507`

能查到，但当时 404。**根因**：uwsgi/fastapi 进程当时**没有 reload 进来这两个用户**（业务后端 `/api/frontend/app/external/users/me` 在 8 月 1 日整天 404，sync_client 9005 也长期连不上），user_store 里没有这俩 user_id 的记录。

## 3. 修复

### 3.1 `get_user_name` 404 加 INFO

`server_fastapi.py` line 1100+：

```python
rec = user_store.get_by_user_id(user_id)
if not rec:
    logger.info(
        f"[API] get_user_name 404: user_id={user_id} "
        f"jwt_user_id={jwt_user_id} remote={request.client.host if request.client else '-'}"
    )
    return JSONResponse(
        status_code=404,
        content={"code": 404, "message": "user not found", "data": None},
    )
```

打 3 个字段：`user_id`（被查的）、`jwt_user_id`（调用方）、`remote`（客户端 IP）。

### 3.2 `batch_get_user_names` 跳过 ID 加 INFO

每次请求只在结束时打**一行汇总**，避免 100 个 ID 各打一行刷屏：

```python
if missing_ids:
    sample = ",".join(missing_ids[:5])
    more = len(missing_ids) - min(len(missing_ids), 5)
    logger.info(
        f"[API] batch_get_user_names skip: requested={len(seen)} "
        f"missing={len(missing_ids)} sample={sample}{f' +{more}more' if more > 0 else ''} "
        f"jwt_user_id={jwt_user_id} remote={request.client.host if request.client else '-'}"
    )
```

### 3.3 GET /api/v1/users/names 误用 405 打 INFO

`_http_exception_handler` 兜底处打一行：

```python
if exc.status_code == 405 and request.url.path == "/api/v1/users/names":
    logger.info(
        f"[API] /users/names wrong method: method={request.method} "
        f"remote={request.client.host if request.client else '-'}"
    )
```

**用法**：上线后下次日志里再出现本条 INFO，就说明客户端**还没改 POST**，需要催。

## 4. 已知客户端问题（待客户端修复）

- **客户端代码误用 `GET /api/v1/users/names`**（2 次 historical hits + 这次改完之后仍然可能再出现）。服务端只支持 POST，客户端应改成 `POST + JSON body {user_ids: [...]}`。
- **服务端不再为这个 path 提供 GET 实现**（已在 `/login` 时 cursor, 业务反复掂量后决定）。

## 5. 验证

```bash
# 1. 单查 404 日志
GET /api/v1/users/user_xxxxxxxxxxxx/name (合法 JWT) → 404 + INFO
# 预期: 2026-08-02 18:09:04 INFO [API] get_user_name 404: user_id=user_xxxxxxxxxxxx jwt_user_id=user_a6e69f185da0 remote=...

# 2. batch 跳过汇总
POST /api/v1/users/names body={user_ids: [exists, miss1, miss2, miss3]} → 200 + INFO
# 预期: INFO [API] batch_get_user_names skip: requested=4 missing=3 sample=miss1,miss2,miss3 ...

# 3. GET /users/names 误用
GET /api/v1/users/names → 405 + INFO
# 预期: INFO [API] /users/names wrong method: method=GET remote=...
```

## 6. 相关时点

- 2026-07-28 14:38:32  业务后端 `GET /api/app/profile/getCurrentProfile` 404（旧接口已下架）
- 2026-08-01 15:22:34  业务后端 `GET /api/frontend/app/external/users/me` 404（接口未上线）
- 2026-08-02 15:48:19  客户端开始大量调用 `/api/v1/users/{user_id}/name` 全 404
- 2026-08-02 17:53     最后一次 404（之后客户端停止调用）
- 2026-08-02 18:09     服务端 404 INFO 日志上线验证通过
