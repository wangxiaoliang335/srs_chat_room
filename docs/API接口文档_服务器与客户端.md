# 服务器与客户端接口文档

## 服务地址

| 服务 | 地址 |
|------|------|
| API 服务器 | `http://47.107.33.154:8089` |
| WebSocket 服务 | `ws://47.107.33.154:8086` |
| SRS RTMP 推流 | `rtmp://47.107.33.154:1935/live` |
| SRS HTTP-FLV 播放 | `http://47.107.33.154:8080/live` |

---

## 一、翻译服务接口

### 1.1 申请翻译

**请求**
```
POST /api/v1/translation/request
Content-Type: application/json

{
    "room_id": "room_1780139050530",
    "source_user": "211",
    "target_user": "212",
    "to_lang": "en",
    "source_lang": "auto"
}
```

**参数说明**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| room_id | string | 是 | 房间ID |
| source_user | string | 是 | 说话人用户ID |
| target_user | string | 是 | 听翻译的用户ID |
| to_lang | string | 是 | 目标语言代码，如 `en`, `zh`, `ja`, `ko` |
| source_lang | string | 否 | 源语言，默认为 `auto`（自动检测） |

**响应**
```json
{
    "code": 0,
    "message": "success",
    "data": {
        "request_id": "xxx-xxx-xxx",
        "stream_url": "rtmp://47.107.33.154:1935/live/room_1780139050530_211_to_en.flv",
        "play_url": "http://47.107.33.154:8080/live/room_1780139050530_211_to_en.flv"
    }
}
```

**播放地址格式**
```
http://47.107.33.154:8080/live/{room_id}_{source_user}_to_{to_lang}.flv
```

**示例**
```
播放地址: http://47.107.33.154:8080/live/room_1780139050530_211_to_en.flv
```

---

### 1.2 取消翻译

**请求**
```
POST /api/v1/translation/cancel
Content-Type: application/json

{
    "request_id": "xxx-xxx-xxx"
}
```

或按条件取消：

```json
{
    "room_id": "room_1780139050530",
    "source_user": "211",
    "to_lang": "en"
}
```

**响应**
```json
{
    "code": 0,
    "message": "success"
}
```

---

### 1.3 查询翻译状态

**请求**
```
GET /api/v1/translation/requests
```

**响应**
```json
{
    "code": 0,
    "message": "success",
    "data": {
        "requests": [
            {
                "request_id": "xxx",
                "room_id": "room_xxx",
                "source_user": "211",
                "to_lang": "en",
                "status": "ACTIVE"
            }
        ]
    }
}
```

---

### 1.4 查询翻译流状态

**请求**
```
GET /api/v1/translation/streams/{room_id}/{user_id}
```

**响应**
```json
{
    "code": 0,
    "message": "success",
    "data": {
        "stream_exists": true,
        "has_puller": true,
        "pullers": ["client_xxx"]
    }
}
```

---

## 二、WebSocket 推送服务

### 2.1 获取 WebSocket 连接地址

**请求**
```
POST /api/v1/ws/subscribe
Content-Type: application/json

{
    "room_id": "room_1780139050530",
    "user_id": "211"
}
```

**响应**
```json
{
    "code": 0,
    "message": "success",
    "data": {
        "ws_url": "ws://47.107.33.154:8086/ws?room=room_1780139050530&user=211",
        "room_id": "room_1780139050530",
        "user_id": "211"
    }
}
```

---

### 2.2 WebSocket 推送消息格式

连接成功后，服务器会主动推送以下消息：

#### 翻译结果
```json
{
    "type": "translation",
    "request_id": "xxx",
    "original_text": "你好",
    "translated_text": "Hello",
    "is_final": true
}
```

#### 翻译流状态
```json
{
    "type": "translation_status",
    "request_id": "xxx",
    "status": "ACTIVE",
    "message": "翻译服务运行中"
}
```

#### 说话人状态
```json
{
    "type": "speaking",
    "room_id": "room_xxx",
    "user_id": "211",
    "is_speaking": true,
    "duration_ms": 1500
}
```

