#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
文本转语音服务
将翻译后的文本转换为语音，使用百度TTS API
"""

import os
import json
import logging
import requests
import base64
import time
from typing import Optional, bytes

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class BaiduTTSService:
    """百度TTS服务"""
    
    def __init__(self, api_key: str, secret_key: str):
        self.api_key = api_key
        self.secret_key = secret_key
        self.access_token = None
        self.token_expire_time = 0
    
    def get_access_token(self) -> str:
        """获取访问令牌"""
        # 如果token未过期，直接返回
        if self.access_token and time.time() < self.token_expire_time:
            return self.access_token
        
        url = "https://aip.baidubce.com/oauth/2.0/token"
        params = {
            "grant_type": "client_credentials",
            "client_id": self.api_key,
            "client_secret": self.secret_key
        }
        
        try:
            response = requests.post(url, params=params, timeout=10)
            response.raise_for_status()
            result = response.json()
            self.access_token = result.get("access_token")
            
            if not self.access_token:
                raise Exception("Failed to get access token")
            
            # 设置token过期时间（提前5分钟刷新）
            expires_in = result.get("expires_in", 2592000)  # 默认30天
            self.token_expire_time = time.time() + expires_in - 300
            
            logger.info("Successfully obtained TTS access token")
            return self.access_token
            
        except Exception as e:
            logger.error(f"Failed to get TTS access token: {e}")
            raise
    
    def text_to_speech(self, text: str, lang: str = "en", speed: int = 5, pitch: int = 5, volume: int = 5) -> Optional[bytes]:
        """将文本转换为语音
        
        Args:
            text: 要转换的文本
            lang: 语言代码（en=英文, zh=中文）
            speed: 语速（0-15，默认5）
            pitch: 音调（0-15，默认5）
            volume: 音量（0-15，默认5）
        
        Returns:
            音频数据（PCM格式）
        """
        if not text or not text.strip():
            return None
        
        token = self.get_access_token()
        url = f"https://tsn.baidu.com/text2audio"
        
        params = {
            "tex": text,
            "tok": token,
            "cuid": "srs_translation_service",
            "ctp": 1,  # 客户端类型
            "lan": lang,  # 语言
            "spd": speed,  # 语速
            "pit": pitch,  # 音调
            "vol": volume,  # 音量
            "per": 0,  # 发音人（0=女声，1=男声，3=情感男声，4=情感女声）
            "aue": 3,  # 音频格式（3=PCM，4=MP3）
        }
        
        try:
            response = requests.post(url, params=params, timeout=10)
            response.raise_for_status()
            
            # 检查是否是错误响应（JSON格式）
            content_type = response.headers.get('Content-Type', '')
            if 'application/json' in content_type:
                error_data = response.json()
                error_msg = error_data.get('err_msg', 'Unknown error')
                logger.error(f"TTS API error: {error_msg}")
                return None
            
            # 返回音频数据
            audio_data = response.content
            logger.info(f"Successfully converted text to speech: {text[:50]}...")
            return audio_data
            
        except Exception as e:
            logger.error(f"Failed to convert text to speech: {e}")
            return None


class TTSCache:
    """TTS缓存"""
    
    def __init__(self, max_size: int = 100):
        self.cache = {}
        self.max_size = max_size
        self.access_order = []
    
    def get(self, key: str) -> Optional[bytes]:
        """获取缓存的音频"""
        if key in self.cache:
            # 更新访问顺序
            if key in self.access_order:
                self.access_order.remove(key)
            self.access_order.append(key)
            return self.cache[key]
        return None
    
    def put(self, key: str, value: bytes):
        """添加缓存"""
        # 如果缓存已满，删除最久未使用的
        if len(self.cache) >= self.max_size and self.access_order:
            oldest_key = self.access_order.pop(0)
            del self.cache[oldest_key]
        
        self.cache[key] = value
        if key in self.access_order:
            self.access_order.remove(key)
        self.access_order.append(key)


def main():
    """测试TTS服务"""
    api_key = os.getenv("BAIDU_API_KEY", "")
    secret_key = os.getenv("BAIDU_SECRET_KEY", "")
    
    if not api_key or not secret_key:
        logger.error("Please set BAIDU_API_KEY and BAIDU_SECRET_KEY")
        return
    
    tts = BaiduTTSService(api_key, secret_key)
    
    # 测试
    text = "Hello, this is a test message."
    audio = tts.text_to_speech(text, lang="en")
    
    if audio:
        # 保存音频文件
        with open("test_output.pcm", "wb") as f:
            f.write(audio)
        logger.info(f"Audio saved to test_output.pcm, size: {len(audio)} bytes")
    else:
        logger.error("Failed to generate audio")


if __name__ == "__main__":
    main()
