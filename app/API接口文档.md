# SRS 对外API接口文档

本文档列出了SRS服务器提供给其他业务模块调用的所有HTTP API接口。

## 基础信息

- **默认端口**: 1985
- **协议**: HTTP/HTTPS
- **响应格式**: JSON (除非特别说明)
- **编码**: UTF-8

---

## 一、系统信息类接口

### 1. API根路径
- **路径**: `/`
- **方法**: GET
- **描述**: 获取API根信息，包含所有可用接口列表
- **响应示例**:
```json
{
  "code": 0,
  "server": "...",
  "service": "...",
  "pid": "...",
  "urls": {
    "api": "the api root",
    "rtc": {
      "v1": {
        "play": "Play stream",
        "publish": "Publish stream",
        "nack": "Simulate the NACK"
      }
    }
  }
}
```

### 2. API版本信息
- **路径**: `/api/v1/versions`
- **方法**: GET
- **描述**: 获取SRS版本信息
- **响应示例**:
```json
{
  "code": 0,
  "server": "...",
  "service": "...",
  "pid": "...",
  "data": {
    "major": 5,
    "minor": 0,
    "revision": 0,
    "version": "5.0.0"
  }
}
```

### 3. API列表
- **路径**: `/api/`
- **方法**: GET
- **描述**: 获取API版本列表

### 4. API v1列表
- **路径**: `/api/v1/`
- **方法**: GET
- **描述**: 获取v1版本的所有可用接口列表

### 5. 系统摘要信息
- **路径**: `/api/v1/summaries`
- **方法**: GET
- **描述**: 获取SRS系统摘要信息（pid, argv, pwd, cpu, mem等）

### 6. 资源使用情况
- **路径**: `/api/v1/rusages`
- **方法**: GET
- **描述**: 获取系统资源使用情况（rusage信息）

### 7. 自身进程统计
- **路径**: `/api/v1/self_proc_stats`
- **方法**: GET
- **描述**: 获取SRS自身进程的统计信息

### 8. 系统进程统计
- **路径**: `/api/v1/system_proc_stats`
- **方法**: GET
- **描述**: 获取系统进程统计信息

### 9. 内存信息
- **路径**: `/api/v1/meminfos`
- **方法**: GET
- **描述**: 获取系统内存信息

### 10. 作者信息
- **路径**: `/api/v1/authors`
- **方法**: GET
- **描述**: 获取SRS的许可证、版权、作者和贡献者信息

### 11. 功能特性
- **路径**: `/api/v1/features`
- **方法**: GET
- **描述**: 获取SRS支持的功能特性列表

---

## 二、流媒体管理类接口

### 12. 虚拟主机管理
- **路径**: `/api/v1/vhosts/` 或 `/api/v1/vhosts/{vhost_id}`
- **方法**: GET
- **描述**: 获取所有虚拟主机或指定虚拟主机信息
- **查询参数**:
  - `vhost_id` (路径参数): 虚拟主机ID
- **响应示例**:
```json
{
  "code": 0,
  "server": "...",
  "service": "...",
  "pid": "...",
  "vhosts": [...]
}
```

### 13. 流管理
- **路径**: `/api/v1/streams/` 或 `/api/v1/streams/{stream_id}`
- **方法**: GET
- **描述**: 获取所有流或指定流信息
- **查询参数**:
  - `stream_id` (路径参数): 流ID
  - `start` (查询参数): 起始位置，默认0
  - `count` (查询参数): 返回数量，默认10
- **响应示例**:
```json
{
  "code": 0,
  "server": "...",
  "service": "...",
  "pid": "...",
  "streams": [...]
}
```

### 14. 客户端管理
- **路径**: `/api/v1/clients/` 或 `/api/v1/clients/{client_id}`
- **方法**: GET, DELETE
- **描述**: 
  - GET: 获取所有客户端或指定客户端信息
  - DELETE: 踢掉指定客户端连接
