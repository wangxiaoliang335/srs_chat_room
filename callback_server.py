#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SRS HTTP回调服务器
接收SRS的HTTP Hooks回调，自动启动/停止翻译服务
"""

import os
import sys
import json
import logging
import subprocess
import threading
from flask import Flask, request, jsonify
from typing import Dict, Any

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# 存储运行中的翻译服务进程
translation_processes: Dict[str, subprocess.Popen] = {}


def start_translation_service(room_id: str):
    """启动指定房间的翻译服务"""
    if room_id in translation_processes:
        logger.warning(f"Translation service for room {room_id} is already running")
        return
    
    # 启动翻译服务进程
    env = os.environ.copy()
    env['ROOM_ID'] = room_id
    
    try:
        process = subprocess.Popen(
            [sys.executable, 'audio_translation_service.py'],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        
        translation_processes[room_id] = process
        logger.info(f"Started translation service for room: {room_id}, PID: {process.pid}")
        
    except Exception as e:
        logger.error(f"Failed to start translation service for room {room_id}: {e}")


def stop_translation_service(room_id: str):
    """停止指定房间的翻译服务"""
    if room_id not in translation_processes:
        logger.warning(f"Translation service for room {room_id} is not running")
        return
    
    process = translation_processes[room_id]
    
    try:
        process.terminate()
        process.wait(timeout=5)
        del translation_processes[room_id]
        logger.info(f"Stopped translation service for room: {room_id}")
        
    except subprocess.TimeoutExpired:
        process.kill()
        del translation_processes[room_id]
        logger.warning(f"Force killed translation service for room: {room_id}")
    except Exception as e:
        logger.error(f"Error stopping translation service for room {room_id}: {e}")


@app.route('/api/v1/streams/on_publish', methods=['POST'])
def on_publish():
    """处理发布流回调"""
    try:
        data = request.json or {}
        stream_name = data.get('stream', '')
        
        if not stream_name:
            logger.warning("Received on_publish callback without stream name")
            return jsonify({'code': 0}), 200
        
        logger.info(f"Received on_publish callback for stream: {stream_name}")
        
        # 启动翻译服务
        start_translation_service(stream_name)
        
        return jsonify({'code': 0}), 200
        
    except Exception as e:
        logger.error(f"Error handling on_publish: {e}")
        return jsonify({'code': 0}), 200  # 即使出错也返回成功，避免影响SRS


@app.route('/api/v1/streams/on_unpublish', methods=['POST'])
def on_unpublish():
    """处理停止发布回调"""
    try:
        data = request.json or {}
        stream_name = data.get('stream', '')
        
        if not stream_name:
            logger.warning("Received on_unpublish callback without stream name")
            return jsonify({'code': 0}), 200
        
        logger.info(f"Received on_unpublish callback for stream: {stream_name}")
        
        # 停止翻译服务
        stop_translation_service(stream_name)
        
        return jsonify({'code': 0}), 200
        
    except Exception as e:
        logger.error(f"Error handling on_unpublish: {e}")
        return jsonify({'code': 0}), 200


@app.route('/api/v1/streams/status', methods=['GET'])
def get_status():
    """获取翻译服务状态"""
    status = {
        'active_rooms': list(translation_processes.keys()),
        'count': len(translation_processes)
    }
    return jsonify(status), 200


@app.route('/health', methods=['GET'])
def health():
    """健康检查"""
    return jsonify({'status': 'ok'}), 200


if __name__ == '__main__':
    port = int(os.getenv('CALLBACK_PORT', 8085))
    host = os.getenv('CALLBACK_HOST', '0.0.0.0')
    
    logger.info(f"Starting callback server on {host}:{port}")
    app.run(host=host, port=port, debug=False)
