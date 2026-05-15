#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
语音翻译服务
从SRS接收音频流，调用百度实时语音翻译API翻译为目标语言，然后推送回SRS
支持多用户多语言翻译
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

from tts_service import BaiduTTSService
from baidu_asr_client import BaiduASRClient, BaiduMTClient
from language_detector import LanguageDetector

# 配置日志：同时输出到控制台和文件
log_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'audio_translation_service.log')

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


class BaiduTranslationClient:
    """百度语音翻译客户端（ASR + MT + 语言检测）"""

    def __init__(self, api_key: str, secret_key: str, audio_config: Optional[Dict[str, Any]] = None,
                 mt_app_id: str = None, mt_app_secret: str = None):
        self.api_key = api_key
        self.secret_key = secret_key
        self.asr_client = BaiduASRClient(api_key, secret_key)
        # 支持百度翻译开放平台（App ID）或百度AI平台（API Key）
        self.mt_client = BaiduMTClient(
            api_key=api_key, 
            secret_key=secret_key,
            app_id=mt_app_id,
            app_secret=mt_app_secret
        )
        self.language_detector = LanguageDetector()
        # 设置队列最大大小，避免内存溢出
        self.audio_queue = queue.Queue(maxsize=1000)
        self.translated_text_queue = queue.Queue()
        self.is_running = False
        self.audio_buffer = b""
        self._audio_dropped_count = 0  # 统计丢弃的音频块数

        # 音频配置
        self.audio_config = audio_config or {}
        self.sample_rate = self.audio_config.get("sample_rate", 16000)
        self.channels = self.audio_config.get("channels", 1)
        self.asr_format = self.audio_config.get("asr_format", "aac")
        self.buffer_duration_ms = 2000

        # 当前语言（首次识别后自动检测）
        self.current_language = "zh"  # 默认中文
        self.language_detected = False
        self.language_detection_texts = []  # 累积文本用于语言检测
        self.language_detection_min_chars = 10  # 至少累积10个字符再检测
        
        # 目标语言（从环境变量或配置获取）
        self.target_language = os.getenv("TO_LANG", self.audio_config.get("target_language", "en"))
        
        # 目标用户和房间ID（用于推送翻译文本）
        self.target_user = os.getenv("TARGET_USER", "")
        self.room_id = os.getenv("ROOM_ID", "")
        
        # 文本推送服务地址
        self.text_server_url = os.getenv("TEXT_SERVER_URL", "http://localhost:8087")

        # 根据格式计算chunk_size
        # 重要：确保与百度ASR API要求的音频大小一致
        _req_id = os.getenv("REQUEST_ID", "unknown")
        # 延迟初始化chunk_size，在start_translation时确保所有配置都已就绪
        # 使用get_chunk_size方法动态计算
        logger.info(f"[{_req_id}] ASR format: {self.asr_format}, will initialize chunk_size when service starts")
        
    def get_chunk_size(self) -> int:
        """获取ASR chunk大小
        
        Returns:
            chunk大小（字节数）
        """
        if self.asr_format.lower() == "pcm":
            # PCM格式：采样率 * 字节深度(2) * 通道数 * 时长(秒)
            # 2秒的PCM数据：16000 * 2 * 1 * 2 = 64000字节
            return self.sample_rate * 2 * self.channels * 2
        else:
            # 非PCM格式（如AAC）
            # AAC约1秒的数据大小（比特率/8）
            return 20000
    
    def _init_chunk_size(self):
        """初始化chunk_size，确保采样率匹配"""
        _req_id = os.getenv("REQUEST_ID", "unknown")
        self.chunk_size = self.get_chunk_size()
        logger.info(f"[{_req_id}] Chunk size initialized: chunk_size={self.chunk_size} "
                   f"(rate={self.sample_rate}, channels={self.channels}, format={self.asr_format}, "
                   f"expected_bytes_per_sec={self.sample_rate * 2 * self.channels})")

    def start_translation(self):
        """启动翻译服务"""
        self.is_running = True
        
        # 添加请求ID到日志，方便追踪
        self.request_id = os.getenv("REQUEST_ID", "unknown")
        
        # 初始化chunk_size（需要在使用前调用）
        self._init_chunk_size()
        
        # 启动音频处理线程
        threading.Thread(target=self._process_audio_thread, daemon=True).start()
        
        # 修复日志格式，使用双花括号避免被f-string解析
        logger.info("[Translation-%s] Translation service started, "
                   "room=%s, source_user=%s, target_lang=%s, target_user=%s",
                   self.request_id, self.room_id, os.getenv('SOURCE_USER', ''), 
                   self.target_language, self.target_user)
    
    def _process_audio_thread(self):
        """处理音频数据：ASR -> 语言检测 -> MT"""
        chunks_processed = 0
        last_log_time = time.time()
        last_queue_check = time.time()
        queue_empty_count = 0
        
        # 缓冲区大小限制（最多 10 秒的音频数据）
        max_buffer_size = self.sample_rate * self.channels * 2 * 10
        
        logger.info(f"[{self.request_id}] Audio processing thread started, buffer_duration_ms={self.buffer_duration_ms}, chunk_size={self.chunk_size}, max_buffer_size={max_buffer_size}")
        
        while self.is_running:
            try:
                # 从队列获取音频数据
                audio_data = self.audio_queue.get(timeout=1)

                # 累积音频到缓冲区，但限制大小
                self.audio_buffer += audio_data
                
                # 如果缓冲区超过限制，丢弃最老的数据
                if len(self.audio_buffer) > max_buffer_size:
                    excess = len(self.audio_buffer) - max_buffer_size
                    # 丢弃前面 excess 字节
                    self.audio_buffer = self.audio_buffer[excess:]
                    logger.warning(f"[{self.request_id}] Audio buffer overflow, dropped {excess} bytes, buffer_size={len(self.audio_buffer)}")
                
                chunks_processed += 1
                queue_empty_count = 0  # 重置计数器
                
                # 每5秒打印一次缓冲区状态
                current_time = time.time()
                if current_time - last_log_time > 5:
                    dropped_count = self.get_audio_dropped_count()
                    logger.info(f"[{self.request_id}] Audio buffer status: buffer_size={len(self.audio_buffer)}, queue_size={self.audio_queue.qsize()}, chunks_processed={chunks_processed}, ffmpeg_dropped={dropped_count}")
                    last_log_time = current_time

                # 当缓冲区达到一定大小，进行识别
                while len(self.audio_buffer) >= self.chunk_size:
                    # 只取chunk_size大小的数据进行识别
                    chunk_to_process = self.audio_buffer[:self.chunk_size]
                    # 保留剩余的数据在缓冲区中
                    self.audio_buffer = self.audio_buffer[self.chunk_size:]

                    logger.info(f"[{self.request_id}] Processing audio chunk: size={len(chunk_to_process)}, buffer_remaining={len(self.audio_buffer)}")
                    
                    # 验证chunk大小是否与采样率匹配
                    expected_chunk_size = self.get_chunk_size()
                    if len(chunk_to_process) != expected_chunk_size:
                        logger.warning(f"[{self.request_id}] Chunk size mismatch: "
                                     f"actual={len(chunk_to_process)}, expected={expected_chunk_size}. "
                                     f"This may cause ASR rate validation errors.")

                    # 获取当前应该使用的ASR模型
                    dev_pid = self.language_detector.get_asr_dev_pid(self.current_language)
                    logger.info(f"[{self.request_id}] Using ASR model: dev_pid={dev_pid} for language={self.current_language}, "
                               f"rate={self.sample_rate}, format={self.asr_format}")

                    # 调用ASR识别
                    recognized_text = self.asr_client.recognize(
                        chunk_to_process,
                        format=self.asr_format,
                        rate=self.sample_rate,
                        channel=self.channels,
                        dev_pid=dev_pid
                    )

                    if recognized_text and recognized_text.strip():
                        logger.info(f"[{self.request_id}] ASR recognized: '{recognized_text}' (lang={self.current_language})")
                        
                        # 累积文本用于语言检测（langdetect对短文本不稳定）
                        self.language_detection_texts.append(recognized_text)
                        
                        # 首次识别到文本时，检测语言
                        if not self.language_detected:
                            combined_text = ''.join(self.language_detection_texts)
                            logger.info(f"[{self.request_id}] Combined text for lang detection: '{combined_text}' (len={len(combined_text)})")
                            
                            if len(combined_text) >= self.language_detection_min_chars:
                                detected_lang = self.language_detector.detect_language(combined_text)
                                logger.info(f"[{self.request_id}] Langdetect result: '{combined_text}' -> '{detected_lang}'")
                                if detected_lang:
                                    self.current_language = detected_lang
                                    self.language_detected = True
                                    logger.info(f"[{self.request_id}] Language detected: {detected_lang}")
                            else:
                                needed = self.language_detection_min_chars - len(combined_text)
                                logger.info(f"[{self.request_id}] Need {needed} more chars for language detection")

                        # ========== 推送原语音识别文字给房间所有用户 ==========
                        self.push_original_speech_text(
                            recognized_text,
                            self.current_language
                        )

                        # 获取翻译语言对
                        from_lang, to_lang = self.language_detector.get_translation_pair(
                            self.current_language,
                            self.target_language
                        )

                        # 判断是否需要翻译
                        if self.language_detector.needs_translation(from_lang, to_lang):
                            logger.info(f"[{self.request_id}] Translating: {from_lang} -> {to_lang}")
                            
                            # 调用MT翻译
                            translated_text = self.mt_client.translate(
                                recognized_text,
                                from_lang=from_lang,
                                to_lang=to_lang
                            )

                            if translated_text:
                                self.translated_text_queue.put({
                                    "text": translated_text,
                                    "original": recognized_text,
                                    "source_lang": from_lang,
                                    "target_lang": to_lang,
                                    "should_push": True,
                                    "timestamp": time.time()
                                })
                                
                                logger.info(f"[{self.request_id}] Translation queued: '{recognized_text}' -> '{translated_text}'")
                                
                                # 推送翻译文本给客户端
                                self.push_translation_text_to_client(
                                    recognized_text,
                                    translated_text,
                                    from_lang,
                                    to_lang
                                )
                            else:
                                logger.warning(f"[{self.request_id}] MT returned empty translation")
                        else:
                            # 源语言和目标语言相同，不需要翻译，不推送翻译流
                            logger.info(f"[{self.request_id}] No translation needed (source={from_lang}, target={to_lang})")
                    else:
                        logger.debug(f"[{self.request_id}] ASR returned empty result")

            except queue.Empty:
                continue
            except Exception as e:
                logger.error(f"[{self.request_id}] Error processing audio: {e}", exc_info=True)
    
    def add_audio(self, audio_data: bytes):
        """添加音频数据到翻译队列"""
        if self.is_running:
            try:
                # 使用非阻塞模式，如果队列满则丢弃最老的音频块
                self.audio_queue.put(audio_data, block=False)
            except queue.Full:
                # 队列满了，丢弃这个音频块
                self._audio_dropped_count += 1
                if self._audio_dropped_count % 100 == 1:
                    logger.warning(f"[{self.request_id}] Audio queue full, dropped {self._audio_dropped_count} chunks")
    
    def get_audio_dropped_count(self) -> int:
        """获取丢弃的音频块数量"""
        return self._audio_dropped_count
    
    def get_translated_text(self) -> Optional[Dict[str, Any]]:
        """获取翻译后的文本"""
        try:
            return self.translated_text_queue.get_nowait()
        except queue.Empty:
            return None
    
    def push_translation_text_to_client(self, original_text: str, translated_text: str, 
                                        from_lang: str, to_lang: str):
        """推送翻译文本给客户端
        
        Args:
            original_text: 原文
            translated_text: 译文
            from_lang: 源语言
            to_lang: 目标语言
        """
        if not self.target_user:
            logger.debug(f"[{self.request_id}] No target_user, skipping text push")
            return
        
        try:
            import requests
            push_url = f"{self.text_server_url}/api/v1/translation/text/push"
            
            data = {
                "target_user": self.target_user,
                "request_id": self.request_id,
                "room_id": self.room_id,
                "source_user": os.getenv("SOURCE_USER", ""),
                "original_text": original_text,
                "translated_text": translated_text,
                "source_lang": from_lang,
                "target_lang": to_lang,
                "timestamp": time.time()
            }
            
            response = requests.post(push_url, json=data, timeout=3)
            if response.status_code == 200:
                logger.info(f"[{self.request_id}] Pushed translation text to {self.target_user}: "
                          f"{original_text[:20]}... -> {translated_text[:20]}...")
            else:
                logger.warning(f"[{self.request_id}] Failed to push text: {response.status_code}")
                
        except Exception as e:
            logger.error(f"[{self.request_id}] Error pushing translation text: {e}")
    
    def push_original_speech_text(self, original_text: str, source_lang: str):
        """推送原语音识别文字给房间所有用户

        Args:
            original_text: ASR 识别的原文
            source_lang: 源语言
        """
        if not self.room_id:
            logger.debug(f"[{self.request_id}] No room_id, skipping original text push")
            return

        try:
            push_url = f"{self.text_server_url}/api/v1/original/speech/text/push"

            data = {
                "room_id": self.room_id,
                "source_user": os.getenv("SOURCE_USER", ""),
                "original_text": original_text,
                "source_lang": source_lang,
                "timestamp": time.time()
            }

            response = requests.post(push_url, json=data, timeout=3)
            if response.status_code == 200:
                logger.info(f"[{self.request_id}] Pushed original speech text to room {self.room_id}: "
                          f"'{original_text[:30]}...'")
            else:
                logger.warning(f"[{self.request_id}] Failed to push original speech text: {response.status_code}")

        except Exception as e:
            logger.error(f"[{self.request_id}] Error pushing original speech text: {e}")
    
    def stop(self):
        """停止翻译服务"""
        self.is_running = False


