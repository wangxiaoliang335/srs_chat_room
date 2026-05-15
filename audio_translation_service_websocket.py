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
import base64

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
        except queue.Full:
            logger.warning(f"[{self.request_id}] TTS queue full, dropping audio")
    
    def _on_error(self, code: int, msg: str):
        """错误回调"""
        self.stats["errors"] += 1
        logger.error(f"[{self.request_id}] Translation error: code={code}, msg={msg}")
        # 标记需要重连
        self._needs_reconnect = True
    
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
        
        if not self.processor.connect():
            logger.error(f"[{self.request_id}] Failed to connect to Baidu realtime translation")
            return False
        
        self.is_running = True
        logger.info(f"[{self.request_id}] Connected to Baidu realtime translation")
        return True
    
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
        text_server_url = os.getenv("TEXT_SERVER_URL", "http://localhost:8086")
        
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
        
        # 使用动态获取的 vhost
        rtmp_url = f"rtmp://127.0.0.1:1935/live/{self.room_id}_{self.source_user}?vhost={self.stream_vhost}"
        
        # 尝试 RTMP 协议（更稳定）
        # 优化：减少probesize和分析时间以加快启动速度
        ffmpeg_input_cmd = [
            ffmpeg_bin,
            "-fflags", "nobuffer",
            "-flags", "low_delay",
            "-rtmp_live", "live",
            "-analyzeduration", "100000",    # 减少到100ms
            "-probesize", "100000",          # 减少到100KB
            "-max_delay", "1000000",         # 减少到1s
            "-flush_packets", "1",           # 立即刷新包
            "-i", rtmp_url,
            "-vn",
            "-acodec", "pcm_s16le",
            "-ar", str(self.sample_rate),
            "-ac", str(self.channels),
            "-f", "s16le",
            "-thread_queue_size", "256",    # 减少队列大小
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
        """读取音频数据并发送到实时翻译服务"""
        chunk_size = 8192
        http_flv_port = self.audio_config.get("http_flv_port", 8080)
        srs_api_url = self.audio_config.get("srs_api_url", "http://localhost:1985")
        # SRS HTTP FLV路径格式: /live/stream.flv（vhost通过默认vhost或Host头确定）
        http_flv_url = f"http://127.0.0.1:{http_flv_port}/live/{self.room_id}_{self.source_user}.flv"
        
        logger.info(f"[{self.request_id}] Audio read thread started, chunk_size={chunk_size}")
        
        # 翻译服务重连相关
        reconnect_count = 0
        max_reconnects = 100  # 增加到 100 次重连
        reconnect_delay = 1  # 减少到 1 秒延迟
        
        while self.is_running:
            try:
                # 等待源流就绪
                stream_ready = self._wait_for_stream_ready()
                if not stream_ready:
                    logger.warning(f"[{self.request_id}] Stream not ready, waiting...")
                    time.sleep(2)
                
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
                    
                    if not self.realtime_service.connect():
                        logger.error(f"[{self.request_id}] Failed to connect realtime service")
                        time.sleep(5)
                        continue
                    
                    reconnect_count = 0  # 重置重连计数
                    logger.info(f"[{self.request_id}] Connected to realtime translation service")
                
                self._start_ffmpeg_input(http_flv_url)
                logger.info(f"[{self.request_id}] FFmpeg process started with PID: {self.ffmpeg_process.pid}")
                
                read_count = 0
                total_bytes_read = 0
                consecutive_empty_count = 0
                
                while self.is_running and self.ffmpeg_process:
                    # 检查百度是否需要重连（音频无效被断开等）
                    if self.realtime_service and self.realtime_service._needs_reconnect:
                        if reconnect_count >= max_reconnects:
                            logger.warning(f"[{self.request_id}] Max reconnection attempts reached, waiting longer...")
                            # 不退出，继续等待，只是暂停一下
                            time.sleep(5)
                            reconnect_count = 0  # 重置计数，继续等待
                            continue
                        
                        reconnect_count += 1
                        logger.info(f"[{self.request_id}] Reconnecting to Baidu (attempt {reconnect_count}/{max_reconnects})...")
                        
                        # 断开旧连接
                        self.realtime_service.disconnect()
                        time.sleep(reconnect_delay)
                        
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
                        
                        if not self.realtime_service.connect():
                            logger.error(f"[{self.request_id}] Reconnection failed")
                            time.sleep(reconnect_delay)
                            continue
                        
                        reconnect_count = 0  # 重置计数
                        logger.info(f"[{self.request_id}] Reconnected successfully")
                    
                    poll_result = self.ffmpeg_process.poll()
                    if poll_result is not None:
                        logger.error(f"[{self.request_id}] FFmpeg input process exited! returncode={poll_result}")
                        break
                    
                    try:
                        audio_chunk = self.ffmpeg_process.stdout.read(chunk_size)
                    except (BlockingIOError, IOError):
                        time.sleep(0.01)
                        continue
                    except Exception as e:
                        logger.error(f"[{self.request_id}] Error reading from FFmpeg: {e}")
                        break
                    
                    read_count += 1
                    
                    if not audio_chunk:
                        consecutive_empty_count += 1
                        if consecutive_empty_count >= 10000:  # 增加到 10000，约 100 秒
                            logger.warning(f"[{self.request_id}] FFmpeg output empty for too long, assuming stream ended")
                            break
                        if consecutive_empty_count % 500 == 0:  # 减少日志频率
                            logger.info(f"[{self.request_id}] Waiting for audio... consecutive_empty={consecutive_empty_count}")
                        time.sleep(0.01)
                        continue
                    
                    consecutive_empty_count = 0
                    total_bytes_read += len(audio_chunk)
                    
                    # 发送到实时翻译服务
                    if self.realtime_service:
                        self.realtime_service.add_audio(audio_chunk)
                    
                    if read_count % 100 == 0:
                        logger.info(f"[{self.request_id}] Audio stats: total_bytes={total_bytes_read}, chunks={read_count}")
                
                logger.info(f"[{self.request_id}] Audio read loop ended. Total: {total_bytes_read} bytes, {read_count} chunks")
                
                # 断开实时翻译服务
                if self.realtime_service:
                    self.realtime_service.disconnect()
                    self.realtime_service = None
                
                if self.is_running:
                    logger.warning(f"[{self.request_id}] FFmpeg exited, waiting for stream to restart...")
                    time.sleep(0.5)  # 减少等待时间，快速重试
                    
            except Exception as e:
                logger.error(f"[{self.request_id}] Error in audio read thread: {e}", exc_info=True)
                time.sleep(1)
        
        logger.info(f"[{self.request_id}] Audio read thread ended")
    
    def _push_tts_audio_thread(self):
        """推送 TTS 音频到 SRS"""
        # SRS 地址 - 优先使用内网地址，如果连接失败再用公网地址
        srs_host = os.environ.get("SRS_HOST", "127.0.0.1")
        srs_port = os.environ.get("SRS_PORT", "1935")
        
        # 使用标准 RTMP URL 格式: rtmp://host/vhost/app/stream
        # 注意: vhost 名称需要与 SRS 配置一致
        # 使用与源流相同的 vhost (可能是 __defaultVhost__ 或其他)
        vhost = getattr(self, 'stream_vhost', '__defaultVhost__')
        # __defaultVhost__ 在 SRS API 中可能显示为 vid-xxx，但 URL 中使用 __defaultVhost__
        if vhost.startswith('vid-'):
            vhost = '__defaultVhost__'
        # 流名称添加 .flv 后缀，因为客户端通过 HTTP-FLV 播放，需要带 .flv 扩展名
        output_stream_name = f"{self.stream_name}.flv"
        rtmp_url = f"rtmp://{srs_host}:{srs_port}/live/{output_stream_name}?vhost={vhost}"
        logger.info(f"[{self.request_id}] TTS output stream: {rtmp_url}")

        # FFmpeg 命令（接收 MP3 音频）
        # 百度TTS返回16kHz采样率的MP3，需要转码为AAC才能推流到SRS
        ffmpeg_bin = os.environ.get("FFMPEG_BIN", "/usr/bin/ffmpeg")
        ffmpeg_output_cmd = [
            ffmpeg_bin,
            "-hide_banner",           # 隐藏 banner 信息
            "-loglevel", "error",     # 只显示错误
            "-i", "-",                 # 从 stdin 读取，让 FFmpeg 自动检测格式
            "-c:a", "aac",           # 转码为 AAC
            "-ar", "16000",           # 保持 16kHz 采样率
            "-ac", "1",               # 单声道
            "-b:a", "64k",
            "-f", "flv",
            "-reconnect", "1",        # 自动重连
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "2",
            rtmp_url
        ]
        
        logger.info(f"[{self.request_id}] Starting FFmpeg output process: {' '.join(ffmpeg_output_cmd)}")
        
        try:
            # 使用 PIPE 捕获 stderr，并设置 bufsize=0 以立即写入
            self.output_process = subprocess.Popen(
                ffmpeg_output_cmd,
                stdin=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0
            )
            
            logger.info(f"[{self.request_id}] FFmpeg output process started, PID={self.output_process.pid}")
            
            # 启动 stderr 读取线程，确保能捕获所有错误信息
            stderr_buffer = []
            stderr_thread = threading.Thread(
                target=self._read_ffmpeg_stderr,
                args=(self.output_process.stderr, stderr_buffer),
                daemon=True
            )
            stderr_thread.start()
            logger.info(f"[{self.request_id}] stderr reader thread started")
            
            audio_processed = 0
            max_retries = 3
            retry_count = 0
            last_check_time = time.time()
            check_interval = 1.0  # 每秒检查一次进程状态
            
            while self.is_running:
                # 定期检查 FFmpeg 进程状态
                current_time = time.time()
                if current_time - last_check_time >= check_interval:
                    last_check_time = current_time
                    
                    if self.output_process:
                        poll_result = self.output_process.poll()
                        if poll_result is not None:
                            # 进程已退出，获取 stderr 内容
                            stderr_thread.join(timeout=1)  # 等待 stderr 线程结束
                            stderr_text = "".join(stderr_buffer)
                            
                            logger.error(f"[{self.request_id}] FFmpeg output exited! returncode={poll_result}")
                            if stderr_text:
                                logger.error(f"[{self.request_id}] FFmpeg stderr:\n{stderr_text[:3000]}")
                            
                            # 检查是否有 AAC 编码器错误
                            if "Unknown encoder" in stderr_text or "codec not found" in stderr_text:
                                logger.error(f"[{self.request_id}] AAC encoder not found! Check FFmpeg build.")
                            elif "Connection refused" in stderr_text or "Server error" in stderr_text:
                                logger.error(f"[{self.request_id}] Cannot connect to SRS at {rtmp_url}")
                            elif "Invalid data found" in stderr_text or "bitrate" in stderr_text.lower():
                                logger.error(f"[{self.request_id}] MP3 input format error! TTS may be sending wrong format.")
                            
                            retry_count += 1
                            if retry_count <= max_retries:
                                logger.info(f"[{self.request_id}] Restarting FFmpeg output ({retry_count}/{max_retries})...")
                                time.sleep(2)
                                try:
                                    self.output_process = subprocess.Popen(
                                        ffmpeg_output_cmd,
                                        stdin=subprocess.PIPE,
                                        stderr=subprocess.PIPE,
                                        bufsize=0
                                    )
                                    stderr_buffer = []
                                    stderr_thread = threading.Thread(
                                        target=self._read_ffmpeg_stderr,
                                        args=(self.output_process.stderr, stderr_buffer),
                                        daemon=True
                                    )
                                    stderr_thread.start()
                                    logger.info(f"[{self.request_id}] FFmpeg restarted, PID={self.output_process.pid}")
                                except Exception as e:
                                    logger.error(f"[{self.request_id}] Failed to restart FFmpeg: {e}")
                                    break
                            else:
                                logger.error(f"[{self.request_id}] FFmpeg output failed after {max_retries} retries")
                                break
                        else:
                            # 进程正常运行，每 10 秒打印一次状态
                            if audio_processed % 500000 < 1000 and audio_processed > 0:
                                logger.info(f"[{self.request_id}] FFmpeg running OK, audio_processed={audio_processed}")
                
                # 获取 TTS 音频
                tts_audio = None
                if self.realtime_service:
                    tts_audio = self.realtime_service.get_tts_audio(timeout=0.1)
                    if tts_audio:
                        logger.info(f"[{self.request_id}] Got TTS audio from queue: {len(tts_audio)} bytes, queue_size={self.realtime_service.tts_queue.qsize()}")

                # 检查 FFmpeg 状态
                ff_status = None
                if self.output_process:
                    ff_status = self.output_process.poll()
                if tts_audio:
                    logger.info(f"[{self.request_id}] TTS check: audio={len(tts_audio)}B, ff_status={ff_status}, has_stdin={self.output_process.stdin is not None if self.output_process else False}")

                # 如果 FFmpeg 进程已退出，尝试重启
                if self.output_process and ff_status is not None:
                    logger.warning(f"[{self.request_id}] FFmpeg output process exited (code={ff_status}), attempting restart...")
                    try:
                        self.output_process = subprocess.Popen(
                            ffmpeg_output_cmd,
                            stdin=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            bufsize=0
                        )
                        # 重启 stderr 读取线程
                        stderr_buffer = []
                        stderr_thread = threading.Thread(
                            target=self._read_ffmpeg_stderr,
                            args=(self.output_process.stderr, stderr_buffer),
                            daemon=True
                        )
                        stderr_thread.start()
                        logger.info(f"[{self.request_id}] FFmpeg output restarted, PID={self.output_process.pid}")
                    except Exception as e:
                        logger.error(f"[{self.request_id}] Failed to restart FFmpeg output: {e}")
                        self.output_process = None

                if tts_audio and self.output_process and self.output_process.poll() is None and self.output_process.stdin:
                    try:
                        self.output_process.stdin.write(tts_audio)
                        self.output_process.stdin.flush()
                        audio_processed += len(tts_audio)
                        
                        if audio_processed % 100000 < len(tts_audio):
                            logger.info(f"[{self.request_id}] Pushed TTS audio: {audio_processed} bytes total")
                    except (BrokenPipeError, OSError) as e:
                        logger.error(f"[{self.request_id}] Broken pipe writing to FFmpeg: {e}")
                        # 进程可能已经退出，设为 None 以触发重连
                        self.output_process = None
                elif not tts_audio:
                    time.sleep(0.01)
            
            logger.info(f"[{self.request_id}] TTS push thread ended. Total audio: {audio_processed} bytes")
            
        except Exception as e:
            logger.error(f"[{self.request_id}] Error pushing TTS audio: {e}", exc_info=True)
    
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
            
            logger.info(f"[{self.request_id}] TTS push thread ended. Total audio: {audio_processed} bytes")
            
        except Exception as e:
            logger.error(f"[{self.request_id}] Error pushing TTS audio: {e}", exc_info=True)
    
    def stop(self):
        """停止音频流处理"""
        self.is_running = False
        
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
