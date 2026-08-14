# 百度ASR音频格式支持说明

## 概述

**百度语音识别（ASR）API支持多种音频格式，不仅仅是PCM格式。**

## 支持的音频格式

根据百度官方文档，百度ASR API支持以下格式：

| 格式 | 说明 | 适用场景 |
|------|------|----------|
| **PCM** | 原始PCM格式（未压缩） | 实时流式识别（推荐） |
| **WAV** | 无损音频格式 | 文件上传识别 |
| **MP3** | 有损压缩格式 | 通用音频文件 |
| **AMR** | 窄带语音编码格式 | 电话场景 |
| **FLAC** | 压缩无损格式 | 高质量音频 |
| **AAC** | 高级音频编码 | 流媒体场景（RTMP/HTTP-FLV） |

**注意：百度ASR不支持Opus格式**。如果输入流是Opus格式，代码会自动转换为PCM格式。

## 音频参数要求

无论使用哪种格式，百度ASR对音频有统一的技术要求：

- **采样率**：8kHz（电话场景）或 **16kHz（推荐，通用场景）**
- **位深**：16bit
- **声道**：单声道（推荐）
- **信噪比**：需高于15dB

## 当前代码实现

### 统一使用AAC格式（当前默认）

**当前代码统一将所有输入音频转换为AAC格式发送给百度ASR**：

```python
# audio_translation_service.py
ffmpeg_input_cmd = [
    "ffmpeg",
    "-i", input_url,
    "-vn",
    "-acodec", "aac",      # 统一编码为AAC
    "-ar", "16000",
    "-ac", "1",
    "-b:a", "64k",         # AAC比特率64kbps
    "-f", "adts",          # AAC ADTS格式
    "-"
]
```

**优点：**
- ✅ 数据量小（压缩格式），减少网络传输
- ✅ 百度ASR原生支持AAC格式
- ✅ 统一格式，简化代码逻辑
- ✅ 适合流媒体场景

### 支持Opus输入格式（自动转换）

**百度ASR不支持Opus格式**，但如果输入流是Opus格式，代码会自动使用FFmpeg将其转换为AAC格式。

**配置文件** (`audio_translation_config.json`):
```json
{
  "audio": {
    "sample_rate": 16000,
    "channels": 1,
    "asr_format": "aac",
    "input_format": "opus"  // 指定输入格式为Opus（可选，FFmpeg会自动检测）
  }
}
```

**代码会自动处理Opus输入**：
- 如果检测到输入是Opus格式，FFmpeg会自动解码并转换为AAC
- 转换过程对用户透明，无需额外配置

## 格式选择说明

### 当前统一使用AAC格式（推荐）

**当前代码统一使用AAC格式发送给百度ASR**，这是经过优化的选择：

**优点：**
- ✅ 数据量小（压缩格式），减少网络传输量约75%
- ✅ 百度ASR原生支持，识别准确率高
- ✅ 适合流媒体场景（RTMP/HTTP-FLV）
- ✅ 统一格式，简化代码逻辑和维护
- ✅ 减少带宽占用，降低网络成本

**技术细节：**
- AAC比特率：64kbps（适合语音识别）
- 采样率：16kHz（百度ASR推荐）
- 声道：单声道
- 格式：AAC ADTS（百度ASR支持）

**适用场景：**
- ✅ 所有实时语音识别场景（当前默认）
- ✅ 流媒体音频处理
- ✅ 对带宽敏感的场景
- ✅ 需要减少数据传输的场景

## 配置示例

### 示例1：使用AAC格式（当前默认，推荐）

```json
{
  "audio": {
    "sample_rate": 16000,
    "channels": 1,
    "asr_format": "aac"
  }
}
```

**说明：**
- 这是当前默认配置
- 所有输入音频（无论原始格式）都会统一转换为AAC格式
- 适合所有场景，推荐使用

### 示例2：输入流是Opus格式

```json
{
  "audio": {
    "sample_rate": 16000,
    "channels": 1,
    "asr_format": "aac",
    "input_format": "opus"  // 指定输入格式为Opus（可选）
  }
}
```

**说明：**
- 如果输入流是Opus，代码会自动转换为AAC格式发送给百度ASR
- `input_format` 参数是可选的，FFmpeg通常可以自动检测输入格式
- 如果明确知道输入格式，建议设置`input_format`以便日志记录

## 代码修改位置

如果需要修改音频格式，主要涉及以下文件：

1. **`audio_translation_config.json`** - 配置文件
2. **`audio_translation_service.py`** - 音频处理逻辑
3. **`baidu_asr_client.py`** - ASR客户端（已支持多种格式）

## Opus格式处理

### 百度ASR不支持Opus格式

**重要提示：** 百度ASR API目前不支持Opus格式。支持的格式包括：PCM、WAV、MP3、AMR、FLAC、AAC。

### Opus输入的处理方案

如果您的输入流是Opus格式（例如WebRTC推流），代码会自动处理：

1. **自动转换**：使用FFmpeg将Opus解码并转换为AAC格式
2. **透明处理**：转换过程对用户透明，无需额外配置
3. **性能考虑**：Opus转AAC需要解码和编码，会有一定的CPU开销

### 配置示例

```json
{
  "audio": {
    "sample_rate": 16000,
    "channels": 1,
    "asr_format": "aac",
    "input_format": "opus"  // 可选：明确指定输入格式
  }
}
```

### 性能优化建议

- 如果可能，建议在推流端使用AAC格式（百度ASR支持，无需转换）
- 如果必须使用Opus，确保FFmpeg已安装libopus解码器
- 监控CPU使用率，必要时调整`chunk_size_ms`参数
- AAC格式可以减少约75%的数据传输量，降低网络带宽需求

## 注意事项

1. **格式一致性**：当前统一使用AAC格式，`asr_format`应设置为`"aac"`
2. **采样率匹配**：确保采样率符合百度ASR要求（8kHz或16kHz，推荐16kHz）
3. **声道数**：推荐使用单声道
4. **数据块大小**：AAC是压缩格式，chunk_size已根据64kbps比特率优化（约2000字节/200ms）
5. **Opus格式**：如果输入是Opus，会自动转换为AAC，无需额外配置
6. **AAC比特率**：当前设置为64kbps，适合语音识别场景

## 测试建议

1. 使用默认AAC格式测试，确保功能正常
2. 监控CPU使用率和网络带宽，AAC格式可以显著减少带宽占用
3. 如果遇到识别问题，检查采样率和声道数配置
4. 对于Opus输入，确保FFmpeg已安装libopus解码器

## 参考文档

- [百度语音识别API文档](https://cloud.baidu.com/doc/SPEECH/index.html)
- [百度ASR格式要求](https://ai.baidu.com/ai-doc/SPEECH/Vk38lxily)
