#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
百度机器翻译客户端
使用 HTTP API 进行文本翻译
"""

import os
import json
import logging
import requests
import time
import hashlib
from typing import Optional

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


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
    
    # 测试翻译
    mt_client = BaiduMTClient(api_key, secret_key)
    translated = mt_client.translate("你好，世界", "zh", "en")
    if translated:
        logger.info(f"Translation result: {translated}")


if __name__ == "__main__":
    main()
