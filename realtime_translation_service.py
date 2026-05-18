#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
语音翻译服务 - WebSocket 版本
使用百度实时语音翻译 WebSocket API
直接发送二进制音频，无需 Base64 编码

参考 Demo: realtime_trans_python3/realtime-asr-python.py
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
import websocket
import websocket._exceptions as ws_exceptions
from typing import Optional, Dict, Any

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class RealtimeTranslationService:
    """百度实时语音翻译服务

    使用 WebSocket 直接传输二进制音频，集成 ASR + MT + TTS
    无需 Base64 编码，一步到位
    """

    # 实时翻译 WebSocket URL
    WS_URL = "wss://aip.baidubce.com/ws/realtime_speech_trans"

    def __init__(self, app_id: str, app_key: str, audio_config: Optional[Dict[str, Any]] = None):
        self.app_id = app_id
        self.app_key = app_key

        # 音频配置
        self.audio_config = audio_config or {}
        self.sample_rate = self.audio_config.get("sample_rate", 16000)
        self.channels = self.audio_config.get("channels", 1)

        # 语言配置
        self.from_lang = self.audio_config.get("from_lang", "zh")
        self.to_lang = self.audio_config.get("to_lang", "en")

        # 房间和用户配置
        self.room_id = os.getenv("ROOM_ID", "")
        self.source_user = os.getenv("SOURCE_USER", "")
        self.request_id = os.getenv("REQUEST_ID", "unknown")

        # WebSocket 相关
        self.ws = None
        self.ws_thread = None
        self.is_running = False

        # 音频队列：增大到 500 个 chunk（约 75 秒缓冲）
        self.audio_queue = queue.Queue(maxsize=500)

        # 回调函数
        self.on_translation_callback = None
        self.on_tts_audio_callback = None

        # TTS 音频缓冲
        self.tts_audio_buffer = b""

        # SRS 配置
        self.srs_url = os.getenv("SRS_URL", "http://127.0.0.1:8080")

        # FFmpeg 进程
        self.ffmpeg_process = None

        # 调试：保存 PCM 文件
        self._debug_chunk_index = 0
        self._save_all_audio = True  # 设置为 True 保存所有音频

    def set_translation_callback(self, callback):
        """设置翻译结果回调"""
        self.on_translation_callback = callback

    def set_tts_audio_callback(self, callback):
        """设置 TTS 音频回调"""
        self.on_tts_audio_callback = callback

    def _get_start_frame(self) -> Dict[str, Any]:
        """获取开始帧"""
        return {
            "type": "START",
            "from": self.from_lang,
            "to": self.to_lang,
            "sampling_rate": self.sample_rate,
            "return_target_tts": True,  # 返回 TTS 音频
            "tts_speaker": "man",
            "app_id": self.app_id,
            "app_key": self.app_key,
        }

    def _get_finish_frame(self) -> Dict[str, Any]:
        """获取结束帧"""
        return {"type": "FINISH"}

    def _save_debug_audio(self, audio_data: bytes):
        """保存音频数据用于调试"""
        try:
            debug_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'debug_audio')
            os.makedirs(debug_dir, exist_ok=True)

            self._debug_chunk_index += 1
            filename = f"{self.room_id}_{self.source_user}_chunk_{self._debug_chunk_index:04d}.pcm"
            filepath = os.path.join(debug_dir, filename)

            with open(filepath, 'wb') as f:
                f.write(audio_data)

            duration_sec = len(audio_data) / (self.sample_rate * self.channels * 2)
            logger.info(f"[{self.request_id}] Debug audio saved: {filename} ({len(audio_data)} bytes, {duration_sec:.2f}s)")

        except Exception as e:
            logger.warning(f"[{self.request_id}] Failed to save debug audio: {e}")

    def _on_ws_open(self, ws):
        """WebSocket 连接打开"""
        logger.info(f"[{self.request_id}] WebSocket connected")

        # 发送开始帧
        start_frame = self._get_start_frame()
        body = json.dumps(start_frame)
        ws.send(body, websocket.ABNF.OPCODE_TEXT)
        logger.info(f"[{self.request_id}] Sent START frame: {start_frame}")

        # 启动音频发送线程
        threading.Thread(target=self._send_audio_thread, daemon=True).start()

    def _send_audio_thread(self):
        """发送音频数据线程"""
        # 120ms 发送一次：3840 bytes / 120ms = 32KB/s，与采样率匹配
        chunk_ms = 120
        chunk_len = int(self.sample_rate * 2 * self.channels * chunk_ms / 1000)
        logger.info(f"[{self.request_id}] Audio send thread started: chunk_ms={chunk_ms}, chunk_len={chunk_len}")

        while self.is_running:
            try:
                # 从队列获取音频数据
                try:
                    audio_data = self.audio_queue.get(timeout=0.1)
                except queue.Empty:
                    continue

                # 发送音频数据（二进制，无需 Base64！）
                if len(audio_data) >= chunk_len:
                    self.ws.send(audio_data[:chunk_len], websocket.ABNF.OPCODE_BINARY)
                    # 把剩余的放回队列
                    if len(audio_data) > chunk_len:
                        remaining = audio_data[chunk_len:]
                        try:
                            self.audio_queue.put(remaining, block=False)
                        except queue.Full:
                            pass
                else:
                    # 数据不够，等待累积
                    try:
                        self.audio_queue.put(audio_data, block=False)
                    except queue.Full:
                        pass

                # 控制发送速率
                time.sleep(chunk_ms / 1000.0)

            except ws_exceptions.WebSocketConnectionClosedException:
                logger.warning(f"[{self.request_id}] WebSocket closed")
                break
            except Exception as e:
                logger.error(f"[{self.request_id}] Error in send_audio_thread: {e}")
                break

        logger.info(f"[{self.request_id}] Audio send thread ended")

    def _on_ws_message(self, ws, message):
        """收到消息"""
        if isinstance(message, bytes):
            # TTS 音频数据
            # 第一个字节是 type，0x01 = TTS播报报文
            # 后续是 MP3 格式的 TTS 音频数据
            if len(message) > 1:
                tts_type = message[0]
                if tts_type == 0x01:
                    # TTS 播报报文，payload 是 MP3 格式
                    tts_audio = message[1:]
                    self.tts_audio_buffer += tts_audio
                    logger.debug(f"[{self.request_id}] TTS MP3 received: {len(tts_audio)} bytes, total: {len(self.tts_audio_buffer)} bytes")

                    # 回调 TTS 音频
                    if self.on_tts_audio_callback:
                        self.on_tts_audio_callback(tts_audio)
                else:
                    logger.warning(f"[{self.request_id}] Unknown binary message type: 0x{tts_type:02x}")

        elif isinstance(message, str):
            # JSON 消息
            try:
                result = json.loads(message)
                msg_type = result.get("type", "")

                if msg_type == "TRANSLATION":
                    original = result.get("original_text", "")
                    translated = result.get("translated_text", "")

                    logger.info(f"[{self.request_id}] Translation: '{original}' -> '{translated}'")

                    if self.on_translation_callback:
                        self.on_translation_callback(translated, original, self.from_lang, self.to_lang)

                elif msg_type == "ERROR":
                    error_msg = result.get("message", "Unknown error")
                    logger.error(f"[{self.request_id}] Translation error: {error_msg}")

            except json.JSONDecodeError as e:
                logger.warning(f"[{self.request_id}] Failed to parse message: {e}")

    def _on_ws_error(self, ws, error):
        """WebSocket 错误"""
        logger.error(f"[{self.request_id}] WebSocket error: {error}")

    def _on_ws_close(self, ws, close_status_code, close_msg):
        """WebSocket 关闭"""
        logger.info(f"[{self.request_id}] WebSocket closed: {close_status_code} - {close_msg}")
        self.is_running = False

    def connect(self):
        """建立 WebSocket 连接"""
        if self.ws and self.is_running:
            logger.warning("WebSocket already connected")
            return

        logger.info(f"[{self.request_id}] Connecting to {self.WS_URL}")

        self.ws = websocket.WebSocketApp(
            self.WS_URL,
            on_open=self._on_ws_open,
            on_message=self._on_ws_message,
            on_error=self._on_ws_error,
            on_close=self._on_ws_close,
        )

        self.is_running = True
        self.ws_thread = threading.Thread(target=self.ws.run_forever, daemon=True)
        self.ws_thread.start()

    def disconnect(self):
        """断开 WebSocket 连接"""
        logger.info(f"[{self.request_id}] Disconnecting...")

        self.is_running = False

        if self.ws:
            try:
                finish_frame = self._get_finish_frame()
                self.ws.send(json.dumps(finish_frame), websocket.ABNF.OPCODE_TEXT)
                logger.info(f"[{self.request_id}] Sent FINISH frame")
            except Exception as e:
                logger.warning(f"[{self.request_id}] Failed to send FINISH frame: {e}")

            self.ws.close()
            self.ws = None

        if self.ws_thread:
            self.ws_thread.join(timeout=2)
            self.ws_thread = None

        logger.info(f"[{self.request_id}] Disconnected")

    def add_audio(self, audio_data: bytes):
        """添加音频数据

        Args:
            audio_data: PCM 音频数据（二进制）
        """
        if self.is_running:
            try:
                self.audio_queue.put(audio_data, block=False)
            except queue.Full:
                logger.warning(f"[{self.request_id}] Audio queue full")

    def _start_ffmpeg_input(self, http_flv_url: str):
        """启动 FFmpeg 输入进程（从 SRS 读取音频）"""
        ffmpeg_bin = os.environ.get("FFMPEG_BIN", "/usr/bin/ffmpeg")
        ffmpeg_input_cmd = [
            ffmpeg_bin,
            "-fflags", "nobuffer",
            "-flags", "low_delay",
            "-analyzeduration", "500000",
            "-probesize", "500000",
            "-max_delay", "5000000",
            "-i", http_flv_url,
            "-vn",
            "-acodec", "pcm_s16le",  # 输出 PCM
            "-ar", str(self.sample_rate),
            "-ac", str(self.channels),
            "-f", "s16le",
            "-thread_queue_size", "512",
            "-nostdin",
            "-y",
            "-"
        ]

        logger.info(f"[{self.request_id}] Starting FFmpeg input: {' '.join(ffmpeg_input_cmd)}")

        stderr_log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ffmpeg_input.log')
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

    def _wait_for_stream_ready(self, url: str, timeout: int = 10) -> bool:
        """等待流准备就绪"""
        import urllib.request
        import urllib.error

        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                req = urllib.request.Request(url, method='HEAD')
                urllib.request.urlopen(req, timeout=2)
                return True
            except (urllib.error.URLError, Exception):
                time.sleep(0.5)
        return False

    def _read_audio_thread(self):
        """读取音频数据并发送到 WebSocket"""
        chunk_size = 8192
        http_flv_port = self.audio_config.get("http_flv_port", 8080)
        http_flv_url = f"http://127.0.0.1:{http_flv_port}/live/{self.room_id}_{self.source_user}.flv"

        logger.info(f"[{self.request_id}] Audio read thread started")

        while self.is_running:
            try:
                # 等待流就绪
                stream_ready = self._wait_for_stream_ready(http_flv_url)
                if not stream_ready:
                    logger.warning(f"[{self.request_id}] Stream not ready, waiting...")
                    time.sleep(3)
                    continue

                logger.info(f"[{self.request_id}] Starting FFmpeg...")
                self._start_ffmpeg_input(http_flv_url)

                while self.is_running and self.ffmpeg_process:
                    # 检查 FFmpeg 进程状态
                    poll_result = self.ffmpeg_process.poll()
                    if poll_result is not None:
                        logger.error(f"[{self.request_id}] FFmpeg exited! returncode={poll_result}")
                        break

                    try:
                        audio_chunk = self.ffmpeg_process.stdout.read(chunk_size)
                    except (BlockingIOError, IOError):
                        time.sleep(0.01)
                        continue
                    except Exception as e:
                        logger.error(f"[{self.request_id}] Error reading audio: {e}")
                        break

                    if audio_chunk:
                        # 调试：保存 PCM 文件
                        if self._save_all_audio:
                            self._save_debug_audio(audio_chunk)

                        # 发送到 WebSocket（无需 Base64！）
                        self.add_audio(audio_chunk)

                    time.sleep(0.01)

                # FFmpeg 退出后等待重试
                if self.is_running:
                    logger.warning(f"[{self.request_id}] FFmpeg exited, waiting for stream to restart...")
                    time.sleep(3)

            except Exception as e:
                logger.error(f"[{self.request_id}] Error in audio read thread: {e}")
                time.sleep(1)

        logger.info(f"[{self.request_id}] Audio read thread ended")

    def _push_tts_audio_thread(self):
        """推送 TTS 音频到 SRS

        百度返回的 TTS 音频已经是 MP3 格式，直接推送即可
        """
        # RTMP URL
        rtmp_url = f"rtmp://127.0.0.1:1935/live/{self.room_id}_{self.source_user}_translated"

        logger.info(f"[{self.request_id}] TTS push thread started: {rtmp_url}")

        # FFmpeg 命令：直接封装 MP3 为 FLV 推送
        # MP3 需要用 mp3 容器或通过 FLV 推送
        ffmpeg_bin = os.environ.get("FFMPEG_BIN", "/usr/bin/ffmpeg")
        ffmpeg_cmd = [
            ffmpeg_bin,
            "-f", "mp3",          # 输入是 MP3 格式
            "-i", "-",
            "-c:a", "copy",       # 直接复制流，不转码
            "-f", "flv",
            "-re",
            rtmp_url
        ]

        try:
            process = subprocess.Popen(
                ffmpeg_cmd,
                stdin=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0
            )

            logger.info(f"[{self.request_id}] FFmpeg output process started, PID={process.pid}")

            while self.is_running:
                if process.poll() is not None:
                    logger.warning(f"[{self.request_id}] FFmpeg output process died, restarting...")
                    time.sleep(1)
                    process = subprocess.Popen(ffmpeg_cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0)

                # 从 TTS 缓冲读取 MP3 数据
                if self.tts_audio_buffer:
                    audio = self.tts_audio_buffer
                    self.tts_audio_buffer = b""

                    try:
                        process.stdin.write(audio)
                        process.stdin.flush()
                    except Exception as e:
                        logger.error(f"[{self.request_id}] Error writing TTS audio: {e}")

                time.sleep(0.05)

            process.stdin.close()
            process.wait()

        except Exception as e:
            logger.error(f"[{self.request_id}] Error in TTS push thread: {e}")

    def start(self):
        """启动服务"""
        self.request_id = os.getenv("REQUEST_ID", "unknown")

        logger.info(f"[{self.request_id}] Starting RealtimeTranslationService")
        logger.info(f"[{self.request_id}]   app_id: {self.app_id[:8]}...")
        logger.info(f"[{self.request_id}]   from: {self.from_lang} -> to: {self.to_lang}")
        logger.info(f"[{self.request_id}]   sample_rate: {self.sample_rate}, channels: {self.channels}")

        # 连接 WebSocket
        self.connect()

        # 启动音频读取线程
        threading.Thread(target=self._read_audio_thread, daemon=True).start()

        # 启动 TTS 推送线程
        threading.Thread(target=self._push_tts_audio_thread, daemon=True).start()

    def stop(self):
        """停止服务"""
        logger.info(f"[{self.request_id}] Stopping...")

        self.is_running = False

        if self.ffmpeg_process:
            try:
                self.ffmpeg_process.stdout.close()
                self.ffmpeg_process.stderr.close()
                self.ffmpeg_process.terminate()
            except:
                pass
            self.ffmpeg_process = None

        self.disconnect()

        logger.info(f"[{self.request_id}] Stopped")


def main():
    """主函数"""
    # 获取配置
    app_id = os.getenv("BAIDU_APP_ID", "")
    app_key = os.getenv("BAIDU_APP_KEY", "")

    if not app_id or not app_key:
        logger.error("请设置环境变量: BAIDU_APP_ID 和 BAIDU_APP_KEY")
        sys.exit(1)

    # 加载音频配置
    config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'audio_translation_config.json')
    audio_config = {}
    if os.path.exists(config_path):
        with open(config_path, 'r') as f:
            audio_config = json.load(f).get("audio", {})

    # 创建服务
    service = RealtimeTranslationService(
        app_id=app_id,
        app_key=app_key,
        audio_config=audio_config
    )

    # 设置回调
    def on_translation(translated, original, from_lang, to_lang):
        logger.info(f"=== 翻译结果: {original} -> {translated}")
        # 这里可以推送翻译文本到客户端

    def on_tts(audio):
        logger.debug(f"=== TTS 音频: {len(audio)} bytes")

    service.set_translation_callback(on_translation)
    service.set_tts_audio_callback(on_tts)

    # 信号处理
    def signal_handler(sig, frame):
        logger.info("Received signal, shutting down...")
        service.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # 启动
    service.start()

    # 保持运行
    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