- **查询参数**:
  - `client_id` (路径参数): 客户端ID
  - `start` (查询参数): 起始位置，默认0
  - `count` (查询参数): 返回数量，默认10
- **响应示例**:
```json
{
  "code": 0,
  "server": "...",
  "service": "...",
  "pid": "...",
  "clients": [...]
}
```

---

## 三、RTC (WebRTC) 类接口

### 15. RTC播放
- **路径**: `/rtc/v1/play/`
- **方法**: POST
- **描述**: 创建WebRTC播放会话
- **请求体**:
```json
{
  "sdp": "offer...",
  "streamurl": "webrtc://r.ossrs.net/live/livestream",
  "api": "http...",
  "clientip": "...",
  "tid": "..."
}
```
- **查询参数**:
  - `eip` 或 `candidate`: 服务器候选IP
  - `codec`: 编解码器
  - `encrypt` 或 `srtp`: 是否加密
  - `dtls`: 是否启用DTLS
- **响应示例**:
```json
{
  "code": 0,
  "server": "...",
  "service": "...",
  "pid": "...",
  "sdp": "answer...",
  "sessionid": "..."
}
```

### 16. RTC发布
- **路径**: `/rtc/v1/publish/`
- **方法**: POST
- **描述**: 创建WebRTC发布会话
- **请求体**:
```json
{
  "sdp": "offer...",
  "streamurl": "webrtc://r.ossrs.net/live/livestream",
  "api": "http...",
  "clientip": "...",
  "tid": "..."
}
```
- **查询参数**:
  - `eip` 或 `candidate`: 服务器候选IP
  - `codec`: 编解码器
- **响应示例**:
```json
{
  "code": 0,
  "server": "...",
  "service": "...",
  "pid": "...",
  "sdp": "answer...",
  "sessionid": "..."
}
```

### 17. WHIP协议
- **路径**: `/rtc/v1/whip/`
- **方法**: POST, DELETE
- **描述**: 
  - POST: 使用WHIP协议发布流
  - DELETE: 停止发布（需要token参数）
- **请求体**: SDP格式（非JSON）
- **查询参数**:
  - `app`: 应用名，默认"live"
  - `stream`: 流名，默认"livestream"
  - `action`: 操作类型，默认"publish"
  - `eip` 或 `candidate`: 服务器候选IP
  - `codec`: 编解码器
  - `encrypt` 或 `srtp`: 是否加密
  - `dtls`: 是否启用DTLS
  - `ice-ufrag`: ICE用户名片段
  - `ice-pwd`: ICE密码
  - `session`: 会话ID（DELETE时使用）
  - `token`: 会话令牌（DELETE时使用）
- **响应**: 
  - POST: 返回SDP格式，HTTP状态码201
  - DELETE: HTTP状态码200

### 18. WHIP播放 (WHEP)
- **路径**: `/rtc/v1/whip-play/` 或 `/rtc/v1/whep/`
- **方法**: POST, DELETE
- **描述**: 使用WHEP协议播放流
- **请求体**: SDP格式（非JSON）
- **查询参数**: 同WHIP协议
- **响应**: 同WHIP协议

### 19. RTC NACK模拟
- **路径**: `/rtc/v1/nack/`
- **方法**: POST
- **描述**: 模拟NACK丢包（仅用于测试，需要编译时启用SRS_SIMULATOR）
- **查询参数**:
  - `username`: 会话用户名
  - `drop`: 丢包数量
- **响应示例**:
```json
{
  "code": 0,
  "query": {
    "username": "...",
    "drop": "...",
    "help": "?username=string&drop=int"
  }
}
```

---

## 四、配置和管理类接口

### 20. 原始API
- **路径**: `/api/v1/raw`
- **方法**: GET, POST
- **描述**: 原始API，用于查询和更新配置
- **查询参数**:
  - `rpc`: RPC方法
    - `raw`: 查询原始配置
    - `reload`: 重新加载配置
    - `reload-fetch`: 获取重载状态
