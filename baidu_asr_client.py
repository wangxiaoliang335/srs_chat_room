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
    
    # 百度ASR支持的采样率
    SUPPORTED_RATES = [8000, 16000]
    
    def __init__(self, api_key: str, secret_key: str):
        self.api_key = api_key
        self.secret_key = secret_key
        self.access_token = None
        self.token_expire_time = 0
        # 使用 pro_api（极速版）替代 server_api
        self.asr_url = "https://vop.baidu.com/pro_api"
    
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
                  dev_pid: int = 80001) -> Optional[str]:
        """识别音频，返回文本

        Args:
            audio_data: 音频数据（支持多种格式）
            format: 音频格式，支持：pcm, wav, mp3, amr, flac, aac等
            rate: 采样率（16000推荐，8000用于电话场景）
            channel: 声道数（1=单声道，2=双声道）
            cuid: 用户唯一标识
            dev_pid: 语音识别模型（80001=极速版普通话，1537=中文普通话已弃用）

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
            logger.debug("Empty audio data, skipping recognition")
            return None
        
        # 验证采样率
        if rate not in self.SUPPORTED_RATES:
            logger.warning(f"Unsupported sample rate {rate}. "
                          f"Supported rates: {self.SUPPORTED_RATES}. "
                          f"Will attempt anyway, but results may be unreliable.")
        
        # 验证音频数据大小是否合理
        # 对于PCM格式，每秒采样率*通道数*2字节
        if format.lower() == "pcm":
            expected_bytes_per_sec = rate * channel * 2
            min_expected_bytes = expected_bytes_per_sec * 0.1  # 至少0.1秒
            max_expected_bytes = expected_bytes_per_sec * 10   # 最多10秒
            
            if len(audio_data) < min_expected_bytes:
                logger.warning(f"Audio data too short ({len(audio_data)} bytes). "
                             f"Expected at least {min_expected_bytes} bytes for {rate}Hz/{channel}ch PCM. "
                             f"Skipping recognition.")
                return None
            elif len(audio_data) > max_expected_bytes:
                logger.warning(f"Audio data too long ({len(audio_data)} bytes). "
                             f"Expected at most {max_expected_bytes} bytes. "
                             f"Will process but results may be unreliable.")
        
        token = self.get_access_token()
        
        # 将音频数据编码为base64
        speech = base64.b64encode(audio_data).decode('utf-8')
        speech_len = len(audio_data)
        
        # pro_api: token、cuid、dev_pid 在 body 中
        data = {
            "format": format,
            "rate": rate,
            "channel": channel,
            "len": speech_len,
            "speech": speech,
            "token": token,
            "cuid": cuid,
            "dev_pid": dev_pid,
        }
        
        headers = {
            "Content-Type": "application/json"
        }
        
        # 详细记录ASR请求参数
        logger.info(f"ASR request details:")
        logger.info(f"  URL: {self.asr_url}")
        logger.info(f"  Body: format={format}, rate={rate}, channel={channel}, len={speech_len}, dev_pid={dev_pid}")
        logger.info(f"  Audio data size: {len(audio_data)} bytes")
        
        # 验证音频数据是否为有效的PCM
        if format.lower() == "pcm":
            # 对于PCM，期望字节数 = rate * channel * 2 * duration
            # 64000字节 / (16000 * 1 * 2) = 2秒
            expected_duration_sec = speech_len / (rate * channel * 2)
            logger.info(f"  Expected audio duration: {expected_duration_sec:.2f} seconds")
            
            if abs(expected_duration_sec - 2.0) > 0.5:
                logger.warning(f"  Audio duration {expected_duration_sec:.2f}s differs significantly from expected 2s")
        
        try:
            response = requests.post(
                self.asr_url,
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
                err_no = result.get("err_no")
                err_msg = result.get("err_msg", "Unknown error")
                
                # 针对特定错误码的详细处理
                if "rate" in err_msg.lower() or err_no == 20002:
                    # 采样率错误
                    logger.error(f"ASR API rate error (err_no={err_no}): {err_msg}")
                    logger.error(f"Current rate={rate}, expected one of {self.SUPPORTED_RATES}")
                    logger.error(f"Input format={format}, audio_data size={len(audio_data)} bytes")
                    logger.error("Possible causes:")
                    logger.error("  1. FFmpeg output sample rate doesn't match declared rate")
                    logger.error("  2. Audio data is not properly formatted for the declared format")
                    logger.error("  3. Chunk size calculation mismatch")
                    logger.error("  4. Audio stream source has different sample rate than expected")
                else:
                    logger.error(f"ASR API error (err_no={err_no}): {err_msg}")
                return None
                
        except requests.exceptions.RequestException as e:
            logger.error(f"Network error during ASR request: {e}")
            return None
        except Exception as e:
            logger.error(f"Failed to recognize audio: {e}")
            return None


class BaiduMTClient:
    """百度机器翻译客户端
    
    支持两种认证方式：
    1. 百度AI平台：使用 API Key + Secret Key 获取 token
    2. 百度翻译开放平台：使用 App ID + 密钥 签名认证（推荐）
    """
    
    # 百度翻译 API 支持的语言代码映射
    # 内部语言代码 -> 百度翻译 API 语言代码
    BAIDU_LANG_MAP = {
        'zh': 'zh',
        'en': 'en',
        'ja': 'jp',   # 日文用 jp，不是 ja
        'ko': 'kor',  # 韩文用 kor，不是 ko
        'fr': 'fra',  # 法文
        'de': 'de',   # 德文
        'es': 'spa',  # 西班牙文
        'ru': 'ru',   # 俄文
        'ar': 'ara',  # 阿拉伯文
        'pt': 'pt',   # 葡萄牙文
        'it': 'it',   # 意大利文
        'th': 'th',   # 泰文
        'vi': 'vie',  # 越南文
        'id': 'id',   # 印尼文
        'ms': 'may',  # 马来文
        'hi': 'hi',   # 印地文
    }
    
    def __init__(self, api_key: str = None, secret_key: str = None, 
                 app_id: str = None, app_secret: str = None):
        # 百度AI平台认证
        self.api_key = api_key
        self.secret_key = secret_key
        self.access_token = None
        self.token_expire_time = 0
        
        # 百度翻译开放平台认证
        self.app_id = app_id
        self.app_secret = app_secret
        self.mt_url = "https://fanyi-api.baidu.com/api/trans/vip/translate"
        self.ai_mt_url = "https://aip.baidubce.com/rpc/2.0/mt/v2/transtext"
        
        # 优先使用翻译开放平台
        self.use_translation_platform = bool(app_id and app_secret)
    
    def _convert_lang_code(self, lang_code: str) -> str:
        """将内部语言代码转换为百度翻译 API 需要的语言代码"""
        return self.BAIDU_LANG_MAP.get(lang_code, lang_code)
    
    def _translate_with_platform(self, text: str, from_lang: str, to_lang: str) -> Optional[str]:
        """使用百度翻译开放平台API（App ID + 签名）"""
        import hashlib
        
        # 转换语言代码为百度翻译 API 格式
        from_lang_baidu = self._convert_lang_code(from_lang)
        to_lang_baidu = self._convert_lang_code(to_lang)
        
        salt = str(int(time.time() * 1000))
        sign_str = f"{self.app_id}{text}{salt}{self.app_secret}"
        sign = hashlib.md5(sign_str.encode('utf-8')).hexdigest()
        
        params = {
            "q": text,
            "from": from_lang_baidu,
            "to": to_lang_baidu,
            "appid": self.app_id,
            "salt": salt,
            "sign": sign
        }
        
        logger.info(f"[Platform] Translation request: {from_lang}({from_lang_baidu}) -> {to_lang}({to_lang_baidu})")
        
        try:
            response = requests.post(self.mt_url, data=params, timeout=10)
            response.raise_for_status()
            result = response.json()
            
            if "trans_result" in result and len(result["trans_result"]) > 0:
                translated_text = result["trans_result"][0]["dst"]
                logger.info(f"[Platform] Translated: {text} -> {translated_text}")
                return translated_text
            else:
                error_code = result.get("error_code", "Unknown")
                error_msg = result.get("error_msg", "Unknown error")
                logger.error(f"[Platform] Translation API error {error_code}: {error_msg}")
                return None
        except Exception as e:
            logger.error(f"[Platform] Translation request failed: {e}")
            return None
    
    def get_access_token(self) -> str:
        """获取百度AI平台访问令牌"""
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
    
    def _translate_with_ai_platform(self, text: str, from_lang: str, to_lang: str) -> Optional[str]:
        """使用百度AI平台翻译API"""
        # 转换语言代码为百度翻译 API 格式
        from_lang_baidu = self._convert_lang_code(from_lang)
        to_lang_baidu = self._convert_lang_code(to_lang)
        
        try:
            token = self.get_access_token()
            url = f"{self.ai_mt_url}?access_token={token}"
            headers = {"Content-Type": "application/json"}
            data = {"q": text, "from": from_lang_baidu, "to": to_lang_baidu}
            
            logger.info(f"[AI Platform] Translation request: {from_lang}({from_lang_baidu}) -> {to_lang}({to_lang_baidu})")
            
            response = requests.post(url, json=data, headers=headers, timeout=10)
            response.raise_for_status()
            result = response.json()
            
            if "result" in result:
                translated_text = result["result"]["trans_result"][0]["dst"]
                logger.info(f"[AI Platform] Translated: {text} -> {translated_text}")
                return translated_text
            else:
                error_code = result.get("error_code", result.get("error", "Unknown"))
                error_msg = result.get("error_description", result.get("error_msg", "Unknown error"))
                logger.error(f"[AI Platform] Translation API error {error_code}: {error_msg}")
                return None
        except Exception as e:
            logger.error(f"[AI Platform] Translation failed: {e}")
            return None
    
    def translate(self, text: str, from_lang: str = "zh", to_lang: str = "en") -> Optional[str]:
        """翻译文本
        
        优先使用百度翻译开放平台，如果未配置则回退到AI平台
        """
        if not text or not text.strip():
            return None
        
        if self.use_translation_platform:
            result = self._translate_with_platform(text, from_lang, to_lang)
            if result:
                return result
            # 如果翻译平台失败，尝试AI平台作为备选
            if self.api_key and self.secret_key:
                logger.warning("Translation platform failed, trying AI platform as fallback...")
                return self._translate_with_ai_platform(text, from_lang, to_lang)
            return None
        elif self.api_key and self.secret_key:
            return self._translate_with_ai_platform(text, from_lang, to_lang)
        else:
            logger.error("No translation credentials configured")
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