#### 心跳响应
```json
{
    "type": "pong"
}
```

---

### 2.3 客户端心跳

客户端应定期发送心跳（建议每 30 秒）：

```json
{
    "type": "ping"
}
```

服务器会回复：
```json
{
    "type": "pong"
}
```

---

## 三、RTMP 推流地址

### 3.1 客户端推流地址

客户端推流到 SRS 服务器：

```
rtmp://47.107.33.154:1935/live/{room_id}_{user_id}
```

**vhost 说明**：
- SRS 服务器配置了 `__defaultVhost__`
- 如果使用默认 vhost，可以不指定 `?vhost=xxx`
- 服务器会自动识别客户端使用的 vhost 并记录

**FFmpeg 推流示例**：
```bash
ffmpeg -re -i input.aac \
  -vn -af aresample=16000 -ac 1 \
  -c:a aac -b:a 32k \
  -f flv \
  rtmp://47.107.33.154:1935/live/room_xxx_211
```

---

### 3.2 推流参数建议

| 参数 | 值 | 说明 |
|------|-----|------|
| 采样率 | 16000 Hz | 推荐 16kHz |
| 声道 | 单声道 | mono |
| 编码 | AAC | aac 编码 |
| 比特率 | 32k | 推荐 32kbps |

**FFmpeg 推流示例**：
```bash
ffmpeg -re -i input.aac \
  -vn -af aresample=16000 -ac 1 \
  -c:a aac -b:a 32k \
  -f flv \
  rtmp://47.107.33.154:1935/live?vhost=vid-7320903/room_xxx_211
```

---

## 四、HTTP-FLV 播放地址

### 4.1 翻译流播放地址

```
http://47.107.33.154:8080/live/{room_id}_{source_user}_to_{target_lang}.flv
```

**示例**：
```
http://47.107.33.154:8080/live/room_1780139050530_211_to_en.flv
```

---

### 4.2 播放端点

客户端应使用 flv.js 或类似库播放 HTTP-FLV 流：

```javascript
// 使用 flv.js 示例
const flvPlayer = flvjs.createPlayer({
    type: 'flv',
    url: 'http://47.107.33.154:8080/live/room_xxx_211_to_en.flv'
});
flvPlayer.attachMediaElement(videoElement);
flvPlayer.load();
flvPlayer.play();
```

---

## 五、健康检查

### 5.1 服务器健康检查

```
GET /health
```

**响应**
```json
{
    "status": "healthy",
    "timestamp": 1623456789
}
```

---

## 六、错误码

| code | 说明 |
|------|------|
| 0 | 成功 |
| 400 | 参数错误 |
| 404 | 资源不存在 |
| 409 | 资源已存在（如重复申请翻译） |
| 500 | 服务器内部错误 |

---

## 七、完整流程示例

### 7.1 用户 A 说话，用户 B 听翻译

```
1. 用户 A 开始推流
   RTMP: rtmp://47.107.33.154:1935/live/room_xxx_A

2. 用户 B 申请翻译
   POST /api/v1/translation/request
   {
       "room_id": "room_xxx",
       "source_user": "A",
       "target_user": "B",
       "to_lang": "en"
   }
   
   获得 play_url: http://47.107.33.154:8080/live/room_xxx_A_to_en.flv

3. 用户 B 播放翻译流
   使用 flv.js 播放上述地址

4. 用户 B 接收 WebSocket 推送
   收到翻译文本、说话人状态等消息

5. 用户 A 停止推流
   服务器自动停止翻译服务

6. 用户 B 取消翻译（可选）
   POST /api/v1/translation/cancel
```

---

## 八、注意事项

1. **RTMP 推流地址**：直接使用 `rtmp://47.107.33.154:1935/live/{room_id}_{user_id}`，无需指定 vhost

2. **翻译流自动清理**：源流停止后，翻译服务会在 20 分钟内自动退出

3. **心跳机制**：WebSocket 客户端应每 30 秒发送一次心跳

4. **CORS 支持**：服务器已配置允许跨域访问

5. **编码格式**：推流建议使用 AAC 编码，16kHz 单声道
