#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
语音翻译服务
从SRS接收音频流，调用百度实时语音翻译API翻译为英文，然后推送回SRS
"""

import os
import sys
import json
import logging
import subprocess
import threading
import queue
import time
import requests
from typing import Optional, Dict, Any
import base64
from tts_service import BaiduTTSService
from baidu_asr_client import BaiduASRClient, BaiduMTClient

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class BaiduTranslationClient:
    """百度语音翻译客户端（ASR + MT组合）"""
    
    def __init__(self, api_key: str, secret_key: str, audio_config: Optional[Dict[str, Any]] = None):
        self.api_key = api_key
        self.secret_key = secret_key
        self.asr_client = BaiduASRClient(api_key, secret_key)
        self.mt_client = BaiduMTClient(api_key, secret_key)
        self.audio_queue = queue.Queue()
        self.translated_text_queue = queue.Queue()
        self.is_running = False
        self.audio_buffer = b""
        
        # 音频配置
        self.audio_config = audio_config or {}
        self.sample_rate = self.audio_config.get("sample_rate", 16000)
        self.channels = self.audio_config.get("channels", 1)
        self.asr_format = self.audio_config.get("asr_format", "aac")  # 默认使用AAC
        self.buffer_duration_ms = 2000  # 缓冲2秒的音频
        
        # 根据格式计算chunk_size
        # AAC: 压缩格式，大小不固定
        # 假设AAC比特率为64kbps，2秒约为16KB (64kbps * 2秒 / 8 = 16KB)
        # 为了安全起见，使用稍大的值
        if self.asr_format.lower() == "pcm":
            self.chunk_size = self.sample_rate * 2 * 2  # 2秒的PCM数据
        else:
            # 对于AAC格式，64kbps * 2秒 / 8 = 16000字节
            # 考虑到AAC帧对齐和缓冲，使用稍大的值
            self.chunk_size = 20000  # 约2秒的AAC数据（64kbps）
        
    def start_translation(self):
        """启动翻译服务"""
        self.is_running = True
        
        # 启动音频处理线程
        threading.Thread(target=self._process_audio_thread, daemon=True).start()
        
        logger.info("Translation service started (ASR + MT)")
    
    def _process_audio_thread(self):
        """处理音频数据：ASR -> MT"""
        while self.is_running:
            try:
                # 从队列获取音频数据
                audio_data = self.audio_queue.get(timeout=1)
                
                # 累积音频到缓冲区
                self.audio_buffer += audio_data
                
                # 当缓冲区达到一定大小，进行识别
                # 使用循环处理，确保所有达到chunk_size的数据都被处理
                while len(self.audio_buffer) >= self.chunk_size:
                    # 只取chunk_size大小的数据进行识别
                    chunk_to_process = self.audio_buffer[:self.chunk_size]
                    # 保留剩余的数据在缓冲区中
                    self.audio_buffer = self.audio_buffer[self.chunk_size:]
                    
                    # 调用ASR识别
                    # 百度ASR支持多种格式：pcm, wav, mp3, amr, flac, aac等
                    # 格式由配置决定（audio_config中的asr_format）
                    recognized_text = self.asr_client.recognize(
                        chunk_to_process,
                        format=self.asr_format,  # 从配置读取格式
                        rate=self.sample_rate,
                        channel=self.channels
                    )
                    
                    if recognized_text and recognized_text.strip():
                        # 调用MT翻译
                        translated_text = self.mt_client.translate(
                            recognized_text,
                            from_lang="zh",  # 假设输入是中文
                            to_lang="en"     # 翻译为英文
                        )
                        
                        if translated_text:
                            # 将翻译结果放入队列
                            self.translated_text_queue.put({
                                "text": translated_text,
                                "original": recognized_text,
                                "timestamp": time.time()
                            })
                    
            except queue.Empty:
                continue
            except Exception as e:
                logger.error(f"Error processing audio: {e}")
    
    def add_audio(self, audio_data: bytes):
        """添加音频数据到翻译队列"""
        if self.is_running:
            self.audio_queue.put(audio_data)
    
    def get_translated_text(self) -> Optional[Dict[str, Any]]:
        """获取翻译后的文本"""
        try:
            return self.translated_text_queue.get_nowait()
        except queue.Empty:
            return None
    
    def stop(self):
        """停止翻译服务"""
        self.is_running = False
        if self.ws:
            self.ws.close()


class AudioStreamProcessor:
    """音频流处理器"""
    
    def __init__(self, srs_url: str, room_id: str, translation_client: BaiduTranslationClient,
                 audio_config: Optional[Dict[str, Any]] = None):
        self.srs_url = srs_url
        self.room_id = room_id
        self.translation_client = translation_client
        self.ffmpeg_process = None
        self.output_process = None
        self.is_running = False
        
        # 音频配置
        self.audio_config = audio_config or {}
        self.asr_format = self.audio_config.get("asr_format", "aac")  # 统一使用AAC格式
        self.input_format = self.audio_config.get("input_format", None)  # 输入格式：opus, aac, mp3等（None表示自动检测）
        self.sample_rate = self.audio_config.get("sample_rate", 16000)
        self.channels = self.audio_config.get("channels", 1)
        
    def start(self):
        """启动音频流处理"""
        self.is_running = True
        
        # 启动从SRS接收音频的FFmpeg进程
        input_url = f"{self.srs_url}/live/{self.room_id}.flv"
        
        # FFmpeg命令：从HTTP-FLV流接收音频
        # 统一转换为AAC格式发送给百度ASR
        # 百度ASR支持AAC格式，AAC是压缩格式，可以减少数据传输量
        
        # 检测输入格式（用于日志）
        is_opus_input = self.input_format and self.input_format.lower() == "opus"
        if is_opus_input:
            logger.info("Detected Opus input format (will convert to AAC for Baidu ASR)")
        
        # 统一转换为AAC格式
        # FFmpeg会自动检测输入格式（AAC、Opus、MP3等）并转换为AAC
        ffmpeg_input_cmd = [
            "ffmpeg",
            "-i", input_url,
            "-vn",  # 不处理视频
            "-acodec", "aac",  # 编码为AAC
            "-ar", str(self.sample_rate),  # 采样率
            "-ac", str(self.channels),  # 声道数
            "-b:a", "64k",  # AAC比特率（64kbps，适合语音）
            "-f", "adts",  # AAC ADTS格式（百度ASR支持）
            "-"  # 输出到stdout
        ]
        
        if is_opus_input:
            logger.info(f"Converting Opus to AAC format (sample_rate={self.sample_rate}, channels={self.channels}, bitrate=64k)")
        else:
            logger.info(f"Converting to AAC format (sample_rate={self.sample_rate}, channels={self.channels}, bitrate=64k)")
        
        logger.info(f"Starting FFmpeg input process: {' '.join(ffmpeg_input_cmd)}")
        
        try:
            self.ffmpeg_process = subprocess.Popen(
                ffmpeg_input_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0
            )
            
            # 启动音频读取线程
            threading.Thread(target=self._read_audio_thread, daemon=True).start()
            
            # 启动翻译后音频推送线程
            threading.Thread(target=self._push_translated_audio_thread, daemon=True).start()
            
        except Exception as e:
            logger.error(f"Failed to start FFmpeg process: {e}")
            raise
    
    def _read_audio_thread(self):
        """读取音频数据并发送到翻译服务"""
        # AAC格式：64kbps，每次读取约200ms的数据
        # 64kbps * 0.2秒 / 8 = 1600字节，使用稍大的值以确保读取完整帧
        chunk_size = 2000  # 每次读取约200ms的AAC音频数据
        
        while self.is_running and self.ffmpeg_process:
            try:
                # 读取音频数据
                audio_chunk = self.ffmpeg_process.stdout.read(chunk_size)
                
                if not audio_chunk:
                    logger.warning("No audio data received")
                    time.sleep(0.1)
                    continue
                
                # 发送到翻译服务
                self.translation_client.add_audio(audio_chunk)
                
            except Exception as e:
                logger.error(f"Error reading audio: {e}")
                time.sleep(0.1)
    
    def _push_translated_audio_thread(self):
        """推送翻译后的音频到SRS"""
        # RTMP推流地址
        rtmp_url = f"{self.srs_url.replace('http', 'rtmp')}/live/{self.room_id}_translated"
        
        # 初始化TTS服务
        tts_service = None
        try:
            api_key = os.getenv("BAIDU_API_KEY", "")
            secret_key = os.getenv("BAIDU_SECRET_KEY", "")
            if api_key and secret_key:
                tts_service = BaiduTTSService(api_key, secret_key)
                logger.info("TTS service initialized")
        except Exception as e:
            logger.warning(f"Failed to initialize TTS service: {e}")
        
        # FFmpeg命令：将PCM音频推送到RTMP
        ffmpeg_output_cmd = [
            "ffmpeg",
            "-f", "s16le",  # 输入格式PCM
            "-ar", "16000",  # 采样率
            "-ac", "1",  # 单声道
            "-i", "-",  # 从stdin读取
            "-acodec", "aac",  # 编码为AAC
            "-b:a", "64k",  # 音频比特率
            "-f", "flv",  # 输出格式FLV
            rtmp_url
        ]
        
        logger.info(f"Starting FFmpeg output process: {' '.join(ffmpeg_output_cmd)}")
        
        try:
            self.output_process = subprocess.Popen(
                ffmpeg_output_cmd,
                stdin=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0
            )
            
            # 处理翻译后的文本并转换为语音
            while self.is_running:
                translated_data = self.translation_client.get_translated_text()
                if translated_data and tts_service:
                    text = translated_data.get('text', '').strip()
                    if text:
                        # 将翻译后的文本转换为语音
                        audio_data = tts_service.text_to_speech(text, lang="en")
                        if audio_data and self.output_process and self.output_process.stdin:
                            try:
                                # 写入音频数据到FFmpeg
                                self.output_process.stdin.write(audio_data)
                                self.output_process.stdin.flush()
                                logger.info(f"Pushed translated audio for text: {text[:50]}...")
                            except Exception as e:
                                logger.error(f"Error writing audio to FFmpeg: {e}")
                
                time.sleep(0.1)
                
        except Exception as e:
            logger.error(f"Error pushing translated audio: {e}")
    
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
        self.audio_processors = {}
        self.shared_translation_client = None  # 共享的翻译客户端
        
    def initialize(self):
        """初始化服务"""
        # 初始化百度翻译客户端（共享实例）
        api_key = self.config.get("baidu_api_key")
        secret_key = self.config.get("baidu_secret_key")
        
        if not api_key or not secret_key:
            raise ValueError("Baidu API key and secret key are required")
        
        # 获取音频配置
        audio_config = self.config.get("audio", {})
        
        # 创建共享的翻译客户端（所有房间共享）
        self.shared_translation_client = BaiduTranslationClient(api_key, secret_key, audio_config)
        self.shared_translation_client.start_translation()
        
        logger.info("Translation service initialized")
    
    def process_room(self, room_id: str):
        """处理指定房间的音频流"""
        if room_id in self.audio_processors:
            logger.warning(f"Room {room_id} is already being processed")
            return
        
        srs_url = self.config.get("srs_url", "http://localhost:8080")
        
        # 获取音频配置
        audio_config = self.config.get("audio", {})
        
        processor = AudioStreamProcessor(
            srs_url=srs_url,
            room_id=room_id,
            translation_client=self.shared_translation_client,
            audio_config=audio_config
        )
        
        processor.start()
        self.audio_processors[room_id] = processor
        
        logger.info(f"Started processing room: {room_id}")
    
    def stop_room(self, room_id: str):
        """停止处理指定房间"""
        if room_id in self.audio_processors:
            self.audio_processors[room_id].stop()
            del self.audio_processors[room_id]
            logger.info(f"Stopped processing room: {room_id}")
    
    def shutdown(self):
        """关闭服务"""
        # 停止所有房间处理
        for room_id in list(self.audio_processors.keys()):
            self.stop_room(room_id)
        
        # 停止翻译客户端
        if self.shared_translation_client:
            self.shared_translation_client.stop()
        
        logger.info("Translation service shutdown")


def main():
    """主函数"""
    # 从环境变量读取配置
    config = {
        "baidu_api_key": os.getenv("BAIDU_API_KEY", ""),
        "baidu_secret_key": os.getenv("BAIDU_SECRET_KEY", ""),
        "srs_url": os.getenv("SRS_URL", "http://localhost:8080"),
        "room_id": os.getenv("ROOM_ID", "room_123")
    }
    
    if not config["baidu_api_key"] or not config["baidu_secret_key"]:
        logger.error("Please set BAIDU_API_KEY and BAIDU_SECRET_KEY environment variables")
        sys.exit(1)
    
    # 创建并启动服务
    service = TranslationService(config)
    
    try:
        service.initialize()
        service.process_room(config["room_id"])
        
        # 保持运行
        logger.info("Translation service is running. Press Ctrl+C to stop.")
        while True:
            time.sleep(1)
            
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    except Exception as e:
        logger.error(f"Service error: {e}")
    finally:
        service.shutdown()


if __name__ == "__main__":
    main()
