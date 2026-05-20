#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
语音翻译服务 - WebSocket 实时版
使用百度实时语音翻译 WebSocket API 实现真正的低延迟翻译

特点：
- WebSocket 实时流式传输
- 语音识别 + 机器翻译 + TTS 语音合成 一体化
- 真正的实时性，无轮询延迟
- 支持 45 种语言
"""

import os
import sys
import json
import logging
import subprocess
import threading
import queue
import time
import signal
import fcntl
import requests
from typing import Optional, Dict, Any

from baidu_realtime_translation import BaiduRealtimeTranslationClient, RealtimeTranslationProcessor

# 配置日志：同时输出到控制台和文件
log_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'audio_translation_websocket.log')

# 文件日志处理器
file_handler = logging.FileHandler(log_file, encoding='utf-8')
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))

# 控制台日志处理器
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.INFO)
console_handler.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[file_handler, console_handler]
)
logger = logging.getLogger(__name__)


class RealtimeTranslationService:
    """实时翻译服务（WebSocket 版本）
    
    使用百度实时语音翻译 WebSocket API
    """
    
    def __init__(self, app_id: str, app_key: str,
                 from_lang: str, to_lang: str,
                 sample_rate: int = 16000,
                 return_tts: bool = True,
                 tts_speaker: str = "woman"):
        """初始化
        
        Args:
            app_id: 百度应用 App ID
            app_key: 百度应用 App Key  
            from_lang: 源语言
            to_lang: 目标语言
            sample_rate: 采样率
            return_tts: 是否返回 TTS
            tts_speaker: TTS 发音人
        """
        self.app_id = app_id
        self.app_key = app_key
        self.from_lang = from_lang
        self.to_lang = to_lang
        self.sample_rate = sample_rate
        self.return_tts = return_tts
        self.tts_speaker = tts_speaker
        
        self.processor = None
        self.is_running = False
        self.request_id = os.getenv("REQUEST_ID", "unknown")
        
        # 自动重连机制
        # 注意：由于类使用了 @property，这里需要用 __dict__ 直接设置
        self.__dict__["_needs_reconnect"] = False
        self._reconnect_count = 0
        self._max_reconnects = 10  # 最多重连次数
        self._reconnect_delay = 3  # 重连延迟（秒）
        
        # 翻译结果缓冲
        self.translation_buffer = []
        self.translation_queue = queue.Queue(maxsize=100)
        
        # TTS 音频队列
        self.tts_queue = queue.Queue(maxsize=100)

        # TTS 保存配置
        self._tts_save_enabled = False
        self._tts_save_dir = "tts_recordings"
        
        # 输入音频保存配置（发送给百度的原始语音）
        self._input_save_enabled = os.getenv("INPUT_SAVE_ENABLED", "false").lower() == "true"
        self._input_save_dir = os.getenv("INPUT_SAVE_DIR", "input_recordings")
        
        # 统计数据
        self.stats = {
            "audio_sent": 0,
            "translations_received": 0,
            "tts_received": 0,
            "errors": 0
        }
    
    def _on_translation_result(self, result: Dict):
        """翻译结果回调"""
        self.stats["translations_received"] += 1
        
        asr_text = result.get("asr_text", "")
        trans_text = result.get("translation", "")
        is_final = result.get("is_final", False)
        
        if is_final and trans_text:
            logger.info(f"[{self.request_id}] Translation [FIN]: '{asr_text}' -> '{trans_text}'")
            
            # 推送到客户端（文本）
            self._push_translation_text(asr_text, trans_text, self.from_lang, self.to_lang)
            
            # 如果不需要 TTS，则使用本地 TTS
            if not self.return_tts:
                # 这里可以添加本地 TTS 调用
                pass
    
    def _on_tts_audio(self, audio_data: bytes):
        """TTS 音频回调"""
        self.stats["tts_received"] += 1
        self.stats["last_tts_size"] = len(audio_data)
        
        # 将 TTS 音频放入队列
        try:
            self.tts_queue.put_nowait(audio_data)
            queue_size = self.tts_queue.qsize()
            # 每10个TTS包打印一次队列状态
            if self.stats["tts_received"] % 10 == 0:
                logger.info(f"[{self.request_id}] TTS received: size={len(audio_data)} bytes, queue_size={queue_size}")
            if queue_size > 80:  # 队列积压超过80%
                logger.warning(f"[{self.request_id}] TTS queue backlog: queue_size={queue_size}/100 (80%+)")
        except queue.Full:
            logger.warning(f"[{self.request_id}] TTS queue FULL, dropping {len(audio_data)} bytes")
    
    def _on_error(self, code: int, msg: str):
        """错误回调"""
        self.stats["errors"] += 1
        logger.error(f"[{self.request_id}] Translation error: code={code}, msg={msg}")
        # 标记需要重连
        self._needs_reconnect = True

    def set_tts_save_config(self, enabled: bool, save_dir: str = "tts_recordings"):
        """配置 TTS 音频保存

        Args:
            enabled: 是否启用保存
            save_dir: 保存目录
        """
        self._tts_save_enabled = enabled
        self._tts_save_dir = save_dir
        if self.processor:
            self.processor.set_tts_save_config(enabled, save_dir)

    def set_input_save_config(self, enabled: bool, save_dir: str = "input_recordings"):
        """配置输入音频保存（发送给百度的原始语音）

        Args:
            enabled: 是否启用保存
            save_dir: 保存目录
        """
        self._input_save_enabled = enabled
        self._input_save_dir = save_dir
        if self.processor:
            self.processor.set_input_save_config(enabled, save_dir)

    def connect(self) -> bool:
        """连接到百度实时翻译服务"""
        self.processor = RealtimeTranslationProcessor(
            app_id=self.app_id,
            app_key=self.app_key,
            from_lang=self.from_lang,
            to_lang=self.to_lang,
            sample_rate=self.sample_rate,
            return_tts=self.return_tts,
            tts_speaker=self.tts_speaker
        )
        
        # 设置回调
        self.processor.on_translation_callback = self._on_translation_result
        self.processor.on_tts_callback = self._on_tts_audio
        self.processor.on_error_callback = self._on_error

        # 应用 TTS 保存配置
        if self._tts_save_enabled:
            self.processor.set_tts_save_config(True, self._tts_save_dir)
        
        # 应用输入音频保存配置
        if self._input_save_enabled:
            self.processor.set_input_save_config(True, self._input_save_dir)

        if not self.processor.connect():
            logger.error(f"[{self.request_id}] Failed to connect to Baidu realtime translation")
            return False
        
        self.is_running = True
        logger.info(f"[{self.request_id}] Connected to Baidu realtime translation")
        return True
    
    def is_connected(self) -> bool:
        """检查是否连接到百度服务"""
        if not self.processor:
            return False
        return self.processor.is_connected()
    
    @property
    def _needs_reconnect(self) -> bool:
        """检查是否需要重连"""
        return self.__dict__.get("_needs_reconnect", False)
    
    @_needs_reconnect.setter
    def _needs_reconnect(self, value: bool):
        """设置重连标志"""
        self.__dict__["_needs_reconnect"] = value
    
    def add_audio(self, audio_data: bytes):
        """添加音频数据"""
        # 诊断：检查连接状态
        if not self.processor:
            logger.warning(f"[{self.request_id}] Processor not initialized")
            return
        if not self.processor.is_connected():
            logger.warning(f"[{self.request_id}] Processor not connected, skipping audio")
            return
        self.processor.add_audio(audio_data)
        self.stats["audio_sent"] += len(audio_data)
        # 诊断：每64KB打印一次发送状态
        if self.stats["audio_sent"] % 65536 < len(audio_data):
            logger.info(f"[{self.request_id}] add_audio called: {len(audio_data)} bytes, total_sent={self.stats['audio_sent']}")
    
    def get_tts_audio(self, timeout: float = 0.1) -> Optional[bytes]:
        """获取 TTS 音频"""
        try:
            return self.tts_queue.get(timeout=timeout)
        except queue.Empty:
            return None
    
    def disconnect(self):
        """断开连接"""
        self.is_running = False
        self._needs_reconnect = False  # 重置重连标志
        if self.processor:
            self.processor.disconnect()
            self.processor = None
        logger.info(f"[{self.request_id}] Disconnected")
    
    def _push_translation_text(self, original_text: str, translated_text: str,
                               from_lang: str, to_lang: str):
        """推送翻译文本给客户端"""
        target_user = os.getenv("TARGET_USER", "")
        room_id = os.getenv("ROOM_ID", "")
        text_server_url = os.getenv("TEXT_SERVER_URL", "http://localhost:8085")
        
        if not target_user:
            return
        
        try:
            push_url = f"{text_server_url}/api/v1/translation/text/push"
            data = {
                "target_user": target_user,
                "request_id": self.request_id,
                "room_id": room_id,
                "source_user": os.getenv("SOURCE_USER", ""),
                "original_text": original_text,
                "translated_text": translated_text,
                "source_lang": from_lang,
                "target_lang": to_lang,
                "timestamp": time.time()
            }
            
            response = requests.post(push_url, json=data, timeout=3)
            if response.status_code == 200:
                logger.debug(f"[{self.request_id}] Pushed translation text")
        except Exception as e:
            logger.warning(f"[{self.request_id}] Failed to push translation text: {e}")


class AudioStreamProcessorWebSocket:
    """音频流处理器（WebSocket 版本）"""
    
    def __init__(self, srs_url: str, room_id: str, source_user: str, to_lang: str,
                 stream_name: str, app_id: str, app_key: str,
                 audio_config: Optional[Dict[str, Any]] = None,
                 from_lang: str = "auto"):
        self.srs_url = srs_url
        self.room_id = room_id
        self.source_user = source_user
        self.to_lang = to_lang
        self.from_lang = from_lang  # 源语言
        self.stream_name = stream_name
        self.app_id = app_id
        self.app_key = app_key
        self.ffmpeg_process = None
        self.output_process = None
        self.is_running = False
        self.request_id = os.getenv("REQUEST_ID", "unknown")
        self.stream_vhost = "__defaultVhost__"  # 默认 vhost
        
        # 音频配置
        self.audio_config = audio_config or {}
        self.sample_rate = self.audio_config.get("sample_rate", 16000)
        self.channels = self.audio_config.get("channels", 1)
        
        # 实时翻译服务
        self.realtime_service = None
        
        # 静音填充包配置
        # 每块音频大小: 16bits * 1ch * 40ms = 1280 bytes @ 16000Hz
        self.chunk_size = int(self.sample_rate * 2 * 40 / 1000)
        # 静音包（40ms的静音数据）
        self.silence_chunk = b'\x00' * self.chunk_size
        # 静默超时阈值：超过这个时间没有音频数据就发送静音包（秒）
        self.silence_heartbeat_interval = 3.0  # 3秒发一次心跳
        # 上次有音频数据的时间
        self.last_audio_time = time.time()
        # 上次发送心跳的时间
        self.last_heartbeat_time = time.time()
        
        # PCM 保存配置（FFmpeg 转换后、发送给百度前）
        self.pcm_save_enabled = os.getenv("PCM_SAVE_ENABLED", "true").lower() == "true"
        self.pcm_save_dir = os.getenv("PCM_SAVE_DIR", "pcm_recordings")
        self.pcm_save_file = None
        if self.pcm_save_enabled:
            os.makedirs(self.pcm_save_dir, exist_ok=True)
    
    def _start_new_pcm_file(self):
        """创建新的 PCM 保存文件"""
        if not self.pcm_save_enabled:
            return
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        microseconds = int(time.time() * 1000000) % 1000000
        filename = f"pcm_{timestamp}_{microseconds:06d}.pcm"
        filepath = os.path.join(self.pcm_save_dir, filename)
        self.pcm_save_file = open(filepath, 'wb')
        logger.info(f"[{self.request_id}] Started PCM save: {filepath}")
    
    def _write_pcm_audio(self, audio_data: bytes):
        """写入 PCM 音频数据到文件"""
        if not self.pcm_save_enabled:
            return
        if not self.pcm_save_file:
            self._start_new_pcm_file()
        self.pcm_save_file.write(audio_data)
    
    def _close_pcm_file(self):
        """关闭 PCM 保存文件"""
        if self.pcm_save_file:
            try:
                self.pcm_save_file.close()
                logger.info(f"[{self.request_id}] Closed PCM save file")
            except Exception as e:
                logger.warning(f"[{self.request_id}] Failed to close PCM save file: {e}")
            self.pcm_save_file = None
    
    def _get_stream_vhost(self) -> str:
        """从 SRS API 获取流的 vhost"""
        try:
            import json as json_module
            resp = requests.get("http://127.0.0.1:1985/api/v1/streams/", timeout=3)
            data = resp.json()
            stream_pattern = f"{self.room_id}_{self.source_user}"
            for stream in data.get('streams', []):
                if stream.get('name') == stream_pattern and stream.get('clients', 0) > 0:
                    vhost = stream.get('vhost', '__defaultVhost__')
                    logger.info(f"[{self.request_id}] Found active stream vhost: {vhost}")
                    return vhost
        except Exception as e:
            logger.warning(f"[{self.request_id}] Failed to get stream vhost: {e}")
        return "__defaultVhost__"
    
    def _wait_for_stream_ready(self) -> bool:
        """等待 SRS 源流就绪（使用SRS API检查，不依赖HTTP FLV）"""
        stream_pattern = f"{self.room_id}_{self.source_user}"
        logger.info(f"[{self.request_id}] Checking if stream is ready: {stream_pattern}")

        for attempt in range(1, 11):
            try:
                # 使用SRS API检查流是否存在且有客户端
                resp = requests.get("http://127.0.0.1:1985/api/v1/streams/", timeout=3)
                data = resp.json()
                for stream in data.get('streams', []):
                    if stream.get('name') == stream_pattern:
                        clients = stream.get('clients', 0)
                        if clients > 0:
                            # 获取 vhost
                            self.stream_vhost = stream.get('vhost', '__defaultVhost__')
                            logger.info(f"[{self.request_id}] Stream ready (attempt {attempt}), vhost={self.stream_vhost}, clients={clients}")
                            return True
                        else:
                            logger.info(f"[{self.request_id}] Stream not ready (attempt {attempt}): no clients")
                            break
                else:
                    logger.info(f"[{self.request_id}] Stream not found (attempt {attempt})")
            except requests.exceptions.RequestException as e:
                logger.info(f"[{self.request_id}] Stream check failed (attempt {attempt}): {e}")

            if attempt < 10:
                time.sleep(2)

        logger.warning(f"[{self.request_id}] Stream never became ready, will start with default vhost")
        self.stream_vhost = "__defaultVhost__"
        return False
    
    def start(self):
        """启动音频流处理"""
        self.is_running = True
        
        # 先获取流的 vhost
        self.stream_vhost = self._get_stream_vhost()
        
        # 启动音频读取线程
        threading.Thread(target=self._read_audio_thread, daemon=True).start()
        logger.info(f"[{self.request_id}] Audio read thread started")
        
        # 启动 TTS 音频推送线程
        threading.Thread(target=self._push_tts_audio_thread, daemon=True).start()
        logger.info(f"[{self.request_id}] TTS audio push thread started")
    
    def _start_ffmpeg_input(self, http_flv_url: str):
        """启动 FFmpeg 输入进程"""
        ffmpeg_bin = os.environ.get("FFMPEG_BIN", "/usr/bin/ffmpeg")
        
        # 使用动态获取的 vhost (格式: rtmp://ip:port/app?vhost=xxx/stream_key)
        rtmp_url = f"rtmp://127.0.0.1:1935/live?vhost={self.stream_vhost}/{self.room_id}_{self.source_user}"
        
        # 优化：增加线程数、减少分析开销、增大缓冲区
        # 关键优化：
        # - threads=4: 多线程解码，解决 speed 太慢的问题
        # - 最小化 probesize/analyzeduration: 减少启动延迟
        # - 禁用不必要的解码选项
        ffmpeg_input_cmd = [
            ffmpeg_bin,
            "-threads", "4",              # 多线程解码，解决 speed 太慢的问题
            "-fflags", "nobuffer",
            "-flags", "low_delay",
            "-rtmp_live", "live",
            "-analyzeduration", "50000",  # 减少到50ms，加快启动
            "-probesize", "50000",        # 减少到50KB
            "-max_delay", "500000",        # 减少到0.5s
            "-flush_packets", "1",
            "-i", rtmp_url,
            "-vn",
            "-acodec", "pcm_s16le",
            "-ar", str(self.sample_rate),
            "-ac", str(self.channels),
            "-f", "s16le",
            "-thread_queue_size", "512",  # 增大队列缓冲
            "-nostdin",
            "-y",
            "-"
        ]
        
        logger.info(f"[{self.request_id}] Starting FFmpeg input: {rtmp_url}")
        
        stderr_log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 
                                       'audio_translation_ffmpeg_input_websocket.log')
        stderr_file = open(stderr_log_path, 'a')
        
        self.ffmpeg_process = subprocess.Popen(
            ffmpeg_input_cmd,
            stdout=subprocess.PIPE,
            stderr=stderr_file,
            bufsize=0
        )
        
        # 设置 stdout 为非阻塞模式
        stdout_fd = self.ffmpeg_process.stdout.fileno()
        flags = fcntl.fcntl(stdout_fd, fcntl.F_GETFL)
        fcntl.fcntl(stdout_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        
        return self.ffmpeg_process
    
    def _read_audio_thread(self):
        """读取音频数据并发送到实时翻译服务
        
        注意：此线程只有在以下情况下才会退出：
        1. 用户主动停止翻译 (is_running = False)
        否则会一直重试，即使源流中断也会自动恢复
        """
        chunk_size = 8192
        http_flv_port = self.audio_config.get("http_flv_port", 8080)
        srs_api_url = self.audio_config.get("srs_api_url", "http://localhost:1985")
        # SRS HTTP FLV路径格式: /live/stream.flv（vhost通过默认vhost或Host头确定）
        http_flv_url = f"http://127.0.0.1:{http_flv_port}/live/{self.room_id}_{self.source_user}.flv"
        
        logger.info(f"[{self.request_id}] Audio read thread started, chunk_size={chunk_size}")
        
        # 重试配置：直到用户停止才退出
        max_retries = 600          # 最多重试 600 次
        retry_interval = 1         # 每次重试间隔 1 秒
        retry_count = 0
        start_time = time.time()
        last_error_type = None
        
        # 百度翻译服务重连相关
        baidu_reconnect_count = 0
        max_baidu_reconnects = 600  # 增加到 600 次
        
        while self.is_running:
            try:
                # 检查是否超时重置计数器
                elapsed = time.time() - start_time
                if elapsed > 600:
                    logger.info(f"[{self.request_id}] Audio read retry cycle reset, elapsed={elapsed:.1f}s")
                    start_time = time.time()
                    retry_count = 0
                
                # 等待源流就绪
                stream_ready = self._wait_for_stream_ready()
                if not stream_ready:
                    logger.warning(f"[{self.request_id}] Stream not ready, retry={retry_count}/{max_retries}")
                    retry_count += 1
                    time.sleep(retry_interval)
                    continue
                
                # 连接实时翻译服务
                if not self.realtime_service:
                    self.realtime_service = RealtimeTranslationService(
                        app_id=self.app_id,
                        app_key=self.app_key,
                        from_lang=self.from_lang,  # 使用配置的源语言
                        to_lang=self.to_lang,
                        sample_rate=self.sample_rate,
                        return_tts=True,
                        tts_speaker="woman"
                    )

                    # 启用 TTS 音频保存
                    tts_save_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tts_recordings")
                    self.realtime_service.set_tts_save_config(enabled=True, save_dir=tts_save_dir)
                    logger.info(f"[{self.request_id}] TTS recording enabled: {tts_save_dir}")

                    # 启用输入音频保存
                    input_save_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "input_recordings")
                    self.realtime_service.set_input_save_config(enabled=True, save_dir=input_save_dir)
                    logger.info(f"[{self.request_id}] Input recording enabled: {input_save_dir}")

                    if not self.realtime_service.connect():
                        logger.error(f"[{self.request_id}] Failed to connect to Baidu realtime service")
                        time.sleep(retry_interval)
                        continue
                    
                    baidu_reconnect_count = 0  # 重置百度重连计数
                    logger.info(f"[{self.request_id}] Connected to Baidu realtime translation service")
                
                # 启动 ffmpeg 输入进程
                if not self.ffmpeg_process:
                    self._start_ffmpeg_input(http_flv_url)
                    logger.info(f"[{self.request_id}] FFmpeg input started with PID: {self.ffmpeg_process.pid}")
                
                read_count = 0
                total_bytes_read = 0
                consecutive_empty_count = 0
                last_audio_time = time.time()
                last_heartbeat_time = time.time()
                
                while self.is_running and self.ffmpeg_process:
                    # 检查百度是否需要重连（音频无效被断开等）
                    if self.realtime_service and self.realtime_service._needs_reconnect:
                        if baidu_reconnect_count >= max_baidu_reconnects:
                            logger.warning(f"[{self.request_id}] Max Baidu reconnection attempts reached, waiting longer...")
                            time.sleep(5)
                            baidu_reconnect_count = 0  # 重置计数，继续等待
                            continue
                        
                        baidu_reconnect_count += 1
                        logger.info(f"[{self.request_id}] Reconnecting to Baidu (attempt {baidu_reconnect_count}/{max_baidu_reconnects})...")
                        
                        # 断开旧连接
                        if self.realtime_service:
                            self.realtime_service.disconnect()
                        time.sleep(0.5)  # 减少重连延迟，从1秒改为0.5秒
                        
                        # 重新创建连接
                        self.realtime_service = RealtimeTranslationService(
                            app_id=self.app_id,
                            app_key=self.app_key,
                            from_lang=self.from_lang,
                            to_lang=self.to_lang,
                            sample_rate=self.sample_rate,
                            return_tts=True,
                            tts_speaker="woman"
                        )

                        # 启用 TTS 音频保存
                        tts_save_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tts_recordings")
                        self.realtime_service.set_tts_save_config(enabled=True, save_dir=tts_save_dir)

                        # 启用输入音频保存
                        input_save_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "input_recordings")
                        self.realtime_service.set_input_save_config(enabled=True, save_dir=input_save_dir)

                        if not self.realtime_service.connect():
                            logger.error(f"[{self.request_id}] Baidu reconnection failed")
                            time.sleep(0.5)  # 减少重连延迟
                            continue
                        
                        baidu_reconnect_count = 0  # 重置计数
                        last_heartbeat_time = time.time()  # 重置心跳时间
                        logger.info(f"[{self.request_id}] Reconnected to Baidu successfully")
                    
                    # 检查 ffmpeg 进程状态
                    poll_result = self.ffmpeg_process.poll()
                    if poll_result is not None:
                        error_type = f"FFmpeg input exited with code {poll_result}"
                        if error_type != last_error_type:
                            logger.error(f"[{self.request_id}] FFmpeg input process exited! returncode={poll_result}")
                            last_error_type = error_type
                        break
                    
                    # 读取音频数据
                    try:
                        audio_chunk = self.ffmpeg_process.stdout.read(chunk_size)
                    except (BlockingIOError, IOError):
                        time.sleep(0.01)
                        continue
                    except Exception as e:
                        logger.error(f"[{self.request_id}] Error reading from FFmpeg: {e}")
                        break
                    
                    read_count += 1
                    current_time = time.time()
                    
                    if not audio_chunk:
                        consecutive_empty_count += 1
                        
                        # 改进：发送静音填充包保持百度会话活跃
                        # 每隔 silence_heartbeat_interval 秒发送一个静音包
                        time_since_last_heartbeat = current_time - last_heartbeat_time
                        if time_since_last_heartbeat >= self.silence_heartbeat_interval:
                            if self.realtime_service:
                                self.realtime_service.add_audio(self.silence_chunk)
                                last_heartbeat_time = current_time
                                # 减少日志频率：每30秒打印一次
                                if consecutive_empty_count % 3000 == 0:
                                    logger.info(f"[{self.request_id}] Sending silence heartbeat (no audio for {consecutive_empty_count/100:.1f}s)")
                        
                        # 检查是否长时间没有音频数据（可能源流已停止）
                        if consecutive_empty_count >= 10000:  # 约 100 秒
                            logger.warning(f"[{self.request_id}] No audio for 100s, restarting FFmpeg...")
                            break
                        elif consecutive_empty_count % 2000 == 0:  # 减少日志频率，每20秒打印一次
                            logger.info(f"[{self.request_id}] Waiting for audio... consecutive_empty={consecutive_empty_count}")
                        time.sleep(0.01)
                        continue
                    
                    consecutive_empty_count = 0
                    total_bytes_read += len(audio_chunk)
                    last_audio_time = current_time
                    
                    # 调试：检查音频数据内容
                    if total_bytes_read <= 65536:
                        logger.info(f"[{self.request_id}] Received audio chunk: {len(audio_chunk)} bytes, total={total_bytes_read}")
                    
                    # FFmpeg 转换后立即保存 PCM（发送给百度之前）
                    self._write_pcm_audio(audio_chunk)
                    
                    # 发送到实时翻译服务
                    if self.realtime_service:
                        self.realtime_service.add_audio(audio_chunk)
                        last_heartbeat_time = current_time  # 有实际音频时重置心跳时间
                    
                    if read_count % 500 == 0:  # 减少日志频率
                        logger.info(f"[{self.request_id}] Audio stats: total_bytes={total_bytes_read}, chunks={read_count}")
                
                # 循环结束，处理重连
                if self.is_running:
                    # FFmpeg 退出或出错，重启
                    logger.info(f"[{self.request_id}] Audio read loop ended, total={total_bytes_read} bytes, will retry...")
                    
                    # 关闭 PCM 文件
                    self._close_pcm_file()
                    
                    # 清理资源，准备重试
                    if self.ffmpeg_process:
                        try:
                            self.ffmpeg_process.terminate()
                            self.ffmpeg_process.wait(timeout=2)
                        except:
                            pass
                        self.ffmpeg_process = None
                    
                    # 短暂等待后重试
                    time.sleep(retry_interval)
                    
            except Exception as e:
                logger.error(f"[{self.request_id}] Error in audio read thread: {e}", exc_info=True)
                time.sleep(retry_interval)
        
        logger.info(f"[{self.request_id}] Audio read thread ended")

    
    def _push_tts_audio_thread(self):
        """推送 TTS 音频到 SRS
        
        注意：此线程只有在以下情况下才会退出：
        1. 用户主动停止翻译 (is_running = False)
        2. 源流停止推送
        否则会一直重试，最多重试 10 分钟（600 秒）
        """
        # SRS 地址 - 优先使用内网地址，如果连接失败再用公网地址
        srs_host = os.environ.get("SRS_HOST", "127.0.0.1")
        srs_port = os.environ.get("SRS_PORT", "1935")
        
        # 使用标准 RTMP URL 格式: rtmp://host/vhost/app/stream
        # 注意: vhost 名称需要与 SRS 配置一致
        # 翻译流始终使用 __defaultVhost__，因为只有它配置了 http_remux
        vhost = "__defaultVhost__"
        # 流名称添加 .flv 后缀，因为客户端通过 HTTP-FLV 播放，需要带 .flv 扩展名
        output_stream_name = f"{self.stream_name}.flv"
        # RTMP URL 格式: rtmp://ip:port/app?vhost=xxx/stream_key
        rtmp_url = f"rtmp://{srs_host}:{srs_port}/live?vhost={vhost}/{output_stream_name}"
        logger.info(f"[{self.request_id}] TTS output stream: {rtmp_url}")

        # FFmpeg 命令（接收 MP3 音频）
        # 百度TTS返回16kHz采样率的MP3，需要转码为AAC才能推流到SRS
        # 注意：必须使用 -af 音频过滤器强制重采样，-ar 和 -ac 只是输入格式声明
        ffmpeg_bin = os.environ.get("FFMPEG_BIN", "/usr/bin/ffmpeg")
        ffmpeg_output_cmd = [
            ffmpeg_bin,
            "-hide_banner",           # 隐藏 banner 信息
            "-loglevel", "error",     # 只显示错误
            "-i", "-",               # 从 stdin 读取，让 FFmpeg 自动检测格式
            "-af", "aresample=16000:filter_size=64:cutoff=0.95,pan=mono|c0=c0",  # 强制重采样为 16kHz 单声道
            "-c:a", "aac",           # 转码为 AAC
            "-b:a", "64k",
            "-f", "flv",
            "-fflags", "nobuffer",
            "-flags", "low_delay",
            "-max_delay", "5000000",  # 最大延迟 5 秒
            "-reconnect", "1",        # 自动重连
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5",
            rtmp_url
        ]
        
        logger.info(f"[{self.request_id}] Starting FFmpeg output process: {' '.join(ffmpeg_output_cmd)}")
        
        # 重试配置：直到用户停止或源流停止才退出
        max_retries = 600          # 最多重试 600 次
        retry_interval = 1         # 每次重试间隔 1 秒
        retry_count = 0
        start_time = time.time()
        last_error_type = None
        
        # stderr buffer 用于存储 ffmpeg 错误信息
        stderr_buffer = []
        stderr_thread = None
        
        try:
            while self.is_running:
                # 检查是否超时（10 分钟 = 600 秒）
                elapsed = time.time() - start_time
                if elapsed > 600:
                    logger.warning(f"[{self.request_id}] FFmpeg output retry timeout (10min), but still running...")
                    # 重置计数器，继续尝试
                    start_time = time.time()
                    retry_count = 0
                
                # 检查是否需要启动/重启 ffmpeg
                if not self.output_process or self.output_process.poll() is not None:
                    # 关闭之前的进程（如果存在）
                    if self.output_process:
                        try:
                            self.output_process.terminate()
                            self.output_process.wait(timeout=2)
                        except:
                            pass
                    
                    # 记录重试信息
                    if retry_count > 0:
                        logger.info(f"[{self.request_id}] FFmpeg restart attempt {retry_count}/{max_retries}, elapsed={elapsed:.1f}s")
                    
                    retry_count += 1
                    stderr_buffer = []
                    
                    try:
                        # 使用 PIPE 捕获 stderr，并设置 bufsize=0 以立即写入
                        self.output_process = subprocess.Popen(
                            ffmpeg_output_cmd,
                            stdin=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            bufsize=0
                        )
                        
                        # 启动 stderr 读取线程
                        stderr_thread = threading.Thread(
                            target=self._read_ffmpeg_stderr,
                            args=(self.output_process.stderr, stderr_buffer),
                            daemon=True
                        )
                        stderr_thread.start()
                        
                        logger.info(f"[{self.request_id}] FFmpeg output started, PID={self.output_process.pid}")
                    except Exception as e:
                        logger.error(f"[{self.request_id}] Failed to start FFmpeg: {e}")
                        time.sleep(retry_interval)
                        continue
                
                # 检查 FFmpeg 进程状态
                if self.output_process:
                    poll_result = self.output_process.poll()
                    
                    # 如果进程已退出
                    if poll_result is not None:
                        # 获取 stderr 内容
                        if stderr_thread:
                            stderr_thread.join(timeout=1)
                        stderr_text = "".join(stderr_buffer)
                        
                        # 分类错误类型
                        if "Unknown encoder" in stderr_text or "codec not found" in stderr_text:
                            error_type = "AAC encoder not found"
                            logger.error(f"[{self.request_id}] AAC encoder not found! Check FFmpeg build.")
                        elif "Connection refused" in stderr_text:
                            error_type = "SRS connection refused"
                            logger.error(f"[{self.request_id}] Cannot connect to SRS at {rtmp_url} - Connection refused")
                        elif "Server error" in stderr_text or "404" in stderr_text:
                            error_type = "SRS server error"
                            logger.error(f"[{self.request_id}] SRS server error")
                        elif "Invalid data" in stderr_text:
                            error_type = "Invalid input data"
                            logger.error(f"[{self.request_id}] Invalid input data format!")
                        elif poll_result != 0:
                            error_type = f"FFmpeg exited with code {poll_result}"
                            if stderr_text:
                                logger.error(f"[{self.request_id}] FFmpeg stderr: {stderr_text[:500]}")
                        else:
                            error_type = "Unknown"
                            if stderr_text:
                                logger.warning(f"[{self.request_id}] FFmpeg stderr: {stderr_text[:200]}")
                        
                        # 只在错误类型变化时打印警告
                        if error_type != last_error_type:
                            logger.warning(f"[{self.request_id}] FFmpeg output error: {error_type}, retry={retry_count}/{max_retries}, elapsed={elapsed:.1f}s")
                            last_error_type = error_type
                        
                        # 关闭进程引用
                        self.output_process = None
                        time.sleep(retry_interval)
                        continue
                
                # 获取 TTS 音频
                tts_audio = None
                if self.realtime_service:
                    tts_audio = self.realtime_service.get_tts_audio(timeout=0.1)
                
                # 诊断日志：每 2 秒打印一次状态
                if not hasattr(self, '_last_tts_diag_time'):
                    self._last_tts_diag_time = time.time()
                current_time = time.time()
                if current_time - self._last_tts_diag_time >= 2.0:
                    self._last_tts_diag_time = current_time
                    realtime_connected = self.realtime_service.is_connected() if self.realtime_service else False
                    output_running = self.output_process.poll() is None if self.output_process else False
                    output_exists = self.output_process is not None
                    tts_queue_size = self.realtime_service.tts_queue.qsize() if self.realtime_service else 0
                    queue_warning = " [WARNING: queue backlog!]" if tts_queue_size > 80 else ""
                    logger.info(f"[{self.request_id}] TTS Status: realtime_connected={realtime_connected}, output_exists={output_exists}, output_running={output_running}, tts_queue_size={tts_queue_size}{queue_warning}")
                
                # 写入音频数据
                if tts_audio:
                    if self.output_process and self.output_process.poll() is None:
                        try:
                            stdin_valid = self.output_process.stdin is not None
                            process_running = self.output_process.poll() is None
                            
                            if stdin_valid and process_running:
                                self.output_process.stdin.write(tts_audio)
                                self.output_process.stdin.flush()
                                
                                audio_processed = getattr(self, '_audio_processed', 0) + len(tts_audio)
                                setattr(self, '_audio_processed', audio_processed)
                                
                                # 减少日志频率，每10个TTS包打印一次
                                tts_write_count = getattr(self, '_tts_write_count', 0) + 1
                                setattr(self, '_tts_write_count', tts_write_count)
                                if tts_write_count % 10 == 0:
                                    logger.info(f"[{self.request_id}] TTS written: size={len(tts_audio)} bytes, total={audio_processed}, writes={tts_write_count}")
                            else:
                                logger.warning(f"[{self.request_id}] FFmpeg stdin invalid: stdin={stdin_valid}, running={process_running}")
                        except (BrokenPipeError, OSError, IOError) as e:
                            logger.error(f"[{self.request_id}] Broken pipe/IO error writing to FFmpeg: {e}")
                            if self.output_process:
                                try:
                                    self.output_process.terminate()
                                except:
                                    pass
                            self.output_process = None
                    else:
                        # FFmpeg 进程不存在或已退出，打印诊断信息
                        if not self.output_process:
                            logger.warning(f"[{self.request_id}] FFmpeg output process is None (TTS audio dropped: {len(tts_audio)} bytes)")
                        elif self.output_process.poll() is not None:
                            logger.warning(f"[{self.request_id}] FFmpeg output process exited with code: {self.output_process.poll()} (TTS audio dropped: {len(tts_audio)} bytes)")
                else:
                    # 没有 TTS 音频时短暂休眠
                    time.sleep(0.01)
            
            # 正常退出时打印统计
            total_processed = getattr(self, '_audio_processed', 0)
            logger.info(f"[{self.request_id}] TTS push thread ended. Total: {total_processed} bytes, retries={retry_count}")
            
        except Exception as e:
            logger.error(f"[{self.request_id}] Error in TTS push thread: {e}", exc_info=True)
        finally:
            # 确保进程被正确关闭
            if self.output_process:
                try:
                    self.output_process.terminate()
                    self.output_process.wait(timeout=2)
                except:
                    pass
    
    def _read_ffmpeg_stderr(self, stderr_pipe, buffer: list):
        """读取 FFmpeg stderr 的线程函数"""
        try:
            # 使用循环读取 stderr，避免阻塞
            import select
            while True:
                # 使用 select 检查是否有数据可读
                ready, _, _ = select.select([stderr_pipe], [], [], 0.5)
                if ready:
                    try:
                        chunk = os.read(stderr_pipe.fileno(), 4096)
                        if not chunk:
                            break
                        buffer.append(chunk.decode('utf-8', errors='replace'))
                    except (OSError, ValueError):
                        break
                else:
                    # 超时，检查进程是否还在运行
                    if self.output_process and self.output_process.poll() is not None:
                        # 进程已退出，尝试读取剩余的 stderr
                        try:
                            import fcntl
                            # 设置为非阻塞模式
                            fd = stderr_pipe.fileno()
                            flags = fcntl.fcntl(fd, fcntl.F_GETFL)
                            fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
                            while True:
                                try:
                                    chunk = os.read(fd, 4096)
                                    if not chunk:
                                        break
                                    buffer.append(chunk.decode('utf-8', errors='replace'))
                                except (OSError, BlockingIOError):
                                    break
                        except:
                            pass
                        break
        except Exception as e:
            buffer.append(f"stderr read error: {e}\n")
            logger.warning(f"[{getattr(self, 'request_id', 'unknown')}] stderr read error: {e}")
    
    def stop(self):
        """停止音频流处理"""
        self.is_running = False
        
        # 关闭 PCM 文件
        self._close_pcm_file()
        
        if self.realtime_service:
            self.realtime_service.disconnect()
        
        if self.ffmpeg_process:
            self.ffmpeg_process.terminate()
            self.ffmpeg_process.wait()
        
        if self.output_process:
            self.output_process.terminate()
            self.output_process.wait()


class TranslationServiceWebSocket:
    """翻译服务主类（WebSocket 版本）"""
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.audio_processor = None
        self.request_id = os.getenv("REQUEST_ID", "unknown")
    
    def initialize(self):
        """初始化服务"""
        app_id = self.config.get("baidu_app_id")
        app_key = self.config.get("baidu_app_key")
        
        if not app_id or not app_key:
            raise ValueError("Baidu App ID and App Key are required for WebSocket translation")
        
        logger.info(f"[{self.request_id}] Initializing WebSocket translation service")
        logger.info(f"[{self.request_id}] App ID: {app_id[:8]}...")
    
    def start(self):
        """启动处理"""
        srs_url = self.config.get("srs_url", "http://localhost:8080")
        room_id = self.config.get("room_id", "")
        source_user = self.config.get("source_user", "")
        to_lang = self.config.get("to_lang", "en")
        from_lang = self.config.get("from_lang", "auto")  # 源语言
        stream_name = self.config.get("stream_name", "")
        
        app_id = self.config.get("baidu_app_id")
        app_key = self.config.get("baidu_app_key")
        
        logger.info(f"[{self.request_id}] Starting WebSocket AudioStreamProcessor")
        
        audio_config = self.config.get("audio", {})
        
        self.audio_processor = AudioStreamProcessorWebSocket(
            srs_url=srs_url,
            room_id=room_id,
            source_user=source_user,
            to_lang=to_lang,
            from_lang=from_lang,
            stream_name=stream_name,
            app_id=app_id,
            app_key=app_key,
            audio_config=audio_config
        )
        
        self.audio_processor.start()
        
        logger.info(f"[{self.request_id}] Started: room={room_id}, source_user={source_user}, "
                   f"to_lang={to_lang}, stream_name={stream_name}")
    
    def stop(self):
        """停止服务"""
        logger.info(f"[{self.request_id}] Stopping WebSocket translation service...")
        
        if self.audio_processor:
            self.audio_processor.stop()
        
        logger.info(f"[{self.request_id}] WebSocket translation service stopped")


def main():
    """主函数"""
    request_id = os.getenv("REQUEST_ID", "unknown")
    room_id = os.getenv("ROOM_ID", "")
    source_user = os.getenv("SOURCE_USER", "")
    to_lang = os.getenv("TO_LANG", "en")
    from_lang = os.getenv("FROM_LANG", "auto")  # 源语言，默认为 auto
    stream_name = os.getenv("STREAM_NAME", "")
    
    # WebSocket 翻译需要 App ID 和 App Key
    app_id = os.getenv("BAIDU_APP_ID", "")
    app_key = os.getenv("BAIDU_APP_KEY", "")
    
    config = {
        "baidu_app_id": app_id,
        "baidu_app_key": app_key,
        "srs_url": os.getenv("SRS_URL", "http://localhost:8080"),
        "room_id": room_id,
        "source_user": source_user,
        "to_lang": to_lang,
        "from_lang": from_lang,
        "stream_name": stream_name,
    }
    
    # 从配置文件加载音频配置
    config_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 
                               "audio_translation_config.json")
    if os.path.exists(config_file):
        try:
            with open(config_file, 'r', encoding='utf-8') as f:
                file_config = json.load(f)
            if "audio" in file_config:
                config["audio"] = file_config["audio"]
            logger.info(f"[{request_id}] Loaded audio config from {config_file}")
        except Exception as e:
            logger.warning(f"[{request_id}] Failed to load audio config: {e}")
    
    if not app_id or not app_key:
        logger.error(f"[{request_id}] Please set BAIDU_APP_ID and BAIDU_APP_KEY environment variables")
        logger.error(f"[{request_id}] WebSocket translation requires App ID and App Key")
        sys.exit(1)
    
    if not room_id or not source_user or not stream_name:
        logger.error(f"[{request_id}] Please set ROOM_ID, SOURCE_USER, STREAM_NAME environment variables")
        sys.exit(1)
    
    logger.info(f"[{request_id}] Starting WebSocket translation service")
    logger.info(f"[{request_id}]   room={room_id}, source_user={source_user}")
    logger.info(f"[{request_id}]   to_lang={to_lang}, stream_name={stream_name}")
    
    service = TranslationServiceWebSocket(config)
    
    try:
        service.initialize()
        logger.info(f"[{request_id}] Service initialized successfully")
        service.start()
        
        logger.info("WebSocket translation service is running. Press Ctrl+C to stop.")
        while True:
            time.sleep(1)
            
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    except Exception as e:
        logger.error(f"[{request_id}] Service error: {e}", exc_info=True)
    finally:
        service.stop()


if __name__ == "__main__":
    main()