class AudioStreamProcessor:
    """音频流处理器"""
    
    def __init__(self, srs_url: str, room_id: str, source_user: str, to_lang: str,
                 stream_name: str, translation_client: BaiduTranslationClient,
                 audio_config: Optional[Dict[str, Any]] = None):
        self.srs_url = srs_url
        self.room_id = room_id
        self.source_user = source_user
        self.to_lang = to_lang
        self.stream_name = stream_name
        self.translation_client = translation_client
        self.ffmpeg_process = None
        self.output_process = None
        self.is_running = False
        self.request_id = os.getenv("REQUEST_ID", "unknown")
        
        # 音频配置
        self.audio_config = audio_config or {}
        self.asr_format = self.audio_config.get("asr_format", "aac")
        self.input_format = self.audio_config.get("input_format", None)
        self.sample_rate = self.audio_config.get("sample_rate", 16000)
        self.channels = self.audio_config.get("channels", 1)
        
        # FFmpeg 重试配置
        self.max_retries = 5
        self.retry_wait_seconds = 2
        self._retry_count = 0
    
    def _wait_for_stream_ready(self, http_flv_url: str) -> bool:
        """等待 SRS 流就绪
        
        通过检查 HTTP-FLV 流头信息来确认流是否存在且可读
        SRS 会返回 200 和 FLV 头(9字节)，如果流不存在则返回 404
        """
        check_url = http_flv_url
        
        logger.info(f"[{self.request_id}] Checking if stream is ready: {check_url}")
        
        for attempt in range(1, 11):
            try:
                resp = requests.get(check_url, stream=True, timeout=3, 
                                    headers={"Range": "bytes=0-0"})
                if resp.status_code in (200, 206):
                    logger.info(f"[{self.request_id}] Stream ready (attempt {attempt}): {check_url}")
                    return True
                else:
                    logger.info(f"[{self.request_id}] Stream not ready (attempt {attempt}): "
                              f"status={resp.status_code}, retrying in {self.retry_wait_seconds}s...")
            except requests.exceptions.RequestException as e:
                logger.info(f"[{self.request_id}] Stream check failed (attempt {attempt}): {e}, "
                          f"retrying in {self.retry_wait_seconds}s...")
            
            if attempt < 10:
                time.sleep(self.retry_wait_seconds)
        
        logger.warning(f"[{self.request_id}] Stream never became ready after 10 attempts, "
                      f"will start FFmpeg anyway and let it retry...")
        return False
        
    def start(self):
        """启动音频流处理"""
        self.is_running = True
        
        # 启动从SRS接收音频的FFmpeg进程
        # 从对应用户的流拉取音频：{room_id}_{source_user}
        input_stream = f"{self.room_id}_{self.source_user}"
        input_url = f"{self.srs_url}/live/{input_stream}.flv"
        
        # 检测输入格式
        is_opus_input = self.input_format and self.input_format.lower() == "opus"
        
        # 优先检查配置文件中的设置
        if self.input_format:
            logger.info(f"[{self.request_id}] Using configured input format: {self.input_format}")
        else:
            logger.info(f"[{self.request_id}] No input format configured, will auto-detect")
        
        # 构建 FFmpeg 命令，让其自动检测输入流格式
        # 使用 HTTP-FLV 拉流（比 RTMP 更可靠）
        # 使用 127.0.0.1 而不是 localhost，避免 IPv6 解析问题
        http_flv_port = self.audio_config.get("http_flv_port", 8080)
        http_flv_url = f"http://127.0.0.1:{http_flv_port}/live/{self.room_id}_{self.source_user}.flv"
        logger.info(f"[{self.request_id}] HTTP-FLV URL: {http_flv_url}")
        
        # 启动音频读取线程（FFmpeg 会在读取线程中启动并自动重启）
        threading.Thread(target=self._read_audio_thread, daemon=True).start()
        logger.info(f"[{self.request_id}] Audio read thread started")
        
        # 启动翻译后音频推送线程
        threading.Thread(target=self._push_translated_audio_thread, daemon=True).start()
        logger.info(f"[{self.request_id}] Translated audio push thread started")
    
    def _start_ffmpeg_input(self, http_flv_url: str):
        """启动 FFmpeg 输入进程"""
        ffmpeg_bin = os.environ.get("FFMPEG_BIN", "/usr/bin/ffmpeg")
        ffmpeg_input_cmd = [
            ffmpeg_bin,
            "-fflags", "nobuffer",
            "-flags", "low_delay",
            "-analyzeduration", "500000",  # 缩短分析时间，提高启动速度
            "-probesize", "500000",
            "-max_delay", "5000000",  # 最大延迟 5 秒
            "-i", http_flv_url,
            "-vn",
            "-acodec", "pcm_s16le",
            "-ar", str(self.sample_rate),
            "-ac", str(self.channels),
            "-f", "s16le",
            "-thread_queue_size", "512",  # 增加线程队列大小
            "-nostdin",  # 禁用 stdin 处理，避免 SIGPIPE
            "-y",
            "-"
        ]
        
        logger.info(f"[{self.request_id}] Starting FFmpeg input process:")
        logger.info(f"[{self.request_id}]   Source URL: {http_flv_url}")
        logger.info(f"[{self.request_id}]   Output format: pcm_s16le")
        logger.info(f"[{self.request_id}]   Target sample rate: {self.sample_rate} Hz (resampling from 48000Hz)")
        logger.info(f"[{self.request_id}]   Target channels: {self.channels}")
        logger.info(f"[{self.request_id}]   FFmpeg command: {' '.join(ffmpeg_input_cmd)}")
        
        stderr_log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'audio_translation_ffmpeg_input.log')
        stderr_file = open(stderr_log_path, 'a')
        
        # 注意：SIGPIPE 信号处理已不需要，因为 FFmpeg 使用了 -nostdin 参数
        # -nostdin 禁用了 stdin，避免了 SIGPIPE 问题
        
        self.ffmpeg_process = subprocess.Popen(
            ffmpeg_input_cmd,
            stdout=subprocess.PIPE,
            stderr=stderr_file,
            bufsize=0  # 无缓冲，让数据立即传递
        )
        
        # 设置 stdout 为非阻塞模式
        stdout_fd = self.ffmpeg_process.stdout.fileno()
        flags = fcntl.fcntl(stdout_fd, fcntl.F_GETFL)
        fcntl.fcntl(stdout_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        
        return self.ffmpeg_process
    
    def _read_audio_thread(self):
        """读取音频数据并发送到翻译服务"""
        # 使用较大的 chunk size 减少系统调用，提高稳定性
        chunk_size = 8192
        # 使用 127.0.0.1 而不是 localhost，避免 IPv6 解析问题
        http_flv_port = self.audio_config.get("http_flv_port", 8080)
        http_flv_url = f"http://127.0.0.1:{http_flv_port}/live/{self.room_id}_{self.source_user}.flv"
        
        logger.info(f"[{self.request_id}] Audio read thread started, chunk_size={chunk_size}")
        
        while self.is_running:
            try:
                # 检查流是否就绪
                stream_ready = self._wait_for_stream_ready(http_flv_url)
                if not stream_ready:
                    logger.warning(f"[{self.request_id}] Stream not ready, waiting {self.retry_wait_seconds}s before starting FFmpeg...")
                    time.sleep(self.retry_wait_seconds)
                
                logger.info(f"[{self.request_id}] Starting FFmpeg input process...")
                
                try:
                    self._start_ffmpeg_input(http_flv_url)
                    logger.info(f"[{self.request_id}] FFmpeg process started with PID: {self.ffmpeg_process.pid}")
                except Exception as e:
                    logger.error(f"[{self.request_id}] Failed to start FFmpeg: {e}", exc_info=True)
                    break
                
                read_count = 0
                total_bytes_read = 0
                last_log_time = time.time()
                first_audio_time = None
                consecutive_empty_count = 0
                consecutive_short_read = 0
                
                while self.is_running and self.ffmpeg_process:
                    # 检查 FFmpeg 进程状态
                    poll_result = self.ffmpeg_process.poll()
                    if poll_result is not None:
                        logger.error(f"[{self.request_id}] FFmpeg input process exited! returncode={poll_result}")
                        # 读取 stderr 获取详细错误信息
                        try:
                            self.ffmpeg_process.stderr.seek(0)
                            stderr_content = self.ffmpeg_process.stderr.read()
                            if stderr_content:
                                stderr_text = stderr_content.decode('utf-8', errors='ignore')
                                # 只记录最后 1000 字符的关键信息
                                logger.error(f"[{self.request_id}] FFmpeg stderr (last 1000 chars): {stderr_text[-1000:]}")
                        except Exception as e:
                            logger.warning(f"[{self.request_id}] Failed to read FFmpeg stderr: {e}")
                        break
                    
                    try:
                        audio_chunk = self.ffmpeg_process.stdout.read(chunk_size)
                    except (BlockingIOError, IOError) as e:
                        # 非阻塞模式下没有数据可读，这是正常的
                        time.sleep(0.01)
                        continue
                    except Exception as e:
                        logger.error(f"[{self.request_id}] Error reading from FFmpeg stdout: {e}", exc_info=True)
                        break
                    
                    read_count += 1
                    
                    if not audio_chunk:
                        consecutive_empty_count += 1
                        # HTTP-FLV 流可能需要更长时间启动，增加等待次数
                        if consecutive_empty_count >= 1000:  # 1000 * 0.01 = 10秒
                            logger.warning(f"[{self.request_id}] FFmpeg output empty for {consecutive_empty_count} times, assuming stream ended")
                            break
                        
                        if time.time() - last_log_time > 5:
                            logger.info(f"[{self.request_id}] Waiting for audio data... count={read_count}, total_bytes={total_bytes_read}, consecutive_empty={consecutive_empty_count}")
                            last_log_time = time.time()
                        time.sleep(0.01)
                        continue
                    
                    # 重置连续空计数
                    consecutive_empty_count = 0
                    
                    # 检查是否读取到了期望大小的数据
                    chunk_len = len(audio_chunk)
                    if chunk_len < chunk_size:
                        consecutive_short_read += 1
                        if consecutive_short_read > 5 and chunk_len > 0:
                            logger.warning(f"[{self.request_id}] Multiple short reads detected (size={chunk_len}, expected={chunk_size}), stream may be unstable")
                    else:
                        consecutive_short_read = 0
                    
                    total_bytes_read += chunk_len
                    
                    if first_audio_time is None:
                        first_audio_time = time.time()
                        logger.info(f"[{self.request_id}] First audio chunk received! size={chunk_len} bytes, total={total_bytes_read}")
                        
                        # 验证采样率
                        if self.asr_format.lower() == "pcm" and chunk_len > 0:
                            expected_bytes_per_sec = self.sample_rate * self.channels * 2
                            logger.info(f"[{self.request_id}] Sample rate verification:")
                            logger.info(f"[{self.request_id}]   Expected bytes/sec: {expected_bytes_per_sec} (rate={self.sample_rate}, channels={self.channels})")
                            logger.info(f"[{self.request_id}]   Chunk size: {chunk_len} bytes")
                    else:
                        if read_count % 100 == 0:
                            elapsed = time.time() - first_audio_time
                            logger.info(f"[{self.request_id}] Audio stats: total_bytes={total_bytes_read}, chunks={read_count}, avg_chunk_size={total_bytes_read/max(read_count,1):.0f}, elapsed={elapsed:.1f}s")
                    
                    self.translation_client.add_audio(audio_chunk)
                
                logger.info(f"[{self.request_id}] Audio read loop ended. Total: {total_bytes_read} bytes, {read_count} chunks, first_audio_at={first_audio_time}")
                
                # FFmpeg 退出了，不再自动重启
                if self.is_running:
                    logger.warning(f"[{self.request_id}] FFmpeg exited, NOT auto-restarting. Waiting for manual intervention or stream to restart...")
                    # 清理旧的 FFmpeg 进程
                    if self.ffmpeg_process:
                        try:
                            self.ffmpeg_process.stdout.close()
                            self.ffmpeg_process.stderr.close()
                        except:
                            pass
                        self.ffmpeg_process = None
                    # 等待一段时间后退出线程
                    time.sleep(2)
                
            except Exception as e:
                logger.error(f"[{self.request_id}] Error in audio read thread: {e}", exc_info=True)
                time.sleep(1)
        
        logger.info(f"[{self.request_id}] Audio read thread ended")
    
    def _push_translated_audio_thread(self):
        """推送翻译后的音频到SRS"""
        # 翻译流地址: {room_id}_{source_user}_to_{lang}
        # 注意：必须使用 127.0.0.1:1935 而不是 localhost:1935，否则 FFmpeg 可能无法连接
        # 将 http://127.0.0.1:8080 或 http://localhost:8080 转换为 rtmp://127.0.0.1:1935
        srs_url = self.srs_url
        if 'localhost' in srs_url:
            srs_url_for_rtmp = srs_url.replace('http://localhost:8080', 'rtmp://127.0.0.1:1935')
            srs_url_for_rtmp = srs_url_for_rtmp.replace('http://localhost', 'rtmp://127.0.0.1:1935')
        else:
            # 替换协议和端口
            import re
            srs_url_for_rtmp = re.sub(r'^http://127\.0\.0\.1:\d+', 'rtmp://127.0.0.1:1935', srs_url)
        rtmp_url = f"{srs_url_for_rtmp}/live/{self.stream_name}"
        
        logger.info(f"[{self.request_id}] Translation output stream: {rtmp_url}")
        
        # 初始化TTS服务
        tts_service = None
        try:
            api_key = os.getenv("BAIDU_API_KEY", "")
            secret_key = os.getenv("BAIDU_SECRET_KEY", "")
            if api_key and secret_key:
                tts_service = BaiduTTSService(api_key, secret_key)
                logger.info(f"[{self.request_id}] TTS service initialized")
        except Exception as e:
            logger.warning(f"[{self.request_id}] Failed to initialize TTS service: {e}")
        
        # FFmpeg命令：将PCM音频推送到RTMP
        ffmpeg_bin = os.environ.get("FFMPEG_BIN", "/usr/bin/ffmpeg")
        ffmpeg_output_cmd = [
            ffmpeg_bin,
            "-f", "s16le",
            "-ar", "16000",
            "-ac", "1",
            "-i", "-",
            "-acodec", "aac",
            "-b:a", "64k",
            "-f", "flv",
            "-re",
            rtmp_url
        ]
        
        logger.info(f"[{self.request_id}] Starting FFmpeg output process: {' '.join(ffmpeg_output_cmd)}")
        
        max_retries = 3
        retry_count = 0
        
        try:
            self.output_process = subprocess.Popen(
                ffmpeg_output_cmd,
                stdin=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0
            )
            
            logger.info(f"[{self.request_id}] FFmpeg output process started, PID={self.output_process.pid}")
            
            texts_processed = 0
            
            # 处理翻译后的文本并转换为语音
            while self.is_running:
                # 检查 FFmpeg 进程是否退出
                if self.output_process and self.output_process.poll() is not None:
                    stderr = self.output_process.stderr.read() if self.output_process.stderr else b""
                    stderr_text = stderr.decode('utf-8', errors='ignore') if stderr else ""
                    logger.error(f"[{self.request_id}] FFmpeg output process died! returncode={self.output_process.returncode}, stderr: {stderr_text[:500]}")
                    
                    retry_count += 1
                    if retry_count <= max_retries:
                        logger.info(f"[{self.request_id}] Attempting to restart FFmpeg output process ({retry_count}/{max_retries})...")
                        time.sleep(2)
                        self.output_process = subprocess.Popen(
                            ffmpeg_output_cmd,
                            stdin=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            bufsize=0
                        )
                        logger.info(f"[{self.request_id}] FFmpeg output process restarted, PID={self.output_process.pid}")
                    else:
                        logger.error(f"[{self.request_id}] FFmpeg output process failed after {max_retries} restart attempts, giving up")
                        break
                
                translated_data = self.translation_client.get_translated_text()
                if translated_data and tts_service:
                    text = translated_data.get('text', '').strip()
                    if text:
                        target_lang = translated_data.get('target_lang', 'en')
                        tts_lang = translated_data.get('tts_lang', target_lang)
                        
                        texts_processed += 1
                        logger.info(f"[{self.request_id}] Processing TTS for text #{texts_processed}: lang={tts_lang}, text='{text[:50]}...'")

                        audio_data = tts_service.text_to_speech(text, lang=tts_lang)
                        if audio_data:
                            audio_size = len(audio_data)
                            logger.info(f"[{self.request_id}] TTS generated audio: size={audio_size} bytes")
                            
                            if self.output_process and self.output_process.stdin and self.output_process.poll() is None:
                                try:
                                    self.output_process.stdin.write(audio_data)
                                    self.output_process.stdin.flush()
                                    logger.info(f"[{self.request_id}] ✓ Pushed translated audio ({tts_lang}): {text[:50]}...")
                                except Exception as e:
                                    logger.error(f"[{self.request_id}] Error writing audio to FFmpeg: {e}", exc_info=True)
                        else:
                            logger.warning(f"[{self.request_id}] TTS returned empty audio for: {text[:50]}...")
                    else:
                        logger.debug(f"[{self.request_id}] Empty text in translation data")
                elif translated_data and not tts_service:
                    logger.warning(f"[{self.request_id}] TTS service not available, skipping text: {translated_data.get('text', '')[:30]}...")
                
                time.sleep(0.05)  # 稍微快一点的轮询
                
            logger.info(f"[{self.request_id}] Audio push thread ended. Total texts processed: {texts_processed}")
                
        except Exception as e:
            logger.error(f"[{self.request_id}] Error pushing translated audio: {e}", exc_info=True)
    
    def stop(self):
        """停止音频流处理"""
        self.is_running = False
        
        if self.ffmpeg_process:
            self.ffmpeg_process.terminate()
            self.ffmpeg_process.wait()
        
        if self.output_process:
            self.output_process.terminate()
            self.output_process.wait()


class TranslationService:
    """翻译服务主类"""
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.translation_client = None
        self.audio_processor = None
        self.request_id = os.getenv("REQUEST_ID", "unknown")
        
    def initialize(self):
        """初始化服务"""
        api_key = self.config.get("baidu_api_key")
        secret_key = self.config.get("baidu_secret_key")
        
        if not api_key or not secret_key:
            raise ValueError("Baidu API key and secret key are required")
        
        logger.info(f"[{self.request_id}] Initializing with API key length: {len(api_key)}")
        
        audio_config = self.config.get("audio", {})
        
        # 百度翻译开放平台配置（可选）
        mt_app_id = os.getenv("BAIDU_MT_APP_ID", "")
        mt_app_secret = os.getenv("BAIDU_MT_APP_SECRET", "")
        
        if mt_app_id and mt_app_secret:
            logger.info(f"[{self.request_id}] Using Baidu Translation Platform (app_id={mt_app_id[:8]}...)")
        else:
            logger.warning(f"[{self.request_id}] BAIDU_MT_APP_ID/APP_SECRET not set, will try AI Platform")
        
        # 详细记录音频配置
        logger.info(f"[{self.request_id}] Audio config details:")
        logger.info(f"[{self.request_id}]   sample_rate: {audio_config.get('sample_rate', 'NOT SET')}")
        logger.info(f"[{self.request_id}]   channels: {audio_config.get('channels', 'NOT SET')}")
        logger.info(f"[{self.request_id}]   format: {audio_config.get('format', 'NOT SET')}")
        logger.info(f"[{self.request_id}]   asr_format: {audio_config.get('asr_format', 'NOT SET')}")
        logger.info(f"[{self.request_id}]   input_format: {audio_config.get('input_format', 'NOT SET')}")
        
        # 验证采样率配置
        sample_rate = audio_config.get("sample_rate")
        if sample_rate not in [8000, 16000]:
            logger.warning(f"[{self.request_id}] Sample rate {sample_rate} is not standard for Baidu ASR. "
                         f"Will attempt resampling to {sample_rate}Hz.")
        else:
            logger.info(f"[{self.request_id}] Sample rate {sample_rate}Hz is supported by Baidu ASR")
        
        self.translation_client = BaiduTranslationClient(
            api_key, secret_key, audio_config,
            mt_app_id=mt_app_id or None,
            mt_app_secret=mt_app_secret or None
        )
        self.translation_client.start_translation()
        
        logger.info("Translation service initialized")
    
    def start(self):
        """启动处理"""
        srs_url = self.config.get("srs_url", "http://localhost:8080")
        room_id = self.config.get("room_id", "")
        source_user = self.config.get("source_user", "")
        to_lang = self.config.get("to_lang", "en")
        stream_name = self.config.get("stream_name", "")
        
        logger.info(f"[{self.request_id}] Starting AudioStreamProcessor: srs_url={srs_url}, room={room_id}, "
                   f"source_user={source_user}, to_lang={to_lang}, stream_name={stream_name}")
        
        audio_config = self.config.get("audio", {})
        
        self.audio_processor = AudioStreamProcessor(
            srs_url=srs_url,
            room_id=room_id,
            source_user=source_user,
            to_lang=to_lang,
            stream_name=stream_name,
            translation_client=self.translation_client,
            audio_config=audio_config
        )
        
        self.audio_processor.start()
        
        logger.info(f"[{self.request_id}] Started processing: room={room_id}, "
                   f"source_user={source_user}, to_lang={to_lang}, stream_name={stream_name}")
    
    def stop(self):
        """停止服务"""
        logger.info(f"[{self.request_id}] Stopping translation service...")
        
        if self.audio_processor:
            self.audio_processor.stop()
        
        if self.translation_client:
            self.translation_client.stop()
        
        logger.info(f"[{self.request_id}] Translation service stopped")


def main():
    """主函数"""
    request_id = os.getenv("REQUEST_ID", "unknown")
    room_id = os.getenv("ROOM_ID", "")
    source_user = os.getenv("SOURCE_USER", "")
    to_lang = os.getenv("TO_LANG", "en")
    stream_name = os.getenv("STREAM_NAME", "")
    
    config = {
        "baidu_api_key": os.getenv("BAIDU_API_KEY", ""),
        "baidu_secret_key": os.getenv("BAIDU_SECRET_KEY", ""),
        "srs_url": os.getenv("SRS_URL", "http://localhost:8080"),
        "room_id": room_id,
        "source_user": source_user,
        "to_lang": to_lang,
        "stream_name": stream_name,
    }
    
    # 从配置文件加载音频配置
    config_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audio_translation_config.json")
    if os.path.exists(config_file):
        try:
            with open(config_file, 'r', encoding='utf-8') as f:
                file_config = json.load(f)
            if "audio" in file_config:
                config["audio"] = file_config["audio"]
            logger.info(f"[{request_id}] Loaded audio config from {config_file}")
        except Exception as e:
            logger.warning(f"[{request_id}] Failed to load audio config: {e}")
    
    # 确保 asr_format 为 pcm（因为翻译服务从 HTTP-FLV 解码后输出 PCM）
    if "audio" not in config:
        config["audio"] = {}
    if not config["audio"].get("asr_format"):
        config["audio"]["asr_format"] = "pcm"
    
    if not config["baidu_api_key"] or not config["baidu_secret_key"]:
        logger.error(f"[{request_id}] Please set BAIDU_API_KEY and BAIDU_SECRET_KEY environment variables")
        sys.exit(1)
    
    if not config["room_id"] or not config["source_user"] or not config["stream_name"]:
        logger.error(f"[{request_id}] Please set ROOM_ID, SOURCE_USER, STREAM_NAME environment variables")
        sys.exit(1)
    
    logger.info(f"[{request_id}] Starting translation service: room={room_id}, source_user={source_user}, "
               f"to_lang={to_lang}, stream_name={stream_name}")
    
    service = TranslationService(config)
    
    try:
        service.initialize()
        logger.info(f"[{request_id}] Service initialized successfully")
        service.start()
        
        logger.info("Translation service is running. Press Ctrl+C to stop.")
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
