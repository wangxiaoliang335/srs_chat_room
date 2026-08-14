#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
语言检测模块
用于检测文本语言类型，并提供语言代码映射
"""

from langdetect import detect, LangDetectException
from typing import Optional, Dict, Tuple

# 语言代码映射表
# langdetect 返回的语言代码 -> 百度MT/ASR 使用的语言代码
LANGUAGE_CODE_MAP = {
    'zh-cn': 'zh',   # 中文简体
    'zh-tw': 'zh',   # 中文繁体
    'zh': 'zh',      # 中文
    'en': 'en',      # 英文
    'ja': 'ja',      # 日文
    'ko': 'ko',      # 韩文
    'fr': 'fr',      # 法文
    'de': 'de',      # 德文
    'es': 'es',      # 西班牙文
    'ru': 'ru',      # 俄文
    'ar': 'ar',      # 阿拉伯文
    'pt': 'pt',      # 葡萄牙文
    'it': 'it',      # 意大利文
    'th': 'th',      # 泰文
    'vi': 'vi',      # 越南文
    'id': 'id',      # 印尼文
    'ms': 'ms',      # 马来文
    'hi': 'hi',      # 印地文
}

# 百度ASR dev_pid 映射
# 不同语言使用不同的ASR模型（使用极速版80001）
# 注意：百度已弃用旧的dev_pid(1537/1737等)，新API统一使用80001
ASR_DEV_PID_MAP = {
    'zh': 80001,      # 中文普通话（极速版）
    'en': 80001,      # 英文（极速版）
    'ja': 80001,      # 日文（极速版）
    'ko': 80001,      # 韩文（极速版）
}

# 目标语言映射
# 源语言 -> 目标语言 (默认翻译为英文)
TARGET_LANGUAGE_MAP = {
    'zh': 'en',
    'en': 'zh',
    'ja': 'en',
    'ko': 'en',
    'fr': 'en',
    'de': 'en',
    'es': 'en',
    'ru': 'en',
    'ar': 'en',
    'pt': 'en',
    'it': 'en',
    'th': 'en',
    'vi': 'en',
    'id': 'en',
    'ms': 'en',
    'hi': 'en',
}

# 需要翻译的语言对
# 如果源语言和目标语言相同，则不需要翻译
TRANSLATION_NEEDED = {
    ('zh', 'en'): True,
    ('en', 'zh'): True,
    ('ja', 'en'): True,
    ('ko', 'en'): True,
    ('fr', 'en'): True,
    ('de', 'en'): True,
    ('es', 'en'): True,
    ('ru', 'en'): True,
    ('zh', 'zh'): False,  # 不需要翻译
    ('en', 'en'): False,
}


class LanguageDetector:
    """语言检测器"""

    def __init__(self, min_text_length: int = 3):
        """
        初始化语言检测器

        Args:
            min_text_length: 最少需要检测的文本长度
        """
        self.min_text_length = min_text_length

    def detect_language(self, text: str) -> Optional[str]:
        """
        检测文本语言

        Args:
            text: 待检测的文本

        Returns:
            语言代码（如 'zh', 'en', 'ja'），检测失败返回 None
        """
        if not text or len(text.strip()) < self.min_text_length:
            return None

        try:
            # langdetect 返回如 'zh-cn', 'en', 'ja' 等
            raw_lang = detect(text)
            # 转换为标准语言代码
            return LANGUAGE_CODE_MAP.get(raw_lang, raw_lang.split('-')[0])
        except LangDetectException:
            return None
        except Exception:
            return None

    def get_asr_dev_pid(self, language: str) -> int:
        """
        获取ASR识别所使用的 dev_pid

        Args:
            language: 语言代码

        Returns:
            dev_pid 值
        """
        return ASR_DEV_PID_MAP.get(language, 1537)  # 默认中文

    def get_translation_pair(self, source_language: str,
                             target_language: Optional[str] = None) -> Tuple[str, str]:
        """
        获取翻译语言对

        Args:
            source_language: 源语言代码
            target_language: 目标语言代码（可选，默认翻译为英文）

        Returns:
            (源语言, 目标语言) 元组
        """
        if target_language is None:
            target_language = TARGET_LANGUAGE_MAP.get(source_language, 'en')

        return source_language, target_language

    def needs_translation(self, source_language: str,
                          target_language: str) -> bool:
        """
        判断是否需要翻译

        Args:
            source_language: 源语言代码
            target_language: 目标语言代码

        Returns:
            是否需要翻译
        """
        if source_language == target_language:
            return False
        return True


def test():
    """测试函数"""
    detector = LanguageDetector()

    test_texts = [
        ("你好世界", "zh"),
        ("Hello world", "en"),
        ("こんにちは世界", "ja"),
        ("안녕하세요 세계", "ko"),
        ("Bonjour le monde", "fr"),
        ("Hallo Welt", "de"),
    ]

    print("Language Detection Test:")
    print("-" * 60)

    for text, expected in test_texts:
        detected = detector.detect_language(text)
        status = "✓" if detected == expected else "✗"
        print(f"{status} Text: {text:20} Expected: {expected:5} Detected: {detected}")

    print("-" * 60)
    print("\nTranslation Pair Test:")

    for lang in ['zh', 'en', 'ja', 'ko', 'fr']:
        src, tgt = detector.get_translation_pair(lang)
        needs = detector.needs_translation(src, tgt)
        print(f"  {lang} -> {tgt} (needs_translation: {needs})")


if __name__ == "__main__":
    test()
