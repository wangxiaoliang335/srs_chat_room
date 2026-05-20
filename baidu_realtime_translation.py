#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
百度实时语音翻译 WebSocket 客户端
参考 Demo: realtime_trans_python3/realtime-asr-python.py
直接发送二进制音频，不需要 Base64 编码
"""

import os
import json
import logging
import websocket
import threading
import time
import queue
from typing import Optional, Dict, Any, Callable
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class BaiduRealtimeTranslationClient:
    """百度实时语音翻译 WebSocket 客户端

    使用 WebSocket 直接传输二进制音频，无需 Base64 编码
    集成 ASR + MT + TTS，一步到位
    """

    # 实时翻译 WebSocket URL
    WS_URL = "wss://aip.baidubce.com/ws/realtime_speech_trans"

    def __init__(self, app_id: str, app_key: str,
                 from_lang: str = "zh", to_lang: str = "en",
                 sample_rate: int = 16000, channels: int = 1):
        self.app_id = app_id
        self.app_key = app_key
        self.from_lang = from_lang
        self.to_lang = to_lang
        self.sample_rate = sample_rate
        self.channels = channels

        self.ws = None
        self.is_running = False
        self.ws_thread = None

        # 回调函数
        self.on_translation_callback: Optional[Callable] = None
        self.on_tts_audio_callback: Optional[Callable] = None
        self.on_error_callback: Optional[Callable] = None

        # TTS 音频数据缓冲
        self.tts_audio_buffer = b""

        # TTS 音频保存设置
        self.tts_save_dir = "tts_recordings"
        self.tts_save_enabled = False
        self.tts_save_file = None
        self.tts_save_count = 0

        # 输入音频保存设置（发送给百度的原始语音）
        self.input_save_dir = "input_recordings"
        self.input_save_enabled = False
        self.input_save_file = None
        self.input_save_count = 0

        # 请求标识（用于日志）
        self.request_id = "baidu"

        # 音频数据队列（外部放入，WebSocket 线程发送）
        # 增大队列以避免丢包：500 * 3840字节 ≈ 75秒缓冲
        self.audio_queue = queue.Queue(maxsize=500)

    def set_translation_callback(self, callback: Callable):
        """设置翻译结果回调

        Args:
            callback: 回调函数，签名: callback(text, original_text, from_lang, to_lang)
        """
        self.on_translation_callback = callback

    def set_tts_audio_callback(self, callback: Callable):
        """设置 TTS 音频回调

        Args:
            callback: 回调函数，签名: callback(audio_data: bytes)
        """
        self.on_tts_audio_callback = callback

    def set_error_callback(self, callback: Callable):
        """设置错误回调

        Args:
            callback: 回调函数，签名: callback(error_msg: str)
        """
        self.on_error_callback = callback

    def set_tts_save_config(self, enabled: bool, save_dir: str = "tts_recordings"):
        """配置 TTS 音频保存

        Args:
            enabled: 是否启用保存
            save_dir: 保存目录
        """
        self.tts_save_enabled = enabled
        self.tts_save_dir = save_dir
        if enabled and not os.path.exists(save_dir):
            os.makedirs(save_dir, exist_ok=True)
            logger.info(f"Created TTS save directory: {save_dir}")

    def set_input_save_config(self, enabled: bool, save_dir: str = "input_recordings"):
        """配置输入音频保存（发送给百度的原始语音）

        Args:
            enabled: 是否启用保存
            save_dir: 保存目录
        """
        self.input_save_enabled = enabled
        self.input_save_dir = save_dir
        if enabled and not os.path.exists(save_dir):
            os.makedirs(save_dir, exist_ok=True)
            logger.info(f"Created input save directory: {save_dir}")

    def _start_new_input_file(self):
        """开始一个新的输入音频保存文件"""
        if not self.input_save_enabled:
            return
        self.input_save_count += 1
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        filename = f"input_{timestamp}.pcm"
        filepath = os.path.join(self.input_save_dir, filename)
        self.input_save_file = open(filepath, 'wb')
        logger.info(f"[{self.request_id}] Started input save: {filepath}")

    def _write_input_audio(self, audio_data: bytes):
        """写入输入音频数据到文件"""
        if not self.input_save_enabled:
            return
        if not self.input_save_file:
            self._start_new_input_file()
        try:
            self.input_save_file.write(audio_data)
        except Exception as e:
            logger.error(f"[{self.request_id}] Error writing input audio: {e}")

    def _close_input_file(self):
        """关闭当前的输入音频保存文件"""
        if not self.input_save_enabled or not self.input_save_file:
            return
        try:
            if self.input_save_file:
                self.input_save_file.close()
                logger.info(f"[{self.request_id}] Input file saved: {self.input_save_file.name}")
                self.input_save_file = None
        except Exception as e:
            logger.error(f"[{self.request_id}] Error closing input file: {e}")

    def _start_new_tts_file(self):
        """开始一个新的 TTS 保存文件"""
        if not self.tts_save_enabled:
            return
        self.tts_save_count += 1
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        filename = f"tts_{timestamp}.mp3"
        filepath = os.path.join(self.tts_save_dir, filename)
        self.tts_save_file = open(filepath, 'wb')
        logger.info(f"[{self.request_id}] Started TTS save: {filepath}")

    def _write_tts_audio(self, audio_data: bytes):
        """写入 TTS 音频数据到文件"""
        if not self.tts_save_enabled or not self.tts_save_file:
            return
        try:
            self.tts_save_file.write(audio_data)
        except Exception as e:
            logger.error(f"[{self.request_id}] Error writing TTS audio: {e}")

    def _close_tts_file(self):
        """关闭当前的 TTS 保存文件"""
        if not self.tts_save_enabled or not self.tts_save_file:
            return
        try:
            if self.tts_save_file:
                self.tts_save_file.close()
                logger.info(f"[{self.request_id}] TTS file saved: {self.tts_save_file.name}")
                self.tts_save_file = None
        except Exception as e:
            logger.error(f"[{self.request_id}] Error closing TTS file: {e}")

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

    def _on_open(self, ws):
        """WebSocket 连接打开"""
        logger.info("WebSocket connected")

        # 发送开始帧
        start_frame = self._get_start_frame()
        body = json.dumps(start_frame)
        ws.send(body, websocket.ABNF.OPCODE_TEXT)
        logger.info(f"Sent START frame: {start_frame}")

        # 启动音频发送线程
        self.is_running = True
        threading.Thread(target=self._send_audio_thread, daemon=True).start()

    def _send_audio_thread(self):
        """发送音频数据线程"""
        # 百度 WebSocket API 缓冲区限制为 3840 字节
        # 16kHz * 2 bytes * 1 channel * 120ms / 1000 = 3840 字节
        chunk_ms = 120
        chunk_len = int(self.sample_rate * 2 * self.channels * chunk_ms / 1000)
        logger.info(f"Audio send thread started: chunk_ms={chunk_ms}, chunk_len={chunk_len}")

        consecutive_empty = 0

        while self.is_running:
            try:
                # 从队列获取音频数据
                try:
                    audio_data = self.audio_queue.get(timeout=0.1)
                except queue.Empty:
                    consecutive_empty += 1
                    if consecutive_empty > 50:  # 5秒没有数据
                        # 发送静音包保持连接
                        silence = b'\x00' * chunk_len
                        self.ws.send(silence, websocket.ABNF.OPCODE_BINARY)
                        consecutive_empty = 0
                    continue

                consecutive_empty = 0

                # 发送音频数据（二进制）
                if len(audio_data) >= chunk_len:
                    # 发送完整的 chunk
                    self.ws.send(audio_data[:chunk_len], websocket.ABNF.OPCODE_BINARY)

                    # 发送成功后才保存到本地
                    logger.info(f"input_save_enabled={self.input_save_enabled}")
                    if self.input_save_enabled:
                        logger.info(f"Saving to local: chunk_len={chunk_len}, audio_size={len(audio_data[:chunk_len])}")
                        self._write_input_audio(audio_data[:chunk_len])

                    # 把剩余的放回队列
                    if len(audio_data) > chunk_len:
                        remaining = audio_data[chunk_len:]
                        try:
                            self.audio_queue.put(remaining, block=False)
                        except queue.Full:
                            pass
                else:
                    # 数据不够一个 chunk，等待累积
                    try:
                        self.audio_queue.put(audio_data, block=False)
                    except queue.Full:
                        pass

                # 控制发送速率
                time.sleep(chunk_ms / 1000.0)

            except Exception as e:
                logger.error(f"Error in send_audio_thread: {e}")
                break

        logger.info("Audio send thread ended")

    def _on_message(self, ws, message):
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
                    logger.debug(f"Received TTS MP3: {len(tts_audio)} bytes")

                    # 保存到本地文件
                    self._write_tts_audio(tts_audio)

                    if self.on_tts_audio_callback:
                        self.on_tts_audio_callback(tts_audio)
                else:
                    logger.warning(f"Unknown binary message type: 0x{tts_type:02x}")

        elif isinstance(message, str):
            # JSON 消息（翻译结果）
            try:
                result = json.loads(message)
                logger.debug(f"Received result: {result}")

                # 处理翻译结果（根据文档格式）
                if result.get("code") == 0 and result.get("data"):
                    data = result.get("data", {})
                    status = data.get("status", "")

                    if status == "TRN":
                        # 翻译结果
                        result_obj = data.get("result", {})
                        result_type = result_obj.get("type", "")  # MID 或 FIN

                        if result_type == "FIN":
                            # 最终结果
                            sentence = result_obj.get("sentence", "")
                            sentence_trans = result_obj.get("sentence_trans", "")

                            # 每收到最终结果，开启新的TTS保存文件
                            self._start_new_tts_file()

                            if sentence_trans:
                                logger.info(f"Translation [{result_type}]: '{sentence}' -> '{sentence_trans}'")
                                if self.on_translation_callback:
                                    self.on_translation_callback(
                                        sentence_trans, sentence, self.from_lang, self.to_lang
                                    )
                        elif result_type == "MID":
                            # 中间结果
                            asr = result_obj.get("asr", "")
                            asr_trans = result_obj.get("asr_trans", "")

                            if asr_trans:
                                logger.debug(f"Translation [{result_type}]: '{asr}' -> '{asr_trans}'")
                                if self.on_translation_callback:
                                    self.on_translation_callback(
                                        asr_trans, asr, self.from_lang, self.to_lang
                                    )

                    elif status == "END":
                        # 会话结束，关闭TTS文件和输入文件
                        self._close_tts_file()
                        self._close_input_file()
                        logger.info("Session ended")

                elif result.get("code") != 0:
                    error_msg = result.get("msg", "Unknown error")
                    logger.error(f"Translation error: {error_msg}")
                    if self.on_error_callback:
                        self.on_error_callback(error_msg)

            except json.JSONDecodeError as e:
                logger.warning(f"Failed to parse message: {e}, message={message}")

    def _on_error(self, ws, error):
        """WebSocket 错误"""
        logger.error(f"WebSocket error: {error}")
        if self.on_error_callback:
            self.on_error_callback(str(error))

    def _on_close(self, ws, close_status_code, close_msg):
        """WebSocket 关闭"""
        logger.info(f"WebSocket closed: {close_status_code} - {close_msg}")
        self.is_running = False

    def connect(self):
        """建立 WebSocket 连接"""
        if self.ws and self.is_running:
            logger.warning("WebSocket already connected")
            return

        logger.info(f"Connecting to {self.WS_URL}")

        self.ws = websocket.WebSocketApp(
            self.WS_URL,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )

        # 在单独线程中运行
        self.ws_thread = threading.Thread(target=self.ws.run_forever, daemon=True)
        self.ws_thread.start()

    def disconnect(self):
        """断开 WebSocket 连接"""
        logger.info("Disconnecting WebSocket...")

        self.is_running = False

        if self.ws:
            # 发送结束帧
            try:
                finish_frame = self._get_finish_frame()
                self.ws.send(json.dumps(finish_frame), websocket.ABNF.OPCODE_TEXT)
                logger.info("Sent FINISH frame")
            except Exception as e:
                logger.warning(f"Failed to send FINISH frame: {e}")

            # 关闭 WebSocket
            self.ws.close()
            self.ws = None

        if self.ws_thread:
            self.ws_thread.join(timeout=2)
            self.ws_thread = None

        logger.info("WebSocket disconnected")

    def add_audio(self, audio_data: bytes):
        """添加音频数据

        Args:
            audio_data: PCM 音频数据（二进制）
        """
        if self.is_running:
            try:
                self.audio_queue.put(audio_data, block=False)
            except queue.Full:
                logger.warning("Audio queue full, dropping audio")

    def start(self):
        """启动连接"""
        self.connect()

    def stop(self):
        """停止连接"""
        self.disconnect()


class RealtimeTranslationProcessor:
    """实时翻译处理器（封装 BaiduRealtimeTranslationClient）
    
    提供音频处理、翻译回调、TTS 回调、错误回调等功能
    """
    
    def __init__(self, app_id: str, app_key: str,
                 from_lang: str = "zh", to_lang: str = "en",
                 sample_rate: int = 16000, channels: int = 1,
                 return_tts: bool = True, tts_speaker: str = "woman"):
        """初始化处理器
        
        Args:
            app_id: 百度应用 App ID
            app_key: 百度应用 App Key
            from_lang: 源语言
            to_lang: 目标语言
            sample_rate: 采样率
            channels: 声道数
            return_tts: 是否返回 TTS
            tts_speaker: TTS 发音人
        """
        self.app_id = app_id
        self.app_key = app_key
        self.from_lang = from_lang
        self.to_lang = to_lang
        self.sample_rate = sample_rate
        self.channels = channels
        self.return_tts = return_tts
        self.tts_speaker = tts_speaker
        
        self.client = None
        self._connected = False
        
        # 回调函数（与 audio_translation_service_websocket.py 的期望一致）
        self.on_translation_callback = None  # 回调签名: callback(result: Dict)
        self.on_tts_callback = None         # 回调签名: callback(audio_data: bytes)
        self.on_error_callback = None       # 回调签名: callback(code: int, msg: str)
    
    def set_tts_save_config(self, enabled: bool, save_dir: str = "tts_recordings"):
        """配置 TTS 音频保存

        Args:
            enabled: 是否启用保存
            save_dir: 保存目录
        """
        if self.client:
            self.client.set_tts_save_config(enabled, save_dir)

    def set_input_save_config(self, enabled: bool, save_dir: str = "input_recordings"):
        """配置输入音频保存（发送给百度的原始语音）

        Args:
            enabled: 是否启用保存
            save_dir: 保存目录
        """
        logger.info(f"RealtimeTranslationProcessor.set_input_save_config called: enabled={enabled}, save_dir={save_dir}")
        if self.client:
            self.client.set_input_save_config(enabled, save_dir)

    def connect(self) -> bool:
        """建立连接"""
        try:
            self.client = BaiduRealtimeTranslationClient(
                app_id=self.app_id,
                app_key=self.app_key,
                from_lang=self.from_lang,
                to_lang=self.to_lang,
                sample_rate=self.sample_rate,
                channels=self.channels
            )
            
            # 设置回调
            self.client.set_translation_callback(self._on_translation)
            self.client.set_tts_audio_callback(self._on_tts_audio)
            self.client.set_error_callback(self._on_error)
            
            # 启动连接
            self.client.start()
            self._connected = True
            
            logger.info(f"RealtimeTranslationProcessor connected: {self.from_lang} -> {self.to_lang}")
            return True
            
        except Exception as e:
            logger.error(f"Failed to connect RealtimeTranslationProcessor: {e}")
            self._connected = False
            return False
    
    def _on_translation(self, translated_text: str, original_text: str, from_lang: str, to_lang: str):
        """翻译结果回调"""
        # 转换为字典格式以匹配 audio_translation_service_websocket.py 的期望
        result = {
            "asr_text": original_text,
            "translation": translated_text,
            "is_final": True,
            "from_lang": from_lang,
            "to_lang": to_lang
        }
        if self.on_translation_callback:
            self.on_translation_callback(result)
    
    def _on_tts_audio(self, audio_data: bytes):
        """TTS 音频回调"""
        if self.on_tts_callback:
            self.on_tts_callback(audio_data)
    
    def _on_error(self, error_msg: str):
        """错误回调"""
        logger.error(f"Translation error: {error_msg}")
        if self.on_error_callback:
            self.on_error_callback(-1, error_msg)
    
    def disconnect(self):
        """断开连接"""
        self._connected = False
        if self.client:
            self.client.stop()
            self.client = None
        logger.info("RealtimeTranslationProcessor disconnected")
    
    def add_audio(self, audio_data: bytes):
        """添加音频数据"""
        if self.client and self._connected:
            self.client.add_audio(audio_data)
    
    def is_connected(self) -> bool:
        """检查是否已连接"""
        return self._connected and self.client is not None and self.client.is_running


def test():
    """测试"""
    app_id = os.getenv("BAIDU_APP_ID", "")
    app_key = os.getenv("BAIDU_APP_KEY", "")

    if not app_id or not app_key:
        logger.error("Please set BAIDU_APP_ID and BAIDU_APP_KEY")
        return

    client = BaiduRealtimeTranslationClient(
        app_id=app_id,
        app_key=app_key,
        from_lang="zh",
        to_lang="en",
        sample_rate=16000,
        channels=1
    )

    def on_translation(text, original, from_lang, to_lang):
        logger.info(f"=== Translation: {original} -> {text}")

    def on_tts(audio):
        logger.info(f"=== TTS audio received: {len(audio)} bytes")

    client.set_translation_callback(on_translation)
    client.set_tts_audio_callback(on_tts)

    # 启动
    client.start()

    # 模拟发送音频（从文件）
    import sys
    if len(sys.argv) > 1:
        audio_file = sys.argv[1]
        with open(audio_file, 'rb') as f:
            audio = f.read()

        logger.info(f"Sending audio file: {audio_file}, size={len(audio)}")
        client.add_audio(audio)

    # 等待一段时间
    time.sleep(10)

    # 停止
    client.stop()


if __name__ == "__main__":
    test()