- **响应示例**:
```json
{
  "code": 0,
  "data": {
    "err": 0,
    "msg": "...",
    "state": 0,
    "rid": "..."
  }
}
```

### 21. 集群API
- **路径**: `/api/v1/clusters`
- **方法**: GET
- **描述**: 获取集群服务器信息
- **查询参数**:
  - `ip`: IP地址
  - `vhost`: 虚拟主机
  - `app`: 应用名
  - `stream`: 流名
  - `coworker`: 协作服务器
- **响应示例**:
```json
{
  "code": 0,
  "data": {
    "query": {
      "ip": "...",
      "vhost": "...",
      "app": "...",
      "stream": "..."
    },
    "origin": {...}
  }
}
```

---

## 五、监控和指标类接口

### 22. Prometheus指标
- **路径**: `/metrics`
- **方法**: GET
- **描述**: 获取Prometheus格式的监控指标
- **响应格式**: text/plain (Prometheus格式)
- **响应示例**:
```
# HELP srs_cpu_percent SRS cpu used percent.
# TYPE srs_cpu_percent gauge
srs_cpu_percent 10.5

# HELP srs_memory SRS memory used.
# TYPE srs_memory gauge
srs_memory 1024

# HELP srs_streams The number of SRS concurrent streams.
# TYPE srs_streams gauge
srs_streams 5
...
```

---

## 六、测试类接口

### 23. 测试请求信息
- **路径**: `/api/v1/tests/requests`
- **方法**: GET
- **描述**: 返回请求信息，用于HTTP调试

### 24. 测试错误
- **路径**: `/api/v1/tests/errors`
- **方法**: GET
- **描述**: 始终返回错误码100，用于测试错误处理

### 25. 测试重定向
- **路径**: `/api/v1/tests/redirects`
- **方法**: GET
- **描述**: 重定向到 `/api/v1/tests/errors`

---

## 七、其他接口

### 26. TCMalloc信息
- **路径**: `/api/v1/tcmalloc`
- **方法**: GET
- **描述**: 获取TCMalloc内存分配信息（需要编译时启用SRS_GPERF）
- **查询参数**:
  - `page`: 页面类型（summary|detail）
- **响应示例**:
```json
{
  "code": 0,
  "data": {
    "query": {
      "page": "...",
      "help": "?page=summary|detail"
    },
    "release_rate": 0.0,
    "generic": {...},
    "tcmalloc": {...}
  }
}
```

### 27. 控制台
- **路径**: `/console/`
- **方法**: GET
- **描述**: SRS控制台Web界面（静态文件服务）

---

## 通用响应格式

所有API接口的响应都遵循以下格式：

```json
{
  "code": 0,           // 0表示成功，非0表示错误
  "server": "...",     // 服务器ID
  "service": "...",    // 服务ID
  "pid": "...",        // 进程ID
  "data": {...}        // 具体数据（根据接口不同而不同）
}
```

## 错误码说明

- `code = 0`: 成功
- `code != 0`: 失败，具体错误码请参考SRS错误码定义

## 注意事项

1. 所有接口都支持JSONP，可通过`callback`查询参数指定回调函数名
2. RTC相关接口需要SRS启用RTC功能
3. 部分接口（如tcmalloc、nack）需要编译时启用相应功能
4. 接口默认端口为1985，可通过配置文件修改
5. 部分接口可能需要认证，具体请参考SRS安全配置

## 接口分类总结

| 分类 | 接口数量 | 主要用途 |
|------|---------|---------|
| 系统信息类 | 11 | 获取服务器版本、状态、资源使用等信息 |
| 流媒体管理类 | 3 | 管理虚拟主机、流、客户端 |
| RTC类 | 5 | WebRTC播放、发布、WHIP/WHEP协议 |
| 配置管理类 | 2 | 配置查询、重载、集群管理 |
| 监控指标类 | 1 | Prometheus监控指标 |
| 测试类 | 3 | 测试和调试 |
| 其他 | 2 | TCMalloc信息、控制台 |

**总计**: 27个对外API接口
