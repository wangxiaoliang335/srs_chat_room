# 语音翻译服务使用说明

## 功能说明

本服务实现了语音聊天室的实时翻译功能：
1. 从SRS接收音频流
2. 调用百度实时语音翻译API将语音翻译为英文
3. 将翻译后的语音推送回SRS，供其他用户收听

## 环境要求

- Python 3.7+
- FFmpeg（用于音频流处理）
- 百度AI开放平台账号和API密钥

## 安装依赖

```bash
pip install -r requirements.txt
```

## 配置说明

### 1. 环境变量配置

```bash
export BAIDU_API_KEY="your_baidu_api_key"
export BAIDU_SECRET_KEY="your_baidu_secret_key"
export SRS_URL="http://localhost:8080"
export ROOM_ID="room_123"
```

### 2. 配置文件

编辑 `audio_translation_config.json`：

```json
{
  "baidu": {
    "api_key": "your_api_key",
    "secret_key": "your_secret_key"
  },
  "srs": {
    "url": "http://localhost:8080",
    "rtmp_url": "rtmp://localhost/live"
  }
}
```

## 使用方法

### 启动服务

```bash
python audio_translation_service.py
```

### 集成到SRS

#### 方案1：使用HTTP Hooks（推荐）

在SRS配置文件中添加HTTP回调：

```conf
vhost __defaultVhost__ {
    http_hooks {
        enabled         on;
        on_publish      http://your-server:8085/api/v1/streams/on_publish;
        on_unpublish    http://your-server:8085/api/v1/streams/on_unpublish;
    }
}
```

创建HTTP回调服务（`callback_server.py`）：

```python
from flask import Flask, request, jsonify
import subprocess
import os

app = Flask(__name__)

@app.route('/api/v1/streams/on_publish', methods=['POST'])
def on_publish():
    data = request.json
    stream_name = data.get('stream')
    
    # 启动翻译服务
    subprocess.Popen([
        'python', 'audio_translation_service.py',
        '--room-id', stream_name
    ])
    
    return jsonify({'code': 0})

@app.route('/api/v1/streams/on_unpublish', methods=['POST'])
def on_unpublish():
    # 停止翻译服务
    # 实现停止逻辑
    return jsonify({'code': 0})

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8085)
```

#### 方案2：手动启动

为每个房间手动启动翻译服务：

```bash
ROOM_ID=room_123 python audio_translation_service.py
```

## 工作流程

```
用户A说话 → SRS接收音频流 → 翻译服务接收音频
    ↓
翻译服务调用百度API翻译 → 获得英文文本
    ↓
TTS服务将文本转为英文语音 → 推送到SRS
    ↓
其他用户收听翻译后的英文语音
```

详细步骤：

1. **音频接收**：服务从SRS的HTTP-FLV流接收音频
   - 流地址：`http://srs-server:8080/live/{room_id}.flv`
   - 使用FFmpeg从HTTP-FLV流拉取音频

2. **音频转换**：使用FFmpeg将音频转换为PCM格式
   - 格式：PCM 16位，16kHz采样率，单声道
   - 这是百度语音识别API要求的格式

3. **实时翻译**：调用百度实时语音翻译API
   - 使用WebSocket连接（如果支持）
   - 或使用HTTP API实时发送音频数据
   - 接收翻译结果（英文文本）

4. **语音合成**：将翻译后的文本转换为语音
   - 使用百度TTS API
   - 生成英文语音（PCM格式）
   - 支持调整语速、音调、音量

5. **音频推送**：将翻译后的音频推送到SRS
   - RTMP推流地址：`rtmp://srs-server/live/{room_id}_translated`
   - 使用FFmpeg将PCM音频编码为AAC并推流
   - 其他用户订阅翻译后的流：`webrtc://srs-server/live/{room_id}_translated`

## 快速开始

### 1. 配置百度API密钥

```bash
export BAIDU_API_KEY="your_api_key"
export BAIDU_SECRET_KEY="your_secret_key"
```

### 2. 启动翻译服务

```bash
./start_translation_service.sh
```

### 3. 配置SRS使用翻译服务

