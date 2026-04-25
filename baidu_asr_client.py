#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
百度语音识别（ASR）客户端
使用HTTP API进行语音识别
"""

import os
import json
import logging
import requests
import base64
import time
from typing import Optional, Dict, Any

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class BaiduASRClient:
    """百度语音识别客户端"""
    
    def __init__(self, api_key: str, secret_key: str):
        self.api_key = api_key
        self.secret_key = secret_key
        self.access_token = None
        self.token_expire_time = 0
        self.asr_url = "https://vop.baidu.com/server_api"
    
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
            
            logger.info("Successfully obtained ASR access token")
            return self.access_token
            
        except Exception as e:
            logger.error(f"Failed to get ASR access token: {e}")
            raise
    
    def recognize(self, audio_data: bytes, format: str = "pcm", rate: int = 16000,
                  channel: int = 1, cuid: str = "srs_translation",
                  dev_pid: int = 1537) -> Optional[str]:
        """识别音频，返回文本

        Args:
            audio_data: 音频数据（支持多种格式）
            format: 音频格式，支持：pcm, wav, mp3, amr, flac, aac等
            rate: 采样率（16000推荐，8000用于电话场景）
            channel: 声道数（1=单声道，2=双声道）
            cuid: 用户唯一标识
            dev_pid: 语音识别模型（1537=中文普通话，1737=英文，1736=日文/韩文）

        Returns:
            识别出的文本，失败返回None
        
        Note:
            百度ASR支持的格式：
            - PCM: 原始PCM格式（推荐用于实时流）
            - WAV: 无损音频格式
            - MP3: 有损压缩格式
            - AMR: 窄带语音编码
            - FLAC: 压缩无损格式
            - AAC: 高级音频编码（如果输入流是AAC，可直接使用）
        """
        if not audio_data:
            return None
        
        token = self.get_access_token()
        
        # 将音频数据编码为base64
        speech = base64.b64encode(audio_data).decode('utf-8')
        speech_len = len(audio_data)
        
        # 构建请求参数（token、cuid、dev_pid 在 URL 中，format/channel/len/speech/rate 在 body 中）
        params = {
            "dev_pid": dev_pid,
            "cuid": cuid,
            "token": token,
        }
        
        data = {
            "format": format,
            "rate": rate,  # rate 在 body 中
            "channel": channel,
            "len": speech_len,
            "speech": speech,
        }
        
        headers = {
            "Content-Type": "application/json"
        }
        
        try:
            response = requests.post(
                self.asr_url,
                params=params,
                json=data,
                headers=headers,
                timeout=10
            )
            response.raise_for_status()
            result = response.json()
            
            if result.get("err_no") == 0:
                text = result.get("result", [])
                if text:
                    recognized_text = " ".join(text)
                    logger.info(f"Recognized text: {recognized_text}")
                    return recognized_text
                else:
                    logger.warning("No recognition result")
                    return None
            else:
                err_msg = result.get("err_msg", "Unknown error")
                logger.error(f"ASR API error: {err_msg}")
                return None
                
        except Exception as e:
            logger.error(f"Failed to recognize audio: {e}")
            return None


class BaiduMTClient:
    """百度机器翻译客户端
    
    注意：百度翻译API有多个版本：
    - 百度AI平台（语音等）：使用 API Key + Secret Key 获取 token
    - 百度翻译开放平台：使用 App ID + Secret Key 签名认证
    
    当前使用的是百度AI平台的通用接口，如果需要翻译功能，
    需要到百度翻译开放平台(https://fanyi-api.baidu.com)申请单独的App ID
    """
    
    def __init__(self, api_key: str, secret_key: str, use_translation_platform: bool = True):
        self.api_key = api_key
        self.secret_key = secret_key
        self.access_token = None
        self.token_expire_time = 0
        # 百度翻译开放平台 API
        self.mt_url = "https://fanyi-api.baidu.com/api/trans/vip/translate"
        # 备用：百度AI平台翻译API（需要开通）
        self.ai_mt_url = "https://aip.baidubce.com/rpc/2.0/mt/v2/transtext"
        self.use_translation_platform = use_translation_platform
    
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
                logger.error("Failed to get MT access token")
                raise Exception("Failed to get MT access token")
            
            expires_in = result.get("expires_in", 2592000)
            self.token_expire_time = time.time() + expires_in - 300
            
            logger.info("Successfully obtained MT access token")
            return self.access_token
            
        except Exception as e:
            logger.error(f"Failed to get MT access token: {e}")
            raise
    
    def translate(self, text: str, from_lang: str = "zh", to_lang: str = "en") -> Optional[str]:
        """翻译文本
        
        Args:
            text: 要翻译的文本
            from_lang: 源语言代码（zh=中文，en=英文等）
            to_lang: 目标语言代码（en=英文，zh=中文等）
        
        Returns:
            翻译后的文本，失败返回None
        """
        if not text or not text.strip():
            return None
        
        try:
            token = self.get_access_token()
            
            # 百度AI平台翻译API
            url = f"https://aip.baidubce.com/rpc/2.0/mt/v2/transtext?access_token={token}"
            
            headers = {
                "Content-Type": "application/json"
            }
            
            # 注意：百度AI平台翻译API的语言代码与通用代码不同
            # 通用: zh, en -> 平台: zh, en
            data = {
                "q": text,
                "from": from_lang,
                "to": to_lang
            }
            
            response = requests.post(url, json=data, headers=headers, timeout=10)
            response.raise_for_status()
            result = response.json()
            
            if "result" in result:
                translated_text = result["result"]["trans_result"][0]["dst"]
                logger.info(f"Translated: {text} -> {translated_text}")
                return translated_text
            else:
                error_code = result.get("error_code", result.get("error", "Unknown"))
                error_msg = result.get("error_description", result.get("error_msg", "Unknown error"))
                logger.error(f"Translation API error {error_code}: {error_msg}")
                return None
                
        except Exception as e:
            logger.error(f"Failed to translate text: {e}")
            return None


def main():
    """测试"""
    api_key = os.getenv("BAIDU_API_KEY", "")
    secret_key = os.getenv("BAIDU_SECRET_KEY", "")
    
    if not api_key or not secret_key:
        logger.error("Please set BAIDU_API_KEY and BAIDU_SECRET_KEY")
        return
    
    # 测试ASR
    asr_client = BaiduASRClient(api_key, secret_key)
    # 这里需要提供实际的音频数据
    # text = asr_client.recognize(audio_data)
    
    # 测试翻译
    mt_client = BaiduMTClient(api_key, secret_key)
    translated = mt_client.translate("你好，世界", "zh", "en")
    if translated:
        logger.info(f"Translation result: {translated}")


if __name__ == "__main__":
    main()
