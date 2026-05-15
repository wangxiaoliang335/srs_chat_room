#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
百度实时语音翻译 WebSocket 客户端
使用 WebSocket 协议实现真正的实时语音翻译
支持：语音识别 + 机器翻译 + TTS语音合成
"""

import os
import json
import logging
import threading
import queue
import time
import struct
import base64
from typing import Optional, Dict, Any, Callable
from websocket import create_connection, WebSocketException

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class BaiduRealtimeTranslationClient:
    """百度实时语音翻译 WebSocket 客户端
    
    使用 WebSocket 协议实现真正的实时流式翻译
    - 支持 45 种语言
    - 实时语音识别
    - 实时机器翻译
    - 实时 TTS 语音合成（可选）
    
    音频要求：
    - 格式：PCM
    - 采样率：8kHz、16kHz、44.1kHz
    - 位深：16bits
    - 单声道
    - 小端序
    """
    
    def __init__(self, app_id: str, app_key: str, 
                 from_lang: str = "zh", to_lang: str = "en",
                 sample_rate: int = 16000,
                 return_tts: bool = True,
                 tts_speaker: str = "woman",
                 on_translation_result: Callable[[Dict], None] = None,
                 on_tts_audio: Callable[[bytes], None] = None,
                 on_error: Callable[[int, str], None] = None):
        """初始化实时翻译客户端
        
        Args:
            app_id: 百度应用 App ID
            app_key: 百度应用 App Key
            from_lang: 源语言代码（如 "zh", "en"）
            to_lang: 目标语言代码（如 "en", "zh"）
            sample_rate: 采样率（8000, 16000, 44100）
            return_tts: 是否返回 TTS 音频
            tts_speaker: TTS 发音人（"man" 或 "woman"）
            on_translation_result: 翻译结果回调 {asr, translation, is_final, ...}
            on_tts_audio: TTS 音频回调（二进制 MP3 数据）
            on_error: 错误回调 (error_code, error_msg)
        """
        self.app_id = app_id
        self.app_key = app_key
        self.from_lang = from_lang
        self.to_lang = to_lang
        self.sample_rate = sample_rate
        self.return_tts = return_tts
        self.tts_speaker = tts_speaker
        
        self.on_translation_result = on_translation_result
        self.on_tts_audio = on_tts_audio
        self.on_error = on_error
        
        self.ws = None
        self.is_connected = False
        self.is_running = False
        self.receive_thread = None
        self.lock = threading.Lock()
        self.bytes_sent = 0
        
        # WebSocket URL
        self.ws_url = "wss://aip.baidubce.com/ws/realtime_speech_trans"
        
        # 音频包配置（40ms @ 16000Hz = 1280 bytes）
        self.audio_chunk_duration_ms = 40
        self.audio_chunk_size = int(sample_rate * 2 * self.audio_chunk_duration_ms / 1000)  # 16bits * 1ch * 40ms
        
        logger.info(f"BaiduRealtimeTranslationClient initialized: {from_lang} -> {to_lang}, "
                   f"sample_rate={sample_rate}, return_tts={return_tts}")
    
    def connect(self) -> bool:
        """建立 WebSocket 连接并发送开始报文
        
        Returns:
            连接是否成功
        """
        try:
            logger.info(f"Connecting to {self.ws_url}...")
            # 设置 60 秒超时
            self.ws = create_connection(self.ws_url, timeout=60)
            
            # 发送开始报文
            start_msg = {
                "type": "START",
                "from": self.from_lang,
                "to": self.to_lang,
                "app_id": self.app_id,
                "app_key": self.app_key,
                "sampling_rate": self.sample_rate,
                "return_target_tts": 1 if self.return_tts else 0,
                "tts_speaker": self.tts_speaker
            }
            
            logger.info(f"Sending START message: {json.dumps(start_msg)}")
            self.ws.send(json.dumps(start_msg))
            
            # 等待确认（60秒超时）
            logger.info("Waiting for START response...")
            self.ws.settimeout(60)
            response = self.ws.recv()
            logger.info(f"Received START response: {response}")
            resp_data = json.loads(response)
            
            if resp_data.get("code") == 0 and resp_data.get("data", {}).get("status") == "STA":
                logger.info("WebSocket connection established, translation started")
                self.is_connected = True
                self.is_running = True
                
                # 启动接收线程
                self.receive_thread = threading.Thread(target=self._receive_thread, daemon=True)
                self.receive_thread.start()
                
                return True
            else:
                error_msg = resp_data.get("msg", "Unknown error")
                error_code = resp_data.get("code", -1)
                logger.error(f"Failed to start translation: code={error_code}, msg={error_msg}")
                self.ws.close()
                self.ws = None
                return False
                
        except WebSocketException as e:
            logger.error(f"WebSocket connection failed: {e}")
            return False
        except Exception as e:
            logger.error(f"Unexpected error during connection: {e}", exc_info=True)
            return False
    
    def _receive_thread(self):
        """接收服务器消息的线程"""
        logger.info("Receive thread started")
        
        while self.is_running and self.is_connected:
            try:
                if self.ws:
                    # 设置超时，避免一直阻塞
                    self.ws.settimeout(5.0)  # 增加到 5 秒
                    message = self.ws.recv()
                    
                    if message is None:
                        continue
                    
                    # 检查消息类型
                    if isinstance(message, bytes):
                        # 二进制消息：TTS 音频
                        # 格式：1字节 type + payload
                        if len(message) > 1:
                            msg_type = message[0]
                            audio_data = message[1:]
                            
                            if msg_type == 0x01:
                                # TTS 音频
                                logger.info(f"Received TTS audio: {len(audio_data)} bytes")
                                # 诊断：检查音频数据格式
                                # MP3帧同步以0xFF开头，第二字节的高3位为111表示MPEG Audio
                                if len(audio_data) >= 2:
                                    first_two = audio_data[:2]
                                    # 0xFF 0xE0-0xFF 表示有效的MP3帧同步
                                    if first_two[0] == 0xFF and (first_two[1] & 0xE0) == 0xE0:
                                        mpeg_version = (first_two[1] >> 3) & 0x03
                                        layer = (first_two[1] >> 1) & 0x03
                                        version_str = {0: "MPEG-2.5", 1: "Reserved", 2: "MPEG-2", 3: "MPEG-1"}[mpeg_version]
                                        layer_str = {0: "Reserved", 1: "Layer III", 2: "Layer II", 3: "Layer I"}[layer]
                                        logger.info(f"TTS audio format: MP3 ({version_str}, {layer_str})")
                                    else:
                                        logger.warning(f"TTS audio format: Unknown (header: {first_two.hex()})")
                                if self.on_tts_audio:
                                    self.on_tts_audio(audio_data)
                            else:
                                logger.warning(f"Unknown binary message type: 0x{msg_type:02x}")
                    else:
                        # 文本消息：JSON 格式的翻译结果
                        data = json.loads(message)
                        self._handle_text_message(data)
                        
            except TimeoutError:
                # 超时，继续循环（这是正常的，保持心跳）
                continue
            except TimeoutError as e:
                # SSL socket 超时
                continue
            except OSError as e:
                # 连接断开等 OS 错误
                if self.is_running:
                    logger.warning(f"Connection error in receive thread: {e}")
                break
            except Exception as e:
                if self.is_running:
                    # 检查是否是 WebSocket 超时
                    error_str = str(e)
                    if "timeout" in error_str.lower() or "timed out" in error_str.lower():
                        logger.info("WebSocket receive timeout, continuing...")
                        continue
                    logger.error(f"Error in receive thread: {e}", exc_info=True)
                break
        
        logger.info("Receive thread ended")
    
    def _handle_text_message(self, data: Dict):
        """处理文本消息"""
        code = data.get("code", -1)
        msg = data.get("msg", "")
        
        if code != 0:
            # 错误消息
            logger.error(f"Translation error: code={code}, msg={msg}")
            if self.on_error:
                self.on_error(code, msg)
            return
        
        # 检查状态
        status = data.get("data", {}).get("status", "")
        
        if status == "TRN":
            # 翻译结果
            result = data.get("data", {}).get("result", {})
            result_type = result.get("type", "")  # MID 或 FIN
            
            # 提取识别和翻译文本
            asr_text = result.get("asr", "") or result.get("sentence", "")
            trans_text = result.get("asr_trans", "") or result.get("sentence_trans", "")
            is_final = (result_type == "FIN")
            
            if asr_text or trans_text:
                translation_result = {
                    "asr_text": asr_text,
                    "translation": trans_text,
                    "is_final": is_final,
                    "type": result_type,
                    "from_lang": self.from_lang,
                    "to_lang": self.to_lang
                }
                
                if is_final:
                    logger.info(f"[FIN] ASR: '{asr_text}' -> Translation: '{trans_text}'")
                else:
                    logger.debug(f"[MID] ASR: '{asr_text}' -> Translation: '{trans_text}'")
                
                if self.on_translation_result:
                    self.on_translation_result(translation_result)
                    
        elif status == "END":
            # 会话结束
            logger.info("Translation session ended")
            self.disconnect()
    
    def send_audio(self, audio_data: bytes) -> bool:
        """发送音频数据
        
        Args:
            audio_data: PCM 音频数据（二进制）
            
        Returns:
            发送是否成功
        """
        if not self.is_connected or not self.ws:
            logger.warning("Not connected, cannot send audio")
            return False
        
        try:
            with self.lock:
                self.ws.send(audio_data, opcode=2)  # Binary opcode
            self.bytes_sent += len(audio_data)
            # 每约1秒打印一次发送状态
            if self.bytes_sent % 32000 < 4096:
                logger.info(f"Audio sent: total={self.bytes_sent} bytes")
            return True
        except Exception as e:
            logger.error(f"Failed to send audio: {e}")
            return False
    
    def send_audio_chunk(self, audio_chunk: bytes) -> bool:
        """发送一个音频块（40ms）
        
        这是 send_audio 的别名，保持 API 一致性
        """
        return self.send_audio(audio_chunk)
    
    def disconnect(self):
        """断开连接"""
        logger.info("Disconnecting...")
        self.is_running = False
        self.is_connected = False
        
        try:
            if self.ws:
                # 发送结束报文
                try:
                    self.ws.settimeout(5)
                    self.ws.send(json.dumps({"type": "FINISH"}))
                    
                    # 等待确认
                    response = self.ws.recv()
                    resp_data = json.loads(response)
                    if resp_data.get("data", {}).get("status") == "END":
                        logger.info("Received END confirmation")
                except:
                    pass
                
                self.ws.close()
                self.ws = None
        except Exception as e:
            logger.warning(f"Error during disconnect: {e}")
        
        logger.info("Disconnected")
    
    def __del__(self):
        """析构函数，确保断开连接"""
        if self.is_connected:
            self.disconnect()


class RealtimeTranslationProcessor:
    """实时翻译处理器
    
    将 WebSocket 翻译客户端与音频流处理结合
    """
    
    def __init__(self, app_id: str, app_key: str,
                 from_lang: str = "zh", to_lang: str = "en",
                 sample_rate: int = 16000,
                 return_tts: bool = True,
                 tts_speaker: str = "woman",
                 audio_buffer_ms: int = 40):
        """初始化
        
        Args:
            app_id: 百度应用 App ID
            app_key: 百度应用 App Key
            from_lang: 源语言
            to_lang: 目标语言
            sample_rate: 采样率
            return_tts: 是否返回 TTS
            tts_speaker: TTS 发音人
            audio_buffer_ms: 音频缓冲时长（毫秒），建议 40-100ms
        """
        self.app_id = app_id
        self.app_key = app_key
        self.from_lang = from_lang
        self.to_lang = to_lang
        self.sample_rate = sample_rate
        self.return_tts = return_tts
        self.tts_speaker = tts_speaker
        
        # 计算每块音频大小
        self.audio_chunk_size = int(sample_rate * 2 * audio_buffer_ms / 1000)  # 16bits * 1ch * ms
        
        # 翻译结果队列
        self.translation_queue = queue.Queue(maxsize=100)
        
        # TTS 音频队列
        self.tts_queue = queue.Queue(maxsize=100)
        
        # 音频缓冲
        self.audio_buffer = b""
        self.max_buffer_size = self.audio_chunk_size * 10  # 最多缓冲 10 块
        
        # 客户端引用
        self.client = None
        
        # 重连标志
        self._needs_reconnect = False
        
        # 回调函数
        def on_translation(result):
            try:
                self.translation_queue.put_nowait(result)
            except queue.Full:
                logger.warning("Translation queue full, dropping result")
        
        def on_tts(audio):
            try:
                self.tts_queue.put_nowait(audio)
            except queue.Full:
                logger.warning("TTS queue full, dropping audio")
        
        def on_error(code, msg):
            logger.error(f"Translation error: {code} - {msg}")
        
        self.on_translation_callback = on_translation
        self.on_tts_callback = on_tts
        self.on_error_callback = on_error
    
    def connect(self) -> bool:
        """连接到百度实时翻译服务"""
        self.client = BaiduRealtimeTranslationClient(
            app_id=self.app_id,
            app_key=self.app_key,
            from_lang=self.from_lang,
            to_lang=self.to_lang,
            sample_rate=self.sample_rate,
            return_tts=self.return_tts,
            tts_speaker=self.tts_speaker,
            on_translation_result=self.on_translation_callback,
            on_tts_audio=self.on_tts_callback,
            on_error=self.on_error_callback
        )
        
        return self.client.connect()
    
    def add_audio(self, audio_data: bytes):
        """添加音频数据
        
        Args:
            audio_data: PCM 音频数据
        """
        if not self.client or not self.client.is_connected:
            logger.debug("Client not connected, skipping audio")
            return
        
        # 添加到缓冲
        self.audio_buffer += audio_data
        
        # 如果缓冲太大，丢弃最老的数据
        if len(self.audio_buffer) > self.max_buffer_size:
            excess = len(self.audio_buffer) - self.max_buffer_size
            self.audio_buffer = self.audio_buffer[excess:]
        
        # 发送完整的音频块
        sent_count = 0
        while len(self.audio_buffer) >= self.audio_chunk_size:
            chunk = self.audio_buffer[:self.audio_chunk_size]
            self.audio_buffer = self.audio_buffer[self.audio_chunk_size:]
            if self.client.send_audio(chunk):
                sent_count += 1
        
        if sent_count > 0:
            logger.debug(f"Sent {sent_count} audio chunks")
    
    def add_audio_chunk(self, chunk: bytes):
        """添加一个音频块（直接发送，不缓冲）"""
        if self.client and self.client.is_connected:
            self.client.send_audio(chunk)
    
    def get_translation(self, timeout: float = 0.1) -> Optional[Dict]:
        """获取翻译结果
        
        Args:
            timeout: 等待超时（秒）
            
        Returns:
            翻译结果字典，如果超时返回 None
        """
        try:
            return self.translation_queue.get(timeout=timeout)
        except queue.Empty:
            return None
    
    def get_tts_audio(self, timeout: float = 0.1) -> Optional[bytes]:
        """获取 TTS 音频
        
        Args:
            timeout: 等待超时（秒）
            
        Returns:
            TTS 音频数据（二进制 MP3），如果超时返回 None
        """
        try:
            return self.tts_queue.get(timeout=timeout)
        except queue.Empty:
            return None
    
    def disconnect(self):
        """断开连接"""
        if self.client:
            self.client.disconnect()
            self.client = None
    
    def is_connected(self) -> bool:
        """检查是否已连接"""
        return self.client is not None and self.client.is_connected
    
    def __del__(self):
        """析构函数"""
        self.disconnect()


def main():
    """测试实时翻译"""
    import argparse
    
    parser = argparse.ArgumentParser(description="百度实时语音翻译 WebSocket 测试")
    parser.add_argument("--app-id", required=True, help="百度应用 App ID")
    parser.add_argument("--app-key", required=True, help="百度应用 App Key")
    parser.add_argument("--from", default="zh", help="源语言 (默认: zh)")
    parser.add_argument("--to", default="en", help="目标语言 (默认: en)")
    parser.add_argument("--sample-rate", type=int, default=16000, help="采样率 (默认: 16000)")
    parser.add_argument("--no-tts", action="store_true", help="不返回 TTS 音频")
    parser.add_argument("--speaker", default="woman", choices=["man", "woman"], help="TTS 发音人")
    parser.add_argument("--test-file", help="测试音频文件路径 (PCM 格式)")
    
    args = parser.parse_args()
    
    translations_received = 0
    
    def on_translation(result):
        nonlocal translations_received
        translations_received += 1
        print(f"\n[{result['type']}] ASR: {result['asr_text']}")
        print(f"[{result['type']}] Translation: {result['translation']}")
    
    def on_tts(audio):
        print(f"\n[TTS] Received audio: {len(audio)} bytes")
        # 可以保存为文件测试
        with open("test_tts.mp3", "wb") as f:
            f.write(audio)
        print("[TTS] Saved to test_tts.mp3")
    
    def on_error(code, msg):
        print(f"\n[ERROR] {code}: {msg}")
    
    # 创建客户端
    client = BaiduRealtimeTranslationClient(
        app_id=args.app_id,
        app_key=args.app_key,
        from_lang=getattr(args, 'from'),
        to_lang=args.to,
        sample_rate=args.sample_rate,
        return_tts=not args.no_tts,
        tts_speaker=args.speaker,
        on_translation_result=on_translation,
        on_tts_audio=on_tts,
        on_error=on_error
    )
    
    # 连接
    print("Connecting...")
    if not client.connect():
        print("Failed to connect")
        return
    
    print("Connected! Sending audio...")
    
    if args.test_file:
        # 从文件读取音频测试
        with open(args.test_file, "rb") as f:
            audio_data = f.read()
        
        # 分块发送
        chunk_size = int(args.sample_rate * 2 * 40 / 1000)  # 40ms
        for i in range(0, len(audio_data), chunk_size):
            chunk = audio_data[i:i+chunk_size]
            client.send_audio(chunk)
            time.sleep(0.04)  # 40ms
            
            if not client.is_connected:
                break
        
        time.sleep(2)
    else:
        # 模拟音频发送
        print("No test file, simulating audio...")
        import numpy as np
        
        chunk_size = int(args.sample_rate * 2 * 40 / 1000)
        for i in range(50):  # 发送 50 块 ~2秒
            # 生成静音（或者使用实际音频）
            chunk = b'\x00' * chunk_size
            client.send_audio(chunk)
            time.sleep(0.04)
            
            if not client.is_connected:
                break
    
    print(f"\nReceived {translations_received} translations")
    client.disconnect()
    print("Done")


if __name__ == "__main__":
    main()