使用提供的配置文件启动SRS：

```bash
./objs/srs -c conf/rtc_with_translation.conf
```

### 4. 测试

1. 用户A发布音频流到房间 `room_123`
2. 翻译服务自动启动，开始翻译
3. 其他用户订阅翻译后的流：`{room_id}_translated`

## 注意事项

1. **百度API配置**：
   - 需要在[百度AI开放平台](https://ai.baidu.com/)申请以下服务：
     - 实时语音识别（ASR）
     - 机器翻译（MT）
     - 语音合成（TTS）
   - 获取API Key和Secret Key
   - 注意API的调用频率限制和配额

2. **音频格式要求**：
   - 输入：SRS输出的音频（AAC/OPUS等）
   - 转换后：PCM 16位，16kHz采样率，单声道
   - 输出：AAC编码，64kbps比特率

3. **延迟处理**：
   - 翻译过程会有一定延迟（通常1-3秒）
   - 建议使用缓冲机制平滑延迟
   - 可以考虑使用流式翻译减少延迟

4. **错误处理**：
   - 服务会自动重连WebSocket
   - FFmpeg进程异常退出时会自动重启
   - API调用失败会记录日志并继续尝试

5. **资源消耗**：
   - 每个房间需要2个FFmpeg进程（输入+输出）
   - 每个房间需要独立的翻译客户端连接
   - 注意服务器CPU和内存限制

6. **网络要求**：
   - 需要稳定的网络连接到百度API服务器
   - 建议使用国内服务器以减少延迟

7. **成本考虑**：
   - 百度API按调用次数或时长计费
   - 建议监控API使用量
   - 可以考虑缓存常用翻译结果

## 故障排查

### 1. 无法连接SRS

检查：
- SRS服务是否运行
- HTTP-FLV流是否可用
- 网络连接是否正常

### 2. 翻译失败

检查：
- 百度API密钥是否正确
- API配额是否充足
- 网络连接是否正常

### 3. 音频无法推送

检查：
- RTMP推流地址是否正确
- SRS是否允许推流
- FFmpeg是否正常安装

## 架构说明

### 服务组件

1. **callback_server.py**: HTTP回调服务器
   - 接收SRS的HTTP Hooks回调
   - 自动启动/停止翻译服务
   - 管理翻译服务进程

2. **audio_translation_service.py**: 核心翻译服务
   - 从SRS接收音频流
   - 调用百度翻译API
   - 推送翻译后的音频

3. **tts_service.py**: 文本转语音服务
   - 将翻译后的文本转换为语音
   - 使用百度TTS API

### 数据流

```
SRS音频流 (HTTP-FLV)
    ↓
FFmpeg (音频转换: AAC → PCM)
    ↓
百度实时语音识别 (ASR)
    ↓
百度机器翻译 (MT: 中文 → 英文)
    ↓
百度语音合成 (TTS: 文本 → 英文语音)
    ↓
FFmpeg (音频编码: PCM → AAC)
    ↓
SRS推流 (RTMP)
    ↓
用户接收翻译后的音频
```

## 扩展功能

### 支持多语言翻译

修改配置支持翻译到其他语言：

```python
# 在BaiduTranslationClient中添加目标语言参数
target_language = "en"  # 英文，可改为 "ja"(日文), "ko"(韩文) 等
```

### 支持文本翻译（不转换语音）

如果只需要文本翻译，可以：
1. 接收翻译后的文本
2. 通过WebSocket或HTTP API发送给客户端
3. 客户端显示翻译文本（字幕形式）

### 集成其他翻译服务

可以替换为其他翻译服务：
- Google Cloud Translation API
- Azure Speech Translation
- 阿里云语音识别和翻译
- 腾讯云语音识别

### 优化建议

1. **缓存机制**：
   - 缓存常用短语的翻译结果
   - 减少API调用次数

2. **批量处理**：
   - 将短音频片段合并处理
   - 提高翻译准确性

3. **错误恢复**：
   - 实现自动重试机制
   - 失败时使用备用翻译服务

4. **性能优化**：
   - 使用异步处理
   - 多线程处理多个房间

## 许可证

MIT License
